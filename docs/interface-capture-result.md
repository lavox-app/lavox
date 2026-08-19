---
type: interface
project: Capture-AIOS
name: CaptureResult
version: 1.0.0-draft
tags: [project/capture-aios, interface]
---

# `CaptureResult` — a központi adat-interfész

> [!note] Ez a motor → export adatmodell.
> A capture-motor ezt **előállítja**, az ObsidianExporter ezt **fogyasztja**. Amíg az alak fix, a UI/export mock-adattal is fejleszthető. **Verziózott**: új opcionális mező nem törő; mező törlése/átnevezése breaking → verzió-bump.

Kapcsolódó: [[PROJECT-BRIEF]] · [[TRACK-A-David]] · [[INTERFACE-SystemAudioCapture]]

## A séma (v1)

```typescript
type CaptureResult = {
  id: string;                       // uuid v4
  type: "meeting" | "dictation" | "note";
  status: "partial" | "final";
  created_at: string;               // ISO-8601 UTC
  duration_sec: number;
  language: string;                 // BCP-47, default "hu"
  source_app?: string | null;       // "Zoom" | "Meet" | "mic" | null

  media: {
    audio_path: string;
    video_url?: string | null;      // R2/Cloudflare URL, vagy null
  };

  speakers: Array<{ id: string; label: string; is_me?: boolean }>;

  segments: Array<{
    start: number; end: number;     // mp
    speaker: string;                // speakers[].id
    text: string;
  }>;

  // Az LLM-pass UTÁN töltődik (előtte null/üres):
  summary?: string | null;
  action_items?: string[];
  title?: string | null;
  tags?: string[];

  meta?: Record<string, unknown>;
};
```

## Mező-felelősség
| Mező | Motor tölti | Export olvassa |
|---|---|---|
| id, type, status, created_at, duration, language | ✅ | ✅ |
| media.audio_path / video_url | ✅ | linkeli / web-link |
| speakers[] / segments[] | ✅ (STT+diarizáció) | speaker-label markdown |
| summary, action_items, title, tags | ✅ (LLM-pass) | frontmatter + törzs |

## Élezetek
- **Nincs diarizáció** (diktálás): egy speaker `S1`, `is_me:true`.
- **LLM-pass előtt**: `summary/...` null → az export ezt kezelje (ne hibázzon).
- **Streaming**: több `partial`, végén egy `final`; a `final` az igazság.

## Nyitott döntések (M0 előtt)
> [!question] D1 — `segments[]` granularitás
> Szegmens-szint (mondat) vs szó-szint (`words[]`). **Ajánlás:** v0.1 szegmens-szint; a szó-szint additív mező később.
> - [ ] Döntés: __________

> [!question] D2 — Streaming vs batch
> Live átirat (Wispr-élmény) most, vagy a végén egyben? **Ajánlás:** v0.1 **batch** (egyszerűbb motor); live diktálás v0.2. A `status` marad a sémában.
> - [ ] Döntés: __________

## Mock
`mocks/sample-meeting.json` — a séma kitöltött példánya. Kézzel karbantartott fixture; az export-tesztek (`export.rs`) ellene futnak, így a séma-eltérés fordításkor kibukik.

## Jóváhagyás
- [ ] D1 + D2 eldöntve
- [ ] v1.0.0 lock → innen csak additív változás
