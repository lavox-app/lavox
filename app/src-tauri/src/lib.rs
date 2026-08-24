// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/

mod bridge;
mod calendar;
mod export;
mod gauth;
mod dictionary;
mod hotkey;
mod inject;
mod notch;
mod model;
mod recorder;
mod remote;
mod transcribe;

use model::CaptureResult;
use transcribe::TranscriptResult;

use std::sync::Mutex as StdMutex;

static ACTIVE_RECORDING: StdMutex<Option<recorder::StreamingRecorder>> = StdMutex::new(None);
// Running instance of the system-audio recorder helper (syscap) + target WAV path.
// A meeting recording captures two tracks: microphone (you) + system audio (the others).
static ACTIVE_SYSCAP: StdMutex<Option<(std::process::Child, String)>> = StdMutex::new(None);
// Id of the ACTIVE meeting recording (= the name of the persistent directory).
static ACTIVE_MEETING_ID: StdMutex<Option<String>> = StdMutex::new(None);
// Kind of recording: "meeting" (Meet Bridge / calendar) or "video" (manual, from the bar).
static ACTIVE_RECORD_KIND: StdMutex<Option<String>> = StdMutex::new(None);
// Unix ms timestamp of when recording started — needed at stop to align the
// Meet CC captions (bridge buffer) to relative time.
static ACTIVE_RECORD_STARTED_MS: StdMutex<Option<i64>> = StdMutex::new(None);
// Microphone chosen by the bar's mic picker (None = default).
static SELECTED_MIC: StdMutex<Option<String>> = StdMutex::new(None);
// Display index chosen by the bar's screen picker (0 = first/main).
static SELECTED_DISPLAY: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

fn selected_mic() -> Option<String> {
    SELECTED_MIC.lock().ok().and_then(|g| g.clone())
}
// SEPARATE slot for dictation — does NOT share the meeting/calendar recording
// mutex, so the two cannot collide (that caused the "push-to-talk almost works" bug).
static ACTIVE_DICTATION: StdMutex<Option<recorder::StreamingRecorder>> = StdMutex::new(None);
// Bundle ID of the app active when dictation STARTED — we re-activate it on insertion.
static DICTATION_TARGET_APP: StdMutex<Option<String>> = StdMutex::new(None);
// The frontend signals when the pill is idle (thin line) — only then does it follow the cursor.
static FOLLOW_ENABLED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(true);
// Whether the stop poller is running (reliable stop: we poll for Space being released).
static STOP_POLLER_ACTIVE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
// Whether the mic-level emitter is running (real-time waveform — only while recording).
static MIC_EMIT_ACTIVE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
// The notch info (detected at startup on the main thread, since NSScreen is main-thread-only).
static NOTCH_INFO: StdMutex<Option<notch::NotchInfo>> = StdMutex::new(None);
// The app handle for the display-reconfiguration callback (event-driven notch refresh).
static NOTCH_APP_HANDLE: StdMutex<Option<tauri::AppHandle>> = StdMutex::new(None);

/// macOS display reconfiguration (monitor connects/disconnects, resolution/scaling
/// or layout change) — event-driven, NOT polling. Only video connections
/// (HDMI/Thunderbolt/built-in) trigger it, Bluetooth does NOT.
#[cfg(target_os = "macos")]
mod display_watch {
    use std::os::raw::c_void;
    pub type CGDirectDisplayID = u32;
    pub type CGDisplayChangeSummaryFlags = u32;
    pub type CGDisplayReconfigurationCallBack =
        extern "C" fn(CGDirectDisplayID, CGDisplayChangeSummaryFlags, *mut c_void);
    pub const K_CG_DISPLAY_BEGIN_CONFIGURATION_FLAG: u32 = 1 << 0;
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        pub fn CGDisplayRegisterReconfigurationCallback(
            callback: CGDisplayReconfigurationCallBack,
            user_info: *mut c_void,
        ) -> i32;
    }
}

/// The display-reconfiguration callback (runs on the main run loop). Re-detects
/// the notch + notifies the overlay → the bar self-corrects on monitor/resolution changes.
#[cfg(target_os = "macos")]
extern "C" fn on_display_reconfig(
    _display: display_watch::CGDirectDisplayID,
    flags: display_watch::CGDisplayChangeSummaryFlags,
    _user: *mut std::os::raw::c_void,
) {
    // Skip the START of the reconfiguration — detect at the end (with the actual flags).
    if flags == display_watch::K_CG_DISPLAY_BEGIN_CONFIGURATION_FLAG {
        return;
    }
    let ni = notch::detect();
    if let Ok(mut g) = NOTCH_INFO.lock() {
        *g = Some(ni.clone());
    }
    dbg(&format!(
        "DISPLAY_RECONFIG notch has={} left={:.0} right={:.0} screenW={:.0}",
        ni.has_notch, ni.notch_left, ni.notch_right, ni.screen_width
    ));
    use tauri::{Emitter, Manager};
    if let Some(app) = NOTCH_APP_HANDLE.lock().ok().and_then(|g| g.clone()) {
        if let Some(overlay) = app.get_webview_window("overlay") {
            let _ = overlay.emit("notch-refreshed", ni);
        }
    }
}

/// The detected notch info (the frontend aligns the compact layout to it).
#[tauri::command]
fn get_notch_info() -> notch::NotchInfo {
    NOTCH_INFO
        .lock()
        .ok()
        .and_then(|g| g.clone())
        .unwrap_or_default()
}

/// Re-detects the notch (on the MAIN thread — NSScreen is main-thread-only) +
/// updates NOTCH_INFO, and notifies the overlay with a "notch-refreshed" event.
/// The frontend calls it periodically → the position stays correct even after
/// a monitor is plugged/unplugged.
#[tauri::command]
fn refresh_notch(app: tauri::AppHandle) {
    let app2 = app.clone();
    let _ = app.run_on_main_thread(move || {
        let ni = notch::detect();
        if let Ok(mut g) = NOTCH_INFO.lock() {
            *g = Some(ni.clone());
        }
        use tauri::{Emitter, Manager};
        if let Some(overlay) = app2.get_webview_window("overlay") {
            let _ = overlay.emit("notch-refreshed", ni);
        }
    });
}

// Whether a key is currently down (HID hardware state). The global-shortcut
// Released event is unreliable for chords, so for push-to-talk stop we poll THIS.
#[cfg(target_os = "macos")]
fn key_is_down(keycode: u16) -> bool {
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGEventSourceKeyState(state_id: i32, key: u16) -> bool;
    }
    // 1 = kCGEventSourceStateHIDSystemState (real hardware state)
    unsafe { CGEventSourceKeyState(1, keycode) }
}
#[cfg(not(target_os = "macos"))]
fn key_is_down(_keycode: u16) -> bool {
    false
}

/// Set by the frontend: idle (thin line) → true, anything else → false.
/// Only when idle does the pill follow the cursor across screens.
#[tauri::command]
fn set_follow_enabled(enabled: bool) {
    FOLLOW_ENABLED.store(enabled, std::sync::atomic::Ordering::Relaxed);
}

/// Bundle ID of the currently active (frontmost) app. We filter OUR OWN app
/// out so we never paste into ourselves (if the pill happened to be frontmost).
fn capture_frontmost_app() -> Option<String> {
    let out = std::process::Command::new("osascript")
        .args([
            "-e",
            "tell application \"System Events\" to bundle identifier of first application process whose frontmost is true",
        ])
        .output()
        .ok()?;
    let bundle = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if bundle.is_empty() || bundle.contains("plansmart.hangar") {
        None
    } else {
        Some(bundle)
    }
}

