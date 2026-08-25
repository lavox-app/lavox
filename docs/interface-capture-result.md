---
type: interface
project: Capture-AIOS
name: CaptureResult
version: 1.0.0-draft
tags: [project/capture-aios, interface]
---

# `CaptureResult`: the central data interface

> [!note] This is the engine → export data model.
> The capture engine **produces** it, the ObsidianExporter **consumes** it. As long as the shape is fixed, the UI/export can be developed against mock data. **Versioned**: adding a new optional field is non-breaking; deleting/renaming a field is breaking → version bump.

Related: [[PROJECT-BRIEF]] · [[TRACK-A-David]] · [[INTERFACE-SystemAudioCapture]]

## The schema (v1)

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
    video_url?: string | null;      // R2/Cloudflare URL, or null
  };

  speakers: Array<{ id: string; label: string; is_me?: boolean }>;

  segments: Array<{
    start: number; end: number;     // seconds
    speaker: string;                // speakers[].id
    text: string;
  }>;

  // Filled AFTER the LLM pass (null/empty before it):
  summary?: string | null;
  action_items?: string[];
  title?: string | null;
  tags?: string[];

  meta?: Record<string, unknown>;
};
```

## Field ownership
| Field | Engine fills | Export reads |
|---|---|---|
| id, type, status, created_at, duration, language | ✅ | ✅ |
| media.audio_path / video_url | ✅ | links it / web link |
| speakers[] / segments[] | ✅ (STT+diarization) | speaker-label markdown |
| summary, action_items, title, tags | ✅ (LLM pass) | frontmatter + body |

## Edge cases
- **No diarization** (dictation): a single speaker `S1`, `is_me:true`.
- **Before the LLM pass**: `summary/...` is null → the export must handle this (not error out).
- **Streaming**: multiple `partial`s, one `final` at the end; the `final` is the source of truth.

## Open decisions (before M0)
> [!question] D1: `segments[]` granularity
> Segment level (sentence) vs word level (`words[]`). **Recommendation:** v0.1 segment level; word level as an additive field later.
> - [ ] Decision: __________

> [!question] D2: Streaming vs batch
> Live transcript (Wispr-style experience) now, or all at once at the end? **Recommendation:** v0.1 **batch** (simpler engine); live dictation in v0.2. `status` stays in the schema.
> - [ ] Decision: __________

## Mock
`mocks/sample-meeting.json`: a filled-in instance of the schema. Hand-maintained fixture; the export tests (`export.rs`) run against it, so any schema drift surfaces at compile time.

## Sign-off
- [ ] D1 + D2 decided
- [ ] v1.0.0 lock → only additive changes from here on
