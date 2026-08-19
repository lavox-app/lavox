"""Lavox Memory — fúziós memória-mag (M0).

A beszédből (meeting, diktálás) épülő személyes memória, amit bármelyik
AI-eszköz MCP-n keresztül olvas. Nincs saját keresőfelület: a Lavox itt
alapréteg, az interfész a Claude Code / ChatGPT / bármi.

HÁROM RÉTEG:
  1. VERBATIM (kanonikus): a nyers átirat kontextus-fejléces ~400 tokenes
     chunkokban. Soha nem törlődik. A kontrollált mérések szerint (Letta,
     "verbatim chunks beat lossy extraction") a nyers réteg önállóan is erős
     — ezért ez a tár igazsága, nem a kinyert réteg.
  2. ÁLLÍTÁS (kinyert index, M1-től töltődik): típusozott állítások
     (decision/fact/preference/commitment), bitemporális oszlopokkal.
     A bitemporalitás itt SÉMA, nem framework: occurred_at + invalidated_at
     + superseded_by megadja a "mit hittünk júliusban" lekérdezhetőséget
     LLM-per-write költség és külső gráf-DB nélkül.
  3. PROFIL-MAG (M1): ~500 tokenes összefoglaló session-start injektáláshoz.

FÚZIÓ: 4 párhuzamos lista (chunk-vektor, chunk-FTS, állítás-vektor,
állítás-FTS) → RRF (k=60) → kereszt-csatolás (a chunk-találat behúzza a rá
mutató ÉLŐ állításokat, az állítás-találat a forrás-chunkját) → additív,
korlátos frissesség-bónusz KIZÁRÓLAG időérzékeny kérdésnél. A szorzó-alapú
age-decay bizonyítottan recall-gyilkos (saját mérés: 19/20 → 4/20).

TÁROLÁS: SQLite (WAL) + sqlite-vec + FTS5 — egy fájl, nulla üzemeltetés a
felhasználó gépén. A felhős tier (M2) ugyanezt a sémát tükrözi Postgresben.
A vektorok model_id-vel címkézettek: két modell vektorai együtt élhetnek
migráció alatt, és a self-hoster azzal embeddel, amivel akar.
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
# Konfiguráció
# ---------------------------------------------------------------------------

MEMORY_DIR = Path(os.environ.get("LAVOX_MEMORY_DIR", str(Path.home() / "Lavox" / "memory")))
DB_PATH = MEMORY_DIR / "lavox-memory.db"

# Az első támogatott modellt választjuk. A default kicsi (gyors első élmény,
# HU+EN); a séma modell-független, a csere párhuzamos újra-embeddeléssel megy.
PREFERRED_MODELS = [
    ("intfloat/multilingual-e5-small", 384, "e5"),
    ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", 384, "plain"),
    ("intfloat/multilingual-e5-large", 1024, "e5"),
]

# ~400 token ≈ ~1400 karakter (a magyar tokenizálás pazarlóbb, ez konzervatív)
CHUNK_MAX_CHARS = 1400
CHUNK_MIN_CHARS = 120          # ennél rövidebb szomszédos szegmensek összevonása
RRF_K = 60
PER_LIST_LIMIT = 50
RECENCY_GAMMA = 0.15           # a max RRF-pontszám max 15%-a — additív, korlátos
RECENCY_HALFLIFE_DAYS = 30.0

# Időérzékeny kérdés-minták (HU+EN). Ha egyik sem talál, a recency-tag 0.
_TIME_SENSITIVE = re.compile(
    r"\b(ma|mai|tegnap|legut[oó]bb|mostan[aá]ban|m[uú]lt h[eé]t|h[eé]ten|"
    r"friss|jelenleg|aktu[aá]lis|utols[oó]|"
    r"today|yesterday|recent|recently|last week|this week|latest|current|now)\b",
    re.IGNORECASE,
)

_embedder = None
_embedder_meta: tuple[str, int, str] | None = None


# ---------------------------------------------------------------------------
# Séma
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS recordings (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,            -- meeting | dictation | note
  title       TEXT,
  occurred_at TEXT NOT NULL,            -- ISO, esemény-idő
  duration_sec REAL,
  participants TEXT,                    -- JSON lista
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
  text         TEXT NOT NULL,           -- nyers szöveg (kanonikus)
  header       TEXT NOT NULL,           -- kontextus-fejléc (embeddelve + FTS-ben)
  UNIQUE (recording_id, seq)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, header, content=chunks, content_rowid=id
);

-- Állítás-réteg (M1 tölti; a séma és a keresés már most kész rá).
-- A bitemporalitás lényege három oszlop — nem kell hozzá gráf-framework:
--   occurred_at   = mikor hangzott el (esemény-idő)
--   recorded_at   = mikor került a memóriába (rögzítés-idő)
--   invalidated_at + superseded_by = felülírás-lánc, törlés SOHA nincs
CREATE TABLE IF NOT EXISTS assertions (
  id            INTEGER PRIMARY KEY,
  type          TEXT NOT NULL,          -- decision | fact | preference | commitment | task
  text          TEXT NOT NULL,          -- önhordó megfogalmazás
  data          TEXT,                   -- JSON: decision-nél {chosen, alternatives[], reasoning}
  source        TEXT NOT NULL,          -- extracted | user_stated | agent  (poisoning-védelem)
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

-- Melyik embedding-modell aktív. A vektorok modell-címkézett vec0 táblákban
-- élnek (vec_chunk_<tag>, vec_assertion_<tag>) — modellcsere = új tábla
-- párhuzamos feltöltése, átkapcsolás, régi eldobása. Nincs leállás.
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
    db.execute("PRAGMA journal_mode=WAL")       # több kliens, nincs korrupció
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
# Embedding (fastembed — ONNX, lokális, modell-cserélhető)
# ---------------------------------------------------------------------------

def _pick_model() -> tuple[str, int, str]:
    from fastembed import TextEmbedding
    supported = {m["model"] for m in TextEmbedding.list_supported_models()}
    for model_id, dim, style in PREFERRED_MODELS:
        if model_id in supported:
            return model_id, dim, style
    raise RuntimeError(
        "Egyik preferált embedding-modell sem támogatott ebben a fastembed-verzióban. "
        f"Támogatott: {sorted(supported)[:10]}…"
    )


def get_embedder():
    """Lusta, folyamat-szintű singleton — a modell egyszer töltődik be."""
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
# Chunkolás — beszélőváltásnál vágva, kontextus-fejléccel
# ---------------------------------------------------------------------------

def _fmt_ts(sec: float | None) -> str:
    if sec is None:
        return "?"
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def build_chunks(recording: dict[str, Any], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Átirat-szegmensek → kontextus-fejléces chunkok.

    Szabályok (a mérések szerint a chunkoló-algoritmus másodrendű, a fejléc
    elsőrendű — ezért az algoritmus egyszerű, a fejléc gazdag):
      - beszélőváltásnál mindig új chunk,
      - azonos beszélő szegmensei összevonva CHUNK_MAX_CHARS-ig,
      - a törpe-szegmensek (< CHUNK_MIN_CHARS) nem kapnak önálló chunkot.
    """
    date = (recording.get("occurred_at") or "")[:10]
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
        # törpe csoportot beszélőváltásnál is tovább görgetjük, hogy ne legyen
        # kereshetetlen morzsa ("igen", "oké") önálló chunkként
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
        # Kontextus-injekció: a fejléc bekerül az embeddelt szövegbe ÉS az
        # FTS-be. Az Anthropic mérése szerint ez -35..49% retrieval-hiba —
        # nálunk a metaadat ingyen van (a felvételből jön), LLM sem kell hozzá.
        head_bits = [date, kind, title]
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
    """Egy felvétel (meeting/diktálás) betöltése a memóriába. Idempotens."""
    rid = recording["id"]
    exists = db.execute("SELECT 1 FROM recordings WHERE id=?", (rid,)).fetchone()
    if exists and not force:
        return {"id": rid, "status": "skipped (már bent van)"}
    if exists and force:
        _delete_recording(db, rid)

    model_id, dim, style = active_model(db)
    tag = _model_tag(model_id)
    chunks = build_chunks(recording, segments)
    if not chunks:
        return {"id": rid, "status": "üres átirat, kihagyva"}

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
    """Force-reingest belső segédje. A verbatim réteg normál úton sosem törlődik."""
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
# Keresés — 4 lista → RRF → kereszt-csatolás → feltételes recency
# ---------------------------------------------------------------------------

