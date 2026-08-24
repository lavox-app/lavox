#!/usr/bin/env python3
"""Run the eval set against the fusion search and score recall.

A question counts as a hit at rank r if ANY evidence substring appears
(case-insensitively) in the result's chunk or assertion text at that rank.
Reports recall@1 / recall@3 / recall@8 overall and per category.

Run:  python3 eval/run_eval.py [--limit 8]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory  # noqa: E402

DB = memory.connect()

QFILE = Path(__file__).parent / "questions.jsonl"
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 8


def result_texts(hit: dict) -> str:
    parts = [
        hit.get("snippet", "") or "",
        hit.get("header", "") or "",
        hit.get("text", "") or "",
    ]
    for key in ("linked_assertions", "assertions"):
        for a in hit.get(key) or []:
            parts.append(json.dumps(a, ensure_ascii=False))
    if hit.get("assertion"):
        parts.append(json.dumps(hit["assertion"], ensure_ascii=False))
    return " ".join(parts).lower()


def first_hit_rank(question: dict, results: list[dict]) -> int | None:
    evid = [e.lower() for e in question["evidence"]]
    for rank, hit in enumerate(results, start=1):
        blob = result_texts(hit)
        if any(e in blob for e in evid):
            return rank
    return None


def main() -> None:
    questions = [
        json.loads(line)
        for line in QFILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ranks: dict[str, list[int | None]] = defaultdict(list)
    misses = []
    for q in questions:
        results = memory.search(DB, q["question"], limit=LIMIT, include_superseded=True)
        rank = first_hit_rank(q, results)
        ranks[q["category"]].append(rank)
        if rank is None:
            misses.append(q)
        flag = f"@{rank}" if rank else "MISS"
        print(f"  {q['id']} [{q['category']:>20}] {flag:>5}  {q['question'][:64]}")

    def recall(rs: list[int | None], k: int) -> str:
        hits = sum(1 for r in rs if r is not None and r <= k)
        return f"{hits}/{len(rs)}"

    all_ranks = [r for rs in ranks.values() for r in rs]
    print("\n══ Összesítés ══")
    print(f"  recall@1: {recall(all_ranks, 1)}   recall@3: {recall(all_ranks, 3)}   recall@{LIMIT}: {recall(all_ranks, LIMIT)}")
    print("\n══ Kategóriánként (recall@3) ══")
    for cat, rs in sorted(ranks.items()):
        print(f"  {cat:>22}: {recall(rs, 3)}")
    if misses:
        print("\n══ MISS-ek ══")
        for q in misses:
            print(f"  {q['id']}: {q['question']}  [bizonyíték: {q['evidence']}]")


if __name__ == "__main__":
    main()
