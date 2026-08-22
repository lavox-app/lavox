"""
Lavox accounts — users, workspaces, membership, API tokens.

OPT-IN: the module is only active when LAVOX_MULTI_TENANT=1 AND LAVOX_PG_DSN
is set. DISABLED by default — self-hosted/local installations thus keep
working exactly as before (single-user mode, no registration, no login).
This is the free tier promise: Lavox running on your own machine never
requires an account.

In cloud mode (multi-tenant), however, every request is bound to a user and
a workspace, and the server checks the membership — without this, anyone
could request any workspace's data by rewriting the X-Workspace-Id header.

Env:
  LAVOX_MULTI_TENANT=1       enables the module (otherwise everything stays as before)
  LAVOX_PG_DSN               the same Postgres that meetings.py uses

Password: stdlib hashlib.scrypt (memory-hard KDF) — no new dependency.
Token: opaque random string; the DB stores only its SHA-256 digest, so a
table leak yields no usable token. Revocable, and the same mechanism is
used by the webapp and the Lavox Hub.
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

# The workspace identifier ends up in R2 keys and directory names — it must
# match the rules of diarize._WS_RE: [A-Za-z0-9_-], max 64.
_WS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MIN_PASSWORD_LEN = 8

# scrypt parameters. The current ones (n=2^16, r=8, p=2 → ~64 MiB) are the
# recommended 2026 minimum; older, weaker hashes remain verifiable because
# the parameters are written into the stored string, and on login we silently
# rehash with the stronger settings (see needs_rehash).
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**16, 8, 2
_MAXMEM = 256 * 1024 * 1024

# The legacy format (without parameters) was produced with these:
_LEGACY = dict(n=2**14, r=8, p=1)


def available() -> bool:
    """Is multi-tenant mode active? If not, the server runs in the old single-user mode."""
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
-- For existing installations (idempotent):
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

-- Pairing the Lavox Hub (Mac app): the webapp generates a short-lived code,
-- the user types it into the Hub, the Hub redeems it for a device token
-- (api_tokens, kind='hub').
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

# ── Hub pairing ───────────────────────────────────────────────────────────────

PAIRING_CODE_TTL = 600      # the code is valid for 10 minutes
HUB_ONLINE_WINDOW = 60      # a heartbeat within this = the Hub is online

# Without confusable characters (no 0/O, 1/I/L).
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _gen_pairing_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def create_pairing_code(user_id: str, workspace_id: str) -> dict[str, Any]:
    """Requested by the webapp on behalf of the logged-in user. Short-lived, single-use."""
    code = _gen_pairing_code()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO pairing_codes (code, user_id, workspace_id, expires_at)
               VALUES (%s, %s, %s, now() + make_interval(secs => %s))""",
            (code, user_id, workspace_id, PAIRING_CODE_TTL),
        )
    return {"code": code, "expires_in": PAIRING_CODE_TTL}


def claim_pairing_code(code: str) -> dict[str, Any] | None:
    """Called by the Hub (WITHOUT auth — the code itself is the secret).
    Redeems it for a device token. The code is single-use and expires; None
    if invalid/expired/already claimed."""
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
    """Called periodically by the Hub with its device token. Updates last_used_at."""
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
    """Asked by the webapp: is the user's Hub online? (is there a fresh heartbeat)."""
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


# ── password ──────────────────────────────────────────────────────────────────


def _hash_password(password: str) -> str:
    """Format: scrypt$n$r$p$salt_hex$dk_hex — the parameters are embedded,
    so the settings can be raised without making old hashes unverifiable."""
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
            # legacy: without parameters
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
    # timing-independent comparison
    return hmac.compare_digest(dk.hex(), dk_hex)


def _needs_rehash(stored: str) -> bool:
    parsed = _parse_hash(stored)
    if not parsed:
        return True
    n, r, p, _, _ = parsed
    return (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)


# Dummy hash: the KDF is run even on a failed login, so the response time does
# not reveal whether the given e-mail exists (user enumeration).
#
# LAZY, deliberately: computing the hash costs ~64 MiB and several hundred ms.
# Computed at module level it would run on EVERY startup — including
# self-hosted installations where the account layer is never active. This way
# it is only produced on the first real login attempt.
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


