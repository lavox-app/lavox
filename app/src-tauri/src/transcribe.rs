//! M1.2 — whisper.cpp transcription via whisper-rs.
//! Takes a WAV file path + model path → returns segments with timestamps.
//!
//! Hallucination protection (2026-08-14): on silence, whisper — due to its
//! Hungarian training data (subtitled videos) — typically invents closing
//! phrases like "köszönöm" ("thank you") or "namaste". Four layers of defense:
//!   1. the model is loaded once and stays in memory (the "slow start" was
//!      caused by EVERY dictation reloading the 1.6 GB model),
//!   2. leading/trailing silence is trimmed (push-to-talk always includes
//!      key noise + a breath at the edges) — if the whole recording is
//!      silence, whisper is never started,
//!   3. no_context: dictations are independent, so there is no "köszönöm,
//!      köszönöm" repetition rolling over from the previous buffer,
//!   4. segment-level post-filtering: anything whisper wrote for a time window
//!      whose audio energy is at silence level is a hallucination → dropped;
//!      in windows with high no-speech probability, known hallucination
//!      phrases are dropped too (but a genuinely spoken "köszönöm" is
//!      protected by the energy measurement).

use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex, OnceLock};
use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TranscriptSegment {
    pub start_ms: i64,
    pub end_ms: i64,
    pub text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TranscriptResult {
    pub language: String,
    pub segments: Vec<TranscriptSegment>,
    pub full_text: String,
}

// ── Silence detection ───────────────────────────────────────────────────────
/// 20 ms window at 16 kHz.
const WINDOW: usize = 320;
/// RMS threshold below which a window counts as silence (~ -44 dBFS). Typical
/// room noise floor stays below it; even quiet speech is well above it.
const SILENCE_RMS: f32 = 0.006;
/// Margin around the cut (250 ms) so word starts/ends are not clipped.
const EDGE_MARGIN: usize = 4000;
/// Less actual speech than this (300 ms) → empty result, without whisper.
const MIN_SPEECH: usize = 4800;

fn rms(chunk: &[f32]) -> f32 {
    if chunk.is_empty() {
        return 0.0;
    }
    (chunk.iter().map(|s| s * s).sum::<f32>() / chunk.len() as f32).sqrt()
}

/// Trims leading/trailing silence. `None` = no speech in the recording.
/// Returns: (trimmed slice, cut start in ms — for shifting timestamps back).
fn trim_silence(samples: &[f32]) -> Option<(&[f32], i64)> {
    let n = samples.len();
    let mut first: Option<usize> = None;
    let mut last = 0usize;
    let mut i = 0;
    while i < n {
        let end = (i + WINDOW).min(n);
        if rms(&samples[i..end]) > SILENCE_RMS {
            if first.is_none() {
                first = Some(i);
            }
            last = end;
        }
        i += WINDOW;
    }
    let first = first?;
    if last.saturating_sub(first) < MIN_SPEECH {
        return None;
    }
    let start = first.saturating_sub(EDGE_MARGIN);
    let end = (last + EDGE_MARGIN).min(n);
    Some((&samples[start..end], (start as i64) * 1000 / 16_000))
}

/// Is there at least one speech-level 20 ms window in the segment's time range?
/// (Timestamps refer to the TRIMMED audio — the caller passes that as well.)
fn segment_has_speech(samples: &[f32], start_ms: i64, end_ms: i64) -> bool {
    let a = ((start_ms.max(0) as usize) * 16).min(samples.len());
    let b = ((end_ms.max(0) as usize) * 16).min(samples.len());
    if b <= a {
        return false;
    }
    samples[a..b].chunks(WINDOW).any(|c| rms(c) > SILENCE_RMS)
}

/// Known silence hallucinations (lowercase, punctuation-free form). The
/// Hungarian entries are whisper ASR hallucination artifacts matched against
/// Hungarian speech — keep them byte-identical, do NOT translate. They are
/// dropped ONLY under high no-speech probability — for a genuinely spoken
/// "köszönöm", no-speech is low and the energy check lets it through.
const HALLUCINATIONS: &[&str] = &[
    "köszönöm",
    "köszönöm szépen",
    "köszönöm a figyelmet",
    "köszönöm hogy megnéztétek",
    "köszönjük a figyelmet",
    "namaste",
    "feliratok",
    "felirat",
    "thank you",
    "thanks for watching",
    "subtitles by the amara org community",
    "you",
];

fn is_hallucination(text: &str) -> bool {
    let normalized: String = text
        .to_lowercase()
        .chars()
        .filter(|c| c.is_alphanumeric() || c.is_whitespace())
        .collect();
    let t = normalized.split_whitespace().collect::<Vec<_>>().join(" ");
    HALLUCINATIONS.iter().any(|h| t == *h)
}

// ── Model cache ─────────────────────────────────────────────────────────────
/// The loaded whisper context (model) stays in memory after the first
/// dictation — loading takes several seconds and used to run on EVERY call.
/// Cost: the model (~1.6 GB) stays resident; that is the standard trade-off
/// dedicated dictation apps (e.g. superwhisper) make for instant response.
static MODEL_CACHE: OnceLock<Mutex<Option<(String, Arc<WhisperContext>)>>> = OnceLock::new();

fn cached_context(model_path: &str) -> Result<Arc<WhisperContext>, String> {
    let cell = MODEL_CACHE.get_or_init(|| Mutex::new(None));
    let mut guard = cell.lock().map_err(|_| "model cache lock poisoned".to_string())?;
    if let Some((path, ctx)) = guard.as_ref() {
        if path == model_path {
            return Ok(ctx.clone());
        }
    }
    let ctx = WhisperContext::new_with_params(model_path, WhisperContextParameters::default())
        .map_err(|e| format!("model load failed: {e}"))?;
    let arc = Arc::new(ctx);
    *guard = Some((model_path.to_string(), arc.clone()));
    Ok(arc)
}

/// Load WAV, resample to 16kHz mono f32, run whisper, return segments.
/// `lang`: ISO code of the desired language (e.g. "hu", "en"), or None →
/// auto-detect (used when multiple languages are enabled in settings).
pub fn transcribe_wav(
    wav_path: &str,
    model_path: &str,
    lang: Option<&str>,
    translate_to_english: bool,
) -> Result<TranscriptResult, String> {
    let all_samples = load_wav_16khz_mono(wav_path)?;
    let result_language = if translate_to_english {
        "en".to_string()
    } else {
        lang.unwrap_or("auto").to_string()
    };

    // Silence trimming. With no speech, whisper is never started → nothing
    // to hallucinate, and the result is instant.
    let Some((samples, offset_ms)) = trim_silence(&all_samples) else {
        return Ok(TranscriptResult {
            language: result_language,
            segments: Vec::new(),
            full_text: String::new(),
        });
    };

    let ctx = cached_context(model_path)?;
    let mut state = ctx.create_state().map_err(|e| format!("state create failed: {e}"))?;

    // Beam search gives notably better accuracy than greedy for Hungarian.
    let mut params = FullParams::new(SamplingStrategy::BeamSearch {
        beam_size: 5,
        patience: -1.0,
    });
    params.set_language(lang); // None → whisper auto-detect
    // TRANSLATE TO ENGLISH: whisper translates any speech to English (English only).
    params.set_translate(translate_to_english);
    params.set_print_progress(false);
    params.set_print_realtime(false);
    params.set_print_timestamps(false);
    params.set_suppress_blank(true);
    params.set_suppress_nst(true);
    // Dictations are independent: the previous buffer's text must NOT be
    // context — otherwise the model tends to roll the previous closing
    // phrase ("köszönöm") forward.
    params.set_no_context(true);
    // Personal dictionary → vocabulary biasing. Dictations are short (<30 s),
    // so the whole utterance sits in the first window where the prompt applies.
    let dict = crate::dictionary::load();
    let dict_prompt = dict.initial_prompt();
    if let Some(ref prompt) = dict_prompt {
        params.set_initial_prompt(prompt);
    }
    // Use all physical cores for faster inference.
    let threads = std::thread::available_parallelism()
        .map(|n| n.get() as i32)
        .unwrap_or(4);
    params.set_n_threads(threads);

    state
        .full(params, samples)
        .map_err(|e| format!("transcription failed: {e}"))?;

    let n = state.full_n_segments();
    let mut segments = Vec::with_capacity(n as usize);
    let mut full_text = String::new();

    for i in 0..n {
        let seg = state
            .get_segment(i)
            .ok_or_else(|| format!("segment {i} not found"))?;
        let start_ms = seg.start_timestamp() * 10;
        let end_ms = seg.end_timestamp() * 10;
        let text = dict.apply(
            seg.to_str_lossy()
                .map_err(|e| format!("segment text failed: {e}"))?
                .trim(),
        );
        if text.is_empty() {
            continue;
        }

        // ── Hallucination filtering ────────────────────────────────────────
        let no_speech = seg.no_speech_probability();
        // (a) text over a silence-level time window → definitely invented
        if !segment_has_speech(samples, start_ms, end_ms) {
            continue;
        }
        // (b) whisper itself thinks it is silence AND a known hallucination phrase
        if no_speech > 0.5 && is_hallucination(&text) {
            continue;
        }
        // (c) very high no-speech: whatever the text is, it did not come from speech
        if no_speech > 0.9 {
            continue;
        }

        if !full_text.is_empty() {
            full_text.push(' ');
        }
        full_text.push_str(&text);
        segments.push(TranscriptSegment {
            // Timestamps must refer to the ORIGINAL (untrimmed) recording.
            start_ms: start_ms + offset_ms,
            end_ms: end_ms + offset_ms,
            text,
        });
    }

    Ok(TranscriptResult {
        language: result_language,
        segments,
        full_text,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Speech-like signal (0.3-amplitude sine) — well above the silence threshold.
    fn speech(n: usize) -> Vec<f32> {
        (0..n).map(|i| 0.3 * (i as f32 * 0.05).sin()).collect()
    }
    /// Room noise floor: deterministic, very quiet signal BELOW the threshold.
    fn quiet(n: usize) -> Vec<f32> {
        (0..n).map(|i| 0.0008 * (i as f32 * 0.31).sin()).collect()
    }

    #[test]
    fn silent_recording_never_reaches_whisper() {
        // 3 seconds of noise floor only → None, so whisper is never started.
        assert!(trim_silence(&quiet(48_000)).is_none());
    }

    #[test]
    fn too_short_click_does_not_count_as_speech() {
        // 100 ms of "speech" surrounded by silence — below MIN_SPEECH (300 ms).
        let mut v = quiet(16_000);
        v.extend(speech(1_600));
        v.extend(quiet(16_000));
        assert!(trim_silence(&v).is_none());
    }

    #[test]
    fn real_speech_passes_and_offset_is_correct() {
        // 1 s silence + 1 s speech + 1 s silence
        let mut v = quiet(16_000);
        v.extend(speech(16_000));
        v.extend(quiet(16_000));
        let (trimmed, offset_ms) = trim_silence(&v).expect("must detect speech");
        // The cut starts EDGE_MARGIN (250 ms) before the speech → ~750 ms.
        assert!(
            (600..=900).contains(&offset_ms),
            "offset {offset_ms} ms is outside the expected range"
        );
        // The trimmed part is shorter than the original, but contains the speech.
        assert!(trimmed.len() < v.len());
        assert!(trimmed.len() >= 16_000);
    }

    #[test]
    fn hallucination_detection_normalizes() {
        // Hungarian phrases below are functional data: they must match the
        // (Hungarian) hallucination blacklist exactly — do not translate.
        assert!(is_hallucination("Köszönöm!"));
        assert!(is_hallucination("  köszönöm szépen  "));
        assert!(is_hallucination("Namaste."));
        assert!(is_hallucination("Thank you."));
        // Real sentences that contain the word — NOT hallucinations.
        assert!(!is_hallucination("köszönöm hogy elküldted a szerződést"));
        assert!(!is_hallucination("köszönöm a gyors választ"));
        assert!(!is_hallucination("ez egy normális mondat"));
    }

    #[test]
    fn segment_energy_check() {
        // 2 s: [0-1s] speech, [1-2s] silence
        let mut v = speech(16_000);
        v.extend(quiet(16_000));
        assert!(segment_has_speech(&v, 0, 1000), "first second is speech");
        assert!(!segment_has_speech(&v, 1000, 2000), "second second is silence");
        // Range outside the recording → no speech (no panic).
        assert!(!segment_has_speech(&v, 5000, 6000));
    }

    #[test]
    fn genuinely_spoken_thank_you_is_kept() {
        // The CRITICAL case: the user really says "köszönöm" (thank you).
        // The energy check must let it through — rule (b) only filters under
        // high no-speech, which whisper does not report for real speech.
        let v = speech(16_000);
        assert!(segment_has_speech(&v, 0, 1000));
    }
}

/// Read WAV via hound, convert to 16kHz mono f32 (what whisper.cpp expects).
/// pub(crate): the track mixing in remote.rs uses this too.
pub(crate) fn load_wav_16khz_mono(path: &str) -> Result<Vec<f32>, String> {
    let mut reader = hound::WavReader::open(path).map_err(|e| format!("WAV open failed: {e}"))?;
    let spec = reader.spec();

    let samples_f32: Vec<f32> = match spec.sample_format {
        hound::SampleFormat::Int => {
            let max = (1 << (spec.bits_per_sample - 1)) as f32;
            reader
                .samples::<i32>()
                .map(|s| s.map(|v| v as f32 / max))
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| e.to_string())?
        }
        hound::SampleFormat::Float => reader
            .samples::<f32>()
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| e.to_string())?,
    };

    // Mix to mono if multi-channel
    let mono = if spec.channels > 1 {
        let ch = spec.channels as usize;
        samples_f32
            .chunks(ch)
            .map(|frame| frame.iter().sum::<f32>() / ch as f32)
            .collect()
    } else {
        samples_f32
    };

    let source_rate = spec.sample_rate;
    if source_rate == 16000 {
        return Ok(mono);
    }

    Ok(resample_to_16k(&mono, source_rate))
}

/// Resample to 16kHz. Downsampling (e.g. 48kHz mic → 16kHz) uses windowed
/// averaging as an anti-aliasing low-pass before decimation; naive linear
/// interpolation would fold high frequencies back into the speech band and
/// hurt transcription accuracy. Upsampling falls back to linear interpolation.
fn resample_to_16k(input: &[f32], source_rate: u32) -> Vec<f32> {
    let target = 16000.0_f64;
    let src = source_rate as f64;
    let ratio = target / src;
    let out_len = (input.len() as f64 * ratio) as usize;
    let mut out = Vec::with_capacity(out_len);

    if src > target {
        // Downsample: average each input window mapped to one output sample.
        let step = src / target; // > 1.0
        for i in 0..out_len {
            let start = (i as f64 * step) as usize;
            let end = (((i + 1) as f64 * step) as usize).min(input.len());
            if end > start {
                let sum: f32 = input[start..end].iter().sum();
                out.push(sum / (end - start) as f32);
            } else if let Some(&s) = input.get(start) {
                out.push(s);
            }
        }
    } else {
        // Upsample: linear interpolation.
        for i in 0..out_len {
            let src_idx = i as f64 / ratio;
            let idx0 = src_idx as usize;
            let frac = (src_idx - idx0 as f64) as f32;
            let s0 = input.get(idx0).copied().unwrap_or(0.0);
            let s1 = input.get(idx0 + 1).copied().unwrap_or(s0);
            out.push(s0 + frac * (s1 - s0));
        }
    }

    out
}
