---
type: interface
project: Capture-AIOS
name: SystemAudioCapture
tags: [project/capture-aios, interface]
---

# `SystemAudioCapture` — a platform-audio interfész

> [!note] Ez a Track A ↔ Track B varrat.
> Egyetlen Rust trait, két platform-implementáció. A Mac-oldalt a [[TRACK-A-David|core app]] adja, a Windows-oldalt a [[TRACK-B-Adam|Windows-platform]]. Mindkettő ugyanazt az audio-stream formátumot állítja elő → a fölötte lévő pipeline (STT, diarizáció) platform-független.

Kapcsolódó: [[TRACK-A-David]] · [[TRACK-B-Adam]] · [[INTERFACE-CaptureResult]]

## A trait (vázlat)

```rust
/// Egy forrás (mikrofon vagy rendszerhang) folyamatos rögzítése.
pub trait SystemAudioCapture: Send {
    /// Elindítja a rögzítést; a mintákat a callbacken adja vissza.
    fn start(&mut self, on_samples: impl FnMut(AudioChunk)) -> Result<(), AudioError>;
    fn stop(&mut self) -> Result<(), AudioError>;
    /// Elérhető eszközök (mic / rendszerhang-endpoint).
    fn devices() -> Vec<AudioDevice>;
}

pub struct AudioChunk {
    pub samples: Vec<f32>,   // PCM, normalizált
    pub sample_rate: u32,    // pl. 16000 (a whisper.cpp-hez)
    pub channels: u16,
    pub source: AudioSource, // Mic | System
    pub ts_ms: u64,          // mintavételi idő
}

pub enum AudioSource { Mic, System }
```

## Implementációk

| Platform | Fájl | Mic | Rendszerhang | Felelős |
|---|---|---|---|---|
| macOS | `audio/macos.rs` | cpal | Core Audio tap / ScreenCaptureKit | [[TRACK-A-David|core]] |
| Windows | `audio/windows.rs` | cpal | **WASAPI loopback** | [[TRACK-B-Adam|Windows]] |
| *(stub)* | `audio/stub.rs` | – | – | core (hogy Macen Windows nélkül is forduljon) |

## Megegyezendő részletek (mindkét impl ezt tartsa)
- **Kimeneti formátum**: 16 kHz, mono, f32 PCM (a whisper.cpp ezt várja) — a resample az impl dolga.
- **Mic vs rendszerhang külön stream** (a `source` mező alapján), hogy a `speaker.is_me` később szétválasztható legyen.
- **Hibakezelés**: ha egy eszköz eltűnik (hot-plug), `AudioError` + graceful stop, ne pánikoljon.

## Státusz
- [ ] Trait véglegesítve (core)
- [ ] macOS-impl
- [ ] Windows-stub (hogy a core Macen fusson)
- [ ] Windows-impl (WASAPI)