/// Starts a dictation recording (its own slot). If a previous recording got
/// stuck for any reason, we DROP it and start fresh → it can never get stuck
/// on an "already running" error.
#[tauri::command]
fn start_dictation_record(app: tauri::AppHandle) -> Result<String, String> {
    use std::sync::atomic::Ordering;
    let level_handle;
    {
        let mut guard = ACTIVE_DICTATION.lock().map_err(|e| e.to_string())?;
        if guard.is_some() {
            let _ = guard.take(); // drop the stuck recording
        }
        let rec = recorder::StreamingRecorder::start(selected_mic())?;
        level_handle = rec.level_handle();
        *guard = Some(rec);
    }
    // REAL-TIME WAVEFORM: every ~50ms we send the frontend the current mic
    // level (RMS), so the bars rise ONLY when you are actually speaking.
    // The emitter runs until the recording ends (MIC_EMIT_ACTIVE).
    MIC_EMIT_ACTIVE.store(true, Ordering::SeqCst);
    let app_emit = app.clone();
    std::thread::spawn(move || {
        use tauri::{Emitter, Manager};
        while MIC_EMIT_ACTIVE.load(Ordering::SeqCst) {
            let level = f32::from_bits(level_handle.load(Ordering::Relaxed));
            if let Some(overlay) = app_emit.get_webview_window("overlay") {
                let _ = overlay.emit("mic-level", level);
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
    });
    // The recording is already running — now save which app was active (we
    // re-activate it on insertion). Only update to a valid (non-own) app, so a
    // transient pill focus cannot overwrite the real target.
    if let Some(front) = capture_frontmost_app() {
        if let Ok(mut t) = DICTATION_TARGET_APP.lock() {
            *t = Some(front);
        }
    }
    Ok("recording".to_string())
}

/// Stops dictation + saves the WAV; returns the path.
#[tauri::command]
fn stop_dictation_record() -> Result<String, String> {
    MIC_EMIT_ACTIVE.store(false, std::sync::atomic::Ordering::SeqCst);
    let mut guard = ACTIVE_DICTATION.lock().map_err(|e| e.to_string())?;
    let rec = guard.take().ok_or("No active dictation")?;
    let path = std::env::temp_dir().join("lavox-dictation.wav");
    rec.stop_and_save(&path.to_string_lossy())?;
    Ok(path.to_string_lossy().to_string())
}

/// Submits the finished dictation to Lavox Memory (local server, fire-and-forget).
///
/// Meetings flow into memory automatically via the server's /api/transcribe;
/// dictation, however, is produced HERE, on the Hub's whisper.cpp — so the
/// finished text is submitted separately. Best-effort: if the server is not
/// running or has no memory module, we let it go silently (it must NEVER
/// affect dictation's main path — transcription + insertion).
#[tauri::command]
async fn dictation_learn(raw: String, corrected: String) -> Result<(), String> {
    // The user edited a dictation on the overlay — the diff teaches the
    // personal dictionary. Fire-and-forget: a dropped correction only means
    // one missed learning opportunity, never a blocked UI.
    if raw.trim().is_empty() || corrected.trim().is_empty() || raw == corrected {
        return Ok(());
    }
    let cfg = remote::load_config();
    let url = format!("{}/api/dictionary/learn", cfg.url.trim_end_matches('/'));
    tauri::async_runtime::spawn(async move {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build();
        if let Ok(client) = client {
            let _ = client
                .post(&url)
                .json(&serde_json::json!({ "raw": raw, "corrected": corrected }))
                .send()
                .await;
        }
    });
    Ok(())
}

#[tauri::command]
async fn memory_ingest_dictation(text: String) -> Result<(), String> {
    if text.trim().len() < 25 {
        return Ok(());
    }
    let cfg = remote::load_config();
    let url = format!("{}/api/memory/ingest", cfg.url.trim_end_matches('/'));
    tauri::async_runtime::spawn(async move {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build();
        if let Ok(client) = client {
            let _ = client
                .post(&url)
                .json(&serde_json::json!({ "text": text, "kind": "dictation" }))
                .send()
                .await;
        }
    });
    Ok(())
}

#[tauri::command]
async fn set_calendar_token(
    state: tauri::State<'_, calendar::SharedCalendarState>,
    access_token: String,
    expires_at: i64,
) -> Result<(), String> {
    let mut s = state.lock().await;
    s.token = Some(calendar::CalendarToken {
        access_token,
        expires_at,
    });
    Ok(())
}

#[tauri::command]
async fn clear_calendar_token(
    state: tauri::State<'_, calendar::SharedCalendarState>,
) -> Result<(), String> {
    let mut s = state.lock().await;
    s.token = None;
    Ok(())
}

// ---- GOOGLE CALENDAR: native (desktop) sign-in ----
// The old browser-based GIS solution was blocked by the Tauri CSP in the
// packaged app, and without a refresh token it silently died after 1 hour.
// See gauth.rs.

/// Sign-in: system browser → loopback redirect → access+refresh tokens.
#[tauri::command]
async fn calendar_login(
    app: tauri::AppHandle,
    state: tauri::State<'_, calendar::SharedCalendarState>,
) -> Result<serde_json::Value, String> {
    let tokens = gauth::login(&app).await?;
    {
        let mut s = state.lock().await;
        s.token = Some(calendar::CalendarToken {
            access_token: tokens.access_token.clone(),
            expires_at: tokens.expires_at,
        });
    }
    Ok(serde_json::json!({
        "email": tokens.email,
        "expiresAt": tokens.expires_at,
    }))
}

/// Calendar-connection status for Settings. `configured` says whether the
/// build contains a Google desktop-client ID — without one there is no point
/// showing the sign-in button.
#[tauri::command]
fn calendar_status() -> serde_json::Value {
    let stored = gauth::load_tokens();
    serde_json::json!({
        "configured": gauth::configured(),
        "connected": stored.is_some(),
        "email": stored.as_ref().and_then(|t| t.email.clone()),
    })
}

#[tauri::command]
async fn calendar_logout(
    state: tauri::State<'_, calendar::SharedCalendarState>,
) -> Result<(), String> {
    gauth::clear_tokens();
    let mut s = state.lock().await;
    s.token = None;
    Ok(())
}

// ---- AUTO-RECORD SETTING (persistent; survives reinstalls) ----
fn auto_record_file() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    std::path::PathBuf::from(home)
        .join("Library/Application Support/live.plansmart.hangar/auto_record.json")
}

/// The meeting extension's connection status for the Settings panel: whether
/// the bridge runs (always yes while the app is alive), and when the last
/// event arrived from the extension.
#[tauri::command]
fn get_bridge_status() -> serde_json::Value {
    let (last_ms, kind) = bridge::last_event();
    serde_json::json!({
        "port": 5192,
        "lastEventMs": last_ms,
        "lastEventKind": kind,
    })
}

/// The persisted auto-record setting (the bar's meet-joined decision reads it too).
#[tauri::command]
fn get_auto_record() -> bool {
    std::fs::read_to_string(auto_record_file())
        .ok()
        .and_then(|s| s.trim().parse::<bool>().ok())
        .unwrap_or(false)
}

#[tauri::command]
async fn set_auto_record(
    app: tauri::AppHandle,
    state: tauri::State<'_, calendar::SharedCalendarState>,
    enabled: bool,
) -> Result<(), String> {
    // Persist to a file (survives reinstalls, not just localStorage).
    let path = auto_record_file();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(&path, enabled.to_string());
    // Update the in-memory calendar state (used by the calendar poller).
    {
        let mut s = state.lock().await;
        s.auto_record = enabled;
    }
    // Notify the bar overlay to refresh its ref for the meet-joined decision.
    use tauri::{Emitter, Manager};
    if let Some(overlay) = app.get_webview_window("overlay") {
        let _ = overlay.emit("auto-record-changed", enabled);
    }
    Ok(())
}

#[tauri::command]
async fn get_calendar_status(
    state: tauri::State<'_, calendar::SharedCalendarState>,
) -> Result<serde_json::Value, String> {
    let s = state.lock().await;
    Ok(serde_json::json!({
        "connected": s.token.is_some(),
        "auto_record": s.auto_record,
    }))
}

/// Resolves the syscap system-audio helper: bundle Resources (normal + the
/// `_up_` gotcha — see the Lavox Hub Codesign cert note) and dev-mode paths.
fn find_syscap() -> Option<std::path::PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let macos_dir = exe.parent()?;
    let candidates = [
        macos_dir.join("../Resources/helpers/syscap"),
        macos_dir.join("../Resources/_up_/helpers/syscap"),
        // dev mode: src-tauri/helpers relative to the cwd
        std::path::PathBuf::from("helpers/syscap"),
        std::path::PathBuf::from("src-tauri/helpers/syscap"),
    ];
    for c in candidates {
        if c.exists() {
            return Some(c);
        }
    }
    None
}

#[tauri::command]
fn start_meeting_record() -> Result<String, String> {
    start_capture_with_kind("meeting")
}

/// Manual video recording from the bar — the same machinery (mic + screen +
/// system audio), just entered into the registry with kind "video".
#[tauri::command]
fn start_video_record() -> Result<String, String> {
    start_capture_with_kind("video")
}

fn start_capture_with_kind(kind: &str) -> Result<String, String> {
    let mut guard = ACTIVE_RECORDING.lock().map_err(|e| e.to_string())?;
    if guard.is_some() {
        return Err("A recording is already running".to_string());
    }
    let rec = recorder::StreamingRecorder::start(selected_mic())?;
    *guard = Some(rec);
    if let Ok(mut kg) = ACTIVE_RECORD_KIND.lock() {
        *kg = Some(kind.to_string());
    }

    // The recording's directory is created at the persistent location ALREADY
    // AT START — system audio is written straight there, stop puts the mic
    // track in the same place.
    let rec_id = chrono::Local::now().format("%Y%m%d_%H%M%S").to_string();
    let rec_dir = meetings_dir().join(&rec_id);
    let _ = std::fs::create_dir_all(&rec_dir);
    if let Ok(mut mg) = ACTIVE_MEETING_ID.lock() {
        *mg = Some(rec_id);
    }
    if let Ok(mut sg) = ACTIVE_RECORD_STARTED_MS.lock() {
        *sg = Some(chrono::Local::now().timestamp_millis());
    }

    // System-audio track (the OTHER meeting participants): syscap helper.
    // If the helper is missing or there is no permission, we carry on in
    // mic-only mode — stop reports back what got saved.
    let sys_path = rec_dir.join("system.wav");
    let video_path = rec_dir.join("screen.mov");
    let display_idx = SELECTED_DISPLAY.load(std::sync::atomic::Ordering::Relaxed);
    if let Some(helper) = find_syscap() {
        match std::process::Command::new(&helper)
            .arg(&sys_path)
            .arg("--video")
            .arg(&video_path)
            .arg("--display")
            .arg(display_idx.to_string())
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
        {
            Ok(child) => {
                if let Ok(mut sg) = ACTIVE_SYSCAP.lock() {
                    *sg = Some((child, sys_path.to_string_lossy().to_string()));
                }
                dbg("SYSCAP_STARTED");
            }
            Err(e) => dbg(&format!("SYSCAP_SPAWN_FAIL {e}")),
        }
    } else {
        dbg("SYSCAP_NOT_FOUND");
    }

    Ok("recording".to_string())
}

