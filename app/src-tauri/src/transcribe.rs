//! M1.2 — whisper.cpp transcription via whisper-rs.
//! Takes a WAV file path + model path → returns segments with timestamps.
//!
//! Hallucináció-védelem (2026-08-14): a whisper a csendre — a magyar
//! tanítóanyag (feliratos videók) miatt — tipikusan "köszönöm"-szerű záró
//! mondatokat vagy "namaste"-t talál ki. A védelem négy rétege:
//!   1. a modell egyszer töltődik be és memóriában marad (a "lassú eleje" oka
//!      az volt, hogy MINDEN diktálás újratöltötte az 1,6 GB-os modellt),
//!   2. a felvétel eleji/végi csend levágása (a push-to-talk mindig tartalmaz
//!      billentyű-zajt + levegővételt a széleken) — ha az egész felvétel
//!      csend, a whisper el sem indul,
//!   3. no_context: a diktálások függetlenek, nincs "köszönöm, köszönöm"
//!      ismétlés-görgetés az előző pufferből,
//!   4. szegmens-szintű utószűrés: amit a whisper olyan időablakra írt,
//!      amelyben a hang energiája csend-szintű, az hallucináció → eldobjuk;
//!      magas no-speech valószínűségű ablakban az ismert hallucináció-frázisok
//!      szintén kiesnek (de valódi kimondott "köszönöm"-öt az energia-mérés
//!      megvéd).

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

// ── Csend-detektálás ────────────────────────────────────────────────────────
/// 20 ms ablak 16 kHz-en.
const WINDOW: usize = 320;
/// RMS-küszöb, ami alatt egy ablak csendnek számít (~ -44 dBFS). A tipikus
/// szoba-alapzaj ez alatt marad, halk beszéd is jóval fölötte van.
const SILENCE_RMS: f32 = 0.006;
/// Margó a vágásnál (250 ms), hogy a szóvégek/szókezdetek ne csorbuljanak.
const EDGE_MARGIN: usize = 4000;
/// Ennél rövidebb tényleges beszéd (300 ms) → üres eredmény, whisper nélkül.
const MIN_SPEECH: usize = 4800;

fn rms(chunk: &[f32]) -> f32 {
    if chunk.is_empty() {
        return 0.0;
    }
    (chunk.iter().map(|s| s * s).sum::<f32>() / chunk.len() as f32).sqrt()
}

/// Eleji/végi csend levágása. `None` = nincs beszéd a felvételben.
/// Vissza: (vágott szelet, a vágás kezdete ms-ben — a timestampek visszatolásához).
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

/// Van-e legalább egy beszéd-erősségű 20 ms-ablak a szegmens időtartományában?
/// (A timestampek a VÁGOTT hangra vonatkoznak — a hívó is azt adja át.)
fn segment_has_speech(samples: &[f32], start_ms: i64, end_ms: i64) -> bool {
    let a = ((start_ms.max(0) as usize) * 16).min(samples.len());
    let b = ((end_ms.max(0) as usize) * 16).min(samples.len());
    if b <= a {
        return false;
    }
    samples[a..b].chunks(WINDOW).any(|c| rms(c) > SILENCE_RMS)
}

/// Ismert csend-hallucinációk (kisbetűs, írásjel nélküli alak). CSAK magas
/// no-speech valószínűség mellett esnek ki — valódi kimondott "köszönöm"-nél
/// a no-speech alacsony és az energia-ellenőrzés is átengedi.
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

