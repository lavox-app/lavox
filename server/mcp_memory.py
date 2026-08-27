"""Lavox Memory MCP server (M1: reading + disciplined writing).

Memory is the base layer; the interface is Claude Code / ChatGPT / any MCP
client. The tool names are deliberate: `search` and `fetch` are the
OpenAI-compatible pair (ChatGPT rejects the server without them outside
Developer Mode, and Deep Research calls only these two), so the M2 remote
HTTP release ships without renaming any tools.

The write tools (remember/correct) are disciplined: mandatory source
labeling, duplicate-suspicion warnings, and correct NEVER deletes, it
writes a supersedes chain (the overridden assertion stays retrievable).
This is the core of the defense against memory poisoning: every entry must
be traceable to its origin, and nothing may vanish without a trace.

A documented lesson of the research is that the model rarely reaches for
memory on its own based on tool descriptions alone, hence (a) the
`instructions` field also steers it, and (b) the descriptions state:
BEFORE saying you don't know, search.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

import memory

server = MCPServer(
    name="lavox-memory",
    instructions=(
        "Lavox Memory: the user's personal spoken-memory archive, built from their "
        "recorded meetings and voice notes (Hungarian and English). "
        "ALWAYS search this memory BEFORE answering any question about the user's "
        "past meetings, decisions, clients, projects, commitments or preferences "
        "— and BEFORE saying you don't know something about them."
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
    """Searches the user's personal spoken-memory archive, everything they have said,
    decided, or discussed in recorded meetings and voice notes.

    Use this BEFORE answering any question about the user's past decisions,
    clients, projects, prices, commitments, or preferences, and BEFORE saying
    you don't know something about them. Hungarian and English queries both
    work. Prefer 2-3 varied queries over one broad query.

    Returns ranked snippets with an id, call `fetch` with an id for the full
    transcript segment. Results marked "superseded" are decisions that were
    later overridden; by default only active items are returned
    (include_superseded=true shows history, "what did we believe in July").

    Args:
        query: natural-language question or keywords (HU or EN)
        limit: max results (default 8)
        include_superseded: include overridden assertions too
        kind: filter, meeting | dictation | note
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
    relevant but truncated, or when you must quote the user exactly.

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
    """Chronological list of recordings (meetings, dictations), use for
    date-based questions like "what meetings did the user have last week", which
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
    """How much memory exists and how far back it reaches, call this first if
    you are unsure whether searching is worthwhile (e.g. empty or very new
    archive)."""
    return json.dumps(memory.stats(db()), ensure_ascii=False)


@server.tool()
def remember(text: str, type: str = "fact", data: dict | None = None) -> str:
    """Records a NEW durable fact into the user's long-term memory. ONLY call this
    when the user explicitly asks you to remember something, or clearly states a
    decision, preference, or commitment that will still matter in a month.

    Do NOT save: your own inferences, task-specific details, anything you are
    not certain the user actually said, or anything `search` already returns.
    One atomic statement per call, self-contained wording (names, not
    pronouns), in the language the user used.

    For type="decision", pass data={"chosen": ..., "alternatives": [...],
    "reasoning": ...}, the rejected alternatives are the most valuable part.

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
            "Very similar active memories exist; if this REPLACES one of them, "
            "call correct() with its id instead of leaving both active."
        )
        out["similar"] = close
    return json.dumps(out, ensure_ascii=False)


@server.tool()
def correct(old_id: str, new_text: str, reason: str | None = None) -> str:
    """Marks an existing memory as superseded and stores the corrected version.
    Use when `search` returns something the user has since contradicted or
    changed. The old memory is NEVER deleted, it stays retrievable with
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
    """A compact "who is the user / what is currently in flight" summary built
    from active memories (valid decisions, open commitments, key facts).
    Call this at the START of a session to ground yourself before answering
    anything about the user's work."""
    return memory.build_profile(db())


if __name__ == "__main__":
    server.run()