/// Persistent directory for meeting recordings: ~/Documents/Lavox/meetings.
/// (Instead of temp — the temp folder may be wiped by a restart.)
/// MIGRATION: on first run the old ~/Documents/Hangar folder is renamed to
/// Lavox, and the absolute paths stored in the JSONs are rewritten too — so
/// not a single old recording/note export is lost.
pub(crate) fn meetings_dir() -> std::path::PathBuf {
    let docs = dirs_home().join("Documents");
    let new_root = docs.join("Lavox");
    let old_root = docs.join("Hangar");
    static MIGRATE: std::sync::Once = std::sync::Once::new();
    MIGRATE.call_once(|| {
        if old_root.exists() && !new_root.exists() && std::fs::rename(&old_root, &new_root).is_ok() {
            // Rewrite the stored absolute paths in every .json file (index + captures).
            fn rewrite_json_paths(dir: &std::path::Path) {
                let Ok(rd) = std::fs::read_dir(dir) else { return };
                for e in rd.flatten() {
                    let p = e.path();
                    if p.is_dir() {
                        rewrite_json_paths(&p);
                    } else if p.extension().and_then(|x| x.to_str()) == Some("json") {
                        if let Ok(s) = std::fs::read_to_string(&p) {
                            if s.contains("/Documents/Hangar/") {
                                let _ = std::fs::write(&p, s.replace("/Documents/Hangar/", "/Documents/Lavox/"));
                            }
                        }
                    }
                }
            }
            rewrite_json_paths(&new_root);
            dbg("MIGRATED ~/Documents/Hangar -> ~/Documents/Lavox");
        }
    });
    let base = new_root.join("meetings");
    let _ = std::fs::create_dir_all(&base);
    base
}

fn dirs_home() -> std::path::PathBuf {
    std::env::var_os("HOME")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
}

/// Metadata of a finished meeting recording — the index.json entry.
#[derive(serde::Serialize, serde::Deserialize, Clone)]
pub(crate) struct MeetingRecordEntry {
    pub id: String,
    pub title: String,
    pub created_at: String,
    pub duration_sec: f64,
    pub mic: Option<String>,
    pub system: Option<String>,
    #[serde(default)]
    pub video: Option<String>,
    /// "meeting" or "video" — the dashboard categorizes based on this.
    #[serde(default = "default_kind")]
    pub kind: String,
    #[serde(default)]
    pub imported: bool,
}

fn default_kind() -> String {
    "meeting".to_string()
}

