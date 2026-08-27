"""Shareable meeting links: viewing without an account.

The real value of the Personal tier: you record a meeting, send a link, and
the other party views the transcript and plays the recording without
registering.

SECURITY PRINCIPLES (this is the ONLY unauthenticated data endpoint):

1. The token itself is the secret, `secrets.token_urlsafe(32)` (~256 bits).
   The database stores ONLY its SHA-256 digest, just like api_tokens: a DB
   leak therefore yields no working links.
2. The public projection is RESTRICTED (`_PUBLIC_FIELDS`). `workspace`,
   `meet_code`, `participants`, `speaker_sources`, `evaluation`,
   `screenshots` NEVER go out, these are internal or third-party data.
3. Revocable (`revoked_at`) and optionally expiring (`expires_at`).
4. Against guessing there is an IP-based rate limit on the caller side (app.py).
5. Viewing modifies nothing on the meeting, it only bumps a counter.
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

# What the shared view MAY receive. Every other column is deliberately omitted.
_PUBLIC_FIELDS = ("title", "created_at", "duration_sec", "transcript", "speakers")

# Lifetime of the shared playback URL (s). Shorter than the logged-in view's
# (6 hours), because it determines how long a revocation takes to take effect
# on ALREADY loaded pages. 30 minutes: a long recording can still be watched
# in one sitting, but a revocation really locks down within half an hour.
# (Reloading the page requests a fresh URL, so this does not limit the viewer.)
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
-- A single LIVE share per meeting: clicking "Share" again returns the same
-- link instead of invalidating the one the user has already sent out.
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
    """Return the live share, or create a new one.

    None if the meeting does not exist in this workspace (we do not leak
    existence information about other workspaces' meetings).

    IMPORTANT: if a live share already exists, the RAW token CANNOT be
    recovered (we only store its digest). In that case `token=None` is
    returned with an `exists=True` flag, from this the caller knows the
    link has already been issued, and if a new one is needed, the old one
    must be revoked first.
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
    """Revoke the existing link + issue a new one (if the old one leaked)."""
    if mtg.get_meeting(workspace, meeting_id) is None:
        return None
    revoke_share(workspace, meeting_id)
    return create_or_get_share(workspace, meeting_id, expires_days)


def revoke_share(workspace: str, meeting_id: str) -> bool:
    """Revoke the live share. True if there was anything to revoke."""
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE meeting_shares SET revoked_at=now()
               WHERE workspace=%s AND meeting_id=%s AND revoked_at IS NULL""",
            (workspace, meeting_id),
        )
        return cur.rowcount > 0


def share_status(workspace: str, meeting_id: str) -> dict[str, Any] | None:
    """The share's status for the owner (without the token)."""
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
    """PUBLIC resolution, the RESTRICTED view given to the link holder.

    None in every failure case (unknown/revoked/expired token, deleted
    meeting), the caller returns a uniform 404 so the response does not
    reveal which case occurred.
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
    if full is None:  # the meeting has been deleted since
        return None

    # RESTRICTED projection, nothing outside the whitelist goes out.
    out: dict[str, Any] = {k: full.get(k) for k in _PUBLIC_FIELDS}
    # The playback URL is generated with a SHORTER lifetime than in the
    # logged-in view: an issued presigned URL keeps working until its expiry
    # even after the link is revoked, so this is the revocation lead time.
    out["media_urls"] = mtg.media_urls_for(full.get("media") or {}, SHARED_URL_TTL)
    return out
