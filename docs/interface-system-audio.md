---
type: interface
project: Capture-AIOS
name: SystemAudioCapture
tags: [project/capture-aios, interface]
---

# `SystemAudioCapture`: the platform audio interface

> [!note] This is the seam between Track A and Track B.
> One Rust trait, two platform implementations. The Mac side comes from the core app, the Windows side from a future Windows platform layer. Both produce the same audio stream format, so the pipeline above them (STT, diarization) is platform-independent.

> **Design note.** This document comes from the planning phase and describes the intended contract. Where it and the code differ, the code is authoritative.

In the current code the macOS side is `recorder.rs` (cpal, microphone) plus the Swift helper `helpers/syscap` (ScreenCaptureKit, system audio); the trait below is the target shape.

Related: [CaptureResult](interface-capture-result.md)

## The trait (sketch)

```rust
/// Continuous capture of one source (microphone or system audio).
pub trait SystemAudioCapture: Send {
    /// Starts capturing; delivers samples through the callback.
    fn start(&mut self, on_samples: impl FnMut(AudioChunk)) -> Result<(), AudioError>;
    fn stop(&mut self) -> Result<(), AudioError>;
    /// Available devices (microphone / system-audio endpoint).
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
| macOS | `audio/macos.rs` | cpal | Core Audio tap / ScreenCaptureKit | core app |
| Windows | `audio/windows.rs` | cpal | **WASAPI loopback** | Windows layer (planned) |
| *(stub)* | `audio/stub.rs` | – | – | core (so the Mac build compiles without the Windows side) |

## Contract both implementations must honor
- **Output format**: 16 kHz, mono, f32 PCM (what whisper.cpp expects). Resampling is the implementation's job.
- **Microphone and system audio as separate streams** (via the `source` field), so `speaker.is_me` can be derived later.
- **Error handling**: if a device disappears (hot-plug), return an `AudioError` and stop gracefully. Never panic.

## Status
- [ ] Trait finalized (core)
- [ ] macOS implementation
- [ ] Windows stub (so core runs on the Mac)
- [ ] Windows implementation (WASAPI)
