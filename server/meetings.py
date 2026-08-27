"""
Meeting store: Postgres (metadata+transcript) + Cloudflare R2 (media blobs).

The client (lavox.app webapp or hangar-dashboard) sends the metadata as JSON,
receives presigned PUT URLs for the large blobs, and uploads directly to R2;
the media does not flow through the VPS.

Env (all required to activate the module, otherwise the endpoints return 503):
  LAVOX_PG_DSN               postgresql://lavox:...@lead-db:5432/lavox
  LAVOX_R2_ACCOUNT_ID        Cloudflare account id
  LAVOX_R2_ACCESS_KEY_ID     R2 API token access key
  LAVOX_R2_SECRET_ACCESS_KEY R2 API token secret
  LAVOX_R2_BUCKET            e.g. lavox-media
"""

import os
import re
import uuid
from typing import Any

import boto3
import psycopg
from psycopg.types.json import Jsonb

PG_DSN = os.environ.get("LAVOX_PG_DSN", "")
R2_ACCOUNT_ID = os.environ.get("LAVOX_R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("LAVOX_R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("LAVOX_R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("LAVOX_R2_BUCKET", "")

UPLOAD_URL_TTL = 3600       # presigned PUT validity (s)
PLAYBACK_URL_TTL = 3600 * 6  # presigned GET validity (s)

MEDIA_KINDS = ("audio", "mic", "mixed", "video")
_EXT_RE = re.compile(r"^[a-z0-9]{1,8}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def available() -> bool:
    return bool(PG_DSN and R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET)


def _r2():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def _conn() -> psycopg.Connection:
    return psycopg.connect(PG_DSN)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meetings (
  id text,
  workspace text NOT NULL DEFAULT 'default',
  title text NOT NULL,
  type text NOT NULL DEFAULT 'meeting',
  created_at timestamptz NOT NULL,
  duration_sec double precision NOT NULL DEFAULT 0,
  speakers jsonb NOT NULL DEFAULT '[]',
  speaker_sources jsonb,
  participants jsonb,
  transcript jsonb NOT NULL DEFAULT '[]',
  evaluation jsonb,
  screenshots jsonb,
  media jsonb NOT NULL DEFAULT '{}',
  meet_code text,
  source text,
  status text NOT NULL DEFAULT 'pending',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace, id)
);
CREATE INDEX IF NOT EXISTS meetings_ws_created ON meetings (workspace, created_at DESC);
"""

# Migration from the old GLOBAL (id) primary key to the (workspace, id) pair.
#
# Why: the meeting id is sent by the client and generated in a predictable
# form (`rec-<epoch_ms>`). With a global key, the id space is SHARED between
# tenants, so an attacker could reserve ids in advance and the victim's
# upload would permanently conflict (the recording would never make it up),
# while the 409/200 difference would reveal when a foreign workspace finished
# a recording. With a composite key, two tenants' identical ids structurally
# cannot collide.
#
# Idempotent: only runs when the current primary key is single-column.
MIGRATE_PK_SQL = """
DO $$
DECLARE
  pk_name text;
  pk_cols int;
BEGIN
  SELECT c.conname, cardinality(c.conkey)
    INTO pk_name, pk_cols
  FROM pg_constraint c
  JOIN pg_class t ON t.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
  WHERE t.relname = 'meetings' AND c.contype = 'p' AND n.nspname = current_schema();

  IF pk_name IS NOT NULL AND pk_cols = 1 THEN
    EXECUTE format('ALTER TABLE meetings DROP CONSTRAINT %I', pk_name);
    ALTER TABLE meetings ADD PRIMARY KEY (workspace, id);
    RAISE NOTICE 'meetings PK migrated: (id) -> (workspace, id)';
  END IF;
END $$;
"""


def init_schema() -> None:
    with _conn() as conn:
        conn.execute(SCHEMA_SQL)
        conn.execute(MIGRATE_PK_SQL)


CORS_ORIGINS = [
    "http://localhost:5190",
    "http://127.0.0.1:5190",
    "https://lavox.app",
    "https://app.lavox.cloud",
]


def ensure_bucket_cors() -> None:
    """Direct browser-to-R2 upload (presigned PUT) requires bucket CORS."""
    _r2().put_bucket_cors(
        Bucket=R2_BUCKET,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedOrigins": CORS_ORIGINS,
                    "AllowedMethods": ["PUT", "GET", "HEAD"],
                    "AllowedHeaders": ["*"],
                    "MaxAgeSeconds": 3600,
                }
            ]
        },
    )


def _media_key(workspace: str, meeting_id: str, kind: str, ext: str) -> str:
    return f"{workspace}/{meeting_id}/{kind}.{ext}"


def create_meeting(workspace: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Save metadata + presigned PUT URLs for the requested media files."""
    meeting_id = str(payload.get("id") or f"mtg_{uuid.uuid4().hex[:12]}")
    if not _ID_RE.match(meeting_id):
        raise ValueError("Invalid meeting id (A-Za-z0-9_-, max 64).")

    media_req: dict[str, Any] = payload.get("media") or {}
    media_meta: dict[str, Any] = {}
    upload_urls: dict[str, str] = {}
    r2 = _r2() if media_req else None
    for kind, spec in media_req.items():
        if kind not in MEDIA_KINDS:
            raise ValueError(f"Unknown media kind: {kind} (supported: {', '.join(MEDIA_KINDS)})")
        ext = str((spec or {}).get("ext", "")).lower()
        if not _EXT_RE.match(ext):
            raise ValueError(f"Invalid extension for the {kind} track: {ext!r}")
        key = _media_key(workspace, meeting_id, kind, ext)
        media_meta[kind] = {"key": key, "ext": ext, "uploaded": False}
        upload_urls[kind] = r2.generate_presigned_url(
            "put_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=UPLOAD_URL_TTL,
        )

    status = "pending" if media_meta else "complete"
    with _conn() as conn:
        # The key is (workspace, id), so ON CONFLICT can only match the row
        # with the same id in the OWN workspace: a foreign tenant's row can
        # neither be overwritten nor blocked with a "reserved id".
        conn.execute(
            """
            INSERT INTO meetings
              (id, workspace, title, type, created_at, duration_sec, speakers,
               speaker_sources, participants, transcript, evaluation, screenshots,
               media, meet_code, source, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (workspace, id) DO UPDATE SET
              title=EXCLUDED.title, duration_sec=EXCLUDED.duration_sec,
              speakers=EXCLUDED.speakers, speaker_sources=EXCLUDED.speaker_sources,
              participants=EXCLUDED.participants, transcript=EXCLUDED.transcript,
              evaluation=EXCLUDED.evaluation, screenshots=EXCLUDED.screenshots,
              media=EXCLUDED.media, meet_code=EXCLUDED.meet_code,
              source=EXCLUDED.source, status=EXCLUDED.status, updated_at=now()
            """,
            (
                meeting_id,
                workspace,
                # "Névtelen felvétel" = "Untitled recording". Kept in Hungarian:
                # it is a user-visible default title stored in the DB for the
                # Hungarian-language product UI.
                str(payload.get("title") or "Névtelen felvétel"),
                str(payload.get("type") or "meeting"),
                payload.get("created_at"),
                float(payload.get("duration_sec") or 0),
                Jsonb(payload.get("speakers") or []),
                Jsonb(payload.get("speaker_sources")) if payload.get("speaker_sources") is not None else None,
                Jsonb(payload.get("participants")) if payload.get("participants") is not None else None,
                Jsonb(payload.get("transcript") or []),
                Jsonb(payload.get("evaluation")) if payload.get("evaluation") is not None else None,
                Jsonb(payload.get("screenshots")) if payload.get("screenshots") is not None else None,
                Jsonb(media_meta),
                payload.get("meet_code"),
                payload.get("source"),
                status,
            ),
        )
    return {"id": meeting_id, "status": status, "upload_urls": upload_urls}


def save_meeting_direct(
    workspace: str,
    meeting_id: str,
    meta: dict[str, Any],
    media_files: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    """Server-side DIRECT save: uploads the files sitting on the server to R2
    (not via presigned URLs) + metadata to Postgres. Called by /api/transcribe
    when auto_save=true → the recording lands in the webapp BY ITSELF after
    transcription. media_files: {kind: (local_path, ext)}.
    """
    if not _ID_RE.match(meeting_id):
        raise ValueError("Invalid meeting id.")
    r2 = _r2() if media_files else None
    media_meta: dict[str, Any] = {}
    for kind, (path, ext) in media_files.items():
        if kind not in MEDIA_KINDS or not _EXT_RE.match(ext.lower()):
            continue
        key = _media_key(workspace, meeting_id, kind, ext.lower())
        r2.upload_file(path, R2_BUCKET, key)
        media_meta[kind] = {"key": key, "ext": ext.lower(), "uploaded": True}

    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO meetings
              (id, workspace, title, type, created_at, duration_sec, speakers,
               speaker_sources, participants, transcript, evaluation, screenshots,
               media, meet_code, source, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (workspace, id) DO UPDATE SET
              title=EXCLUDED.title, duration_sec=EXCLUDED.duration_sec,
              speakers=EXCLUDED.speakers, transcript=EXCLUDED.transcript,
              media=meetings.media || EXCLUDED.media, status=EXCLUDED.status,
              updated_at=now()
            """,
            (
                meeting_id, workspace,
                # "Névtelen felvétel" = "Untitled recording", see create_meeting.
                str(meta.get("title") or "Névtelen felvétel"),
                str(meta.get("type") or "meeting"),
                meta.get("created_at"),
                float(meta.get("duration_sec") or 0),
                Jsonb(meta.get("speakers") or []),
                None, None,
                Jsonb(meta.get("transcript") or []),
                None, None,
                Jsonb(media_meta),
                meta.get("meet_code"),
                meta.get("source") or "auto",
                "complete",
            ),
        )
    return {"id": meeting_id, "status": "complete", "media_kinds": sorted(media_meta.keys())}


def complete_meeting(workspace: str, meeting_id: str) -> dict[str, Any]:
    """Verify blob uploads (head_object) and finalize the meeting."""
    row = _get_row(workspace, meeting_id)
    if row is None:
        raise KeyError(meeting_id)
    media: dict[str, Any] = row["media"] or {}
    r2 = _r2()
    missing = []
    for kind, meta in media.items():
        try:
            r2.head_object(Bucket=R2_BUCKET, Key=meta["key"])
            meta["uploaded"] = True
        except Exception:
            missing.append(kind)
    status = "complete" if not missing else "pending"
    with _conn() as conn:
        conn.execute(
            "UPDATE meetings SET media=%s, status=%s, updated_at=now() WHERE id=%s AND workspace=%s",
            (Jsonb(media), status, meeting_id, workspace),
        )
    return {"id": meeting_id, "status": status, "missing": missing}


_LIST_COLS = "id, title, type, created_at, duration_sec, speakers, media, meet_code, source, status, updated_at"


def list_meetings(workspace: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        cur = conn.execute(
            f"SELECT {_LIST_COLS} FROM meetings WHERE workspace=%s ORDER BY created_at DESC",
            (workspace,),
        )
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]
    out = []
    for row in rows:
        item = dict(zip(cols, row))
        item["created_at"] = item["created_at"].isoformat()
        item["updated_at"] = item["updated_at"].isoformat()
        item["media_kinds"] = sorted((item.pop("media") or {}).keys())
        out.append(item)
    return out


def _get_row(workspace: str, meeting_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        cur = conn.execute(
            "SELECT * FROM meetings WHERE id=%s AND workspace=%s", (meeting_id, workspace)
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
    return dict(zip(cols, row))


def media_urls_for(media: dict[str, Any], ttl: int) -> dict[str, str]:
    """Playback URLs with a CUSTOM lifetime.

    The shared (public) view gets a shorter TTL than the logged-in one: a
    once-issued presigned URL keeps working until its expiry even AFTER the
    link is revoked, so the TTL is the effective revocation lead time.
    """
    if not media:
        return {}
    r2 = _r2()
    return {
        kind: r2.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": meta["key"]},
            ExpiresIn=ttl,
        )
        for kind, meta in media.items()
        if meta.get("uploaded")
    }


def get_meeting(workspace: str, meeting_id: str) -> dict[str, Any] | None:
    row = _get_row(workspace, meeting_id)
    if row is None:
        return None
    row["created_at"] = row["created_at"].isoformat()
    row["updated_at"] = row["updated_at"].isoformat()
    media: dict[str, Any] = row.get("media") or {}
    if media:
        r2 = _r2()
        row["media_urls"] = {
            kind: r2.generate_presigned_url(
                "get_object",
                Params={"Bucket": R2_BUCKET, "Key": meta["key"]},
                ExpiresIn=PLAYBACK_URL_TTL,
            )
            for kind, meta in media.items()
            if meta.get("uploaded")
        }
    else:
        row["media_urls"] = {}
    return row


_PATCHABLE = {"title", "speakers", "speaker_sources", "evaluation", "transcript"}
_JSONB_FIELDS = {"speakers", "speaker_sources", "evaluation", "transcript"}


def patch_meeting(workspace: str, meeting_id: str, updates: dict[str, Any]) -> bool:
    fields = {k: v for k, v in updates.items() if k in _PATCHABLE}
    if not fields:
        return False
    sets = ", ".join(f"{k}=%s" for k in fields)
    values = [Jsonb(v) if k in _JSONB_FIELDS else v for k, v in fields.items()]
    with _conn() as conn:
        cur = conn.execute(
            f"UPDATE meetings SET {sets}, updated_at=now() WHERE id=%s AND workspace=%s",
            (*values, meeting_id, workspace),
        )
        return cur.rowcount > 0


def delete_meeting(workspace: str, meeting_id: str) -> bool:
    row = _get_row(workspace, meeting_id)
    if row is None:
        return False
    media: dict[str, Any] = row.get("media") or {}
    if media:
        r2 = _r2()
        for meta in media.values():
            try:
                r2.delete_object(Bucket=R2_BUCKET, Key=meta["key"])
            except Exception:
                pass  # deleting the DB row matters more; the orphan object can be cleaned up later
    with _conn() as conn:
        conn.execute(
            "DELETE FROM meetings WHERE id=%s AND workspace=%s", (meeting_id, workspace)
        )
    return True
