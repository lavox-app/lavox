"""Lavox Memory — extraction layer (M1): typed assertions from speech.

An LLM extracts durable assertions from the recording transcript (decision /
fact / commitment / preference / task); for decisions it also captures the
REJECTED ALTERNATIVES and the reasoning — this is the core of the system,
something no competitor on the market does (ADR from your voice).

PRINCIPLES (from the research measurements):
- Extraction is an INDEX, not a replacement: the raw transcript remains the
  canonical store, every assertion is anchored to a source chunk via a quote
  (provenance).
- Write-time conflict resolution: each new assertion is confronted with the
  similar LIVE assertions; if it contradicts one about the same subject, the
  old one enters the supersedes chain (invalidated_at + superseded_by) —
  NOTHING is ever deleted.
- Extraction is OPTIONAL: without LAVOX_LLM_KEY the system still works in a
  degraded mode (the verbatim layer searches on its own). No forced LLM.

Usage (batch, over every recording not yet extracted):
  LAVOX_LLM_KEY=... .venv/bin/python3 extract.py            # all
  LAVOX_LLM_KEY=... .venv/bin/python3 extract.py <rec_id>   # one recording
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

import httpx

import memory

LLM_KEY = os.environ.get("LAVOX_LLM_KEY", "")
LLM_MODEL = os.environ.get("LAVOX_LLM_MODEL", "anthropic/claude-haiku-4-5")
# Any OpenAI-compatible chat-completions endpoint can be used (Ollama, vLLM,
# OpenAI, ...) — the default is OpenRouter.
LLM_URL = os.environ.get(
    "LAVOX_LLM_URL", "https://openrouter.ai/api/v1/chat/completions"
)

MAX_ASSERTIONS = 12
MAX_TRANSCRIPT_CHARS = 180_000   # fits comfortably into the haiku context
# vec0 distance threshold for "might be about the same thing" candidates.
# For normalized vectors L2² = 2−2·cos; 0.9 ≈ cos 0.55 — a wide net on
# purpose, since the final verdict is made by the LLM anyway.
SIMILAR_DIST_THRESHOLD = 0.9

EXTRACT_SYSTEM = """You extract DURABLE assertions from a meeting or dictation transcript into a personal memory system. Only include what will still matter weeks from now.

Types:
- decision: a decision that was stated out loud. The data field is MANDATORY for it: {"chosen": what was chosen, "alternatives": [the options that were considered and rejected — ONLY if they were actually mentioned], "reasoning": the stated rationale}. This is the most valuable type: six months from now the question will not be WHAT was decided, but WHY NOT the other option.
- fact: a durable fact (price, deadline, requirement, name, status).
- commitment: who promised what, to whom, by when.
- preference: a durable preference or way of working.
- task: a concrete task that was assigned.

IMPORTANT about the input: the transcript comes from AUTOMATIC speech recognition and contains many misheard, garbled words. This is normal — your job is precisely to extract what IS RECOGNIZABLE despite the noise. If a word is obviously a mishearing but the intended meaning is clear from context, use the intended meaning. Because of the noise, NEVER return an empty list if the topics and assertions of the conversation are identifiable — express the uncertainty in the confidence field instead (0.4-0.7 for noisy sources).

Rules:
1. Only what can be derived from the conversation — do not invent new information. The intended meaning of garbled words may be inferred, but you must not add whole assertions.
2. Give a "quote" for every assertion: a SHORT (5-15 word), VERBATIM excerpt from the transcript that the assertion comes from. Copy it exactly, do not fix its errors.
3. The assertion text must be SELF-CONTAINED: use names, not pronouns ("Dani requested two changes on the homepage", not "he requested two changes").
4. Write the assertion text in the SAME LANGUAGE as the transcript: Hungarian transcript → Hungarian assertions, English transcript → English assertions.
5. At most 12 assertions — the most important ones. Skip small talk, greetings, repetition.
6. If there is no substantive durable content, return an empty list.
7. confidence: 0.0-1.0 — how unambiguously it was stated.

