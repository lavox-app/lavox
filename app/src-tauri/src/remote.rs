//! Remote transcription + diarizáció kliens — a self-hosted / Lavox-hosted
//! Lavox szerver (`server/app.py`) hívása, válasz leképezése a
//! `model::CaptureResult` (Segment/Speaker) structokra.
//!
//! Két üzemmódot szolgál ki ugyanazzal a kóddal:
//!  - local/self-hosted (fizetős tier): a user saját szerver-URL-je
//!  - cloud (ingyenes tier): Lavox-hosted URL + workspace-azonosító

use serde::{Deserialize, Serialize};

use crate::model::{CaptureResult, CaptureType, Media, Segment, Speaker, Status};

// ---------------------------------------------------------------------------
// Szerver-konfiguráció (perzisztens, Application Support)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerConfig {
    pub url: String,
    #[serde(default)]
    pub api_key: String,
    /// Multi-tenant szkópolás a cloud tieren; lokálban maradhat "default".
    #[serde(default = "default_workspace")]
    pub workspace: String,
}

fn default_workspace() -> String {
    "default".to_string()
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            url: "http://127.0.0.1:8040".to_string(),
            api_key: String::new(),
            workspace: default_workspace(),
        }
    }
}

fn config_file() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    std::path::PathBuf::from(home).join("Library/Application Support/live.plansmart.hangar/server.json")
}

