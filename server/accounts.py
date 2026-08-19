"""
Lavox fiókok — userek, workspace-ek, tagság, API-tokenek.

OPT-IN: a modul csak akkor aktív, ha LAVOX_MULTI_TENANT=1 ÉS van LAVOX_PG_DSN.
Alapértelmezésben KIKAPCSOLT — a self-hosted/lokál telepítés így pontosan úgy
működik tovább, mint eddig (egy-felhasználós mód, nincs regisztráció, nincs
bejelentkezés). Ez a free tier ígérete: a saját gépeden futó Lavoxhoz soha nem
kell fiók.

Felhő módban (multi-tenant) viszont minden kérés egy userhez és egy workspace-hez
kötött, és a szerver ellenőrzi a tagságot — enélkül bárki bármelyik workspace
adatát elkérhetné az X-Workspace-Id fejléc átírásával.

Env:
  LAVOX_MULTI_TENANT=1       a modul bekapcsolása (különben minden marad a régiben)
  LAVOX_PG_DSN               ugyanaz a Postgres, amit a meetings.py használ

Jelszó: stdlib hashlib.scrypt (memória-kemény KDF) — nincs új függőség.
Token: átlátszatlan (opaque) véletlen string; a DB csak a SHA-256 lenyomatát
tárolja, így a tábla kiszivárgása nem ad használható tokent. Visszavonható,
és ugyanezt használja a webapp és a Lavox Hub is.
"""

import hashlib
import hmac
import os
import re
import secrets
import time
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

PG_DSN = os.environ.get("LAVOX_PG_DSN", "")
MULTI_TENANT = os.environ.get("LAVOX_MULTI_TENANT", "") == "1"

# A workspace azonosító R2-kulcsba és könyvtárnévbe kerül — a diarize._WS_RE
# szabályaival kell egyeznie: [A-Za-z0-9_-], max 64.
_WS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MIN_PASSWORD_LEN = 8

# scrypt paraméterek. Az aktuális (n=2^16, r=8, p=2 → ~64 MiB) a 2026-os
# ajánlott minimum; a régebbi, gyengébb hash-ek verifikálhatók maradnak, mert a
# paraméterek bele vannak írva a tárolt stringbe, és belépéskor csendben
# újrahasheljük az erősebb beállítással (lásd needs_rehash).
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**16, 8, 2
_MAXMEM = 256 * 1024 * 1024

# A legacy formátum (paraméterek nélkül) ezekkel készült:
_LEGACY = dict(n=2**14, r=8, p=1)


def available() -> bool:
    """Multi-tenant mód aktív? Ha nem, a szerver a régi egy-felhasználós módban fut."""
    return bool(MULTI_TENANT and PG_DSN)


def _conn() -> psycopg.Connection:
    return psycopg.connect(PG_DSN, row_factory=dict_row)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
  id text PRIMARY KEY,
  email text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  name text NOT NULL DEFAULT '',
  first_name text NOT NULL DEFAULT '',
  last_name text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);
