"""Lavox Memory — backfill: meglévő meetingek betöltése a memóriába.

Bemenet: JSONL (soronként egy meeting), a VPS Postgresből exportálva:
  {"id","title","kind","occurred_at","duration_sec","participants":[],
   "transcript":[{"start","end","text","speaker"}], "speakers":[{"id","label"}]}

A speaker-azonosítókat (spk_xxx / SPEAKER_01) a speakers-tábla label-jeire
oldjuk fel, hogy a chunk-fejlécben nevek legyenek — a kontextus-injekció
értéke pont a kereshető neveken múlik.

Futtatás:
  .venv/bin/python3 backfill_memory.py meetings.jsonl [--force]
"""

from __future__ import annotations

import json
import sys

import memory


def resolve_speakers(transcript, speakers):
    label = {}
    for s in speakers or []:
        if isinstance(s, dict) and s.get("id"):
            label[s["id"]] = s.get("label") or s["id"]
    out = []
    for seg in transcript or []:
        seg = dict(seg)
        spk = seg.get("speaker")
        if spk in label:
            seg["speaker"] = label[spk]
        out.append(seg)
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    force = "--force" in sys.argv

    db = memory.connect()
    total = ok = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            m = json.loads(line)
            segments = resolve_speakers(m.get("transcript"), m.get("speakers"))
            participants = m.get("participants") or [
                s.get("label") for s in (m.get("speakers") or [])
                if isinstance(s, dict) and s.get("label")
            ]
            rec = {
                "id": m["id"],
                "kind": m.get("kind") or "meeting",
                "title": m.get("title"),
                "occurred_at": m.get("occurred_at"),
                "duration_sec": m.get("duration_sec"),
                "participants": [p for p in participants if p],
                "meta": {"source": "backfill"},
            }
            res = memory.ingest_recording(db, rec, segments, force=force)
            print(f"  {m['id']}: {res['status']}"
                  + (f" ({res.get('chunks')} chunk)" if res.get("chunks") else ""))
            if res["status"] == "ok":
                ok += 1
    print(f"\nKész: {ok}/{total} betöltve.")
    print(json.dumps(memory.stats(db), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
