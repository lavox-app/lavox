//! Remote transcription + diarization client — calls the self-hosted /
//! Lavox-hosted Lavox server (`server/app.py`) and maps the response onto the
//! `model::CaptureResult` (Segment/Speaker) structs.
//!
//! Serves two modes with the same code:
//!  - local/self-hosted (paid tier): the user's own server URL
//!  - cloud (free tier): Lavox-hosted URL + workspace identifier

use serde::{Deserialize, Serialize};

use crate::model::{CaptureResult, CaptureType, Media, Segment, Speaker, Status};

// ---------------------------------------------------------------------------
// Server configuration (persistent, Application Support)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerConfig {
    pub url: String,
    #[serde(default)]
    pub api_key: String,
    /// Multi-tenant scoping on the cloud tier; locally it may stay "default".
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
// The server's response format
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
    /// Only present when diarize=true.
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
// Mixing the mic + system-audio tracks into one 16 kHz mono WAV
// ---------------------------------------------------------------------------

/// Cuts the given time spans out of a WAV and concatenates them into a single
/// 16 kHz mono sample file (for rename-harvest voice learning). Spans are
/// (start, end) pairs in seconds; we stop at max_total_sec. Returns whether
/// there was enough material.
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
    // A meaningful voice sample needs at least ~5s of speech.
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

/// Writes a single track as 16 kHz mono WAV (pre-upload compression — the raw
/// 48 kHz tracks are needlessly large, and the server works at 16 kHz anyway).
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

/// The meeting's two tracks (mic = me, system = the others) into a single mixed
/// file. If only one exists, that one is written out as 16 kHz mono.
pub fn mix_tracks(mic: Option<&str>, system: Option<&str>, out_path: &str) -> Result<(), String> {
    let load = |p: &str| crate::transcribe::load_wav_16khz_mono(p);
    let (a, b) = match (mic, system) {
        (Some(m), Some(s)) => (load(m).ok(), load(s).ok()),
        (Some(m), None) => (load(m).ok(), None),
        (None, Some(s)) => (load(s).ok(), None),
        (None, None) => return Err("No audio track in the recording".to_string()),
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
        _ => return Err("Neither audio track is readable".to_string()),
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
// HTTP calls
// ---------------------------------------------------------------------------

fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(900))
        .build()
        .expect("reqwest client")
}

async fn file_part(path: &str) -> Result<reqwest::multipart::Part, String> {
    let bytes = tokio::fs::read(path).await.map_err(|e| format!("file read: {e}"))?;
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
        status.canonical_reason().unwrap_or("unknown error").to_string()
    } else {
        body
    };
    Err(format!("Server error ({status}): {detail}"))
}

/// Meeting audio → diarized transcript → CaptureResult.
///
/// TWO-TRACK mode (`mic_path` given): the system audio (`audio_path` = the
/// others) and the microphone (= the recording user) are uploaded SEPARATELY —
/// making the "me vs. them" question deterministic, platform-independently.
/// The old mixed-track call (mic_path=None) keeps working backwards-compatibly.
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
    // Voice-learning (harvest) switch — can be disabled in Settings.
    form = form.text("harvest", if harvest { "true" } else { "false" });
    // AUTO-SAVE: after transcription the server saves the recording to the
    // cloud on its own (Postgres+R2) → it appears in the webapp immediately,
    // without a manual upload. The metadata is passed here.
    form = form.text("auto_save", "true");
    form = form.text("meeting_id", capture_id.to_string());
    form = form.text("created_at", created_at.to_string());
    if let Some(t) = title.clone() {
        form = form.text("title", t);
    }
    // Candidate names (calendar attendees) for text-based name inference.
    if let Some(names) = candidate_names {
        if let Ok(json) = serde_json::to_string(names) {
            form = form.text("candidate_names", json);
        }
    }
    // Meet CC captions (if any) — the server's fusion uses them to assign real
    // speaker names to the whisper segments.
    if let Some(cp) = captions_path {
        if let Ok(json) = std::fs::read_to_string(cp) {
            form = form.text("captions_json", json);
        }
    }
    let req = apply_headers(client().post(&url), cfg).multipart(form);
    let resp = error_for_status(req.send().await.map_err(|e| format!("connection: {e}"))?).await?;
    let api: ApiTranscribeResponse = resp.json().await.map_err(|e| format!("response parse: {e}"))?;

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

