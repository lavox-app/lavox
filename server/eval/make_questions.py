#!/usr/bin/env python3
"""Seed eval questions from the REAL memory content.

Reads assertions (decisions first, superseded pairs specially) and chunks,
asks an LLM to write natural questions a user would actually ask, with the
expected evidence substrings. Output: eval/questions.jsonl, REVIEW BY HAND.

Run:  LAVOX_LLM_KEY=... python3 eval/make_questions.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory  # noqa: E402

import httpx  # noqa: E402

LLM_KEY = os.environ.get("LAVOX_LLM_KEY", "")
LLM_URL = os.environ.get(
    "LAVOX_LLM_URL", "https://openrouter.ai/api/v1/chat/completions"
)
MODEL = os.environ.get("LAVOX_LLM_MODEL", "anthropic/claude-haiku-4-5")
OUT = Path(__file__).parent / "questions.jsonl"

SYSTEM = """You create evaluation questions for a personal spoken-memory search system.
Given a stored assertion (and its verbatim source), write ONE natural question the
memory's owner would ask weeks later, IN THE SAME LANGUAGE as the assertion.
Do NOT reuse the assertion's exact wording, ask the way a person naturally would
(synonyms, different angle). Also pick 1-3 SHORT evidence substrings (verbatim,
5-30 chars each) that a correct search result must contain, take them from the
assertion text or the quote, prefer distinctive proper nouns or numbers.
Return STRICT JSON: {"question": "...", "evidence": ["...", "..."]}"""


def llm(user: str) -> dict:
    r = httpx.post(
        LLM_URL,
        headers={"Authorization": f"Bearer {LLM_KEY}", "User-Agent": "lavox-eval"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 400,
        },
        timeout=60,
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    import re

    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0))


def main() -> None:
    if not LLM_KEY:
        raise SystemExit("LAVOX_LLM_KEY hiányzik")
    db = memory.connect()
    rows = db.execute(
        """select a.id, a.type, a.text, a.data, a.superseded_by, a.invalidated_at,
                  c.text as chunk_text
           from assertions a left join chunks c on c.id = a.source_chunk_id
           order by case a.type when 'decision' then 0 else 1 end, a.id"""
    ).fetchall()
    questions = []
    seen_current = 0
    for row in rows:
        aid, kind, text, data, superseded_by, invalidated, chunk_text = row
        is_superseded = bool(superseded_by or invalidated)
        # cap: at most ~40 questions; superseded ones are all kept (rare and valuable)
        if not is_superseded:
            if seen_current >= 32:
                continue
            seen_current += 1
        payload = f"kind: {kind}\nassertion: {text}\n"
        if data:
            payload += f"data: {data}\n"
        if chunk_text:
            payload += f"verbatim source (excerpt): {chunk_text[:400]}\n"
        if is_superseded:
            payload += (
                "NOTE: this assertion was LATER SUPERSEDED. Write a HISTORY question "
                "(what did we believe/decide BEFORE the change), phrased in past tense."
            )
        try:
            out = llm(payload)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip assertion:{aid} ({exc})")
            continue
        questions.append(
            {
                "id": f"q{len(questions)+1:02d}",
                "category": "superseded-decision" if is_superseded else kind,
                "question": out["question"],
                "evidence": out["evidence"],
                "source_assertion": aid,
            }
        )
        print(f"  {questions[-1]['id']} [{questions[-1]['category']}] {out['question'][:70]}")
    OUT.write_text(
        "\n".join(json.dumps(q, ensure_ascii=False) for q in questions) + "\n",
        encoding="utf-8",
    )
    print(f"\n{len(questions)} questions → {OUT}. REVIEW BY HAND.")


if __name__ == "__main__":
    main()