pub(crate) fn read_meetings_index() -> Vec<MeetingRecordEntry> {
    let p = meetings_dir().join("index.json");
    std::fs::read_to_string(p)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

pub(crate) fn write_meetings_index(entries: &[MeetingRecordEntry]) {
    let p = meetings_dir().join("index.json");
    if let Ok(json) = serde_json::to_string_pretty(entries) {
        let _ = std::fs::write(p, json);
    }
}

fn wav_duration_sec(path: &std::path::Path) -> f64 {
    hound::WavReader::open(path)
        .map(|r| r.duration() as f64 / r.spec().sample_rate.max(1) as f64)
        .unwrap_or(0.0)
}

#[tauri::command]
fn stop_meeting_record(title: String) -> Result<String, String> {
    let mut guard = ACTIVE_RECORDING.lock().map_err(|e| e.to_string())?;
    let rec = guard.take().ok_or("No active recording")?;
    let safe_title = title
        .chars()
        .filter(|c| c.is_alphanumeric() || *c == '-' || *c == '_' || *c == ' ')
        .collect::<String>();
    // The persistent directory created at start (fallback: fresh ts on desync).
    let rec_id = ACTIVE_MEETING_ID
        .lock()
        .ok()
        .and_then(|mut g| g.take())
        .unwrap_or_else(|| chrono::Local::now().format("%Y%m%d_%H%M%S").to_string());
    let rec_dir = meetings_dir().join(&rec_id);
    std::fs::create_dir_all(&rec_dir).map_err(|e| e.to_string())?;
    let mic_path = rec_dir.join("mic.wav");
    rec.stop_and_save(&mic_path.to_string_lossy())?;

    // Stopping the system-audio helper: close stdin (EOF) → graceful finalize,
    // kill after a grace period.
    let mut system_path: Option<String> = None;
    if let Ok(mut sg) = ACTIVE_SYSCAP.lock() {
        if let Some((mut child, sys_path)) = sg.take() {
            drop(child.stdin.take()); // EOF → the helper finalizes the WAV
            // In video mode finalizing the .mov takes +1.2s — allow plenty.
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(6);
            loop {
                match child.try_wait() {
                    Ok(Some(_)) => break,
                    Ok(None) if std::time::Instant::now() < deadline => {
                        std::thread::sleep(std::time::Duration::from_millis(100));
                    }
                    _ => {
                        let _ = child.kill();
                        let _ = child.wait();
                        break;
                    }
                }
            }
            // Only counts if actual audio got into it (WAV header > 44 bytes).
            let ok = std::fs::metadata(&sys_path).map(|m| m.len() > 1000).unwrap_or(false);
            if ok {
                system_path = Some(sys_path);
            } else {
                let _ = std::fs::remove_file(&sys_path);
                dbg("SYSCAP_NO_AUDIO (missing permission?)");
            }
        }
    }

    // Video track: only counts if a .mov of meaningful size was produced.
    let video_file = rec_dir.join("screen.mov");
    let video_path = if std::fs::metadata(&video_file).map(|m| m.len() > 50_000).unwrap_or(false) {
        Some(video_file.to_string_lossy().to_string())
    } else {
        let _ = std::fs::remove_file(&video_file);
        None
    };

    let kind = ACTIVE_RECORD_KIND
        .lock()
        .ok()
        .and_then(|mut g| g.take())
        .unwrap_or_else(|| "meeting".to_string());

    // Registry entry — the dashboard imports from here via the bridge.
    let entry = MeetingRecordEntry {
        id: rec_id.clone(),
        title: if safe_title.trim().is_empty() {
            format!("{} {rec_id}", if kind == "video" { "Video" } else { "Meeting" })
        } else { safe_title },
        created_at: chrono::Local::now().to_rfc3339(),
        duration_sec: wav_duration_sec(&mic_path),
        mic: Some(mic_path.to_string_lossy().to_string()),
        system: system_path.clone(),
        video: video_path.clone(),
        kind,
        imported: false,
    };
    let mut index = read_meetings_index();
    index.retain(|e| e.id != entry.id);
    index.push(entry);
    write_meetings_index(&index);

    // Meet CC captions (bridge buffer) → captions.json into the recording's
    // folder. Aligned to seconds relative to recording start; the transcription
    // fusion (server) uses it to assign real names to the whisper segments.
    let started_ms = ACTIVE_RECORD_STARTED_MS
        .lock()
        .ok()
        .and_then(|mut g| g.take());
    if let Some(start_ms) = started_ms {
        let now = chrono::Local::now().timestamp_millis();
        let events: Vec<serde_json::Value> = bridge::drain_captions()
            .into_iter()
            .filter(|e| {
                let in_window = e.t >= start_ms - 5_000 && e.t <= now + 5_000;
                let useful = !e.text.trim().is_empty() || e.kind == "active-speaker";
                in_window && useful
            })
            .map(|e| {
                serde_json::json!({
                    "t": ((e.t - start_ms) as f64 / 1000.0 * 100.0).round() / 100.0,
                    "type": e.kind,
                    "name": e.name,
                    "text": e.text,
                })
            })
            .collect();
        if !events.is_empty() {
            let payload = serde_json::json!({ "rec_id": rec_id, "started_ms": start_ms, "events": events });
            if let Ok(json) = serde_json::to_string_pretty(&payload) {
                // Losing captions.json would silently degrade fusion (unnamed
                // segments), so at least log the write error — the meeting
                // itself is already saved, so we don't fail here, just signal.
                if let Err(e) = std::fs::write(rec_dir.join("captions.json"), json) {
                    dbg(&format!("CAPTIONS_WRITE_FAIL: {e}"));
                }
            }
        }
    }

    // We return JSON — the frontend learns from it what got saved.
    Ok(serde_json::json!({
        "mic": mic_path.to_string_lossy(),
        "system": system_path,
        "video": video_path,
    })
    .to_string())
}

// ---- M5: REMOTE TRANSCRIPTION + DIARIZATION (speaker identification) ----

#[tauri::command]
fn get_server_config() -> remote::ServerConfig {
    remote::load_config()
}

#[tauri::command]
fn set_server_config(config: remote::ServerConfig) -> Result<(), String> {
    remote::save_config(&config)
}

/// Redeems the pairing code shown in the webapp onboarding. After a successful
/// redemption the server config is automatically set to the Lavox cloud
/// (url+api_key+workspace saved) — the frontend does not need a separate
/// set_server_config call, only to update its own state with the returned config.
#[tauri::command]
async fn hub_pair_claim(code: String) -> Result<remote::ServerConfig, String> {
    let cfg = remote::pair_claim(&code).await?;
    remote::save_config(&cfg)?;
    Ok(cfg)
}

/// List of finished recordings (contents of meetings/index.json, newest first).
#[tauri::command]
fn list_meetings() -> Vec<MeetingRecordEntry> {
    let mut v = read_meetings_index();
    v.sort_by(|a, b| b.created_at.cmp(&a.created_at));
    v
}

/// A recording's finished transcript (capture.json), if already transcribed.
#[tauri::command]
fn load_capture(rec_id: String) -> Result<Option<CaptureResult>, String> {
    let p = meetings_dir().join(&rec_id).join("capture.json");
    if !p.exists() {
        return Ok(None);
    }
    let s = std::fs::read_to_string(&p).map_err(|e| e.to_string())?;
    serde_json::from_str(&s).map(Some).map_err(|e| e.to_string())
}

/// Writes the capture (e.g. enriched with an AI summary) back into the recording's folder.
#[tauri::command]
fn save_capture(rec_id: String, capture: CaptureResult) -> Result<(), String> {
    let dir = meetings_dir().join(&rec_id);
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let json = serde_json::to_string_pretty(&capture).map_err(|e| e.to_string())?;
    std::fs::write(dir.join("capture.json"), json).map_err(|e| e.to_string())
}

/// Diarized transcription of a finished meeting recording: the mic + system
/// tracks are mixed into one 16 kHz mono file, the server transcribes it and
/// assigns speakers, the result comes back as a CaptureResult (+ capture.json
/// in the recording's folder).
#[tauri::command]
async fn remote_transcribe_meeting(
    rec_id: String,
    lang: Option<String>,
    num_speakers: Option<i32>,
    harvest: Option<bool>,
    cal_state: tauri::State<'_, calendar::SharedCalendarState>,
) -> Result<CaptureResult, String> {
    let entry = read_meetings_index()
        .into_iter()
        .find(|e| e.id == rec_id)
        .ok_or_else(|| format!("No such recording: {rec_id}"))?;

    let rec_dir = meetings_dir().join(&rec_id);
    let mixed_path = rec_dir.join("mixed_16k.wav").to_string_lossy().to_string();
    let mic = entry.mic.clone();
    let system = entry.system.clone();

    // TWO-TRACK mode when both tracks exist: the tracks are uploaded SEPARATELY
    // (compressed to 16 kHz) — the "me vs. them" separation stays deterministic.
    // The mixed mixed_16k.wav is still produced, but only for playback.
    let two_track = mic.is_some() && system.is_some();
    let mic16_path = rec_dir.join("mic_16k.wav").to_string_lossy().to_string();
    let sys16_path = rec_dir.join("system_16k.wav").to_string_lossy().to_string();
    {
        let (mic_c, sys_c) = (mic.clone(), system.clone());
        let (mix_out, mic16, sys16) = (mixed_path.clone(), mic16_path.clone(), sys16_path.clone());
        tauri::async_runtime::spawn_blocking(move || -> Result<(), String> {
            remote::mix_tracks(mic_c.as_deref(), sys_c.as_deref(), &mix_out)?;
            if let (Some(m), Some(s)) = (mic_c.as_deref(), sys_c.as_deref()) {
                remote::resample_track(m, &mic16)?;
                remote::resample_track(s, &sys16)?;
            }
            Ok(())
        })
        .await
        .map_err(|e| e.to_string())??;
    }

    let cfg = remote::load_config();
    let lang = lang.unwrap_or_else(|| load_langs().first().cloned().unwrap_or_else(|| "en".into()));
    // Meet CC captions (if the bridge captured them) — the server uses them for fusion.
    let captions_file = rec_dir.join("captions.json");
    let captions_path = captions_file
        .exists()
        .then(|| captions_file.to_string_lossy().to_string());
    // In two-track mode "file" = the others' track, "mic_file" = the recording user's.
    let (audio_arg, mic_arg) = if two_track {
        (sys16_path.clone(), Some(mic16_path.clone()))
    } else {
        (mixed_path.clone(), None)
    };

    // Calendar attendees as the candidate pool for name inference. Invited ≠
    // present — the server assigns names from this only with confirming evidence.
    let candidate_names: Option<Vec<String>> = {
        let token = cal_state.lock().await.token.as_ref().map(|t| t.access_token.clone());
        if let Some(tok) = token {
            let end = chrono::DateTime::parse_from_rfc3339(&entry.created_at)
                .map(|d| d.to_rfc3339())
                .unwrap_or_else(|_| chrono::Local::now().to_rfc3339());
            let start = chrono::DateTime::parse_from_rfc3339(&entry.created_at)
                .map(|d| (d - chrono::Duration::seconds(entry.duration_sec as i64 + 600)).to_rfc3339())
                .unwrap_or_else(|_| end.clone());
            calendar::attendees_for_window(&tok, &start, &end).await.ok().filter(|v| !v.is_empty())
        } else {
            None
        }
    };
    let result = remote::transcribe_diarized(
        &cfg,
        &audio_arg,
        &rec_id,
        &entry.created_at,
        Some(entry.title.clone()).filter(|t| !t.is_empty()),
        &lang,
        num_speakers.unwrap_or(-1),
        captions_path.as_deref(),
        mic_arg.as_deref(),
        harvest.unwrap_or(true),
        candidate_names.as_deref(),
    )
    .await?;

    // Delete the upload copies (mixed_16k.wav stays for playback) and point
    // the playback reference at the MIXED file — otherwise media.audio_path
    // would point at the just-deleted system_16k.wav.
    let mut result = result;
    if two_track {
        let _ = std::fs::remove_file(&mic16_path);
        let _ = std::fs::remove_file(&sys16_path);
        result.media.audio_path = mixed_path.clone();
    }

    // capture.json is the ONLY persistent copy of the finished transcript — if
    // this write fails silently, the UI reports "transcript ready" but
    // load_capture later returns empty (vanished transcript). So we propagate
    // the write error AS AN ERROR, letting the frontend show a "TRANSCRIPT
    // ERROR" notification.
    let json = serde_json::to_string_pretty(&result)
        .map_err(|e| format!("capture.json serialization: {e}"))?;
    std::fs::write(rec_dir.join("capture.json"), json)
        .map_err(|e| format!("capture.json write failed ({}): {e}", rec_dir.display()))?;
    Ok(result)
}

/// Speaker enrollment: uploads a WAV sample recorded with `record_mic` (or any
/// WAV) to the server with a name. Same name → new sample for the profile.
#[tauri::command]
async fn enroll_speaker(wav_path: String, name: String, is_me: bool) -> Result<String, String> {
    let cfg = remote::load_config();
    remote::enroll_speaker(&cfg, &wav_path, &name, is_me).await
}

/// RENAMES a speaker in the finished transcript + optional voice learning
/// ("tag-once" flow, Otter-style): the cluster's longest segments are cut from
/// the mixed audio and uploaded as a voice sample under the new name — on the
/// next recording the person is recognizable without captions or renaming.
#[tauri::command]
async fn rename_speaker(
    rec_id: String,
    speaker_id: String,
    new_name: String,
    learn: Option<bool>,
) -> Result<CaptureResult, String> {
    let name = new_name.trim().to_string();
    if name.is_empty() {
        return Err("Empty name".to_string());
    }
    let rec_dir = meetings_dir().join(&rec_id);
    let capture_path = rec_dir.join("capture.json");
    let raw = std::fs::read_to_string(&capture_path)
        .map_err(|e| format!("capture.json read: {e}"))?;
    let mut capture: CaptureResult =
        serde_json::from_str(&raw).map_err(|e| format!("capture.json parse: {e}"))?;

    let mut found = false;
    for sp in capture.speakers.iter_mut() {
        if sp.id == speaker_id {
            sp.label = name.clone();
            found = true;
        }
    }
    if !found {
        return Err(format!("No such speaker: {speaker_id}"));
    }
    let json = serde_json::to_string_pretty(&capture)
        .map_err(|e| format!("serialization: {e}"))?;
    std::fs::write(&capture_path, json).map_err(|e| format!("capture.json write: {e}"))?;

    // Voice learning from the renamed cluster's speech (can be disabled).
    if learn.unwrap_or(true) {
        let audio = capture.media.audio_path.clone();
        // The cluster's segments by length, up to ~30s from the longest ones.
        let mut spans: Vec<(f64, f64)> = capture
            .segments
            .iter()
            .filter(|s| s.speaker == speaker_id && s.end - s.start >= 1.0)
            .map(|s| (s.start, s.end))
            .collect();
        spans.sort_by(|a, b| (b.1 - b.0).partial_cmp(&(a.1 - a.0)).unwrap_or(std::cmp::Ordering::Equal));
        let sample_path = rec_dir.join("rename_sample.wav").to_string_lossy().to_string();
        let cut = tauri::async_runtime::spawn_blocking({
            let sample_path = sample_path.clone();
            move || remote::cut_spans_to_wav(&audio, &spans, 30.0, &sample_path)
        })
        .await
        .map_err(|e| e.to_string())?;
        match cut {
            Ok(true) => {
                let cfg = remote::load_config();
                // A learning failure does NOT break the rename — just log it.
                if let Err(e) = remote::enroll_speaker(&cfg, &sample_path, &name, false).await {
                    dbg(&format!("RENAME_HARVEST_FAIL: {e}"));
                }
                let _ = std::fs::remove_file(&sample_path);
            }
            Ok(false) => dbg("RENAME_HARVEST_SKIP: not enough speech to learn from"),
            Err(e) => dbg(&format!("RENAME_HARVEST_CUT_FAIL: {e}")),
        }
    }
    Ok(capture)
}

#[tauri::command]
async fn list_enrolled_speakers() -> Result<String, String> {
    let cfg = remote::load_config();
    remote::list_speakers(&cfg).await
}

#[tauri::command]
async fn delete_enrolled_speaker(speaker_id: String) -> Result<String, String> {
    let cfg = remote::load_config();
    remote::delete_speaker(&cfg, &speaker_id).await
}

/// List available microphone (input) device names.
#[tauri::command]
fn list_mics() -> Result<Vec<String>, String> {
    recorder::list_input_devices()
}

/// The bar's mic picker: the selected microphone (empty/None → default).
#[tauri::command]
fn set_recording_mic(name: Option<String>) {
    if let Ok(mut g) = SELECTED_MIC.lock() {
        *g = name.filter(|s| !s.trim().is_empty());
    }
}

/// The current recording microphone (None = default).
#[tauri::command]
fn get_recording_mic() -> Option<String> {
    selected_mic()
}

/// The available displays — "Display N (WIDTH×HEIGHT)". The index matches the
/// syscap `--display` argument. Thread-safe (CoreGraphics, not main-thread).
#[tauri::command]
fn list_displays() -> Vec<String> {
    #[cfg(target_os = "macos")]
    {
        use core_graphics::display::CGDisplay;
        match CGDisplay::active_displays() {
            Ok(ids) => ids
                .iter()
                .enumerate()
                .map(|(i, &id)| {
                    let d = CGDisplay::new(id);
                    let b = d.bounds();
                    let main = if d.is_main() { " — main" } else { "" };
                    format!(
                        "Display {} ({}×{}){}",
                        i + 1,
                        b.size.width as i64,
                        b.size.height as i64,
                        main
                    )
                })
                .collect(),
            Err(_) => vec!["Display 1".to_string()],
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        vec!["Display 1".to_string()]
    }
}

/// The bar's screen picker: index of the display to record (0 = first).
#[tauri::command]
fn set_recording_display(index: usize) {
    SELECTED_DISPLAY.store(index, std::sync::atomic::Ordering::Relaxed);
}

/// Index of the current recording display.
#[tauri::command]
fn get_recording_display() -> usize {
    SELECTED_DISPLAY.load(std::sync::atomic::Ordering::Relaxed)
}

/// Record the default mic for `seconds` to a temp WAV; returns the file path.
#[tauri::command]
fn record_mic(seconds: u32) -> Result<String, String> {
    let path = std::env::temp_dir().join("lavox-mic-test.wav");
    let p = path.to_string_lossy().to_string();
    recorder::record_mic_to_wav(&p, seconds)?;
    Ok(p)
}

// ---- LANGUAGE SETTING (enabled languages; persistent) ----
static ENABLED_LANGS: StdMutex<Vec<String>> = StdMutex::new(Vec::new());

fn langs_file() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    std::path::PathBuf::from(home)
        .join("Library/Application Support/live.plansmart.hangar/languages.json")
}
fn load_langs() -> Vec<String> {
    if let Ok(s) = std::fs::read_to_string(langs_file()) {
        if let Ok(v) = serde_json::from_str::<Vec<String>>(&s) {
            if !v.is_empty() {
                return v;
            }
        }
    }
    vec!["en".to_string()] // default: English (EN-first product)
}
fn save_langs(langs: &[String]) {
    let path = langs_file();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(s) = serde_json::to_string(langs) {
        let _ = std::fs::write(path, s);
    }
}

/// The enabled languages (ISO codes, e.g. ["hu","en"]).
#[tauri::command]
fn get_languages() -> Vec<String> {
    let mut g = ENABLED_LANGS.lock().unwrap();
    if g.is_empty() {
        *g = load_langs();
    }
    g.clone()
}
/// Set the enabled languages + persist them.
#[tauri::command]
fn set_languages(langs: Vec<String>) {
    let langs = if langs.is_empty() {
        vec!["en".to_string()]
    } else {
        langs
    };
    save_langs(&langs);
    if let Ok(mut g) = ENABLED_LANGS.lock() {
        *g = langs;
    }
}

// ---- HOTKEY SETTING (dictation-trigger combo; persistent) ----
use crate::hotkey::HotkeyCombo;
static HOTKEY_COMBO: StdMutex<Option<HotkeyCombo>> = StdMutex::new(None);

fn hotkey_file() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    std::path::PathBuf::from(home)
        .join("Library/Application Support/live.plansmart.hangar/hotkey.json")
}
fn load_hotkey() -> HotkeyCombo {
    if let Ok(s) = std::fs::read_to_string(hotkey_file()) {
        if let Ok(c) = serde_json::from_str::<HotkeyCombo>(&s) {
            return c;
        }
    }
    HotkeyCombo::default() // Fn-only
}
fn save_hotkey(combo: &HotkeyCombo) {
    let path = hotkey_file();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(s) = serde_json::to_string(combo) {
        let _ = std::fs::write(path, s);
    }
}

