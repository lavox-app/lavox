"""Lavox Memory — kinyerő réteg (M1): beszédből típusozott állítások.

A felvétel-átiratból egy LLM tartós állításokat nyer ki (decision / fact /
commitment / preference / task), a döntéseknél az ELVETETT ALTERNATÍVÁKKAL
és az indoklással együtt — ez a rendszer magja, amit egyetlen piaci
versenytárs sem csinál (ADR a hangodból).

ELVEK (a kutatás méréseiből):
- A kinyerés INDEX, nem helyettesítés: a nyers átirat marad a kanonikus tár,
  minden állítás quote-tal horgonyzódik egy forrás-chunkhoz (provenance).
- Write-time konfliktus-döntés: az új állítás a hozzá hasonló ÉLŐ állításokkal
  szembesül; ha ugyanarról szól ellentmondóan, a régi supersedes-láncba kerül
  (invalidated_at + superseded_by) — törlés SOHA nincs.
- A kinyerés OPCIONÁLIS: LAVOX_LLM_KEY nélkül a rendszer degradáltan, de
  működik (a verbatim réteg önállóan is keres). Nincs kényszer-LLM.

Futtatás (batch, minden még ki nem nyert felvételre):
  LAVOX_LLM_KEY=... .venv/bin/python3 extract.py            # mind
  LAVOX_LLM_KEY=... .venv/bin/python3 extract.py <rec_id>   # egy felvétel
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
# Bármely OpenAI-kompatibilis chat-completions végpont megadható (Ollama, vLLM,
# OpenAI, ...) — alapértelmezés az OpenRouter.
LLM_URL = os.environ.get(
    "LAVOX_LLM_URL", "https://openrouter.ai/api/v1/chat/completions"
)

MAX_ASSERTIONS = 12
MAX_TRANSCRIPT_CHARS = 180_000   # haiku-kontextusba bőven belefér
# vec0 távolság-küszöb a "lehet, hogy ugyanarról szól" jelöltekhez.
# Normalizált vektoroknál L2² = 2−2·cos; 0.9 ≈ cos 0.55 — tágra vett háló,
# a végső döntést úgyis az LLM hozza.
SIMILAR_DIST_THRESHOLD = 0.9

EXTRACT_SYSTEM = """Meeting- vagy diktálás-átiratból nyersz ki TARTÓS állításokat egy személyes memória-rendszerbe. Csak olyat, ami hetek múlva is számítani fog.

Típusok:
- decision: kimondott döntés. KÖTELEZŐ hozzá a data mező: {"chosen": mi lett választva, "alternatives": [amiket mérlegeltek és elvetettek — CSAK ha tényleg elhangzottak], "reasoning": a kimondott indok}. A legértékesebb típus: fél év múlva a kérdés nem az lesz, MIT döntöttek, hanem hogy MIÉRT NEM a másikat.
- fact: tartós tény (ár, határidő, követelmény, név, állapot).
- commitment: ki mit ígért, kinek, mikorra.
- preference: tartós preferencia vagy munkamódszer.
- task: kiadott, konkrét feladat.

FONTOS a bemenetről: az átirat AUTOMATIKUS beszédfelismerésből származik, és sok benne a félrehallott, torzult szó. Ez normális — a feladatod pont az, hogy a zaj ellenére kinyerd, ami FELISMERHETŐ. Ha egy szó nyilvánvalóan félrehallás, de a kontextusból egyértelmű a szándékolt jelentés, használd a szándékolt jelentést. A zaj miatt SOHA ne adj vissza üres listát, ha a beszélgetés témái és állításai azonosíthatók — a bizonytalanságot a confidence mezőben jelezd (zajos forrásnál 0.4-0.7).

Szabályok:
1. Csak ami a beszélgetésből kivehető — új információt ne találj ki. A torzult szavak szándékolt jelentése kikövetkeztethető, de teljes állításokat nem adhatsz hozzá.
2. Minden állításhoz adj "quote"-ot: egy RÖVID (5-15 szavas), SZÓ SZERINTI részlet az átiratból, ahonnan az állítás származik. Pontosan másold, ne javítsd a hibáit.
3. Az állítás szövege ÖNHORDÓ legyen: nevekkel, ne névmásokkal ("Dani két módosítást kért a főoldalon", nem "két módosítást kért").
4. Magyar átiratból magyarul, angolból angolul.
5. Legfeljebb 12 állítás — a legfontosabbak. Small talk, köszönés, ismétlés kihagyva.
6. Ha nincs érdemi tartós tartalom, adj üres listát.
7. confidence: 0.0-1.0 — mennyire egyértelműen hangzott el.