// ── Modell-gyorsítótár ──────────────────────────────────────────────────────
/// A betöltött whisper-kontextus (modell) memóriában marad az első diktálás
/// után — a modell-betöltés több másodperc, és eddig MINDEN híváskor lefutott.
/// Ára: a modell (~1,6 GB) rezidens marad; ez a dedikált diktáló-appok
/// (pl. superwhisper) bevett kompromisszuma a azonnali reakcióidőért.
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
/// `lang`: a kívánt nyelv ISO kódja (pl. "hu", "en"), vagy None → auto-detektálás
/// (akkor, ha több nyelv engedélyezett a beállításban).
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

    // Csend-vágás. Ha nincs beszéd, a whisper el sem indul → nincs mit
    // hallucinálnia, és az eredmény azonnali.
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
    // ANGOLRA FORDÍTÁS: a whisper bármilyen beszédet angolra fordít (csak angolra tud).
    params.set_translate(translate_to_english);
    params.set_print_progress(false);
    params.set_print_realtime(false);
    params.set_print_timestamps(false);
    params.set_suppress_blank(true);
    params.set_suppress_nst(true);
    // A diktálások függetlenek: az előző puffer szövege NE legyen kontextus —
    // enélkül a modell hajlamos az előző zárófrázist ("köszönöm") továbbgörgetni.
    params.set_no_context(true);
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
        let text = seg
            .to_str_lossy()
            .map_err(|e| format!("segment text failed: {e}"))?
            .trim()
            .to_string();
        if text.is_empty() {
            continue;
        }

        // ── Hallucináció-szűrés ────────────────────────────────────────────
        let no_speech = seg.no_speech_probability();
        // (a) szöveg csend-szintű időablakra → biztosan kitalált
        if !segment_has_speech(samples, start_ms, end_ms) {
            continue;
        }
        // (b) a whisper maga is csendnek érzi ÉS ismert hallucináció-frázis
        if no_speech > 0.5 && is_hallucination(&text) {
            continue;
        }
        // (c) nagyon magas no-speech: bármi is a szöveg, nem beszédből jött
        if no_speech > 0.9 {
            continue;
        }

        if !full_text.is_empty() {
            full_text.push(' ');
        }
        full_text.push_str(&text);
        segments.push(TranscriptSegment {
            // A timestampek az EREDETI (vágatlan) felvételre vonatkozzanak.
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

    /// Beszéd-szerű jel (0,3 amplitúdójú szinusz) — bőven a csend-küszöb fölött.
    fn speech(n: usize) -> Vec<f32> {
        (0..n).map(|i| 0.3 * (i as f32 * 0.05).sin()).collect()
    }
    /// Szoba-alapzaj: determinisztikus, nagyon halk jel a küszöb ALATT.
    fn quiet(n: usize) -> Vec<f32> {
        (0..n).map(|i| 0.0008 * (i as f32 * 0.31).sin()).collect()
    }

    #[test]
    fn csendes_felvetel_nem_kerul_whisperbe() {
        // 3 másodperc csak alapzaj → None, tehát a whisper el sem indul.
        assert!(trim_silence(&quiet(48_000)).is_none());
    }

    #[test]
    fn tul_rovid_kattanas_nem_szamit_beszednek() {
        // 100 ms "beszéd" csenddel körülvéve — a MIN_SPEECH (300 ms) alatt van.
        let mut v = quiet(16_000);
        v.extend(speech(1_600));
        v.extend(quiet(16_000));
        assert!(trim_silence(&v).is_none());
    }

    #[test]
    fn valodi_beszed_atmegy_es_az_offset_helyes() {
        // 1 s csend + 1 s beszéd + 1 s csend
        let mut v = quiet(16_000);
        v.extend(speech(16_000));
        v.extend(quiet(16_000));
        let (trimmed, offset_ms) = trim_silence(&v).expect("beszédet fel kell ismernie");
        // A vágás a beszéd elé EDGE_MARGIN-nal (250 ms) kezdődik → ~750 ms.
        assert!(
            (600..=900).contains(&offset_ms),
            "offset {offset_ms} ms kívül esik a várt tartományon"
        );
        // A vágott rész rövidebb az eredetinél, de a beszédet tartalmazza.
        assert!(trimmed.len() < v.len());
        assert!(trimmed.len() >= 16_000);
    }

    #[test]
    fn hallucinacio_felismeres_normalizal() {
        assert!(is_hallucination("Köszönöm!"));
        assert!(is_hallucination("  köszönöm szépen  "));
        assert!(is_hallucination("Namaste."));
        assert!(is_hallucination("Thank you."));
        // Valódi mondat, ami tartalmazza a szót — NEM hallucináció.
        assert!(!is_hallucination("köszönöm hogy elküldted a szerződést"));
        assert!(!is_hallucination("köszönöm a gyors választ"));
        assert!(!is_hallucination("ez egy normális mondat"));
    }

    #[test]
    fn segment_energia_ellenorzes() {
        // 2 s: [0-1s] beszéd, [1-2s] csend
        let mut v = speech(16_000);
        v.extend(quiet(16_000));
        assert!(segment_has_speech(&v, 0, 1000), "az első másodperc beszéd");
        assert!(!segment_has_speech(&v, 1000, 2000), "a második csend");
        // Tartomány a felvételen kívül → nincs beszéd (nem panic).
        assert!(!segment_has_speech(&v, 5000, 6000));
    }

    #[test]
    fn kimondott_koszonom_megmarad() {
        // A KRITIKUS eset: a user tényleg azt mondja, hogy "köszönöm".
        // Az energia-ellenőrzésnek át kell engednie — a (b) szabály csak
        // magas no-speech mellett szűr, amit valódi beszédnél a whisper nem ad.
        let v = speech(16_000);
        assert!(segment_has_speech(&v, 0, 1000));
    }
}

/// Read WAV via hound, convert to 16kHz mono f32 (what whisper.cpp expects).
/// pub(crate): a remote.rs sáv-keverése is ezt használja.
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
