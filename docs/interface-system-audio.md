---
type: interface
project: Capture-AIOS
name: SystemAudioCapture
tags: [project/capture-aios, interface]
---

# `SystemAudioCapture` — the platform audio interface

> [!note] This is the Track A ↔ Track B seam.
> A single Rust trait, two platform implementations. The Mac side comes from the [[TRACK-A-David|core app]], the Windows side from the [[TRACK-B-Adam|Windows platform]]. Both produce the same audio stream format → the pipeline above them (STT, diarization) is platform-independent.

Related: [[TRACK-A-David]] · [[TRACK-B-Adam]] · [[INTERFACE-CaptureResult]]

## The trait (sketch)

```rust
/// Continuous capture of one source (microphone or system audio).
pub trait SystemAudioCapture: Send {
    /// Starts capturing; delivers samples via the callback.
    fn start(&mut self, on_samples: impl FnMut(AudioChunk)) -> Result<(), AudioError>;
    fn stop(&mut self) -> Result<(), AudioError>;
    /// Available devices (mic / system-audio endpoint).
    fn devices() -> Vec<AudioDevice>;
}

pub struct AudioChunk {
    pub samples: Vec<f32>,   // PCM, normalized
    pub sample_rate: u32,    // e.g. 16000 (for whisper.cpp)
    pub channels: u16,
    pub source: AudioSource, // Mic | System
    pub ts_ms: u64,          // capture timestamp
}

pub enum AudioSource { Mic, System }
```

## Implementations

| Platform | File | Mic | System audio | Owner |
|---|---|---|---|---|
| macOS | `audio/macos.rs` | cpal | Core Audio tap / ScreenCaptureKit | [[TRACK-A-David|core]] |
| Windows | `audio/windows.rs` | cpal | **WASAPI loopback** | [[TRACK-B-Adam|Windows]] |
| *(stub)* | `audio/stub.rs` | – | – | core (so it compiles on Mac without the Windows side) |

## Details both implementations must agree on
- **Output format**: 16 kHz, mono, f32 PCM (what whisper.cpp expects) — resampling is the implementation's job.
- **Mic and system audio as separate streams** (via the `source` field), so `speaker.is_me` can be separated later.
- **Error handling**: if a device disappears (hot-plug), `AudioError` + graceful stop, never panic.

## Status
- [ ] Trait finalized (core)
- [ ] macOS impl
- [ ] Windows stub (so core runs on Mac)
- [ ] Windows impl (WASAPI)