Válasz KIZÁRÓLAG ez a JSON:
{"assertions":[{"type":"...","text":"...","quote":"...","confidence":0.9,"data":{...}}]}"""

RECONCILE_SYSTEM = """Két állítás egy személyes memóriából: egy RÉGI (már tárolt) és egy ÚJ (most készül). Döntsd el a viszonyukat:

- "duplicate": ugyanazt az információt hordozzák — az újat nem kell tárolni.
- "supersedes": ugyanarról a kérdésről szólnak, de az ÚJ ellentmond a réginek vagy frissebb döntés/állapot — az új felülírja a régit.
- "separate": különböző dolgokról szólnak, mindkettő önállóan érvényes.

Csak akkor "supersedes", ha TÉNYLEG ugyanaz a kérdés (ugyanaz a projekt/tárgy) és valódi az ellentmondás vagy frissítés. Kétség esetén "separate".

Válasz KIZÁRÓLAG: {"relation":"duplicate|supersedes|separate"}"""


def _llm(system: str, user: str, max_tokens: int = 4000) -> dict[str, Any]:
    resp = httpx.post(
        LLM_URL,
        headers={
            "Authorization": f"Bearer {LLM_KEY}",
            "Content-Type": "application/json",
            # az OpenRouter/Cerebras 403-at ad default python User-Agentre
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
    # védekező JSON-kinyerés (kódfence-be csomagolás ellen)
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
    """A quote horgonyzása a forrás-chunkhoz. A provenance kötelező elv —
    ha a pontos szövegrész nem található (a modell javított a helyesíráson),
    FTS-sel a legjobb chunkra esünk vissza ugyanabban a felvételben."""
    if not quote:
        return None
    norm = re.sub(r"\s+", " ", quote.strip().lower())
    rows = db.execute(
        "SELECT id, text FROM chunks WHERE recording_id=?", (recording_id,)
    ).fetchall()
    for cid, text in rows:
        if norm[:60] in re.sub(r"\s+", " ", text.lower()):
            return cid
    # fallback: FTS a felvételen belül
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
    """Az új állítás viszonya a meglévő ÉLŐ állításokhoz.
    Visszatérés: ("separate"|"duplicate"|"supersedes", régi_id|None)."""
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
            f"RÉGI: {old[1]}\n\nÚJ: {new_text}",
            max_tokens=100,
        ).get("relation", "separate")
        if verdict in ("duplicate", "supersedes"):
            return verdict, old[0]
    return "separate", None


def extract_recording(db, recording_id: str, verbose: bool = True) -> dict[str, Any]:
    """Egy felvétel kinyerése: LLM → állítások → reconcile → mentés."""
    if not LLM_KEY:
        return {"error": "LAVOX_LLM_KEY nincs beállítva — a kinyerés kihagyva"}

    rec = db.execute(
        "SELECT id, kind, title, occurred_at, meta FROM recordings WHERE id=?",
        (recording_id,),
    ).fetchone()
    if not rec:
        return {"error": f"nincs ilyen felvétel: {recording_id}"}
    meta = json.loads(rec[4] or "{}")
    if meta.get("extracted_at"):
        return {"id": recording_id, "status": "skipped (már kinyerve)"}

    transcript = _transcript_text(db, recording_id)
    if len(transcript) < 200:
        _mark_extracted(db, recording_id, meta, 0)
        return {"id": recording_id, "status": "túl rövid, kihagyva"}

    user_msg = (
        f"Felvétel: {rec[2] or '(cím nélkül)'} | típus: {rec[1]} | dátum: {rec[3][:10]}\n\n"
        f"ÁTIRAT:\n{transcript}"
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
                print(f"    ~ duplikátum, kihagyva: {text[:70]}")
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
                print(f"    ⤷ felülírja a(z) assertion:{old_id}-t: {text[:70]}")
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
