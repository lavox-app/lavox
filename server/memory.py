"""Lavox Memory: fusion memory core (M0).

A personal memory built from speech (meetings, dictation), read by any AI
tool over MCP. There is no dedicated search UI: Lavox is a base layer here,
the interface is Claude Code / ChatGPT / anything.

THREE LAYERS:
  1. VERBATIM (canonical): the raw transcript in ~400-token chunks with
     context headers. Never deleted. Controlled measurements (Letta,
     "verbatim chunks beat lossy extraction") show the raw layer is strong
     on its own, so THIS is the store's source of truth, not the extracted
     layer.
  2. ASSERTION (extracted index, populated from M1): typed assertions
     (decision/fact/preference/commitment) with bitemporal columns.
     Bitemporality here is a SCHEMA, not a framework: occurred_at +
     invalidated_at + superseded_by provide "what did we believe in July"
     queryability without LLM-per-write cost or an external graph DB.
  3. PROFILE CORE (M1): ~500-token summary for session-start injection.

FUSION: 4 parallel lists (chunk-vector, chunk-FTS, assertion-vector,
assertion-FTS) → RRF (k=60) → cross-linking (a chunk hit pulls in the LIVE
assertions pointing at it, an assertion hit pulls in its source chunk) →
additive, bounded recency bonus ONLY for time-sensitive questions.
Multiplicative age-decay is a proven recall killer (own measurement:
19/20 → 4/20).

STORAGE: SQLite (WAL) + sqlite-vec + FTS5, one file, zero ops on the
user's machine. The cloud tier (M2) mirrors the same schema in Postgres.
Vectors are tagged with a model_id: two models' vectors can coexist during
a migration, and self-hosters embed with whatever they want.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MEMORY_DIR = Path(os.environ.get("LAVOX_MEMORY_DIR", str(Path.home() / "Lavox" / "memory")))
DB_PATH = MEMORY_DIR / "lavox-memory.db"

# The first supported model is chosen. The default is small (fast first
# experience, HU+EN); the schema is model-agnostic, swapping is done via
# parallel re-embedding.
PREFERRED_MODELS = [
    ("intfloat/multilingual-e5-small", 384, "e5"),
    ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", 384, "plain"),
    ("intfloat/multilingual-e5-large", 1024, "e5"),
]

# ~400 tokens ≈ ~1400 chars (Hungarian tokenization is more wasteful; this is conservative)
CHUNK_MAX_CHARS = 1400
CHUNK_MIN_CHARS = 120          # neighbouring segments shorter than this get merged
RRF_K = 60
PER_LIST_LIMIT = 50
RECENCY_GAMMA = 0.15           # at most 15% of the max RRF score, additive, bounded
RECENCY_HALFLIFE_DAYS = 30.0

# Time-sensitive question patterns (HU+EN). If none match, the recency term is 0.
# NOTE: the Hungarian words in this regex are functional data, they are
# matched against Hungarian user queries. Do not translate them.
_TIME_SENSITIVE = re.compile(
    r"\b(ma|mai|tegnap|legut[oó]bb|mostan[aá]ban|m[uú]lt h[eé]t|h[eé]ten|"
    r"friss|jelenleg|aktu[aá]lis|utols[oó]|"
    r"today|yesterday|recent|recently|last week|this week|latest|current|now)\b",
    re.IGNORECASE,
)

_embedder = None
_embedder_meta: tuple[str, int, str] | None = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS recordings (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,            -- meeting | dictation | note
  title       TEXT,
  occurred_at TEXT NOT NULL,            -- ISO, event time
  duration_sec REAL,
  participants TEXT,                    -- JSON list
  meta        TEXT,                     -- JSON
  ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS chunks (
  id           INTEGER PRIMARY KEY,
  recording_id TEXT NOT NULL REFERENCES recordings(id),
  seq          INTEGER NOT NULL,
  speaker      TEXT,
  t_start      REAL,
  t_end        REAL,
  text         TEXT NOT NULL,           -- raw text (canonical)
  header       TEXT NOT NULL,           -- context header (embedded + in FTS)
  UNIQUE (recording_id, seq)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, header, content=chunks, content_rowid=id
);

-- Assertion layer (populated by M1; the schema and search are ready now).
-- The essence of bitemporality is three columns, no graph framework needed:
--   occurred_at   = when it was said (event time)
--   recorded_at   = when it entered the memory (record time)
--   invalidated_at + superseded_by = supersedes chain, NOTHING is ever deleted
CREATE TABLE IF NOT EXISTS assertions (
  id            INTEGER PRIMARY KEY,
  type          TEXT NOT NULL,          -- decision | fact | preference | commitment | task
  text          TEXT NOT NULL,          -- self-contained wording
  data          TEXT,                   -- JSON: for decisions {chosen, alternatives[], reasoning}
  source        TEXT NOT NULL,          -- extracted | user_stated | agent  (poisoning defense)
  source_chunk_id INTEGER REFERENCES chunks(id),
  occurred_at   TEXT NOT NULL,
  recorded_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  invalidated_at TEXT,
  superseded_by INTEGER REFERENCES assertions(id),
  confidence    REAL
);

CREATE VIRTUAL TABLE IF NOT EXISTS assertions_fts USING fts5(
  text, content=assertions, content_rowid=id
);

-- Which embedding model is active. Vectors live in model-tagged vec0 tables
-- (vec_chunk_<tag>, vec_assertion_<tag>), a model swap = filling a new table
-- in parallel, switching over, dropping the old one. No downtime.
CREATE TABLE IF NOT EXISTS embedding_models (
  model_id  TEXT PRIMARY KEY,
  dim       INTEGER NOT NULL,
  query_style TEXT NOT NULL,            -- 'e5' (query:/passage: prefix) | 'plain'
  is_active INTEGER NOT NULL DEFAULT 0,
  added_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""


def _model_tag(model_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", model_id.lower()).strip("_")[-40:]


def connect() -> sqlite3.Connection:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")       # multiple clients, no corruption
    db.execute("PRAGMA busy_timeout=5000")
    db.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript(SCHEMA_SQL)
    return db


def _ensure_vec_tables(db: sqlite3.Connection, model_id: str, dim: int) -> None:
    tag = _model_tag(model_id)
    for base in ("chunk", "assertion"):
        db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_{base}_{tag} "
            f"USING vec0(embedding float[{dim}])"
        )


# ---------------------------------------------------------------------------
# Embedding (fastembed: ONNX, local, model-swappable)
# ---------------------------------------------------------------------------

def _pick_model() -> tuple[str, int, str]:
    from fastembed import TextEmbedding
    supported = {m["model"] for m in TextEmbedding.list_supported_models()}
    for model_id, dim, style in PREFERRED_MODELS:
        if model_id in supported:
            return model_id, dim, style
    raise RuntimeError(
        "None of the preferred embedding models are supported in this fastembed version. "
        f"Supported: {sorted(supported)[:10]}…"
    )


def get_embedder():
    """Lazy, process-level singleton, the model is loaded once."""
    global _embedder, _embedder_meta
    if _embedder is None:
        from fastembed import TextEmbedding
        model_id, dim, style = _pick_model()
        _embedder = TextEmbedding(model_name=model_id)
        _embedder_meta = (model_id, dim, style)
    return _embedder, _embedder_meta


def active_model(db: sqlite3.Connection) -> tuple[str, int, str]:
    row = db.execute(
        "SELECT model_id, dim, query_style FROM embedding_models WHERE is_active=1"
    ).fetchone()
    if row:
        return row[0], row[1], row[2]
    _, meta = get_embedder()
    model_id, dim, style = meta
    db.execute(
        "INSERT OR REPLACE INTO embedding_models (model_id, dim, query_style, is_active) "
        "VALUES (?,?,?,1)",
        (model_id, dim, style),
    )
    _ensure_vec_tables(db, model_id, dim)
    db.commit()
    return model_id, dim, style


def _serialize(vec: Iterable[float]) -> bytes:
    v = list(vec)
    return struct.pack(f"{len(v)}f", *v)


def embed_passages(texts: list[str], style: str) -> list[bytes]:
    emb, _ = get_embedder()
    prepped = [f"passage: {t}" if style == "e5" else t for t in texts]
    return [_serialize(v) for v in emb.embed(prepped)]


def embed_query(text: str, style: str) -> bytes:
    emb, _ = get_embedder()
    prepped = f"query: {text}" if style == "e5" else text
    return _serialize(next(iter(emb.embed([prepped]))))


# ---------------------------------------------------------------------------
# Chunking: split on speaker change, with a context header
# ---------------------------------------------------------------------------

def _fmt_ts(sec: float | None) -> str:
    if sec is None:
        return "?"
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def build_chunks(recording: dict[str, Any], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Transcript segments → chunks with context headers.

    Rules (measurements show the chunking algorithm is second-order, the
    header is first-order, hence a simple algorithm and a rich header):
      - always a new chunk on speaker change,
      - segments of the same speaker merged up to CHUNK_MAX_CHARS,
      - tiny segments (< CHUNK_MIN_CHARS) do not get a chunk of their own.
    """
    date = (recording.get("occurred_at") or "")[:10]
    # "(cím nélkül)" = "(untitled)". Kept in Hungarian: the header is embedded
    # and FTS-indexed alongside Hungarian transcripts, and already-ingested
    # chunks in existing databases contain the Hungarian header labels.
    title = recording.get("title") or "(cím nélkül)"
    kind = recording.get("kind") or "meeting"
    participants = recording.get("participants") or []
    part_str = ", ".join(participants) if participants else ""

    groups: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker = seg.get("speaker") or ""
        same_speaker = cur is not None and cur["speaker"] == speaker
        fits = cur is not None and len(cur["text"]) + len(text) + 1 <= CHUNK_MAX_CHARS
        tiny = cur is not None and len(cur["text"]) < CHUNK_MIN_CHARS
        # tiny groups keep rolling forward even across a speaker change, so
        # unsearchable crumbs ("igen", "oké") don't end up as standalone chunks
        if cur is not None and (same_speaker and fits or (tiny and fits)):
            cur["text"] += " " + text
            cur["t_end"] = seg.get("end", cur["t_end"])
            if not same_speaker and speaker:
                cur["speaker"] = cur["speaker"] or speaker
        else:
            if cur is not None:
                groups.append(cur)
            cur = {
                "speaker": speaker,
                "t_start": seg.get("start"),
                "t_end": seg.get("end"),
                "text": text,
            }
    if cur is not None:
        groups.append(cur)

    chunks = []
    for i, g in enumerate(groups):
        # Context injection: the header goes into the embedded text AND into
        # FTS. Anthropic measured a 35-49% drop in retrieval failures from this,
        # and for us the metadata is free (it comes from the recording), no LLM needed.
        head_bits = [date, kind, title]
        # The "résztvevők:" (participants) and "beszélő:" (speaker) labels are
        # functional data: they are embedded/FTS-indexed with Hungarian
        # transcripts and must stay identical to headers already stored in
        # existing databases.
        if part_str:
            head_bits.append(f"résztvevők: {part_str}")
        if g["speaker"]:
            head_bits.append(f"beszélő: {g['speaker']} @ {_fmt_ts(g['t_start'])}")
        header = "[" + " | ".join(b for b in head_bits if b) + "]"
        chunks.append({
            "seq": i,
            "speaker": g["speaker"] or None,
            "t_start": g["t_start"],
            "t_end": g["t_end"],
            "text": g["text"],
            "header": header,
        })
    return chunks


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def ingest_recording(
    db: sqlite3.Connection,
    recording: dict[str, Any],
    segments: list[dict[str, Any]],
    force: bool = False,
) -> dict[str, Any]:
    """Ingest one recording (meeting/dictation) into the memory. Idempotent."""
    rid = recording["id"]
    exists = db.execute("SELECT 1 FROM recordings WHERE id=?", (rid,)).fetchone()
    if exists and not force:
        return {"id": rid, "status": "skipped (already ingested)"}
    if exists and force:
        _delete_recording(db, rid)

    model_id, dim, style = active_model(db)
    tag = _model_tag(model_id)
    chunks = build_chunks(recording, segments)
    if not chunks:
        return {"id": rid, "status": "empty transcript, skipped"}

    db.execute(
        "INSERT INTO recordings (id, kind, title, occurred_at, duration_sec, participants, meta) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            rid,
            recording.get("kind") or "meeting",
            recording.get("title"),
            recording.get("occurred_at") or datetime.now(timezone.utc).isoformat(),
            recording.get("duration_sec"),
            json.dumps(recording.get("participants") or [], ensure_ascii=False),
            json.dumps(recording.get("meta") or {}, ensure_ascii=False),
        ),
    )

    texts = [f"{c['header']}\n{c['text']}" for c in chunks]
    vectors = embed_passages(texts, style)

    for c, vec in zip(chunks, vectors):
        cur = db.execute(
            "INSERT INTO chunks (recording_id, seq, speaker, t_start, t_end, text, header) "
            "VALUES (?,?,?,?,?,?,?)",
            (rid, c["seq"], c["speaker"], c["t_start"], c["t_end"], c["text"], c["header"]),
        )
        chunk_id = cur.lastrowid
        db.execute(
            "INSERT INTO chunks_fts (rowid, text, header) VALUES (?,?,?)",
            (chunk_id, c["text"], c["header"]),
        )
        db.execute(
            f"INSERT INTO vec_chunk_{tag} (rowid, embedding) VALUES (?,?)",
            (chunk_id, vec),
        )
    db.commit()
    return {"id": rid, "status": "ok", "chunks": len(chunks), "model": model_id}