-- Meglévő telepítéshez (idempotens):
ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name text NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name text NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS workspaces (
  id text PRIMARY KEY,
  name text NOT NULL,
  plan text NOT NULL DEFAULT 'personal',
  owner_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspace_members (
  workspace_id text NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role text NOT NULL DEFAULT 'member',
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS api_tokens (
  token_hash text PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind text NOT NULL DEFAULT 'web',
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz
);

-- A Lavox Hub (Mac app) párosítása: a webapp rövid életű kódot generál, a user
-- beírja a Hubba, a Hub beváltja egy eszköz-tokenre (api_tokens, kind='hub').
CREATE TABLE IF NOT EXISTS pairing_codes (
  code text PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  workspace_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  claimed_at timestamptz
);

CREATE INDEX IF NOT EXISTS ws_members_user ON workspace_members (user_id);
CREATE INDEX IF NOT EXISTS api_tokens_user ON api_tokens (user_id);
CREATE INDEX IF NOT EXISTS api_tokens_hub ON api_tokens (user_id, kind);
"""

# ── Hub-párosítás ─────────────────────────────────────────────────────────────

PAIRING_CODE_TTL = 600      # a kód 10 percig érvényes
HUB_ONLINE_WINDOW = 60      # ennyin belüli heartbeat = a Hub online

# Összetéveszthető karakterek nélkül (nincs 0/O, 1/I/L).
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _gen_pairing_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def create_pairing_code(user_id: str, workspace_id: str) -> dict[str, Any]:
    """A webapp kéri, a bejelentkezett user nevében. Rövid életű, egyszer beváltható."""
    code = _gen_pairing_code()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO pairing_codes (code, user_id, workspace_id, expires_at)
               VALUES (%s, %s, %s, now() + make_interval(secs => %s))""",
            (code, user_id, workspace_id, PAIRING_CODE_TTL),
        )
    return {"code": code, "expires_in": PAIRING_CODE_TTL}


def claim_pairing_code(code: str) -> dict[str, Any] | None:
    """A Hub hívja (auth NÉLKÜL — a kód maga a titok). Beváltja eszköz-tokenre.
    A kód egyszer használatos és lejár; None, ha érvénytelen/lejárt/már beváltott."""
    code = (code or "").strip().upper()
    with _conn() as conn:
        row = conn.execute(
            """SELECT user_id, workspace_id, claimed_at, (expires_at < now()) AS expired
               FROM pairing_codes WHERE code=%s""",
            (code,),
        ).fetchone()
        if not row or row["claimed_at"] is not None or row["expired"]:
            return None
        conn.execute("UPDATE pairing_codes SET claimed_at=now() WHERE code=%s", (code,))
        token = _issue_token(conn, row["user_id"], kind="hub")
    return {"token": token, "workspace": row["workspace_id"]}


def record_hub_heartbeat(token: str) -> dict[str, Any] | None:
    """A Hub periodikusan hívja az eszköz-tokenjével. Frissíti a last_used_at-ot."""
    if not available() or not token:
        return None
    th = _token_hash(token)
    with _conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM api_tokens WHERE token_hash=%s AND kind='hub'", (th,)
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE api_tokens SET last_used_at=now() WHERE token_hash=%s", (th,))
        spaces = _workspaces_for(conn, row["user_id"])
    return {"user_id": row["user_id"], "workspaces": spaces}


def hub_status(user_id: str) -> dict[str, Any]:
    """A webapp kérdezi: online-e a user Hubja? (van-e friss heartbeat)."""
    with _conn() as conn:
        row = conn.execute(
            """SELECT max(last_used_at) AS last_seen,
                      (max(last_used_at) > now() - make_interval(secs => %s)) AS online
               FROM api_tokens WHERE user_id=%s AND kind='hub'""",
            (HUB_ONLINE_WINDOW, user_id),
        ).fetchone()
    last = row["last_seen"]
    return {
        "connected": bool(row["online"]),
        "last_seen": last.isoformat() if last else None,
    }


def init_schema() -> None:
    with _conn() as conn:
        conn.execute(SCHEMA_SQL)


# ── jelszó ────────────────────────────────────────────────────────────────────


def _hash_password(password: str) -> str:
    """Formátum: scrypt$n$r$p$salt_hex$dk_hex — a paraméterek benne vannak,
    így a beállítás emelhető anélkül, hogy a régi hash-ek verifikálhatatlanná
    válnának."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=32, maxmem=_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def _parse_hash(stored: str) -> tuple[int, int, int, bytes, str] | None:
    parts = stored.split("$")
    try:
        if len(parts) == 6 and parts[0] == "scrypt":
            _, n, r, p, salt_hex, dk_hex = parts
            return int(n), int(r), int(p), bytes.fromhex(salt_hex), dk_hex
        if len(parts) == 3 and parts[0] == "scrypt":
            # legacy: paraméterek nélkül
            _, salt_hex, dk_hex = parts
            return _LEGACY["n"], _LEGACY["r"], _LEGACY["p"], bytes.fromhex(salt_hex), dk_hex
    except Exception:
        return None
    return None


def _verify_password(password: str, stored: str) -> bool:
    parsed = _parse_hash(stored)
    if not parsed:
        return False
    n, r, p, salt, dk_hex = parsed
    try:
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=n, r=r, p=p, dklen=32, maxmem=_MAXMEM,
        )
    except Exception:
        return False
    # időzítés-független összehasonlítás
    return hmac.compare_digest(dk.hex(), dk_hex)


def _needs_rehash(stored: str) -> bool:
    parsed = _parse_hash(stored)
    if not parsed:
        return True
    n, r, p, _, _ = parsed
    return (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)


# Dummy hash: sikertelen belépésnél is lefuttatjuk a KDF-et, hogy a válaszidő ne
# árulja el, létezik-e az adott e-mail (user-enumeráció).
#
# LUSTA, szándékosan: a hash kiszámítása ~64 MiB és több száz ms. Modul-szinten
# számolva ez MINDEN indításkor lefutna — a self-hosted telepítésen is, ahol a
# fiók-réteg soha nem aktív. Így csak az első valódi belépés-kísérletkor készül el.
_DUMMY_HASH_CACHE: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH_CACHE
    if _DUMMY_HASH_CACHE is None:
        _DUMMY_HASH_CACHE = _hash_password(secrets.token_hex(16))
    return _DUMMY_HASH_CACHE


# ── token ─────────────────────────────────────────────────────────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_token(conn: psycopg.Connection, user_id: str, kind: str = "web") -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO api_tokens (token_hash, user_id, kind) VALUES (%s, %s, %s)",
        (_token_hash(token), user_id, kind),
    )
    return token


def revoke_token(token: str) -> None:
    if not available():
        return
    with _conn() as conn:
        conn.execute("DELETE FROM api_tokens WHERE token_hash=%s", (_token_hash(token),))


# ── regisztráció / bejelentkezés ──────────────────────────────────────────────


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _new_workspace_id() -> str:
    return "ws_" + uuid.uuid4().hex[:12]


def register(
    email: str,
    password: str,
    first_name: str = "",
    last_name: str = "",
    kind: str = "web",
) -> dict[str, Any]:
    """Új user + hozzá tartozó személyes workspace. ValueError-t dob, ha az input rossz.

    Nincs felhasználónév: az e-mail az egyedi azonosító és a belépési név is.
    A keresztnevet külön tároljuk, mert az átiratokban a beszélő-címke ezt használja.
    """
    email = _normalize_email(email)
    if not _EMAIL_RE.match(email):
        raise ValueError("Érvénytelen e-mail cím.")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise ValueError(f"A jelszó legalább {MIN_PASSWORD_LEN} karakter legyen.")

    first = (first_name or "").strip()
    last = (last_name or "").strip()

    user_id = "u_" + uuid.uuid4().hex[:12]
    ws_id = _new_workspace_id()
    display = " ".join(p for p in (first, last) if p) or email.split("@")[0]

    with _conn() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE email=%s", (email,)).fetchone()
        if exists:
            raise ValueError("Ezzel az e-mail címmel már van fiók.")

        conn.execute(
            """INSERT INTO users (id, email, password_hash, name, first_name, last_name)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user_id, email, _hash_password(password), display, first, last),
        )
        conn.execute(
            "INSERT INTO workspaces (id, name, plan, owner_id) VALUES (%s, %s, %s, %s)",
            (ws_id, f"{display} workspace", "personal", user_id),
        )
        conn.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, %s)",
            (ws_id, user_id, "owner"),
        )
        token = _issue_token(conn, user_id, kind)

    return {
        "user": {
            "id": user_id, "email": email, "name": display,
            "first_name": first, "last_name": last,
        },
        "workspaces": [{"id": ws_id, "name": f"{display} workspace", "role": "owner", "plan": "personal"}],
        "token": token,
    }