Respond with EXACTLY this JSON and nothing else:
{"assertions":[{"type":"...","text":"...","quote":"...","confidence":0.9,"data":{...}}]}"""

RECONCILE_SYSTEM = """Two assertions from a personal memory: an OLD one (already stored) and a NEW one (being created now). Decide their relationship:

- "duplicate": they carry the same information — the new one does not need to be stored.
- "supersedes": they are about the same question, but the NEW one contradicts the old one or is a more recent decision/status — the new one overrides the old one.
- "separate": they are about different things, both are independently valid.

Only answer "supersedes" if it is REALLY the same question (same project/subject) and the contradiction or update is genuine. When in doubt, "separate".

Respond with EXACTLY: {"relation":"duplicate|supersedes|separate"}"""


def _llm(system: str, user: str, max_tokens: int = 4000) -> dict[str, Any]:
    resp = httpx.post(
        LLM_URL,
        headers={
            "Authorization": f"Bearer {LLM_KEY}",
            "Content-Type": "application/json",
            # OpenRouter/Cerebras returns 403 for the default python User-Agent
            "User-Agent": "lavox-memory/1.0",
        },
        json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
        },
        timeout=180,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    # defensive JSON extraction (guards against code-fence wrapping)
    m = re.search(r"\{.*\}", content, re.DOTALL)
    return json.loads(m.group(0) if m else content)


def _transcript_text(db, recording_id: str) -> str:
    rows = db.execute(
        "SELECT speaker, text FROM chunks WHERE recording_id=? ORDER BY seq",
        (recording_id,),
    ).fetchall()
    lines = []
    for speaker, text in rows:
        prefix = f"{speaker}: " if speaker else ""
        lines.append(prefix + text)
    return "\n".join(lines)[:MAX_TRANSCRIPT_CHARS]


def _anchor_quote(db, recording_id: str, quote: str) -> int | None:
    """Anchor the quote to its source chunk. Provenance is a mandatory
    principle — if the exact passage is not found (the model fixed the
    spelling), fall back via FTS to the best chunk within the same
    recording."""
    if not quote:
        return None
    norm = re.sub(r"\s+", " ", quote.strip().lower())
    rows = db.execute(
        "SELECT id, text FROM chunks WHERE recording_id=?", (recording_id,)
    ).fetchall()
    for cid, text in rows:
        if norm[:60] in re.sub(r"\s+", " ", text.lower()):
            return cid
    # fallback: FTS within the recording
    fq = memory._fts_query(quote)
    try:
        hits = db.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
            "ORDER BY bm25(chunks_fts) LIMIT 20", (fq,)
        ).fetchall()
    except Exception:
        hits = []
    valid = {r[0] for r in rows}
    for (rid,) in hits:
        if rid in valid:
            return rid
    return rows[0][0] if rows else None


def _reconcile(db, new_text: str, new_vec: bytes, model_tag: str) -> tuple[str, int | None]:
    """Relationship of the new assertion to the existing LIVE assertions.
    Returns: ("separate"|"duplicate"|"supersedes", old_id|None)."""
    rows = db.execute(
        f"SELECT rowid, distance FROM vec_assertion_{model_tag} "
        f"WHERE embedding MATCH ? ORDER BY distance LIMIT 3",
        (new_vec,),
    ).fetchall()
    for rid, dist in rows:
        if dist > SIMILAR_DIST_THRESHOLD:
            continue
        old = db.execute(
            "SELECT id, text FROM assertions WHERE id=? AND invalidated_at IS NULL",
            (rid,),
        ).fetchone()
        if not old:
            continue
        verdict = _llm(
            RECONCILE_SYSTEM,
            f"OLD: {old[1]}\n\nNEW: {new_text}",
            max_tokens=100,
        ).get("relation", "separate")
        if verdict in ("duplicate", "supersedes"):
            return verdict, old[0]
    return "separate", None


def extract_recording(db, recording_id: str, verbose: bool = True) -> dict[str, Any]:
    """Extract one recording: LLM → assertions → reconcile → save."""
    if not LLM_KEY:
        return {"error": "LAVOX_LLM_KEY is not set — extraction skipped"}

    rec = db.execute(
        "SELECT id, kind, title, occurred_at, meta FROM recordings WHERE id=?",
        (recording_id,),
    ).fetchone()
    if not rec:
        return {"error": f"no such recording: {recording_id}"}
    meta = json.loads(rec[4] or "{}")
    if meta.get("extracted_at"):
        return {"id": recording_id, "status": "skipped (already extracted)"}

    transcript = _transcript_text(db, recording_id)
    if len(transcript) < 200:
        _mark_extracted(db, recording_id, meta, 0)
        return {"id": recording_id, "status": "too short, skipped"}

    user_msg = (
        f"Recording: {rec[2] or '(untitled)'} | type: {rec[1]} | date: {rec[3][:10]}\n\n"
        f"TRANSCRIPT:\n{transcript}"
    )
    out = _llm(EXTRACT_SYSTEM, user_msg, max_tokens=4000)
    assertions = out.get("assertions", [])[:MAX_ASSERTIONS]

    model_id, dim, style = memory.active_model(db)
    tag = memory._model_tag(model_id)

    saved, skipped, superseded = 0, 0, 0
    for a in assertions:
        typ = a.get("type")
        text = (a.get("text") or "").strip()
        if typ not in ("decision", "fact", "commitment", "preference", "task") or not text:
            continue
        vec = memory.embed_passages([text], style)[0]
        relation, old_id = _reconcile(db, text, vec, tag)
        if relation == "duplicate":
            skipped += 1
            if verbose:
                print(f"    ~ duplicate, skipped: {text[:70]}")
            continue

        chunk_id = _anchor_quote(db, recording_id, a.get("quote") or "")
        new_id = memory.save_assertion(
            db,
            type=typ,
            text=text,
            data=a.get("data"),
            source="extracted",
            source_chunk_id=chunk_id,
            occurred_at=rec[3],
            confidence=a.get("confidence"),
        )
        if relation == "supersedes" and old_id:
            memory.supersede_assertion(db, old_id, new_id)
            superseded += 1
            if verbose:
                print(f"    ⤷ supersedes assertion:{old_id}: {text[:70]}")
        elif verbose:
            marker = {"decision": "◆", "fact": "·", "commitment": "→", "preference": "♥", "task": "☐"}.get(typ, "·")
            print(f"    {marker} [{typ}] {text[:80]}")
        saved += 1

    _mark_extracted(db, recording_id, meta, saved)
    return {"id": recording_id, "status": "ok", "saved": saved,
            "duplicates_skipped": skipped, "superseded": superseded}


def _mark_extracted(db, recording_id: str, meta: dict, count: int) -> None:
    from datetime import datetime, timezone
    meta["extracted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta["assertions_extracted"] = count
    db.execute(
        "UPDATE recordings SET meta=? WHERE id=?",
        (json.dumps(meta, ensure_ascii=False), recording_id),
    )
    db.commit()


def main() -> int:
    db = memory.connect()
    if len(sys.argv) > 1:
        ids = [sys.argv[1]]
    else:
        ids = [r[0] for r in db.execute(
            "SELECT id FROM recordings ORDER BY occurred_at"
        ).fetchall()]
    for rid in ids:
        title = db.execute("SELECT title FROM recordings WHERE id=?", (rid,)).fetchone()
        print(f"\n═══ {rid}  {((title or [None])[0] or '')[:50]}")
        res = extract_recording(db, rid)
        print(f"    → {res}")
    print("\n" + json.dumps(memory.stats(db), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