def _delete_recording(db: sqlite3.Connection, rid: str) -> None:
    """Internal helper for force-reingest. The verbatim layer is never deleted
    through the normal path."""
    rows = db.execute("SELECT id FROM chunks WHERE recording_id=?", (rid,)).fetchall()
    ids = [r[0] for r in rows]
    for model_row in db.execute("SELECT model_id FROM embedding_models").fetchall():
        tag = _model_tag(model_row[0])
        for cid in ids:
            db.execute(f"DELETE FROM vec_chunk_{tag} WHERE rowid=?", (cid,))
    for cid in ids:
        db.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
    db.execute("DELETE FROM chunks WHERE recording_id=?", (rid,))
    db.execute("DELETE FROM recordings WHERE id=?", (rid,))


# ---------------------------------------------------------------------------
# Search: 4 lists → RRF → cross-linking → conditional recency
# ---------------------------------------------------------------------------

def _fts_query(user_query: str) -> str:
    """User text → safe FTS5 query.

    Every token is quoted (no syntax injection); tokens of 4+ characters get
    a prefix star, cheap protection against Hungarian suffixes ("szerződés"
    finds "szerződésről"). Tokens are implicitly AND-ed; if AND would return
    nothing, the caller can switch to OR.
    """
    tokens = re.findall(r"\w+", user_query, re.UNICODE)
    parts = []
    for t in tokens:
        if len(t) >= 4:
            parts.append(f'"{t}"*')
        elif t:
            parts.append(f'"{t}"')
    # OR mode: BM25 ranks multi-match results first anyway, while AND would
    # zero out the lexical branch as soon as a single question word is missing
    # from the chunk ("what did we DISCUSS about project X", 'discuss' is not
    # in the transcript).
    return " OR ".join(parts) if parts else '""'