/// The current dictation-trigger combo.
#[tauri::command]
fn get_hotkey() -> HotkeyCombo {
    let mut g = HOTKEY_COMBO.lock().unwrap();
    if g.is_none() {
        *g = Some(load_hotkey());
    }
    g.clone().unwrap_or_default()
}

/// Set the dictation-trigger combo + persist it + LIVE reconfiguration.
#[tauri::command]
fn set_hotkey(combo: HotkeyCombo) {
    save_hotkey(&combo);
    if let Ok(mut g) = HOTKEY_COMBO.lock() {
        *g = Some(combo.clone());
    }
    crate::hotkey::set_active_combo(combo); // the CGEventTap reads it from here (lives in Task 3)
}

/// Initial load for the CGEventTap — the persisted combo (Fn default), without command context.
pub(crate) fn hotkey_load_for_tap() -> crate::hotkey::HotkeyCombo {
    let mut g = HOTKEY_COMBO.lock().unwrap();
    if g.is_none() {
        *g = Some(load_hotkey());
    }
    g.clone().unwrap_or_default()
}

// ---- SCRATCHPAD (quick notes; persistent) ----
#[derive(serde::Serialize, serde::Deserialize, Clone)]
struct Note {
    id: String,
    text: String,
    created: i64, // unix millis
    #[serde(default)]
    updated: i64,
    #[serde(default)]
    pinned: bool,
}
static NOTES: StdMutex<Option<Vec<Note>>> = StdMutex::new(None);