pub fn load_config() -> ServerConfig {
    std::fs::read_to_string(config_file())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

pub fn save_config(cfg: &ServerConfig) -> Result<(), String> {
    let path = config_file();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let json = serde_json::to_string_pretty(cfg).map_err(|e| e.to_string())?;
    std::fs::write(path, json).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// A szerver válasz-formátuma
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct ApiSpeaker {
    id: String,
    label: String,
    #[serde(default)]
    is_me: bool,
}

#[derive(Debug, Deserialize)]
struct ApiSegment {
    start: f64,
    end: f64,
    text: String,
    /// Csak diarize=true esetén jön.
    speaker: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ApiTranscribeResponse {
    #[allow(dead_code)]
    full_text: String,
    segments: Vec<ApiSegment>,
    language: String,
    duration: f64,
    speakers: Option<Vec<ApiSpeaker>>,
}

// ---------------------------------------------------------------------------
// Mic + rendszerhang sávok keverése egy 16 kHz mono WAV-ba
// ---------------------------------------------------------------------------

/// Adott idő-szakaszok kivágása egy WAV-ból és összefűzése egyetlen 16 kHz
/// mono mintafájlba (a rename-harvest hangtanulásához). A spans (start, end)
/// párok másodpercben; max_total_sec-nél megállunk. Vissza: volt-e elég anyag.
pub fn cut_spans_to_wav(
    input: &str,
    spans: &[(f64, f64)],
    max_total_sec: f64,
    out_path: &str,
) -> Result<bool, String> {
    let samples = crate::transcribe::load_wav_16khz_mono(input)?;
    let sr = 16000usize;
    let mut collected: Vec<f32> = Vec::new();
    for (start, end) in spans {
        if collected.len() as f64 / sr as f64 >= max_total_sec {
            break;
        }
        let i0 = ((*start) * sr as f64) as usize;
        let i1 = (((*end) * sr as f64) as usize).min(samples.len());
        if i0 >= i1 {
            continue;
        }
        collected.extend_from_slice(&samples[i0..i1]);
    }
    // Legalább ~5s beszéd kell egy értelmes hangmintához.
    if (collected.len() as f64 / sr as f64) < 5.0 {
        return Ok(false);
    }
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: 16000,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut writer = hound::WavWriter::create(out_path, spec).map_err(|e| e.to_string())?;
    for s in collected {
        writer
            .write_sample((s * i16::MAX as f32) as i16)
            .map_err(|e| e.to_string())?;
    }
    writer.finalize().map_err(|e| e.to_string())?;
    Ok(true)
}

/// Egyetlen sáv 16 kHz mono WAV-ba írása (feltöltés előtti tömörítés — a nyers
/// 48 kHz-es sávok fölöslegesen nagyok, a szerver úgyis 16 kHz-en dolgozik).
pub fn resample_track(input: &str, out_path: &str) -> Result<(), String> {
    let samples = crate::transcribe::load_wav_16khz_mono(input)?;
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: 16000,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut writer = hound::WavWriter::create(out_path, spec).map_err(|e| e.to_string())?;
    for s in samples {
        writer
            .write_sample((s * i16::MAX as f32) as i16)
            .map_err(|e| e.to_string())?;
    }
    writer.finalize().map_err(|e| e.to_string())
}

/// A meeting két sávja (mic = én, system = a többiek) egyetlen kevert fájlba.
/// Ha csak az egyik létezik, azt írjuk ki 16 kHz monóként.
pub fn mix_tracks(mic: Option<&str>, system: Option<&str>, out_path: &str) -> Result<(), String> {
    let load = |p: &str| crate::transcribe::load_wav_16khz_mono(p);
    let (a, b) = match (mic, system) {
        (Some(m), Some(s)) => (load(m).ok(), load(s).ok()),
        (Some(m), None) => (load(m).ok(), None),
        (None, Some(s)) => (load(s).ok(), None),
        (None, None) => return Err("Nincs hangsáv a felvételben".to_string()),
    };
    let mixed: Vec<f32> = match (a, b) {
        (Some(a), Some(b)) => {
            let n = a.len().max(b.len());
            (0..n)
                .map(|i| {
                    let x = a.get(i).copied().unwrap_or(0.0) + b.get(i).copied().unwrap_or(0.0);
                    x.clamp(-1.0, 1.0)
                })
                .collect()
        }
        (Some(a), None) => a,
        _ => return Err("Egyik hangsáv sem olvasható".to_string()),
    };

    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: 16000,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut writer = hound::WavWriter::create(out_path, spec).map_err(|e| e.to_string())?;
    for s in mixed {
        writer
            .write_sample((s * i16::MAX as f32) as i16)
            .map_err(|e| e.to_string())?;
    }
    writer.finalize().map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// HTTP hívások
// ---------------------------------------------------------------------------

fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(900))
        .build()
        .expect("reqwest client")
}

async fn file_part(path: &str) -> Result<reqwest::multipart::Part, String> {
    let bytes = tokio::fs::read(path).await.map_err(|e| format!("fájl olvasás: {e}"))?;
    let name = std::path::Path::new(path)
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| "audio.wav".to_string());
    Ok(reqwest::multipart::Part::bytes(bytes)
        .file_name(name)
        .mime_str("audio/wav")
        .map_err(|e| e.to_string())?)
}

fn apply_headers(req: reqwest::RequestBuilder, cfg: &ServerConfig) -> reqwest::RequestBuilder {
    let mut req = req.header("X-Workspace-Id", cfg.workspace.as_str());
    if !cfg.api_key.is_empty() {
        req = req.header("Authorization", format!("Bearer {}", cfg.api_key));
    }
    req
}

async fn error_for_status(resp: reqwest::Response) -> Result<reqwest::Response, String> {
    if resp.status().is_success() {
        return Ok(resp);
    }
    let status = resp.status();
    let body = resp.text().await.unwrap_or_default();
    let detail = if body.trim().is_empty() {
        status.canonical_reason().unwrap_or("ismeretlen hiba").to_string()
    } else {
        body
    };
    Err(format!("Szerver hiba ({status}): {detail}"))
}

/// Meeting-audio → diarizált átirat → CaptureResult.
///
/// KÉT-SÁVOS mód (`mic_path` megadva): a rendszerhang (`audio_path` = a
/// többiek) és a mikrofon (= a felvevő) KÜLÖN megy fel — az "én vs. ők"
/// kérdés így determinisztikus, platformtól függetlenül. A régi kevert-sávos
/// hívás (mic_path=None) visszafelé kompatibilisen működik.
pub async fn transcribe_diarized(
    cfg: &ServerConfig,
    audio_path: &str,
    capture_id: &str,
    created_at: &str,
    title: Option<String>,
    lang: &str,
    num_speakers: i32,
    captions_path: Option<&str>,
    mic_path: Option<&str>,
    harvest: bool,
    candidate_names: Option<&[String]>,
) -> Result<CaptureResult, String> {
    let url = format!(
        "{}/api/transcribe?diarize=true&lang={}&num_speakers={}",
        cfg.url.trim_end_matches('/'),
        lang,
        num_speakers
    );
    let mut form = reqwest::multipart::Form::new().part("file", file_part(audio_path).await?);
    if let Some(mp) = mic_path {
        form = form.part("mic_file", file_part(mp).await?);
    }
    // Hangtanulás (harvest) kapcsoló — a Beállításokban kikapcsolható.
    form = form.text("harvest", if harvest { "true" } else { "false" });
    // AUTO-SAVE: a szerver a transzkripció után magától felhőbe menti a
    // felvételt (Postgres+R2) → a webappban azonnal megjelenik, kézi
    // feltöltés nélkül. A metaadatot itt adjuk át.
    form = form.text("auto_save", "true");
    form = form.text("meeting_id", capture_id.to_string());
    form = form.text("created_at", created_at.to_string());
    if let Some(t) = title.clone() {
        form = form.text("title", t);
    }
    // Jelölt-nevek (naptár-résztvevők) a szöveg-alapú név-következtetéshez.
    if let Some(names) = candidate_names {
        if let Ok(json) = serde_json::to_string(names) {
            form = form.text("candidate_names", json);
        }
    }
    // Meet CC feliratok (ha vannak) — a szerver fúziója ezekből rendel valódi
    // beszélő-neveket a whisper-szegmensekhez.
    if let Some(cp) = captions_path {
        if let Ok(json) = std::fs::read_to_string(cp) {
            form = form.text("captions_json", json);
        }
    }
    let req = apply_headers(client().post(&url), cfg).multipart(form);
    let resp = error_for_status(req.send().await.map_err(|e| format!("kapcsolat: {e}"))?).await?;
    let api: ApiTranscribeResponse = resp.json().await.map_err(|e| format!("válasz parse: {e}"))?;

    let speakers: Vec<Speaker> = api
        .speakers
        .unwrap_or_else(|| {
            vec![ApiSpeaker {
                id: "SPEAKER_00".to_string(),
                label: "Speaker 1".to_string(),
                is_me: false,
            }]
        })
        .into_iter()
        .map(|s| Speaker {
            id: s.id,
            label: s.label,
            is_me: s.is_me,
        })
        .collect();

    let segments: Vec<Segment> = api
        .segments
        .into_iter()
        .map(|s| Segment {
            start: s.start,
            end: s.end,
            speaker: s.speaker.unwrap_or_else(|| "SPEAKER_00".to_string()),
            text: s.text,
        })
        .collect();

    Ok(CaptureResult {
        id: capture_id.to_string(),
        kind: CaptureType::Meeting,
        status: Status::Final,
        created_at: created_at.to_string(),
        duration_sec: api.duration,
        language: api.language,
        source_app: None,
        media: Media {
            audio_path: audio_path.to_string(),
            video_url: None,
        },
        speakers,
        segments,
        summary: None,
        action_items: Vec::new(),
        title,
        tags: Vec::new(),
    })
}

/// Beszélő-enrollment: hangminta feltöltése névvel. A szerver JSON-ját adjuk
/// vissza nyersen (id, name, is_me, num_samples) — a frontend megjeleníti.
pub async fn enroll_speaker(
    cfg: &ServerConfig,
    audio_path: &str,
    name: &str,
    is_me: bool,
) -> Result<String, String> {
    let url = format!("{}/api/speakers", cfg.url.trim_end_matches('/'));
    let form = reqwest::multipart::Form::new()
        .part("file", file_part(audio_path).await?)
        .text("name", name.to_string())
        .text("is_me", if is_me { "true" } else { "false" });
    let req = apply_headers(client().post(&url), cfg).multipart(form);
    let resp = error_for_status(req.send().await.map_err(|e| format!("kapcsolat: {e}"))?).await?;
    resp.text().await.map_err(|e| e.to_string())
}

/// A workspace enrollment-profiljai (JSON string a frontendnek).
pub async fn list_speakers(cfg: &ServerConfig) -> Result<String, String> {
    let url = format!("{}/api/speakers", cfg.url.trim_end_matches('/'));
    let resp = error_for_status(
        apply_headers(client().get(&url), cfg)
            .send()
            .await
            .map_err(|e| format!("kapcsolat: {e}"))?,
    )
    .await?;
    resp.text().await.map_err(|e| e.to_string())
}

/// Enrollment-profil törlése.
pub async fn delete_speaker(cfg: &ServerConfig, speaker_id: &str) -> Result<String, String> {
    let url = format!("{}/api/speakers/{}", cfg.url.trim_end_matches('/'), speaker_id);
    let resp = error_for_status(
        apply_headers(client().delete(&url), cfg)
            .send()
            .await
            .map_err(|e| format!("kapcsolat: {e}"))?,
    )
    .await?;
    resp.text().await.map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Felhő-párosítás (device-code flow) — a webapp/backend fele: lásd
// lavox-web/docs/hub-pairing.md. A Hub itt csak KÉT hívást tesz: a kód
// beváltása egyszer, utána periodikus heartbeat, amíg fut.
// ---------------------------------------------------------------------------

/// A Lavox-hosted (ingyenes/cloud tier) backend rögzített címe. Sikeres
/// párosítás után ide állítjuk a ServerConfig.url-t is — onnantól a Hub a
/// meglévő remote_transcribe_meeting/enroll_speaker/stb. hívásokat is emiatt
/// erre a szerverre küldi, a device-tokennel (cfg.api_key) hitelesítve.
const LAVOX_CLOUD_URL: &str = "https://api.lavox.cloud";

#[derive(Debug, Deserialize)]
struct PairClaimResponse {
    token: String,
    workspace: String,
}

/// A webapp onboardingjában megjelenő pároztató kód beváltása eszköz-tokenre.
/// Auth nélkül hívható (a kód maga a titok) — 404, ha érvénytelen/lejárt.
pub async fn pair_claim(code: &str) -> Result<ServerConfig, String> {
    let url = format!("{LAVOX_CLOUD_URL}/api/hub/pair/claim");
    let resp = client()
        .post(&url)
        .json(&serde_json::json!({ "code": code }))
        .send()
        .await
        .map_err(|e| format!("kapcsolat: {e}"))?;
    if resp.status() == reqwest::StatusCode::NOT_FOUND {
        return Err("Érvénytelen vagy lejárt párosító kód.".to_string());
    }
    let claim: PairClaimResponse = error_for_status(resp)
        .await?
        .json()
        .await
        .map_err(|e| format!("válasz parse: {e}"))?;
    Ok(ServerConfig {
        url: LAVOX_CLOUD_URL.to_string(),
        api_key: claim.token,
        workspace: claim.workspace,
    })
}

/// Egyetlen heartbeat-ütés az eszköz-tokennel — a hívó (start_heartbeat_loop)
/// hívja periodikusan, amíg a Hub fut. 401 esetén a token visszavonva/érvénytelen.
async fn heartbeat_once(cfg: &ServerConfig) -> Result<(), String> {
    let url = format!("{LAVOX_CLOUD_URL}/api/hub/heartbeat");
    let resp = client()
        .post(&url)
        .header("Authorization", format!("Bearer {}", cfg.api_key))
        .send()
        .await
        .map_err(|e| format!("kapcsolat: {e}"))?;
    error_for_status(resp).await?;
    Ok(())
}

/// Háttér-loop: 30 másodpercenként újraolvassa a konfigot (hogy a friss
/// párosítást azonnal felvegye újraindítás nélkül), és ha van eszköz-token
/// és a szerver a Lavox-felhő, heartbeatet küld. Csendben kihagyja magát
/// self-hosted (nem-cloud) konfignál — ott nincs mit heartbeatelni.
pub fn start_heartbeat_loop() {
    tauri::async_runtime::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(30));
        loop {
            interval.tick().await;
            let cfg = load_config();
            if cfg.api_key.is_empty() || cfg.url.trim_end_matches('/') != LAVOX_CLOUD_URL {
                continue;
            }
            if let Err(e) = heartbeat_once(&cfg).await {
                crate::dbg(&format!("HUB_HEARTBEAT_FAIL: {e}"));
            }
        }
    });
}