/// Speaker enrollment: uploads a voice sample with a name. Returns the server's
/// JSON raw (id, name, is_me, num_samples) — the frontend renders it.
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
    let resp = error_for_status(req.send().await.map_err(|e| format!("connection: {e}"))?).await?;
    resp.text().await.map_err(|e| e.to_string())
}

/// The workspace's enrollment profiles (JSON string for the frontend).
pub async fn list_speakers(cfg: &ServerConfig) -> Result<String, String> {
    let url = format!("{}/api/speakers", cfg.url.trim_end_matches('/'));
    let resp = error_for_status(
        apply_headers(client().get(&url), cfg)
            .send()
            .await
            .map_err(|e| format!("connection: {e}"))?,
    )
    .await?;
    resp.text().await.map_err(|e| e.to_string())
}

/// Deletes an enrollment profile.
pub async fn delete_speaker(cfg: &ServerConfig, speaker_id: &str) -> Result<String, String> {
    let url = format!("{}/api/speakers/{}", cfg.url.trim_end_matches('/'), speaker_id);
    let resp = error_for_status(
        apply_headers(client().delete(&url), cfg)
            .send()
            .await
            .map_err(|e| format!("connection: {e}"))?,
    )
    .await?;
    resp.text().await.map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Cloud pairing (device-code flow) — for the webapp/backend side see
// lavox-web/docs/hub-pairing.md. The Hub makes only TWO calls here: redeem
// the code once, then a periodic heartbeat while it runs.
// ---------------------------------------------------------------------------

/// Fixed address of the Lavox-hosted (free/cloud tier) backend. After a
/// successful pairing the ServerConfig.url is pointed here too — from then on
/// the Hub sends the existing remote_transcribe_meeting/enroll_speaker/etc.
/// calls to this server as well, authenticated with the device token
/// (cfg.api_key).
const LAVOX_CLOUD_URL: &str = "https://api.lavox.cloud";

#[derive(Debug, Deserialize)]
struct PairClaimResponse {
    token: String,
    workspace: String,
}

/// Redeems the pairing code shown in the webapp onboarding for a device token.
/// Callable without auth (the code itself is the secret) — 404 if invalid/expired.
pub async fn pair_claim(code: &str) -> Result<ServerConfig, String> {
    let url = format!("{LAVOX_CLOUD_URL}/api/hub/pair/claim");
    let resp = client()
        .post(&url)
        .json(&serde_json::json!({ "code": code }))
        .send()
        .await
        .map_err(|e| format!("connection: {e}"))?;
    if resp.status() == reqwest::StatusCode::NOT_FOUND {
        return Err("Invalid or expired pairing code.".to_string());
    }
    let claim: PairClaimResponse = error_for_status(resp)
        .await?
        .json()
        .await
        .map_err(|e| format!("response parse: {e}"))?;
    Ok(ServerConfig {
        url: LAVOX_CLOUD_URL.to_string(),
        api_key: claim.token,
        workspace: claim.workspace,
    })
}

/// A single heartbeat with the device token — the caller (start_heartbeat_loop)
/// invokes it periodically while the Hub runs. 401 means the token was revoked/invalid.
async fn heartbeat_once(cfg: &ServerConfig) -> Result<(), String> {
    let url = format!("{LAVOX_CLOUD_URL}/api/hub/heartbeat");
    let resp = client()
        .post(&url)
        .header("Authorization", format!("Bearer {}", cfg.api_key))
        .send()
        .await
        .map_err(|e| format!("connection: {e}"))?;
    error_for_status(resp).await?;
    Ok(())
}

/// Background loop: re-reads the config every 30 seconds (so a fresh pairing
/// is picked up immediately without a restart), and if there is a device token
/// and the server is the Lavox cloud, sends a heartbeat. Silently skips itself
/// on a self-hosted (non-cloud) config — nothing to heartbeat there.
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