def login(email: str, password: str, kind: str = "web") -> dict[str, Any] | None:
    """Sikeres belépésnél user+workspace-ek+token, különben None (nem áruljuk el, melyik volt rossz)."""
    email = _normalize_email(email)
    with _conn() as conn:
        row = conn.execute(
            """SELECT id, email, name, first_name, last_name, password_hash
               FROM users WHERE email=%s""", (email,)
        ).fetchone()

        if not row:
            # Ismeretlen e-mail: akkor is lefuttatjuk a KDF-et, hogy a válaszidő
            # ne különböztesse meg a "nincs ilyen user" és a "rossz jelszó" esetet.
            _verify_password(password or "", _dummy_hash())
            return None
        if not _verify_password(password or "", row["password_hash"]):
            return None

        # Ha a hash gyengébb paraméterekkel készült, csendben újrahasheljük —
        # így a beállítás emelése visszamenőleg is érvényesül.
        if _needs_rehash(row["password_hash"]):
            conn.execute(
                "UPDATE users SET password_hash=%s WHERE id=%s",
                (_hash_password(password), row["id"]),
            )

        token = _issue_token(conn, row["id"], kind)
        spaces = _workspaces_for(conn, row["id"])

    return {
        "user": {
            "id": row["id"], "email": row["email"], "name": row["name"],
            "first_name": row["first_name"], "last_name": row["last_name"],
        },
        "workspaces": spaces,
        "token": token,
    }


