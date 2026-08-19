"""Opcionális LLM-alapú beszélő-azonosítás a MARADÉK névtelen klaszterekre.

ALAPBÓL KIKAPCSOLVA: csak akkor fut, ha a LAVOX_LLM_KEY env be van állítva
(OpenRouter-kulcs). Minden determinisztikus réteg (kétsávos szétválasztás,
voice-profil, CC-fúzió, önbemutatkozás-regex) UTÁN fut, kizárólag a még
generikus ("Speaker N") címkéjű klaszterekre.

HALLUCINÁCIÓ-VÉDELEM: név csak a candidate_pool-ból (igazoltan meghívott /
jelenlévő nevek) osztható ki, és a modellnek explicit megengedett a "nem
tudom" válasz. Pool nélkül a réteg NEM fut — szabad szöveges találgatást
soha nem engedünk be az átiratba.
"""

from __future__ import annotations

import json
import os
import re

import requests

LLM_KEY = os.environ.get("LAVOX_LLM_KEY", "")
LLM_MODEL = os.environ.get("LAVOX_LLM_MODEL", "anthropic/claude-haiku-4-5")
_GENERIC = re.compile(r"^(speaker|beszélő)\s*\d+$", re.IGNORECASE)


def available() -> bool:
    return bool(LLM_KEY)


def identify_remaining(
    segments: list[dict],
    speakers: list[dict],
    candidate_pool: list[str],
    already_named: list[str],
) -> tuple[list[dict], dict]:
    """A generikus klaszterek azonosítása a beszélgetés kontextusából.

    Vissza: (speakers frissítve, stats). Konzervatív: bizonytalan válasznál a
    klaszter névtelen marad.
    """
    if not available() or not candidate_pool:
        return speakers, {"skipped": "disabled" if not LLM_KEY else "no_pool"}

    generic = [s for s in speakers if _GENERIC.match((s.get("label") or "").strip())]
    # Csak a még ki nem osztott pool-nevek jöhetnek szóba.
    unused = [n for n in candidate_pool if n not in already_named]
    if not generic or not unused:
        return speakers, {"skipped": "nothing_to_do"}

    # Klaszterenként a leghosszabb megszólalások (max ~1500 karakter összesen).
    excerpts = []
    for spk in generic:
        segs = sorted(
            (s for s in segments if s.get("speaker") == spk["id"]),
            key=lambda s: -(s["end"] - s["start"]),
        )[:6]
        text = " | ".join(s["text"] for s in sorted(segs, key=lambda s: s["start"]))
        excerpts.append({"id": spk["id"], "label": spk["label"], "text": text[:1500]})

    prompt = (
        "Meeting-átirat részletek névtelen beszélőktől. A LEHETSÉGES nevek "
        f"(mást TILOS kiosztani): {json.dumps(unused, ensure_ascii=False)}\n\n"
        "Szabályok: egy nevet csak egy beszélőre; ha nem vagy legalább 90% "
        'biztos, a válasz "unknown". A meghívás önmagában NEM bizonyíték a '
        "jelenlétre — csak a szövegbeli evidencia számít (önbemutatkozás, "
        "megszólítás-válasz, szerep-utalás).\n\n"
        + "\n".join(f'[{e["id"]}]: {e["text"]}' for e in excerpts)
        + '\n\nVálasz CSAK JSON: {"<id>": "<név vagy unknown>", ...}'
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
        # Pool-kényszer + duplikátum-tiltás — a modell válasza önmagában nem elég.
        if name and name != "unknown" and name in unused and name not in used:
            spk["label"] = name
            used.add(name)
            renamed += 1
    return speakers, {"renamed": renamed, "asked": len(generic)}