# ── registration / login ──────────────────────────────────────────────────────


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
    """New user + their personal workspace. Raises ValueError on bad input.

    There is no username: the e-mail is both the unique identifier and the
    login name. The first name is stored separately because the speaker label
    in transcripts uses it.
    """
    email = _normalize_email(email)
    if not _EMAIL_RE.match(email):
        raise ValueError("Invalid e-mail address.")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise ValueError(f"The password must be at least {MIN_PASSWORD_LEN} characters.")

    first = (first_name or "").strip()
    last = (last_name or "").strip()

    user_id = "u_" + uuid.uuid4().hex[:12]
    ws_id = _new_workspace_id()
    display = " ".join(p for p in (first, last) if p) or email.split("@")[0]

    with _conn() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE email=%s", (email,)).fetchone()
        if exists:
            raise ValueError("An account with this e-mail address already exists.")

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
    """On successful login: user + workspaces + token, otherwise None (we do
    not reveal which part was wrong)."""
    email = _normalize_email(email)
    with _conn() as conn:
        row = conn.execute(
            """SELECT id, email, name, first_name, last_name, password_hash
               FROM users WHERE email=%s""", (email,)
        ).fetchone()

        if not row:
            # Unknown e-mail: we still run the KDF so the response time does
            # not distinguish the "no such user" and "wrong password" cases.
            _verify_password(password or "", _dummy_hash())
            return None
        if not _verify_password(password or "", row["password_hash"]):
            return None

        # If the hash was produced with weaker parameters, silently rehash —
        # so raising the settings also applies retroactively.
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


# ── queries ───────────────────────────────────────────────────────────────────


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
    """The user belonging to the token + their workspaces. Also updates last_used_at."""
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
    """Login with an external provider (Google / Microsoft / Apple).

    The provider has already proven ownership of the e-mail, so there is no
    password here. If an account already belongs to the e-mail address, we
    return it (password login and social login are the SAME account) —
    otherwise we create one with the usual personal workspace.

    In that case `password_hash` is a never-matching sentinel, so password
    login can never get into an OAuth-only account.
    """
    email = _normalize_email(email)
    if not _EMAIL_RE.match(email):
        raise ValueError("The provider did not supply a valid e-mail address.")

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
            # Fill in a missing name with the one from the provider, but never overwrite an existing one.
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
    """User + their workspaces by identifier.

    Used by the webapp's service call: the webapp authenticates itself with
    the service key and specifies WHO the user is in a header — so no
    per-user token has to be placed into the browser-accessible session.
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


# ── brute-force brake ─────────────────────────────────────────────────────────
#
# Simple in-memory sliding window. Sufficient for one process; with multiple
# workers it is worth adding a limit at the reverse-proxy (Caddy/nginx) level too.

_ATTEMPTS: dict[str, list[float]] = {}
_WINDOW_SEC = 15 * 60
_MAX_ATTEMPTS = 8


def too_many_attempts(key: str) -> bool:
    """Has the key (e-mail or IP) reached the attempt limit within the window?"""
    now = time.time()
    hits = [t for t in _ATTEMPTS.get(key, []) if now - t < _WINDOW_SEC]
    _ATTEMPTS[key] = hits
    return len(hits) >= _MAX_ATTEMPTS


def record_attempt(key: str) -> None:
    now = time.time()
    _ATTEMPTS.setdefault(key, []).append(now)
    # Occasional cleanup so the dict does not grow without bound.
    if len(_ATTEMPTS) > 10_000:
        for k in [k for k, v in _ATTEMPTS.items() if not any(now - t < _WINDOW_SEC for t in v)]:
            _ATTEMPTS.pop(k, None)


def clear_attempts(key: str) -> None:
    _ATTEMPTS.pop(key, None)


def is_member(user_id: str, workspace_id: str) -> bool:
    """Is the user a member of the given workspace? This is what closes the IDOR."""
    if not _WS_RE.match(workspace_id or ""):
        return False
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM workspace_members WHERE user_id=%s AND workspace_id=%s",
            (user_id, workspace_id),
        ).fetchone()
    return bool(row)
