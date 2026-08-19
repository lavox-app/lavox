"""Megosztható meeting-linkek — fiók nélküli megtekintés.

A Personal tier tényleges értéke: felveszel egy meetinget, küldesz egy linket,
a másik fél regisztráció nélkül megnézi az átiratot és lejátssza a felvételt.

BIZTONSÁGI ALAPELVEK (ez az EGYETLEN hitelesítés nélküli adat-végpont):

1. A token maga a titok — `secrets.token_urlsafe(32)` (~256 bit). Az adatbázis
   CSAK a SHA-256 lenyomatát tárolja, ahogy az api_tokens is: egy DB-szivárgás
   így nem ad működő linkeket.
2. A publikus vetület SZŰKÍTETT (`_PUBLIC_FIELDS`). A `workspace`, `meet_code`,
   `participants`, `speaker_sources`, `evaluation`, `screenshots` SOHA nem megy
   ki — ezek belső vagy harmadik feleket érintő adatok.
3. Visszavonható (`revoked_at`) és opcionálisan lejáró (`expires_at`).
4. A találgatás ellen a hívó oldalon IP-alapú rate limit van (app.py).
5. A megtekintés nem módosít semmit a meetingen — csak számlálót léptet.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

import meetings as mtg

PG_DSN = os.environ.get("LAVOX_PG_DSN", "")

# Amit a megosztott nézet MEGKAPHAT. Minden más oszlop szándékosan kimarad.
_PUBLIC_FIELDS = ("title", "created_at", "duration_sec", "transcript", "speakers")

# A megosztott lejátszási URL élettartama (s). Rövidebb, mint a bejelentkezett
# nézeté (6 óra), mert ez határozza meg, mennyi idő alatt lép életbe egy
# visszavonás a MÁR betöltött oldalakon. 30 perc: hosszú felvételt is végig
# lehet nézni egy ülésben, de a visszavonás fél órán belül tényleg lezár.
# (Az oldal újratöltése friss URL-t kér, tehát ez nem korlátozza a nézőt.)
SHARED_URL_TTL = 1800

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meeting_shares (
  token_hash   text PRIMARY KEY,
  workspace    text NOT NULL,
  meeting_id   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz,
  revoked_at   timestamptz,
  view_count   integer NOT NULL DEFAULT 0,
  last_viewed_at timestamptz
);
-- Meetingenként egyetlen ÉLŐ megosztás: az újbóli "Megosztás" ugyanazt a
-- linket adja vissza, nem érvényteleníti azt, amit a user már elküldött.
CREATE UNIQUE INDEX IF NOT EXISTS meeting_shares_active_uq
  ON meeting_shares (workspace, meeting_id)
  WHERE revoked_at IS NULL;
"""


def available() -> bool:
    return bool(PG_DSN) and mtg.available()


def _conn() -> psycopg.Connection:
    return psycopg.connect(PG_DSN)


def init_schema() -> None:
    with _conn() as conn:
        conn.execute(SCHEMA_SQL)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_or_get_share(
    workspace: str, meeting_id: str, expires_days: int | None = None
) -> dict[str, Any] | None:
    """Élő megosztás visszaadása, vagy új létrehozása.

    None, ha a meeting nem létezik ebben a workspace-ben (nem szivárogtatunk
    létezés-információt más workspace meetingjeiről).

    FONTOS: ha már van élő megosztás, a NYERS token NEM állítható vissza (csak
    a lenyomatát tároljuk). Ilyenkor `token=None`-t adunk vissza `exists=True`
    jelzéssel — a hívó ebből tudja, hogy a link már kiadva, és ha új kell,
    előbb vissza kell vonni a régit.
    """
    if mtg.get_meeting(workspace, meeting_id) is None:
        return None

    with _conn() as conn:
        row = conn.execute(
            """SELECT created_at, expires_at, view_count FROM meeting_shares
               WHERE workspace=%s AND meeting_id=%s AND revoked_at IS NULL""",
            (workspace, meeting_id),
        ).fetchone()
        if row:
            return {
                "exists": True,
                "token": None,
                "created_at": row[0].isoformat(),
                "expires_at": row[1].isoformat() if row[1] else None,
                "view_count": row[2],
            }

        token = secrets.token_urlsafe(32)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=expires_days)
            if expires_days
            else None
        )
        conn.execute(
            """INSERT INTO meeting_shares (token_hash, workspace, meeting_id, expires_at)
               VALUES (%s, %s, %s, %s)""",
            (_token_hash(token), workspace, meeting_id, expires_at),
        )
    return {
        "exists": False,
        "token": token,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "view_count": 0,
    }