# ── lekérdezések ──────────────────────────────────────────────────────────────


def _workspaces_for(conn: psycopg.Connection, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT w.id, w.name, w.plan, m.role
        FROM workspace_members m
        JOIN workspaces w ON w.id = m.workspace_id
        WHERE m.user_id = %s
        ORDER BY w.created_at
        """,
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def user_by_token(token: str) -> dict[str, Any] | None:
    """A tokenhez tartozó user + workspace-ei. Egyben frissíti a last_used_at-ot."""
    if not available() or not token:
        return None
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.email, u.name, u.first_name, u.last_name
            FROM api_tokens t
            JOIN users u ON u.id = t.user_id
            WHERE t.token_hash = %s
            """,
            (_token_hash(token),),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE api_tokens SET last_used_at=now() WHERE token_hash=%s",
            (_token_hash(token),),
        )
        spaces = _workspaces_for(conn, row["id"])
    return {
        "user": {
            "id": row["id"], "email": row["email"], "name": row["name"],
            "first_name": row.get("first_name", ""), "last_name": row.get("last_name", ""),
        },
        "workspaces": spaces,
    }


def upsert_oauth_user(
    email: str,
    first_name: str = "",
    last_name: str = "",
    provider: str = "oauth",
) -> dict[str, Any]:
    """Belépés külső szolgáltatóval (Google / Microsoft / Apple).

    A szolgáltató már igazolta az e-mail birtoklását, ezért itt nincs jelszó.
    Ha az e-mail címhez már tartozik fiók, azt adjuk vissza (a jelszavas és a
    social belépés UGYANAZ a fiók) — különben létrehozunk egyet a szokásos
    személyes workspace-szel.

    A `password_hash` ilyenkor egy soha nem egyező sentinel, hogy a jelszavas
    login semmiképp ne tudjon belépni egy csak-OAuth fiókba.
    """
    email = _normalize_email(email)
    if not _EMAIL_RE.match(email):
        raise ValueError("A szolgáltató nem adott érvényes e-mail címet.")

    first = (first_name or "").strip()
    last = (last_name or "").strip()
    display = " ".join(p for p in (first, last) if p) or email.split("@")[0]

    with _conn() as conn:
        row = conn.execute(
            "SELECT id, email, name, first_name, last_name FROM users WHERE email=%s",
            (email,),
        ).fetchone()

        if row:
            user_id = row["id"]
            # A hiányzó nevet pótoljuk a szolgáltatótól kapottal, de meglévőt nem írunk felül.
            if (first or last) and not (row["first_name"] or row["last_name"]):
                conn.execute(
                    "UPDATE users SET first_name=%s, last_name=%s, name=%s WHERE id=%s",
                    (first, last, display, user_id),
                )
            user = {"id": user_id, "email": email, "name": row["name"] or display,
                    "first_name": row["first_name"] or first, "last_name": row["last_name"] or last}
        else:
            user_id = "u_" + uuid.uuid4().hex[:12]
            ws_id = _new_workspace_id()
            conn.execute(
                """INSERT INTO users (id, email, password_hash, name, first_name, last_name)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (user_id, email, f"oauth:{provider}", display, first, last),
            )
            conn.execute(
                "INSERT INTO workspaces (id, name, plan, owner_id) VALUES (%s, %s, %s, %s)",
                (ws_id, f"{display} workspace", "personal", user_id),
            )
            conn.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, %s)",
                (ws_id, user_id, "owner"),
            )
            user = {"id": user_id, "email": email, "name": display,
                    "first_name": first, "last_name": last}

        spaces = _workspaces_for(conn, user_id)

    return {"user": user, "workspaces": spaces}


def user_by_id(user_id: str) -> dict[str, Any] | None:
    """User + workspace-ei azonosító alapján.

    Ezt a webapp szolgáltatás-hívása használja: a webapp a szolgáltatás-kulccsal
    hitelesíti magát, és fejlécben adja meg, KI a felhasználó — így nem kell
    per-user tokent a böngésző által elérhető session-be tenni.
    """
    if not available() or not user_id:
        return None
    with _conn() as conn:
        row = conn.execute(
            """SELECT id, email, name, first_name, last_name FROM users WHERE id=%s""", (user_id,)
        ).fetchone()
        if not row:
            return None
        spaces = _workspaces_for(conn, row["id"])
    return {
        "user": {
            "id": row["id"], "email": row["email"], "name": row["name"],
            "first_name": row.get("first_name", ""), "last_name": row.get("last_name", ""),
        },
        "workspaces": spaces,
    }


# ── brute-force fék ───────────────────────────────────────────────────────────
#
# Egyszerű, memóriában tartott csúszóablak. Egy processzhez elég; több worker
# esetén a reverse-proxy (Caddy/nginx) szintjén is érdemes limitet tenni.

_ATTEMPTS: dict[str, list[float]] = {}
_WINDOW_SEC = 15 * 60
_MAX_ATTEMPTS = 8


def too_many_attempts(key: str) -> bool:
    """Elérte-e a kulcs (e-mail vagy IP) a kísérlet-korlátot az ablakon belül?"""
    now = time.time()
    hits = [t for t in _ATTEMPTS.get(key, []) if now - t < _WINDOW_SEC]
    _ATTEMPTS[key] = hits
    return len(hits) >= _MAX_ATTEMPTS


def record_attempt(key: str) -> None:
    now = time.time()
    _ATTEMPTS.setdefault(key, []).append(now)
    # Ritka takarítás, hogy a dict ne nőjön korlátlanul.
    if len(_ATTEMPTS) > 10_000:
        for k in [k for k, v in _ATTEMPTS.items() if not any(now - t < _WINDOW_SEC for t in v)]:
            _ATTEMPTS.pop(k, None)


def clear_attempts(key: str) -> None:
    _ATTEMPTS.pop(key, None)


def is_member(user_id: str, workspace_id: str) -> bool:
    """Tagja-e a user az adott workspace-nek? Ez zárja az IDOR-t."""
    if not _WS_RE.match(workspace_id or ""):
        return False
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM workspace_members WHERE user_id=%s AND workspace_id=%s",
            (user_id, workspace_id),
        ).fetchone()
    return bool(row)