fn notes_file() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    std::path::PathBuf::from(home)
        .join("Library/Application Support/live.plansmart.hangar/scratchpad.json")
}
fn now_millis() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}
fn load_notes() -> Vec<Note> {
    std::fs::read_to_string(notes_file())
        .ok()
        .and_then(|s| serde_json::from_str::<Vec<Note>>(&s).ok())
        .unwrap_or_default()
}
fn save_notes(notes: &[Note]) {
    let path = notes_file();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(s) = serde_json::to_string(notes) {
        let _ = std::fs::write(path, s);
    }
}
/// The notes (newest first).
#[tauri::command]
fn get_notes() -> Vec<Note> {
    let mut g = NOTES.lock().unwrap();
    if g.is_none() {
        *g = Some(load_notes());
    }
    g.clone().unwrap_or_default()
}
/// Notify every window that the notes changed (the notebook refreshes on it).
fn emit_notes_changed(app: &tauri::AppHandle) {
    use tauri::Emitter;
    let _ = app.emit("notes-changed", ());
}
/// Add a new note (to the front of the list). Empty text is not saved.
#[tauri::command]
fn add_note(app: tauri::AppHandle, text: String) -> Option<Note> {
    let t = text.trim();
    let ts = now_millis();
    let note = Note {
        id: format!("{}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0)),
        text: t.to_string(),
        created: ts,
        updated: ts,
        pinned: false,
    };
    let mut g = NOTES.lock().unwrap();
    let mut notes = g.take().unwrap_or_else(load_notes);
    notes.insert(0, note.clone());
    save_notes(&notes);
    *g = Some(notes);
    drop(g);
    emit_notes_changed(&app);
    Some(note)
}
/// Update an existing note's text (from the notebook editor).
#[tauri::command]
fn update_note(app: tauri::AppHandle, id: String, text: String) {
    let ts = now_millis();
    let mut g = NOTES.lock().unwrap();
    let mut notes = g.take().unwrap_or_else(load_notes);
    for n in notes.iter_mut() {
        if n.id == id {
            n.text = text.clone();
            n.updated = ts;
        }
    }
    save_notes(&notes);
    *g = Some(notes);
    drop(g);
    emit_notes_changed(&app);
}
/// Pin / unpin a note — pinned notes go to the top of the list.
#[tauri::command]
fn set_note_pinned(app: tauri::AppHandle, id: String, pinned: bool) {
    let mut g = NOTES.lock().unwrap();
    let mut notes = g.take().unwrap_or_else(load_notes);
    for n in notes.iter_mut() {
        if n.id == id {
            n.pinned = pinned;
        }
    }
    save_notes(&notes);
    *g = Some(notes);
    drop(g);
    emit_notes_changed(&app);
}
/// Delete a note by id.
#[tauri::command]
fn delete_note(app: tauri::AppHandle, id: String) {
    let mut g = NOTES.lock().unwrap();
    let mut notes = g.take().unwrap_or_else(load_notes);
    notes.retain(|n| n.id != id);
    save_notes(&notes);
    *g = Some(notes);
    drop(g);
    emit_notes_changed(&app);
}

/// Whether the notebook window is open.
static NOTEBOOK_OPEN: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
/// Whether the notebook is the FOCUSED window — THIS decides dictation routing (text
/// goes into a note only when we are really in the notebook; otherwise paste at the cursor).
static NOTEBOOK_FOCUSED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Sets the notebook window's glass theme (light / dark / ultra-transparent).
/// Must be called on the MAIN THREAD (NSVisualEffectView + appearance). clear → re-apply.
#[cfg(target_os = "macos")]
fn apply_notebook_glass(win: &tauri::WebviewWindow, theme: &str) {
    use window_vibrancy::{
        apply_vibrancy, clear_vibrancy, NSVisualEffectMaterial, NSVisualEffectState,
    };
    // Remove any previous vibrancy layer so the old material does not get stuck.
    let _ = clear_vibrancy(win);
    let (material, state, appearance) = match theme {
        // Dark: the Sidebar material in dark appearance → dark frosted glass.
        "dark" => (
            NSVisualEffectMaterial::Sidebar,
            NSVisualEffectState::Active,
            tauri::Theme::Dark,
        ),
        // Ultra-transparent: HudWindow = genuinely translucent (the background shows
        // through, darkened), follows focus (lightens when you click away). Does NOT
        // add a white veil like the light window-background materials do.
        "ultra" => (
            NSVisualEffectMaterial::HudWindow,
            NSVisualEffectState::FollowsWindowActiveState,
            tauri::Theme::Dark,
        ),
        // Light (default): vibrant, frosted, light.
        _ => (
            NSVisualEffectMaterial::Sidebar,
            NSVisualEffectState::Active,
            tauri::Theme::Light,
        ),
    };
    let _ = win.set_theme(Some(appearance));
    let _ = apply_vibrancy(win, material, Some(state), Some(10.0));
}

/// Switch the notebook glass theme at runtime (from the ⋯ menu).
#[tauri::command]
fn set_notebook_glass(app: tauri::AppHandle, theme: String) {
    use tauri::Manager;
    if let Some(win) = app.get_webview_window("notebook") {
        let vwin = win.clone();
        let _ = win.run_on_main_thread(move || {
            #[cfg(target_os = "macos")]
            apply_notebook_glass(&vwin, &theme);
            #[cfg(not(target_os = "macos"))]
            let _ = (&vwin, &theme);
        });
    }
}

/// The bar's 📝 button calls this: opens (or brings to front) the small
/// notebook window (Wispr-style). It uses the same scratchpad.json, so bar
/// dictation shows up in it immediately.
#[tauri::command]
fn show_notebook(app: tauri::AppHandle) {
    use std::sync::atomic::Ordering;
    use tauri::Manager;
    if let Some(w) = app.get_webview_window("notebook") {
        let _ = w.show();
        let _ = w.set_focus();
        NOTEBOOK_OPEN.store(true, Ordering::SeqCst);
        NOTEBOOK_FOCUSED.store(true, Ordering::SeqCst);
        return;
    }
    let built = tauri::WebviewWindowBuilder::new(
        &app,
        "notebook",
        tauri::WebviewUrl::App("index.html?window=notebook".into()),
    )
    .title("Lavox Notes")
    .inner_size(780.0, 560.0)
    .min_inner_size(480.0, 360.0)
    .transparent(true)
    .title_bar_style(tauri::TitleBarStyle::Overlay)
    .hidden_title(true)
    // IMPORTANT: the webview must handle OS file drops (HTML5 onDrop), not the Tauri
    // window swallowing them → inserting an image/file dragged into a note keeps working.
    .disable_drag_drop_handler()
    .build();
    if let Ok(win) = built {
        NOTEBOOK_OPEN.store(true, Ordering::SeqCst);
        NOTEBOOK_FOCUSED.store(true, Ordering::SeqCst);
        // Vibrancy is NOT applied here — the frontend calls it with the persisted
        // theme (set_notebook_glass) on mount → a single layer, nothing gets stuck.
        let app2 = app.clone();
        win.on_window_event(move |ev| {
            use tauri::Emitter;
            match ev {
                tauri::WindowEvent::CloseRequested { .. } => {
                    NOTEBOOK_OPEN.store(false, Ordering::SeqCst);
                    NOTEBOOK_FOCUSED.store(false, Ordering::SeqCst);
                    let _ = app2.emit("notebook-closed", ());
                }
                // THIS is the key to dictation routing: if the notebook loses
                // focus (you click into another app), dictation goes THERE, not here.
                tauri::WindowEvent::Focused(focused) => {
                    NOTEBOOK_FOCUSED.store(*focused, Ordering::SeqCst);
                    let _ = app2.emit("notebook-focus", *focused);
                }
                _ => {}
            }
        });
    }
}
/// For dictation routing: whether the notebook is open.
#[tauri::command]
fn is_notebook_open() -> bool {
    NOTEBOOK_OPEN.load(std::sync::atomic::Ordering::SeqCst)
}
/// For dictation routing: whether the notebook is the focused window.
#[tauri::command]
fn is_notebook_focused() -> bool {
    NOTEBOOK_FOCUSED.load(std::sync::atomic::Ordering::SeqCst)
}

/// Open the camera-bubble window in the bottom-right corner of the primary display.
/// A circular, borderless, always-on-top webview — it shows the getUserMedia camera,
/// which the screen recording captures as PiP (no video compositing).
#[tauri::command]
fn show_camera_bubble(app: tauri::AppHandle) {
    use tauri::Manager;
    if let Some(w) = app.get_webview_window("camera-bubble") {
        let _ = w.show();
        position_camera_bubble(&w);
        return;
    }
    let built = tauri::WebviewWindowBuilder::new(
        &app,
        "camera-bubble",
        tauri::WebviewUrl::App("index.html?window=camera-bubble".into()),
    )
    .title("Camera")
    .inner_size(200.0, 200.0)
    .min_inner_size(120.0, 120.0)
    .decorations(false)
    .transparent(true)
    .always_on_top(true)
    .skip_taskbar(true)
    .shadow(false)
    .resizable(true)
    .focused(false)
    .build();
    if let Ok(win) = built {
        position_camera_bubble(&win);
    }
}

/// Places the bubble in the bottom-right corner of the primary display, with a 40 px margin.
fn position_camera_bubble(win: &tauri::WebviewWindow) {
    let monitor = win
        .primary_monitor()
        .ok()
        .flatten()
        .or_else(|| win.current_monitor().ok().flatten());
    if let Some(m) = monitor {
        let scale = m.scale_factor();
        let msize = m.size();
        let mpos = m.position();
        if let Ok(sz) = win.outer_size() {
            let margin = (40.0 * scale) as i32;
            let x = mpos.x + msize.width as i32 - sz.width as i32 - margin;
            let y = mpos.y + msize.height as i32 - sz.height as i32 - margin;
            let _ = win.set_position(tauri::PhysicalPosition::new(x, y));
        }
    }
}

/// Close the camera-bubble window (the camera track is released when the webview unmounts).
#[tauri::command]
fn hide_camera_bubble(app: tauri::AppHandle) {
    use tauri::Manager;
    if let Some(w) = app.get_webview_window("camera-bubble") {
        let _ = w.close();
    }
}

/// Open an image/file inserted into Lavox Notes: decode the data URL
/// (`data:<mime>;base64,<...>`) into a temporary file, then open it with the system
/// default app (Preview / Finder app / etc.) — via the `opener` plugin.
#[tauri::command]
fn open_attachment(app: tauri::AppHandle, data_url: String, filename: String) -> Result<(), String> {
    use base64::Engine;
    let comma = data_url.find(',').ok_or("invalid data URL")?;
    let b64 = &data_url[comma + 1..];
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(b64)
        .map_err(|e| e.to_string())?;
    let dir = std::env::temp_dir().join("lavox-notes-attachments");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    // Unique subdirectory so files with identical names do not overwrite each other.
    let unique_dir = dir.join(format!("{}", now_millis()));
    std::fs::create_dir_all(&unique_dir).map_err(|e| e.to_string())?;
    let safe_name = if filename.trim().is_empty() { "attachment".to_string() } else { filename };
    let path = unique_dir.join(safe_name);
    std::fs::write(&path, bytes).map_err(|e| e.to_string())?;
    use tauri_plugin_opener::OpenerExt;
    app.opener()
        .open_path(path.to_string_lossy().to_string(), None::<&str>)
        .map_err(|e| e.to_string())
}

/// Transcribe a WAV file using whisper.cpp. `model_path` = path to GGML model.
/// Language from settings: 1 enabled → use it; several → auto-detect. If
/// translate-to-English is enabled → auto-detect the source + translate to English.
#[tauri::command]
fn transcribe_wav(wav_path: String, model_path: String) -> Result<TranscriptResult, String> {
    let langs = get_languages();
    // NO translation (the turbo model does not translate — that will be a separate
    // feature). Dictation is PINNED to the configured language: 1 language → force it
    // (other languages are not transcribed correctly); several → auto-detect among the
    // configured ones (in practice en/hu).
    let lang: Option<&str> = if langs.len() == 1 {
        Some(langs[0].as_str())
    } else {
        None
    };
    dbg(&format!("TRANSCRIBE langs={:?} lang={:?}", langs, lang));
    transcribe::transcribe_wav(&wav_path, &model_path, lang, false)
}

// ---- MODEL DOWNLOAD (first-run: whisper GGML model from HuggingFace) ----
// The product default is large-v3-turbo (good Hungarian quality); ~1.6 GB.
const MODEL_URL: &str =
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin";
const MODEL_FILENAME: &str = "ggml-large-v3-turbo.bin";

/// The user's model directory (app-support) — download_model writes here, and
/// find_model searches it too. This keeps the model OUT of the iCloud-synced repo.
fn models_data_dir() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    std::path::PathBuf::from(home)
        .join("Library/Application Support/live.plansmart.hangar/models")
}

