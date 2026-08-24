"""Personal dictation dictionary: vocabulary biasing + learned corrections.

One JSON file (~/Lavox/dictionary.json) is the contract between the server
(owner of writes) and the Hub (reader: builds the whisper initial_prompt and
applies the deterministic replacement layer before inserting text).

Learning follows the Wispr Flow rulebook, adapted:
  - only term-like corrections are learned (proper nouns, technical terms,
    acronyms — approximated as "contains an uppercase letter, a digit or a
    non-alphabetic character"), never plain rewording
  - each side of a learned pair is at most 4 words
  - at most 4 new pairs per learn call (one editing session)
  - pure insertions/deletions are ignored (only replacements teach spelling)
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

DICT_PATH = Path(
    os.environ.get("LAVOX_DICT_PATH", str(Path.home() / "Lavox" / "dictionary.json"))
)

MAX_PAIR_WORDS = 4
MAX_LEARN_PER_CALL = 4
PROMPT_TERM_LIMIT = 40

_WORD_RE = re.compile(r"\S+")


def load() -> dict[str, Any]:
    if DICT_PATH.exists():
        try:
            data = json.loads(DICT_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("terms"), list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "terms": []}


def save(data: dict[str, Any]) -> None:
    DICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DICT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DICT_PATH)


def _find(data: dict[str, Any], term: str) -> dict[str, Any] | None:
    low = term.lower()
    for entry in data["terms"]:
        if entry["term"].lower() == low:
            return entry
    return None


def add_term(term: str, misheard: str | None = None, source: str = "manual") -> dict:
    """Add or update a term; optionally record one misheard variant."""
    term = term.strip()
    if not term:
        raise ValueError("empty term")
    data = load()
    entry = _find(data, term)
    if entry is None:
        entry = {
            "term": term,
            "misheard": [],
            "count": 0,
            "source": source,
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        data["terms"].append(entry)
    entry["count"] = int(entry.get("count", 0)) + 1
    if misheard:
        mis = misheard.strip()
        if mis and mis.lower() != term.lower():
            variants = entry.setdefault("misheard", [])
            if mis.lower() not in [v.lower() for v in variants]:
                variants.append(mis)
    save(data)
    return entry


def remove_term(term: str) -> bool:
    data = load()
    before = len(data["terms"])
    data["terms"] = [e for e in data["terms"] if e["term"].lower() != term.lower()]
    if len(data["terms"]) != before:
        save(data)
        return True
    return False


def _is_term_like(text: str) -> bool:
    """Proper noun / technical term heuristic — what is worth learning."""
    stripped = text.strip()
    if not stripped:
        return False
    # any uppercase letter beyond a sentence-initial position, a digit,
    # or an in-word special character (Lavox, D-U-N-S, sqlite-vec, GPT4)
    if any(ch.isdigit() for ch in stripped):
        return True
    if any(ch in "-_./" for ch in stripped.strip(".,!?")):
        return True
    words = stripped.split()
    for i, w in enumerate(words):
        core = w.strip(".,!?:;()\"'")
        if not core:
            continue
        if core[0].isupper() and i > 0:
            return True
        if i == 0 and core[0].isupper() and len(words) == 1:
            return True  # single capitalized word ("Infisical")
        if any(ch.isupper() for ch in core[1:]):
            return True  # inner capital (iPhone, McP)
    return False


def learn_from_correction(raw: str, corrected: str) -> list[dict[str, str]]:
    """Diff the raw transcript against the user's corrected text and learn
    term-like replacement pairs. Returns the learned pairs."""
    raw_words = _WORD_RE.findall(raw or "")
    cor_words = _WORD_RE.findall(corrected or "")
    if not raw_words or not cor_words:
        return []

    matcher = difflib.SequenceMatcher(
        a=[w.lower() for w in raw_words],
        b=[w.lower() for w in cor_words],
        autojunk=False,
    )
    learned: list[dict[str, str]] = []
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        if op != "replace":
            continue  # pure inserts/deletes teach nothing about spelling
        if (a1 - a0) > MAX_PAIR_WORDS or (b1 - b0) > MAX_PAIR_WORDS:
            continue
        mis = " ".join(raw_words[a0:a1]).strip(".,!?:;")
        good = " ".join(cor_words[b0:b1]).strip(".,!?:;")
        if not mis or not good or mis.lower() == good.lower():
            # case-only fix: still valuable as a replacement (lavox → Lavox)
            if mis != good and _is_term_like(good):
                add_term(good, misheard=mis, source="learned")
                learned.append({"misheard": mis, "term": good})
            continue
        if not _is_term_like(good):
            continue
        add_term(good, misheard=mis, source="learned")
        learned.append({"misheard": mis, "term": good})
        if len(learned) >= MAX_LEARN_PER_CALL:
            break
    return learned


def prompt_terms(limit: int = PROMPT_TERM_LIMIT) -> list[str]:
    """Terms for vocabulary biasing, most used first (prompt budget is small)."""
    data = load()
    ranked = sorted(
        data["terms"],
        key=lambda e: (int(e.get("count", 0)), e.get("added_at", "")),
        reverse=True,
    )
    return [e["term"] for e in ranked[:limit]]


def hotwords_string(limit: int = PROMPT_TERM_LIMIT) -> str | None:
    terms = prompt_terms(limit)
    return ", ".join(terms) if terms else None


def replacements() -> list[tuple[str, str]]:
    """(misheard, correct) pairs for the deterministic post-ASR layer."""
    data = load()
    pairs: list[tuple[str, str]] = []
    for entry in data["terms"]:
        for mis in entry.get("misheard", []):
            pairs.append((mis, entry["term"]))
    # longest first, so multi-word variants win over their substrings
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def apply_replacements(text: str) -> str:
    """Case-insensitive, word-boundary replacement of known mishearings."""
    out = text
    for mis, term in replacements():
        pattern = re.compile(
            r"(?<![\wáéíóöőúüűÁÉÍÓÖŐÚÜŰ])"
            + re.escape(mis)
            + r"(?![\wáéíóöőúüűÁÉÍÓÖŐÚÜŰ])",
            re.IGNORECASE,
        )
        out = pattern.sub(term, out)
    return out
