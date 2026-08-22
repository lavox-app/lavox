"""Optional LLM-based speaker identification for the REMAINING unnamed clusters.

DISABLED BY DEFAULT: it only runs when the LAVOX_LLM_KEY env is set
(OpenRouter key). It runs AFTER every deterministic layer (two-track
separation, voice profile, CC fusion, self-introduction regex), exclusively
on the clusters still carrying a generic ("Speaker N") label.

HALLUCINATION DEFENSE: a name may only be assigned from the candidate_pool
(names verifiably invited / present), and the model is explicitly allowed
to answer "I don't know". Without a pool the layer does NOT run — free-text
guessing is never allowed into the transcript.
"""

from __future__ import annotations

import json
import os
import re

import requests

LLM_KEY = os.environ.get("LAVOX_LLM_KEY", "")
LLM_MODEL = os.environ.get("LAVOX_LLM_MODEL", "anthropic/claude-haiku-4-5")
# "beszélő" is functional data: it matches Hungarian generic speaker labels
# ("Beszélő 1") produced by the diarization pipeline. Do not translate.
_GENERIC = re.compile(r"^(speaker|beszélő)\s*\d+$", re.IGNORECASE)


def available() -> bool:
    return bool(LLM_KEY)


def identify_remaining(
    segments: list[dict],
    speakers: list[dict],
    candidate_pool: list[str],
    already_named: list[str],
) -> tuple[list[dict], dict]:
    """Identify the generic clusters from the conversation context.

    Returns: (speakers updated, stats). Conservative: on an uncertain answer
    the cluster stays unnamed.
    """
    if not available() or not candidate_pool:
        return speakers, {"skipped": "disabled" if not LLM_KEY else "no_pool"}

    generic = [s for s in speakers if _GENERIC.match((s.get("label") or "").strip())]
    # Only pool names not yet assigned are eligible.
    unused = [n for n in candidate_pool if n not in already_named]
    if not generic or not unused:
        return speakers, {"skipped": "nothing_to_do"}

    # Per cluster, the longest utterances (max ~1500 characters total).
    excerpts = []
    for spk in generic:
        segs = sorted(
            (s for s in segments if s.get("speaker") == spk["id"]),
            key=lambda s: -(s["end"] - s["start"]),
        )[:6]
        text = " | ".join(s["text"] for s in sorted(segs, key=lambda s: s["start"]))
        excerpts.append({"id": spk["id"], "label": spk["label"], "text": text[:1500]})

    prompt = (
        "Meeting transcript excerpts from unnamed speakers. The POSSIBLE names "
        f"(assigning any other name is FORBIDDEN): {json.dumps(unused, ensure_ascii=False)}\n\n"
        "Rules: one name may be assigned to only one speaker; if you are not "
        'at least 90% certain, the answer is "unknown". Being invited is by '
        "itself NOT evidence of presence — only textual evidence counts "
        "(self-introduction, response to being addressed, role reference).\n\n"
        + "\n".join(f'[{e["id"]}]: {e["text"]}' for e in excerpts)
        + '\n\nRespond with JSON ONLY: {"<id>": "<name or unknown>", ...}'
    )
    try:
        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {LLM_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
                "temperature": 0,
            },
            timeout=45,
        )
        res.raise_for_status()
        content = res.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.DOTALL)
        mapping = json.loads(m.group(0)) if m else {}
    except Exception as e:
        return speakers, {"error": str(e)}

    renamed, used = 0, set()
    for spk in generic:
        name = (mapping.get(spk["id"]) or "").strip()
        # Pool constraint + duplicate ban — the model's answer alone is not enough.
        if name and name != "unknown" and name in unused and name not in used:
            spk["label"] = name
            used.add(name)
            renamed += 1
    return speakers, {"renamed": renamed, "asked": len(generic)}