def rotate_share(
    workspace: str, meeting_id: str, expires_days: int | None = None
) -> dict[str, Any] | None:
    """A meglévő link visszavonása + új kiadása (ha a régi kiszivárgott)."""
    if mtg.get_meeting(workspace, meeting_id) is None:
        return None
    revoke_share(workspace, meeting_id)
    return create_or_get_share(workspace, meeting_id, expires_days)


def revoke_share(workspace: str, meeting_id: str) -> bool:
    """Az élő megosztás visszavonása. True, ha volt mit visszavonni."""
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE meeting_shares SET revoked_at=now()
               WHERE workspace=%s AND meeting_id=%s AND revoked_at IS NULL""",
            (workspace, meeting_id),
        )
        return cur.rowcount > 0


def share_status(workspace: str, meeting_id: str) -> dict[str, Any] | None:
    """A megosztás állapota a tulajdonosnak (token nélkül)."""
    with _conn() as conn:
        row = conn.execute(
            """SELECT created_at, expires_at, view_count, last_viewed_at
               FROM meeting_shares
               WHERE workspace=%s AND meeting_id=%s AND revoked_at IS NULL""",
            (workspace, meeting_id),
        ).fetchone()
    if not row:
        return {"shared": False}
    return {
        "shared": True,
        "created_at": row[0].isoformat(),
        "expires_at": row[1].isoformat() if row[1] else None,
        "view_count": row[2],
        "last_viewed_at": row[3].isoformat() if row[3] else None,
    }


def resolve_share(token: str) -> dict[str, Any] | None:
    """PUBLIKUS feloldás — a link birtokosának adott, SZŰKÍTETT nézet.

    None minden hibás esetben (ismeretlen/visszavont/lejárt token, törölt
    meeting) — a hívó egységes 404-et ad, hogy a válasz ne árulja el, melyik
    eset állt fenn.
    """
    if not token or len(token) < 20:
        return None

    with _conn() as conn:
        row = conn.execute(
            """SELECT workspace, meeting_id, expires_at FROM meeting_shares
               WHERE token_hash=%s AND revoked_at IS NULL""",
            (_token_hash(token),),
        ).fetchone()
        if not row:
            return None
        workspace, meeting_id, expires_at = row
        if expires_at and expires_at < datetime.now(timezone.utc):
            return None

        conn.execute(
            """UPDATE meeting_shares
               SET view_count = view_count + 1, last_viewed_at = now()
               WHERE token_hash=%s""",
            (_token_hash(token),),
        )

    full = mtg.get_meeting(workspace, meeting_id)
    if full is None:  # a meetinget azóta törölték
        return None

    # SZŰKÍTETT vetület — a whitelistán kívül semmi nem megy ki.
    out: dict[str, Any] = {k: full.get(k) for k in _PUBLIC_FIELDS}
    # A lejátszási URL-t RÖVIDEBB élettartammal állítjuk elő, mint a
    # bejelentkezett nézetben: egy kiadott presigned URL a link visszavonása
    # után is működik a lejáratáig, tehát ez a visszavonás átfutási ideje.
    out["media_urls"] = mtg.media_urls_for(full.get("media") or {}, SHARED_URL_TTL)
    return out
