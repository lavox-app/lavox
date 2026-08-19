"""Lavox Memory — MCP-szerver (M1: olvasás + fegyelmezett írás).

A memória alapréteg; az interfész a Claude Code / ChatGPT / bármely MCP-kliens.
A tool-nevek szándékosak: a `search` és `fetch` az OpenAI-kompatibilis páros
(a ChatGPT Developer Mode nélkül elutasítja a szervert nélkülük, a Deep
Research kizárólag ezt a kettőt hívja) — így M2-ben a remote HTTP kiadás
tool-átnevezés nélkül megy.

Az írás-toolok (remember/correct) fegyelmezettek: kötelező source-jelölés,
duplikátum-gyanú jelzés, és a correct SOHA nem töröl — supersedes-láncot ír
(a felülírt állítás visszakereshető marad). Ez a memory poisoning elleni
védelem magja: minden bejegyzésről tudni kell, honnan jött, és semmi nem
tűnhet el nyomtalanul.

A kutatás dokumentált tanulsága, hogy a modell a tool-leírás alapján magától
ritkán nyúl a memóriához — ezért (a) az `instructions` mező is terel, (b) a
leírások kimondják: MIELŐTT azt mondanád, hogy nem tudod, keress.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

import memory

server = MCPServer(
    name="lavox-memory",
    instructions=(
        "Lavox Memory: David's personal spoken-memory archive, built from his "
        "recorded meetings and voice notes (Hungarian and English). "
        "ALWAYS search this memory BEFORE answering any question about David's "
        "past meetings, decisions, clients, projects, commitments or preferences "
        "— and BEFORE saying you don't know something about him."
    ),
)

_db = None


def db():
    global _db
    if _db is None:
        _db = memory.connect()
    return _db


@server.tool()
def search(
    query: str,
    limit: int = 8,
    include_superseded: bool = False,
    kind: str | None = None,
    since: str | None = None,
) -> str:
    """Searches David's personal spoken-memory archive — everything he has said,
    decided, or discussed in recorded meetings and voice notes.

    Use this BEFORE answering any question about David's past decisions,
    clients, projects, prices, commitments, or preferences, and BEFORE saying
    you don't know something about him. Hungarian and English queries both
    work. Prefer 2-3 varied queries over one broad query.

    Returns ranked snippets with an id — call `fetch` with an id for the full
    transcript segment. Results marked "superseded" are decisions that were
    later overridden; by default only active items are returned
    (include_superseded=true shows history — "what did we believe in July").

    Args:
        query: natural-language question or keywords (HU or EN)
        limit: max results (default 8)
        include_superseded: include overridden assertions too
        kind: filter — meeting | dictation | note
        since: ISO date lower bound, e.g. 2026-07-01
    """
    res = memory.search(
        db(), query, limit=limit, include_superseded=include_superseded,
        kind=kind, since=since,
    )
    if not res:
        return json.dumps({"results": [], "hint": "No hits. Try different keywords, or timeline() for a date-based view."})
    return json.dumps({"results": res}, ensure_ascii=False)


@server.tool()
def fetch(id: str, context: bool = True) -> str:
    """Returns the full verbatim content for one id returned by `search`
    (e.g. "chunk:123" or "assertion:45"), with speaker, timestamps and the
    source recording. For chunks it includes the neighbouring transcript
    segments so quotes keep their context. Use when a search snippet is
    relevant but truncated, or when you must quote David exactly.

    Args:
        id: item id from search results
        context: include neighbouring segments (default true)
    """
    item = memory.fetch(db(), id, context=context)
    if item is None:
        return json.dumps({"error": f"No item with id {id!r}"})
    return json.dumps(item, ensure_ascii=False)


@server.tool()
def timeline(
    start: str | None = None,
    end: str | None = None,
    kind: str | None = None,
    limit: int = 30,
) -> str:
    """Chronological list of recordings (meetings, dictations) — use for
    date-based questions like "what meetings did David have last week", which
    semantic search handles poorly. Dates are ISO (2026-08-01).

    Args:
        start: ISO date lower bound
        end: ISO date upper bound
        kind: meeting | dictation | note
        limit: max entries (default 30)
    """
    return json.dumps(
        {"recordings": memory.timeline(db(), start=start, end=end, kind=kind, limit=limit)},
        ensure_ascii=False,
    )


@server.tool()
def stats() -> str:
    """How much memory exists and how far back it reaches — call this first if
    you are unsure whether searching is worthwhile (e.g. empty or very new
    archive)."""
    return json.dumps(memory.stats(db()), ensure_ascii=False)


@server.tool()
def remember(text: str, type: str = "fact", data: dict | None = None) -> str:
    """Records a NEW durable fact into David's long-term memory. ONLY call this
    when David explicitly asks you to remember something, or clearly states a
    decision, preference, or commitment that will still matter in a month.

    Do NOT save: your own inferences, task-specific details, anything you are
    not certain David actually said, or anything `search` already returns.
    One atomic statement per call, self-contained wording (names, not
    pronouns), in the language David used.

    For type="decision", pass data={"chosen": ..., "alternatives": [...],
    "reasoning": ...} — the rejected alternatives are the most valuable part.

    Args:
        text: the statement to remember (self-contained, one fact)
        type: decision | fact | preference | commitment | task
        data: decision details (chosen/alternatives/reasoning)
    """
    from datetime import datetime, timezone
    if type not in ("decision", "fact", "preference", "commitment", "task"):
        return json.dumps({"error": f"invalid type {type!r}"})
    similar = memory.similar_assertions(db(), text, top_k=3)
    close = [s for s in similar if s["distance"] < 0.55]
    aid = memory.save_assertion(
        db(), type=type, text=text, data=data, source="agent",
        occurred_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    out = {"saved": f"assertion:{aid}"}
    if close:
        out["warning"] = (
            "Very similar active memories exist — if this REPLACES one of them, "
            "call correct() with its id instead of leaving both active."
        )
        out["similar"] = close
    return json.dumps(out, ensure_ascii=False)


@server.tool()
def correct(old_id: str, new_text: str, reason: str | None = None) -> str:
    """Marks an existing memory as superseded and stores the corrected version.
    Use when `search` returns something David has since contradicted or
    changed. The old memory is NEVER deleted — it stays retrievable with
    include_superseded=true, so "what did we believe in July" keeps working.

    Args:
        old_id: the assertion id being corrected (e.g. "assertion:12")
        new_text: the corrected, self-contained statement
        reason: what changed and why (stored with the new memory)
    """
    from datetime import datetime, timezone
    typ, _, raw = old_id.partition(":")
    if typ != "assertion":
        return json.dumps({"error": "old_id must be an assertion id"})
    old = db().execute(
        "SELECT id, type, invalidated_at FROM assertions WHERE id=?", (int(raw),)
    ).fetchone()
    if not old:
        return json.dumps({"error": f"no assertion {old_id}"})
    if old[2] is not None:
        return json.dumps({"error": f"{old_id} is already superseded"})
    data = {"correction_reason": reason} if reason else None
    new_id = memory.save_assertion(
        db(), type=old[1], text=new_text, data=data, source="agent",
        occurred_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    memory.supersede_assertion(db(), old[0], new_id)
    return json.dumps({
        "superseded": old_id,
        "replacement": f"assertion:{new_id}",
        "note": "The old memory remains retrievable with include_superseded=true.",
    })


@server.tool()
def profile() -> str:
    """A compact "who is David / what is currently in flight" summary built
    from active memories (valid decisions, open commitments, key facts).
    Call this at the START of a session to ground yourself before answering
    anything about David's work."""
    return memory.build_profile(db())


if __name__ == "__main__":
    server.run()