def _rrf_merge(lists: dict[str, list[tuple[str, int]]]) -> dict[str, float]:
    """key: 'chunk:<id>' / 'assertion:<id>' → RRF score."""
    scores: dict[str, float] = {}
    for _name, ranked in lists.items():
        for key, rank in ranked:
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
    return scores


def search(
    db: sqlite3.Connection,
    query: str,
    limit: int = 8,
    include_superseded: bool = False,
    kind: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    model_id, dim, style = active_model(db)
    tag = _model_tag(model_id)
    qvec = embed_query(query, style)
    fts_q = _fts_query(query)

    lists: dict[str, list[tuple[str, int]]] = {}

    rows = db.execute(
        f"SELECT rowid FROM vec_chunk_{tag} WHERE embedding MATCH ? "
        f"ORDER BY distance LIMIT ?",
        (qvec, PER_LIST_LIMIT),
    ).fetchall()
    lists["chunk_vec"] = [(f"chunk:{r[0]}", i + 1) for i, r in enumerate(rows)]

    try:
        rows = db.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
            "ORDER BY bm25(chunks_fts, 1.0, 2.0) LIMIT ?",
            (fts_q, PER_LIST_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    lists["chunk_fts"] = [(f"chunk:{r[0]}", i + 1) for i, r in enumerate(rows)]

    # Assertion lists, empty in M0, live from M1; the fusion is ready.
    rows = db.execute(
        f"SELECT rowid FROM vec_assertion_{tag} WHERE embedding MATCH ? "
        f"ORDER BY distance LIMIT ?",
        (qvec, PER_LIST_LIMIT),
    ).fetchall()
    lists["assertion_vec"] = [(f"assertion:{r[0]}", i + 1) for i, r in enumerate(rows)]

    try:
        rows = db.execute(
            "SELECT rowid FROM assertions_fts WHERE assertions_fts MATCH ? "
            "ORDER BY bm25(assertions_fts) LIMIT ?",
            (fts_q, PER_LIST_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    lists["assertion_fts"] = [(f"assertion:{r[0]}", i + 1) for i, r in enumerate(rows)]

    scores = _rrf_merge(lists)
    if not scores:
        return []

    # Conditional, additive, bounded recency, NEVER a multiplier.
    if _TIME_SENSITIVE.search(query):
        max_score = max(scores.values())
        now = time.time()
        for key in list(scores.keys()):
            occ = _occurred_at_for(db, key)
            if occ is None:
                continue
            age_days = max(0.0, (now - occ) / 86400.0)
            scores[key] += RECENCY_GAMMA * max_score * math.exp(-age_days / RECENCY_HALFLIFE_DAYS)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    # Per-recording cap (Cerebras pattern: "dedup + per-file cap → diverse
    # top-N"). Without it, the chunks of a single long meeting could crowd
    # shorter but more relevant recordings out of the final result.
    per_recording_cap = max(2, limit // 3)
    rec_count: dict[str, int] = {}

    results = []
    for key, score in ranked:
        item = _hydrate(db, key, include_superseded=include_superseded)
        if item is None:
            continue
        if kind and item.get("recording_kind") != kind:
            continue
        if since and (item.get("occurred_at") or "") < since:
            continue
        rec = item.get("recording_id") or item.get("id")
        if rec_count.get(rec, 0) >= per_recording_cap:
            continue
        rec_count[rec] = rec_count.get(rec, 0) + 1
        item["score"] = round(score, 5)
        results.append(item)
        if len(results) >= limit:
            break
    return results


def _occurred_at_for(db: sqlite3.Connection, key: str) -> float | None:
    typ, _, raw_id = key.partition(":")
    try:
        if typ == "chunk":
            row = db.execute(
                "SELECT r.occurred_at FROM chunks c JOIN recordings r ON r.id=c.recording_id "
                "WHERE c.id=?",
                (int(raw_id),),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT occurred_at FROM assertions WHERE id=?", (int(raw_id),)
            ).fetchone()
        if not row or not row[0]:
            return None
        return datetime.fromisoformat(row[0].replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _snippet(text: str, max_chars: int = 320) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _hydrate(db: sqlite3.Connection, key: str, include_superseded: bool) -> dict[str, Any] | None:
    typ, _, raw_id = key.partition(":")
    oid = int(raw_id)
    if typ == "chunk":
        row = db.execute(
            "SELECT c.id, c.text, c.header, c.speaker, c.t_start, c.recording_id, "
            "r.title, r.kind, r.occurred_at FROM chunks c "
            "JOIN recordings r ON r.id = c.recording_id WHERE c.id=?",
            (oid,),
        ).fetchone()
        if not row:
            return None
        # Cross-linking: LIVE assertions pointing at the chunk (hits from M1 on)
        assertions = db.execute(
            "SELECT id, type, text FROM assertions WHERE source_chunk_id=? "
            "AND invalidated_at IS NULL LIMIT 5",
            (oid,),
        ).fetchall()
        return {
            "id": f"chunk:{row[0]}",
            "layer": "verbatim",
            "snippet": _snippet(row[1]),
            "header": row[2],
            "speaker": row[3],
            "at_seconds": row[4],
            "recording_id": row[5],
            "recording_title": row[6],
            "recording_kind": row[7],
            "occurred_at": row[8],
            "linked_assertions": [
                {"id": f"assertion:{a[0]}", "type": a[1], "text": a[2]} for a in assertions
            ],
        }
    else:
        row = db.execute(
            "SELECT id, type, text, data, occurred_at, invalidated_at, superseded_by, "
            "source, source_chunk_id FROM assertions WHERE id=?",
            (oid,),
        ).fetchone()
        if not row:
            return None
        if row[5] is not None and not include_superseded:
            return None
        return {
            "id": f"assertion:{row[0]}",
            "layer": "assertion",
            "type": row[1],
            "snippet": _snippet(row[2]),
            "data": json.loads(row[3]) if row[3] else None,
            "occurred_at": row[4],
            "status": "superseded" if row[5] else "active",
            "superseded_by": f"assertion:{row[6]}" if row[6] else None,
            "source": row[7],
            "source_chunk": f"chunk:{row[8]}" if row[8] else None,
        }


def fetch(db: sqlite3.Connection, item_id: str, context: bool = True) -> dict[str, Any] | None:
    """Full content by id, for chunks, together with the neighbouring chunks
    (context expansion on the winner, not the whole corpus, Cerebras pattern)."""
    typ, _, raw_id = item_id.partition(":")
    oid = int(raw_id)
    if typ == "chunk":
        row = db.execute(
            "SELECT c.id, c.text, c.header, c.speaker, c.t_start, c.t_end, c.seq, "
            "c.recording_id, r.title, r.kind, r.occurred_at, r.participants "
            "FROM chunks c JOIN recordings r ON r.id=c.recording_id WHERE c.id=?",
            (oid,),
        ).fetchone()
        if not row:
            return None
        out = {
            "id": f"chunk:{row[0]}",
            "text": row[1],
            "header": row[2],
            "speaker": row[3],
            "t_start": row[4],
            "t_end": row[5],
            "recording": {
                "id": row[7], "title": row[8], "kind": row[9],
                "occurred_at": row[10],
                "participants": json.loads(row[11]) if row[11] else [],
            },
        }
        if context:
            neigh = db.execute(
                "SELECT seq, speaker, text FROM chunks WHERE recording_id=? "
                "AND seq BETWEEN ? AND ? AND id != ? ORDER BY seq",
                (row[7], row[6] - 1, row[6] + 1, oid),
            ).fetchall()
            out["context"] = [
                {"seq": n[0], "speaker": n[1], "text": _snippet(n[2], 500)} for n in neigh
            ]
        return out
    else:
        item = _hydrate(db, item_id, include_superseded=True)
        if item and item.get("source_chunk"):
            item["source_chunk_full"] = fetch(db, item["source_chunk"], context=False)
        return item


def timeline(
    db: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
    kind: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Chronological view, provides what semantic search does poorly."""
    q = ("SELECT id, kind, title, occurred_at, duration_sec FROM recordings WHERE 1=1")
    args: list[Any] = []
    if start:
        q += " AND occurred_at >= ?"; args.append(start)
    if end:
        q += " AND occurred_at <= ?"; args.append(end)
    if kind:
        q += " AND kind = ?"; args.append(kind)
    q += " ORDER BY occurred_at DESC LIMIT ?"; args.append(limit)
    rows = db.execute(q, args).fetchall()
    return [
        {"id": r[0], "kind": r[1], "title": r[2], "occurred_at": r[3], "duration_sec": r[4]}
        for r in rows
    ]


def stats(db: sqlite3.Connection) -> dict[str, Any]:
    model_id, dim, _ = active_model(db)
    n_rec = db.execute("SELECT COUNT(*), MIN(occurred_at), MAX(occurred_at) FROM recordings").fetchone()
    n_chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_assert = db.execute(
        "SELECT COUNT(*), SUM(CASE WHEN invalidated_at IS NULL THEN 1 ELSE 0 END) FROM assertions"
    ).fetchone()
    by_kind = dict(db.execute("SELECT kind, COUNT(*) FROM recordings GROUP BY kind").fetchall())
    return {
        "recordings": n_rec[0],
        "oldest": n_rec[1],
        "newest": n_rec[2],
        "by_kind": by_kind,
        "chunks": n_chunks,
        "assertions_total": n_assert[0] or 0,
        "assertions_active": n_assert[1] or 0,
        "embedding_model": model_id,
        "dim": dim,
        "db_path": str(DB_PATH),
    }


# ---------------------------------------------------------------------------
# Assertion layer, write side (M1)
# ---------------------------------------------------------------------------

def save_assertion(
    db: sqlite3.Connection,
    *,
    type: str,
    text: str,
    source: str,
    occurred_at: str,
    data: dict[str, Any] | None = None,
    source_chunk_id: int | None = None,
    confidence: float | None = None,
) -> int:
    """Save a new assertion, embedding + FTS + vec in one transaction.

    `source` is mandatory and a closed set (poisoning defense): 'extracted'
    (extracted from a transcript by an LLM, with source_chunk_id),
    'user_stated' (explicitly stated by the user), 'agent' (saved by an AI
    tool over MCP).
    """
    if source not in ("extracted", "user_stated", "agent"):
        raise ValueError(f"invalid source: {source!r}")
    model_id, dim, style = active_model(db)
    tag = _model_tag(model_id)
    vec = embed_passages([text], style)[0]
    cur = db.execute(
        "INSERT INTO assertions (type, text, data, source, source_chunk_id, "
        "occurred_at, confidence) VALUES (?,?,?,?,?,?,?)",
        (
            type,
            text,
            json.dumps(data, ensure_ascii=False) if data else None,
            source,
            source_chunk_id,
            occurred_at,
            confidence,
        ),
    )
    aid = cur.lastrowid
    db.execute("INSERT INTO assertions_fts (rowid, text) VALUES (?,?)", (aid, text))
    db.execute(f"INSERT INTO vec_assertion_{tag} (rowid, embedding) VALUES (?,?)", (aid, vec))
    db.commit()
    return aid


def supersede_assertion(db: sqlite3.Connection, old_id: int, new_id: int) -> bool:
    """Mark the old assertion as superseded. NEVER a deletion: the
    invalidated_at + superseded_by chain provides the "what did we believe
    in July" queryability."""
    cur = db.execute(
        "UPDATE assertions SET invalidated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
        "superseded_by=? WHERE id=? AND invalidated_at IS NULL",
        (new_id, old_id),
    )
    db.commit()
    return cur.rowcount > 0


def similar_assertions(
    db: sqlite3.Connection, text: str, top_k: int = 3
) -> list[dict[str, Any]]:
    """The LIVE assertions most similar to the text, for flagging suspected
    duplicates (the MCP write path runs without an LLM, only vector distance
    is available here)."""
    model_id, _, style = active_model(db)
    tag = _model_tag(model_id)
    vec = embed_query(text, style)
    rows = db.execute(
        f"SELECT rowid, distance FROM vec_assertion_{tag} "
        f"WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (vec, top_k),
    ).fetchall()
    out = []
    for rid, dist in rows:
        row = db.execute(
            "SELECT id, type, text FROM assertions WHERE id=? AND invalidated_at IS NULL",
            (rid,),
        ).fetchone()
        if row:
            out.append({"id": f"assertion:{row[0]}", "type": row[1],
                        "text": row[2], "distance": round(dist, 3)})
    return out


def build_profile(db: sqlite3.Connection, max_per_type: int = 6) -> str:
    """~500-token "who am I / what is in flight" core from recent LIVE
    assertions. For session-start injection and the profile MCP tool."""
    lines = ["# Lavox Memory: current picture", ""]
    label = {
        "decision": "Valid decisions",
        "commitment": "Open commitments",
        "task": "Open tasks",
        "fact": "Key facts",
        "preference": "Preferences",
    }
    for typ in ("decision", "commitment", "task", "fact", "preference"):
        rows = db.execute(
            "SELECT text, occurred_at, data FROM assertions "
            "WHERE type=? AND invalidated_at IS NULL "
            "ORDER BY occurred_at DESC LIMIT ?",
            (typ, max_per_type),
        ).fetchall()
        if not rows:
            continue
        lines.append(f"## {label[typ]}")
        for text, occ, data in rows:
            date = (occ or "")[:10]
            lines.append(f"- [{date}] {text}")
            if typ == "decision" and data:
                d = json.loads(data)
                alts = d.get("alternatives") or []
                if alts:
                    lines.append(f"  (rejected: {', '.join(str(a) for a in alts[:3])})")
        lines.append("")
    if len(lines) <= 2:
        return "The memory contains no extracted assertions yet."
    return "\n".join(lines)