/// Download the whisper model (streaming, with progress events).
/// Event: "model-download-progress" {downloaded, total, done?}.
#[tauri::command]
async fn download_model(app: tauri::AppHandle) -> Result<String, String> {
    use tauri::Emitter;
    use tokio::io::AsyncWriteExt;

    let dir = models_data_dir();
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let dest = dir.join(MODEL_FILENAME);
    if dest.exists() {
        return Ok(dest.to_string_lossy().to_string());
    }
    let tmp = dir.join(format!("{MODEL_FILENAME}.part"));

    let mut resp = reqwest::get(MODEL_URL)
        .await
        .map_err(|e| format!("connection: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("download error: HTTP {}", resp.status()));
    }
    let total = resp.content_length().unwrap_or(0);
    let mut file = tokio::fs::File::create(&tmp).await.map_err(|e| e.to_string())?;
    let mut downloaded: u64 = 0;
    let mut last_emit = std::time::Instant::now();
    while let Some(chunk) = resp
        .chunk()
        .await
        .map_err(|e| format!("download interrupted: {e}"))?
    {
        file.write_all(&chunk).await.map_err(|e| e.to_string())?;
        downloaded += chunk.len() as u64;
        if last_emit.elapsed().as_millis() > 300 {
            let _ = app.emit(
                "model-download-progress",
                serde_json::json!({ "downloaded": downloaded, "total": total }),
            );
            last_emit = std::time::Instant::now();
        }
    }
    file.flush().await.map_err(|e| e.to_string())?;
    drop(file);
    std::fs::rename(&tmp, &dest).map_err(|e| e.to_string())?;
    let _ = app.emit(
        "model-download-progress",
        serde_json::json!({ "downloaded": downloaded, "total": total, "done": true }),
    );
    Ok(dest.to_string_lossy().to_string())
}

/// Find the first .bin model in the models/ directory.
/// Checks: app-support (downloaded), project root (dev), next to exe (bundled),
/// and CARGO_MANIFEST_DIR (dev fallback).
#[tauri::command]
fn find_model() -> Result<String, String> {
    let mut candidates: Vec<std::path::PathBuf> = vec![models_data_dir()];

    // Dev mode: CARGO_MANIFEST_DIR points to src-tauri/, model is one level up
    if let Ok(manifest) = std::env::var("CARGO_MANIFEST_DIR") {
        let project_root = std::path::Path::new(&manifest).parent();
        if let Some(root) = project_root {
            candidates.push(root.join("models"));
        }
    }

    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd.join("models"));
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("models"));
            // macOS bundle: exe is in .app/Contents/MacOS/
            if let Some(contents) = dir.parent() {
                candidates.push(contents.join("Resources").join("models"));
                // tauri.conf.json "resources": ["../models/*.bin"] cannot place the
                // ".." directly under Resources/, so it creates an "_up_" subfolder
                // (Resources/_up_/models/...) — check that too.
                candidates.push(contents.join("Resources").join("_up_").join("models"));
            }
        }
    }

    // Collect all .bin models, prefer the largest (= most accurate, e.g. large-v3-turbo).
    let mut best: Option<(u64, std::path::PathBuf)> = None;
    for dir in &candidates {
        if let Ok(entries) = std::fs::read_dir(dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().and_then(|e| e.to_str()) == Some("bin") {
                    let size = entry.metadata().map(|m| m.len()).unwrap_or(0);
                    if best.as_ref().map(|(s, _)| size > *s).unwrap_or(true) {
                        best = Some((size, path));
                    }
                }
            }
        }
    }
    match best {
        Some((_, path)) => Ok(path.to_string_lossy().to_string()),
        None => Err("No GGML model found in the models/ directory".to_string()),
    }
}

/// M2: the global shortcut used to summon the overlay.
///
/// Default: `CmdOrCtrl+Shift+Space`. The Fn key by itself CANNOT be bound via the
/// standard global-shortcut API on macOS (hardware modifier,
/// `tauri-plugin-global-shortcut` does not see it).
// TODO(M2.1): real Fn-key detection via CGEventTap (needs native IOKit/CGEventTap).
const OVERLAY_SHORTCUT: &str = "CmdOrCtrl+Shift+Space";

// The always-on pill's initial (idle) size — a tiny line, so it does not disturb the
// display (Wispr-style). Every further size comes from the frontend via `set_pill_size`.
const PILL_LINE_W: f64 = 200.0;
const PILL_LINE_H: f64 = 24.0;
// Distance from the top of the screen. On notched MacBooks the menu bar/notch is
// ~37px tall → the pill must go BELOW that, or it disappears into the cutout.
const PILL_TOP_MARGIN: f64 = 26.0;

/// Returns the monitor under the cursor (multi-monitor). We use Tauri's
/// `monitor_from_point` API, which handles the coordinate space correctly INTERNALLY —
/// manual bounds-matching is wrong on mixed-scale multi-monitor setups (monitor
/// positions are scaled inconsistently).
fn monitor_under_cursor(window: &tauri::WebviewWindow) -> Option<tauri::Monitor> {
    let cursor = window.cursor_position().ok()?;
    window.monitor_from_point(cursor.x, cursor.y).ok().flatten()
}

/// Positions the pill window at the top of the PRIMARY monitor, horizontally CENTERED,
/// with the given (logical) width + height. The top edge stays fixed, so content grows downward.
///
/// NOTE: we use the primary monitor because Tauri scales multi-monitor coordinates
/// INCONSISTENTLY on mixed-scale setups (a non-primary monitor's position gets multiplied
/// by the primary's scale) → centering on the cursor's monitor drifts.
/// On the primary (0,0) monitor the math is correct → a stable, centered pill.
/// True multi-monitor tracking needs native (NSScreen) code — later.
fn position_pill(window: &tauri::WebviewWindow, width_logical: f64, height_logical: f64) {
    let ni = NOTCH_INFO.lock().ok().and_then(|g| g.clone());
    // TEMPORARY DEBUG TEST: forced fallback position (notch mode off), to find out
    // whether the notch strip (y=0, the area overlapping the menu bar) is the culprit.
    if std::env::var("LAVOX_FORCE_FALLBACK_POS").is_ok() {
        dbg("POSITION_PILL forced fallback (debug)");
    } else
    // NOTCH MODE: align to the CENTER of the notch using fresh NSScreen data (NOTCH_INFO),
    // NOT Tauri's primary_monitor() — that goes stale when a monitor is attached/detached
    // (the old monitor's size/offset gets stuck → the pill drifts out from under the
    // notch). The notch sits at the top of the primary (built-in) screen, origin (0,0).
    if let Some(ni) = ni.as_ref().filter(|n| n.has_notch) {
        let scale = if ni.scale > 0.0 { ni.scale } else { 2.0 };
        let win_w = (width_logical * scale).round() as i32;
        let win_h = (height_logical * scale).round() as i32;
        let notch_center_pt = (ni.notch_left + ni.notch_right) / 2.0;
        let x = (notch_center_pt * scale).round() as i32 - win_w / 2;
        let _ = window.set_size(tauri::PhysicalSize::new(win_w.max(1) as u32, win_h.max(1) as u32));
        let _ = window.set_position(tauri::PhysicalPosition::new(x, 0));
        dbg(&format!("POSITION_PILL notch x={x} y=0 w={win_w} h={win_h}"));
        return;
    }
    // FALLBACK (no notch): center of the monitor, below the menu bar.
    let monitor = window
        .primary_monitor()
        .ok()
        .flatten()
        .or_else(|| window.current_monitor().ok().flatten());
    if let Some(monitor) = monitor {
        let scale = monitor.scale_factor();
        let msize = monitor.size();
        let mpos = monitor.position();
        let win_w = (width_logical * scale) as i32;
        let win_h = (height_logical * scale) as i32;
        let margin = (PILL_TOP_MARGIN * scale) as i32;
        let x = mpos.x + (msize.width as i32 - win_w) / 2;
        let y = mpos.y + margin;
        let _ = window.set_size(tauri::PhysicalSize::new(win_w.max(1) as u32, win_h.max(1) as u32));
        let _ = window.set_position(tauri::PhysicalPosition::new(x, y));
        dbg(&format!("POSITION_PILL float x={x} y={y} w={win_w} h={win_h}"));
    } else {
        dbg("POSITION_PILL no_monitor");
    }
}

/// Instrumentation: timestamped line into /tmp/lavox-ptt.log (for analyzing the
/// push-to-talk pipeline — keydown/keyup, recording, transcription, paste latency).
pub(crate) fn dbg(msg: &str) {
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open("/tmp/lavox-ptt.log")
    {
        let ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        let _ = writeln!(f, "{ms} {msg}");
    }
}

