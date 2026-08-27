---
type: interface
project: Capture-AIOS
name: CaptureResult
version: 1.0.0-draft
tags: [project/capture-aios, interface]
---

# `CaptureResult`: the central data interface

> [!note] This is the engine → export data model.
> The capture engine **produces** it and the ObsidianExporter **consumes** it. As long as the shape is fixed, the UI and the export can be developed against mock data. **Versioned**: adding a new optional field is non-breaking; deleting or renaming a field is breaking and requires a version bump.

> **Design note.** This document comes from the planning phase and describes the intended contract. Where it and the code differ, the code is authoritative.

Related: [SystemAudioCapture](interface-system-audio.md)

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

  // Filled in AFTER the LLM pass (null/empty before it):
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
| media.audio_path / video_url | ✅ | links to the file / web link |
| speakers[] / segments[] | ✅ (STT + diarization) | markdown grouped by speaker |
| summary, action_items, title, tags | ✅ (LLM pass) | frontmatter + body |

## Edge cases
- **No diarization** (dictation): a single speaker `S1` with `is_me: true`.
- **Before the LLM pass**: `summary` and friends are null. The export must handle this gracefully, not error out.
- **Streaming**: several `partial` results, then one `final` at the end; the `final` is the source of truth.

## Open decisions (before M0)
> [!question] D1: `segments[]` granularity
> Segment level (sentence) or word level (`words[]`)? **Recommendation:** segment level for v0.1; word level later, as an additive field.
> - [ ] Decision: __________

> [!question] D2: Streaming or batch
> Live transcript (a Wispr-style experience) now, or everything at once at the end? **Recommendation:** **batch** for v0.1 (simpler engine); live dictation in v0.2. `status` stays in the schema either way.
> - [ ] Decision: __________

## Mock
`app/mocks/sample-meeting.json` is a filled-in instance of the schema. It is a hand-maintained fixture; the export tests in `export.rs` parse it, so any schema drift shows up as a failing test.

## Sign-off
- [ ] D1 and D2 decided
- [ ] v1.0.0 locked: only additive changes from here on