def _fts_query(user_query: str) -> str:
    """Felhasználói szöveg → biztonságos FTS5 query.

    Minden tokent idézőjelezünk (nincs szintaxis-injektálás), a 4+ karakteres
    tokenek prefix-csillagot kapnak — ez a magyar toldalékok elleni olcsó
    védelem ("szerződés" megtalálja a "szerződésről"-t). A tokenek között
    implicit AND van; ha az AND üres találatot adna, a hívó OR-ra válthat.
    """
    tokens = re.findall(r"\w+", user_query, re.UNICODE)
    parts = []
    for t in tokens:
        if len(t) >= 4:
            parts.append(f'"{t}"*')
        elif t:
            parts.append(f'"{t}"')
    # OR-mód: a BM25 úgyis a több egyezést rangsorolja előre, az AND viszont
    # kinullázná a lexikai ágat, amint egyetlen kérdés-szó hiányzik a chunkból
    # ("mit BESZÉLTÜNK az X projektről" — a 'beszéltünk' nincs az átiratban).
    return " OR ".join(parts) if parts else '""'


def _rrf_merge(lists: dict[str, list[tuple[str, int]]]) -> dict[str, float]:
    """kulcs: 'chunk:<id>' / 'assertion:<id>' → RRF-pontszám."""
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

    # Állítás-listák — M0-ban üresek, M1-től élnek; a fúzió kész.
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

    # Feltételes, additív, korlátos recency — SOHA nem szorzó.
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

    # Felvételenkénti cap (Cerebras-minta: "dedup + per-fájl cap → változatos
    # top-N"). Enélkül egyetlen hosszú meeting chunkjai kiszoríthatják a
    # rövidebb, de relevánsabb felvételeket a végeredményből.
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
        # Kereszt-csatolás: a chunkra mutató ÉLŐ állítások (M1-től ad találatot)
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
    """Teljes tartalom id alapján — chunknál a szomszédos chunkokkal együtt
    (kontextus-bővítés a győztesen, nem az egész korpuszon — Cerebras-minta)."""
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
    """Időrendi nézet — azt adja, amit a szemantikus keresés rosszul csinál."""
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
# Állítás-réteg írás-oldala (M1)
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
    """Új állítás mentése — embedding + FTS + vec egy tranzakcióban.

    A `source` kötelező és zárt készletű (poisoning-védelem): 'extracted'
    (LLM nyerte ki átiratból, source_chunk_id-vel), 'user_stated' (a
    felhasználó mondta ki explicit), 'agent' (AI-eszköz mentette MCP-n át).
    """
    if source not in ("extracted", "user_stated", "agent"):
        raise ValueError(f"érvénytelen source: {source!r}")
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
    """A régi állítás felülírás-jelölése. SOHA nem törlés: az invalidated_at
    + superseded_by lánc adja a "mit hittünk júliusban" lekérdezhetőséget."""
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
    """A szöveghez leghasonlóbb ÉLŐ állítások — duplikátum-gyanú jelzésére
    (az MCP write-út LLM nélkül fut, itt csak vektor-távolság van)."""
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
    """~500 tokenes "ki vagyok / mi fut most" mag a friss ÉLŐ állításokból.
    Session-start injektáláshoz és a profile MCP-toolhoz."""
    lines = ["# Lavox Memory — aktuális kép", ""]
    label = {
        "decision": "Érvényes döntések",
        "commitment": "Élő ígéretek",
        "task": "Nyitott feladatok",
        "fact": "Fontos tények",
        "preference": "Preferenciák",
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
                    lines.append(f"  (elvetve: {', '.join(str(a) for a in alts[:3])})")
        lines.append("")
    if len(lines) <= 2:
        return "A memóriában még nincsenek kinyert állítások."
    return "\n".join(lines)