/// The frontend logs pipeline steps with this (start/stop/transcription/paste).
#[tauri::command]
fn dbg_log(msg: String) {
    dbg(&msg);
}

/// M2.2: paste the transcript at the active app's cursor (clipboard + Cmd+V). The
/// frontend calls this when dictation finishes. The overlay does not steal focus, so
/// the target app receives the paste.
#[tauri::command]
fn insert_text(text: String) -> Result<(), String> {
    let target = DICTATION_TARGET_APP
        .lock()
        .ok()
        .and_then(|t| t.clone());
    inject::insert_text(&text, target.as_deref())
}

/// Resize the pill window to the (logical) size given by the frontend, aligned to
/// the top of the screen. The frontend drives the tiny-line ↔ hover ↔ expanded
/// states with this — so every further size tweak is hot-reload, no more Rust
/// builds needed.
#[tauri::command]
fn set_pill_size(app: tauri::AppHandle, width: f64, height: f64) {
    use tauri::Manager;
    if let Some(overlay) = app.get_webview_window("overlay") {
        position_pill(&overlay, width, height);
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    use tauri_plugin_global_shortcut::{Code, Modifiers, Shortcut};

    // CmdOrCtrl+Shift+Space — Super (Cmd) on macOS, Ctrl elsewhere.
    #[cfg(target_os = "macos")]
    let primary = Modifiers::SUPER;
    #[cfg(not(target_os = "macos"))]
    let primary = Modifiers::CONTROL;

    let overlay_shortcut = Shortcut::new(Some(primary | Modifiers::SHIFT), Code::Space);

    let cal_state = calendar::new_state();

    tauri::Builder::default()
        .manage(cal_state.clone())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_nspanel::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(move |_app, shortcut, event| {
                    // The dictation trigger runs on the hotkey.rs CGEventTap
                    // (configurable, Fn default). This handler NO LONGER starts
                    // dictation — only a diagnostic log remains, so there is no
                    // duplicate/conflicting trigger.
                    if shortcut == &overlay_shortcut {
                        dbg(&format!("LEGACY_GS_EVENT state={:?}", event.state));
                    }
                })
                .build(),
        )
        .setup(move |app| {
            use tauri::Manager;
            // PUSH-TO-TALK: start the reliable key listener (CGEventTap) — it sees the
            // ⌘⇧Space down/up transitions directly (the Discord/Wispr approach), instead
            // of Global Shortcut's unreliable Released event. Requires Input Monitoring.
            hotkey::start(app.handle().clone());
            // NOTCH DETECTION (on the main thread — NSScreen is main-thread-only). The
            // frontend fits the Dynamic Island-style compact layout to this.
            let ni = notch::detect();
            dbg(&format!(
                "NOTCH has={} h={:.0} left={:.0} right={:.0} screenW={:.0} scale={:.1}",
                ni.has_notch, ni.notch_height, ni.notch_left, ni.notch_right, ni.screen_width, ni.scale
            ));
            if let Ok(mut g) = NOTCH_INFO.lock() {
                *g = Some(ni);
            }
            // EVENT-DRIVEN notch refresh: we register the display-reconfiguration
            // callback (monitor connects/disconnects, resolution/scaling changes)
            // → the bar corrects itself, NO polling.
            #[cfg(target_os = "macos")]
            {
                if let Ok(mut g) = NOTCH_APP_HANDLE.lock() {
                    *g = Some(app.handle().clone());
                }
                unsafe {
                    display_watch::CGDisplayRegisterReconfigurationCallback(
                        on_display_reconfig,
                        std::ptr::null_mut(),
                    );
                }
            }
            // ALWAYS-INTERACTIVE OVERLAY (Wispr layer): we turn the overlay window into
            // a non-activating NSPanel → it expands on hover EVEN WHEN another app has
            // focus, and it does not steal focus. Visible on every space.
            dbg("OVERLAY_SETUP_START");
            let overlay = app.get_webview_window("overlay").expect("FATAL: overlay window not found!");
            dbg("OVERLAY_FOUND");
            #[cfg(target_os = "macos")]
            {
                use tauri_nspanel::cocoa::appkit::NSWindowCollectionBehavior;
                use tauri_nspanel::WebviewWindowExt as _;
                dbg("PANEL_CONVERT_TRY");
                if let Ok(panel) = overlay.to_panel() {
                    dbg("PANEL_CONVERT_OK");
                    // ABOVE the menu bar (level 24), so the pill is always visible.
                    #[allow(non_upper_case_globals)]
                    const NSStatusWindowLevel: i32 = 25;
                    panel.set_level(NSStatusWindowLevel);
                    // NonActivatingPanel → receives mouse hover/clicks without
                    // activating the app (does not steal focus).
                    #[allow(non_upper_case_globals)]
                    const NSWindowStyleMaskNonActivatingPanel: i32 = 1 << 7;
                    panel.set_style_mask(NSWindowStyleMaskNonActivatingPanel);
                    // Visible on every space + above fullscreen apps too.
                    panel.set_collection_behaviour(
                        NSWindowCollectionBehavior::NSWindowCollectionBehaviorCanJoinAllSpaces
                            | NSWindowCollectionBehavior::NSWindowCollectionBehaviorFullScreenAuxiliary,
                    );
                    // KEY: an NSPanel HIDES by default when the app goes to the background
                    // → must be turned off so the pill is ALWAYS visible (like Wispr).
                    panel.set_hides_on_deactivate(false);
                    // Hover events even when another app has focus.
                    panel.set_accepts_mouse_moved_events(true);
                    // Do not grab keyboard focus unless it is actually needed.
                    panel.set_becomes_key_only_if_needed(true);
                    // NO window shadow → removes the ugly rectangular "frame" around
                    // the glass (an NSPanel casts a window shadow by default).
                    panel.set_has_shadow(false);
                    panel.show();
                    dbg(&format!("PANEL_VISIBLE={}", panel.is_visible()));
                } else {
                    dbg("PANEL_CONVERT_FAIL");
                }
            }
            position_pill(&overlay, PILL_LINE_W, PILL_LINE_H);
            // M4: start the calendar poller in the background.
            calendar::start_polling(app.handle().clone(), cal_state.clone());
            // Cloud pairing: start the heartbeat loop in the background. It silently
            // skips itself while there is no device token (self-hosted mode).
            remote::start_heartbeat_loop();
            // LAVOX Meet Bridge: the Chrome extension signals here (127.0.0.1:5192)
            // when the user joins/leaves a Google Meet → pill recording prompt.
            bridge::start(app.handle().clone());

            // HOVER WITHOUT FOCUS (Wispr experience): a backgrounded WKWebView does not
            // receive DOM hover events, so we NATIVELY poll the global cursor position
            // against the overlay window's frame. On enter/leave we emit a "pill-hover"
            // event → the frontend expands/collapses even when another app has focus.
            let hover_handle = app.handle().clone();
            std::thread::spawn(move || {
                use tauri::{Emitter, Manager};
                let mut was_inside = false;
                loop {
                    std::thread::sleep(std::time::Duration::from_millis(90));
                    let Some(overlay) = hover_handle.get_webview_window("overlay") else {
                        continue;
                    };
                    let (cursor, pos, size) = match (
                        overlay.cursor_position(),
                        overlay.outer_position(),
                        overlay.outer_size(),
                    ) {
                        (Ok(c), Ok(p), Ok(s)) => (c, p, s),
                        _ => continue,
                    };
                    let left = pos.x as f64;
                    let top = pos.y as f64;
                    let right = left + size.width as f64;
                    let bottom = top + size.height as f64;
                    let inside =
                        cursor.x >= left && cursor.x <= right && cursor.y >= top && cursor.y <= bottom;
                    if inside != was_inside {
                        was_inside = inside;
                        let _ = overlay.emit("pill-hover", inside);
                    }
                }
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            list_mics,
            set_recording_mic,
            get_recording_mic,
            list_displays,
            set_recording_display,
            get_recording_display,
            record_mic,
            transcribe_wav,
            memory_ingest_dictation,
            dictation_learn,
            get_server_config,
            set_server_config,
            hub_pair_claim,
            remote_transcribe_meeting,
            rename_speaker,
            enroll_speaker,
            list_enrolled_speakers,
            delete_enrolled_speaker,
            find_model,
            download_model,
            list_meetings,
            load_capture,
            save_capture,
            set_pill_size,
            set_follow_enabled,
            get_notch_info,
            refresh_notch,
            get_hotkey,
            set_hotkey,
            get_languages,
            set_languages,
            get_notes,
            add_note,
            update_note,
            set_note_pinned,
            delete_note,
            show_notebook,
            show_camera_bubble,
            hide_camera_bubble,
            get_bridge_status,
            set_notebook_glass,
            is_notebook_open,
            is_notebook_focused,
            open_attachment,
            insert_text,
            dbg_log,
            set_calendar_token,
            clear_calendar_token,
            calendar_login,
            calendar_status,
            calendar_logout,
            set_auto_record,
            get_auto_record,
            get_calendar_status,
            start_meeting_record,
            start_video_record,
            stop_meeting_record,
            start_dictation_record,
            stop_dictation_record,
            export::export_transcript_to_obsidian,
            export::export_capture_to_obsidian
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
