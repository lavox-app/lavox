// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/

mod bridge;
mod calendar;
mod export;
mod gauth;
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
// A rendszerhang-rögzítő helper (syscap) futó példánya + a cél WAV útvonala.
// A meeting-felvétel két sávot rögzít: mikrofon (te) + rendszerhang (a többiek).
static ACTIVE_SYSCAP: StdMutex<Option<(std::process::Child, String)>> = StdMutex::new(None);
// Az AKTÍV meeting-felvétel azonosítója (= a tartós könyvtár neve).
static ACTIVE_MEETING_ID: StdMutex<Option<String>> = StdMutex::new(None);
// A felvétel fajtája: "meeting" (Meet Bridge / naptár) vagy "video" (kézi, bárból).
static ACTIVE_RECORD_KIND: StdMutex<Option<String>> = StdMutex::new(None);
// A felvétel indulásának unix ms időbélyege — a Meet CC feliratok (bridge
// puffer) relatív időhöz igazításához kell a stopnál.
static ACTIVE_RECORD_STARTED_MS: StdMutex<Option<i64>> = StdMutex::new(None);
// A bar mic-választója által kiválasztott mikrofon (None = alapértelmezett).
static SELECTED_MIC: StdMutex<Option<String>> = StdMutex::new(None);
// A bar képernyő-választója által kiválasztott kijelző-index (0 = első/fő).
static SELECTED_DISPLAY: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

fn selected_mic() -> Option<String> {
    SELECTED_MIC.lock().ok().and_then(|g| g.clone())
}
// KÜLÖN slot a diktálásnak — NEM osztozik a meeting/calendar felvétel-mutexén,
// így a kettő nem ütközik (ez okozta a „push-to-talk majdnem működik" bugot).
static ACTIVE_DICTATION: StdMutex<Option<recorder::StreamingRecorder>> = StdMutex::new(None);
// A diktálás INDULÁSAKOR aktív app bundle ID-ja — ide aktiválunk vissza beillesztéskor.
static DICTATION_TARGET_APP: StdMutex<Option<String>> = StdMutex::new(None);
// A frontend jelzi, mikor idle a pill (vékony vonal) — csak akkor követi a kurzort.
static FOLLOW_ENABLED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(true);
// Fut-e épp a stop-poller (megbízható leállás: a Space elengedését pollozzuk).
static STOP_POLLER_ACTIVE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
// Fut-e a mikrofon-szint emitter (valós idejű waveform — csak felvétel közben).
static MIC_EMIT_ACTIVE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
// A notch-infó (induláskor detektálva a fő szálon, mert NSScreen main-thread-only).
static NOTCH_INFO: StdMutex<Option<notch::NotchInfo>> = StdMutex::new(None);
// Az app-handle a kijelző-átkonfigurálás callbackhez (esemény-vezérelt notch-frissítés).
static NOTCH_APP_HANDLE: StdMutex<Option<tauri::AppHandle>> = StdMutex::new(None);

/// macOS kijelző-átkonfigurálás (monitor csatlakozik/lecsatlakozik, felbontás/skálázás
/// vagy elrendezés vált) — esemény-vezérelt, NEM polling. Csak videós kapcsolat
/// (HDMI/Thunderbolt/built-in) triggereli, Bluetooth NEM.
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

/// A kijelző-átkonfigurálás callbackje (a fő run loopon fut). Újra-detektálja a
/// notch-ot + szól az overlay-nek → a bar magától korrigál monitor-/felbontás-váltáskor.
#[cfg(target_os = "macos")]
extern "C" fn on_display_reconfig(
    _display: display_watch::CGDirectDisplayID,
    flags: display_watch::CGDisplayChangeSummaryFlags,
    _user: *mut std::os::raw::c_void,
) {
    // A konfiguráció KEZDETÉT kihagyjuk — a végén (a tényleges flagekkel) detektálunk.
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

/// A detektált notch-infó (a frontend ehhez igazítja a compact layoutot).
#[tauri::command]
fn get_notch_info() -> notch::NotchInfo {
    NOTCH_INFO
        .lock()
        .ok()
        .and_then(|g| g.clone())
        .unwrap_or_default()
}

/// Újra-detektálja a notch-ot (FŐ szálon — NSScreen main-thread-only) + frissíti a
/// NOTCH_INFO-t, és „notch-refreshed" eseménnyel szól az overlay-nek. A frontend
/// periodikusan hívja → monitor le/felcsatolás után is helyes marad a pozíció.
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

// Egy billentyű épp le van-e nyomva (HID hardver-állapot). A global-shortcut Released
// eseménye chordnál megbízhatatlan, ezért push-to-talk leálláshoz EZT pollozzuk.
#[cfg(target_os = "macos")]
fn key_is_down(keycode: u16) -> bool {
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGEventSourceKeyState(state_id: i32, key: u16) -> bool;
    }
    // 1 = kCGEventSourceStateHIDSystemState (valós hardver-állapot)
    unsafe { CGEventSourceKeyState(1, keycode) }
}
#[cfg(not(target_os = "macos"))]
fn key_is_down(_keycode: u16) -> bool {
    false
}

/// A frontend állítja: idle (vékony vonal) → true, bármi más → false.
/// Csak idle-ben követi a pill a kurzort a képernyők közt.
#[tauri::command]
fn set_follow_enabled(enabled: bool) {
    FOLLOW_ENABLED.store(enabled, std::sync::atomic::Ordering::Relaxed);
}

/// Az épp aktív (frontmost) app bundle ID-ja. A SAJÁT appunkat kiszűrjük, hogy
/// soha ne magunkba illesszünk be (ha a pill épp előtérben lenne).
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

/// Diktálás felvétel indítása (saját slot). Ha valamiért bent ragadt egy korábbi
/// felvétel, ELDOBJUK és frissen indítunk → soha nem ragad be „Már fut" hibára.
#[tauri::command]
fn start_dictation_record(app: tauri::AppHandle) -> Result<String, String> {
    use std::sync::atomic::Ordering;
    let level_handle;
    {
        let mut guard = ACTIVE_DICTATION.lock().map_err(|e| e.to_string())?;
        if guard.is_some() {
            let _ = guard.take(); // a beragadt felvételt eldobjuk
        }
        let rec = recorder::StreamingRecorder::start(selected_mic())?;
        level_handle = rec.level_handle();
        *guard = Some(rec);
    }
    // VALÓS IDEJŰ WAVEFORM: ~50ms-enként elküldjük a frontendnek a pillanatnyi
    // mikrofon-szintet (RMS), hogy a vonalak CSAK akkor emelkedjenek, ha tényleg
    // beszélsz. Az emitter a felvétel végéig fut (MIC_EMIT_ACTIVE).
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
    // A felvétel már fut — most mentjük el, melyik app volt aktív (ide aktiválunk
    // vissza beillesztéskor). Csak érvényes (nem-saját) appra frissítünk, hogy egy
    // átmeneti pill-fókusz ne írja felül a valódi célt.
    if let Some(front) = capture_frontmost_app() {
        if let Ok(mut t) = DICTATION_TARGET_APP.lock() {
            *t = Some(front);
        }
    }
    Ok("recording".to_string())
}

/// Diktálás leállítása + WAV mentése; visszaadja az útvonalat.
#[tauri::command]
fn stop_dictation_record() -> Result<String, String> {
    MIC_EMIT_ACTIVE.store(false, std::sync::atomic::Ordering::SeqCst);
    let mut guard = ACTIVE_DICTATION.lock().map_err(|e| e.to_string())?;
    let rec = guard.take().ok_or("Nincs aktív diktálás")?;
    let path = std::env::temp_dir().join("lavox-dictation.wav");
    rec.stop_and_save(&path.to_string_lossy())?;
    Ok(path.to_string_lossy().to_string())
}

/// A kész diktátum beküldése a Lavox Memory-ba (lokális szerver, fire-and-forget).
///
/// A meetingek a szerver /api/transcribe útján automatikusan a memóriába
/// folynak; a diktálás viszont ITT, a Hub whisper.cpp-jén készül — ezért a
/// kész szöveget külön küldjük be. Best-effort: ha a szerver nem fut vagy
/// nincs memória-modul, csendben elengedjük (a diktálás fő útját — leírás +
/// beillesztés — SOHA nem érintheti).
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

// ---- GOOGLE NAPTÁR: natív (desktop) bejelentkezés ----
// A korábbi böngészős GIS-megoldást a Tauri CSP blokkolta csomagolt appban, és
// refresh token nélkül 1 óra után némán leállt. Lásd gauth.rs.

/// Bejelentkezés: rendszer-böngésző → loopback redirect → access+refresh token.
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

/// A naptár-kapcsolat állapota a Beállításokhoz. A `configured` azt mondja meg,
/// hogy a build tartalmaz-e Google desktop-kliens azonosítót — enélkül a
/// bejelentkezés-gombot nincs értelme megmutatni.
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

// ---- AUTO-RECORD BEÁLLÍTÁS (perzisztens; túléli az újratelepítést) ----
fn auto_record_file() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    std::path::PathBuf::from(home)
        .join("Library/Application Support/live.plansmart.hangar/auto_record.json")
}

/// A meeting-bővítmény kapcsolat-állapota a Settings-panelhez: fut-e a bridge
/// (mindig igen, ha az app él), és mikor jött utoljára a bővítménytől esemény.
#[tauri::command]
fn get_bridge_status() -> serde_json::Value {
    let (last_ms, kind) = bridge::last_event();
    serde_json::json!({
        "port": 5192,
        "lastEventMs": last_ms,
        "lastEventKind": kind,
    })
}

/// A perzisztált auto-record beállítás (a bar meet-joined döntése is ezt olvassa).
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
    // Perzisztálás fájlba (túléli az újratelepítést, nem csak localStorage).
    let path = auto_record_file();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(&path, enabled.to_string());
    // Az in-memory calendar-state frissítése (a naptár-poller ezt használja).
    {
        let mut s = state.lock().await;
        s.auto_record = enabled;
    }
    // A bar overlay-nek szólunk, hogy frissítse a meet-joined döntéshez a refet.
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

/// A syscap rendszerhang-helper feloldása: bundle Resources (normál + `_up_`
/// gotcha — ld. Lavox Hub Codesign cert jegyzet) és dev-módú útvonal.
fn find_syscap() -> Option<std::path::PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let macos_dir = exe.parent()?;
    let candidates = [
        macos_dir.join("../Resources/helpers/syscap"),
        macos_dir.join("../Resources/_up_/helpers/syscap"),
        // dev-mód: src-tauri/helpers a cwd-hez képest
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

/// Kézi videó-felvétel a bárból — ugyanaz a gépezet (mic + képernyő +
/// rendszerhang), csak "video" fajtával kerül a registry-be.
#[tauri::command]
fn start_video_record() -> Result<String, String> {
    start_capture_with_kind("video")
}

fn start_capture_with_kind(kind: &str) -> Result<String, String> {
    let mut guard = ACTIVE_RECORDING.lock().map_err(|e| e.to_string())?;
    if guard.is_some() {
        return Err("Már fut egy rögzítés".to_string());
    }
    let rec = recorder::StreamingRecorder::start(selected_mic())?;
    *guard = Some(rec);
    if let Ok(mut kg) = ACTIVE_RECORD_KIND.lock() {
        *kg = Some(kind.to_string());
    }

    // A felvétel könyvtára MÁR INDULÁSKOR a tartós helyen jön létre —
    // a rendszerhang egyből oda íródik, a stop ugyanide teszi a mic-sávot.
    let rec_id = chrono::Local::now().format("%Y%m%d_%H%M%S").to_string();
    let rec_dir = meetings_dir().join(&rec_id);
    let _ = std::fs::create_dir_all(&rec_dir);
    if let Ok(mut mg) = ACTIVE_MEETING_ID.lock() {
        *mg = Some(rec_id);
    }
    if let Ok(mut sg) = ACTIVE_RECORD_STARTED_MS.lock() {
        *sg = Some(chrono::Local::now().timestamp_millis());
    }

    // Rendszerhang-sáv (a meeting TÖBBI résztvevője): syscap helper.
    // Ha nincs meg a helper vagy nincs engedély, mikrofon-only módban megyünk
    // tovább — a stop jelzi vissza, mi került mentésre.
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

/// A meeting-felvételek tartós könyvtára: ~/Documents/Lavox/meetings.
/// (Temp helyett — a temp mappát az újraindítás törölheti.)
/// MIGRÁCIÓ: a korábbi ~/Documents/Hangar mappát első futáskor átnevezzük
/// Lavox-ra, és a JSON-okban tárolt abszolút utakat is átírjuk — így egyetlen
/// régi felvétel/jegyzet-export sem vész el.
pub(crate) fn meetings_dir() -> std::path::PathBuf {
    let docs = dirs_home().join("Documents");
    let new_root = docs.join("Lavox");
    let old_root = docs.join("Hangar");
    static MIGRATE: std::sync::Once = std::sync::Once::new();
    MIGRATE.call_once(|| {
        if old_root.exists() && !new_root.exists() && std::fs::rename(&old_root, &new_root).is_ok() {
            // A tárolt abszolút utak átírása minden .json fájlban (index + capture-ök).
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

/// Egy lezárt meeting-felvétel metaadatai — az index.json bejegyzése.
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
    /// "meeting" vagy "video" — a dashboard ez alapján kategorizál.
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
    let rec = guard.take().ok_or("Nincs aktív rögzítés")?;
    let safe_title = title
        .chars()
        .filter(|c| c.is_alphanumeric() || *c == '-' || *c == '_' || *c == ' ')
        .collect::<String>();
    // Az indításkor létrehozott tartós könyvtár (fallback: friss ts, ha desync).
    let rec_id = ACTIVE_MEETING_ID
        .lock()
        .ok()
        .and_then(|mut g| g.take())
        .unwrap_or_else(|| chrono::Local::now().format("%Y%m%d_%H%M%S").to_string());
    let rec_dir = meetings_dir().join(&rec_id);
    std::fs::create_dir_all(&rec_dir).map_err(|e| e.to_string())?;
    let mic_path = rec_dir.join("mic.wav");
    rec.stop_and_save(&mic_path.to_string_lossy())?;

    // Rendszerhang-helper leállítása: stdin lezárás (EOF) → graceful finalize,
    // 3s türelmi idő után kill.
    let mut system_path: Option<String> = None;
    if let Ok(mut sg) = ACTIVE_SYSCAP.lock() {
        if let Some((mut child, sys_path)) = sg.take() {
            drop(child.stdin.take()); // EOF → a helper lezárja a WAV-ot
            // Video módban a .mov finalizálása +1.2s — bőven ráhagyunk.
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
            // Csak akkor számít, ha tényleges audio került bele (WAV header > 44 byte).
            let ok = std::fs::metadata(&sys_path).map(|m| m.len() > 1000).unwrap_or(false);
            if ok {
                system_path = Some(sys_path);
            } else {
                let _ = std::fs::remove_file(&sys_path);
                dbg("SYSCAP_NO_AUDIO (engedély hiányzik?)");
            }
        }
    }

    // Videó-sáv: csak akkor számít, ha értelmes méretű .mov készült.
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

    // Registry-bejegyzés — a dashboard a bridge-en át innen importál.
    let entry = MeetingRecordEntry {
        id: rec_id.clone(),
        title: if safe_title.trim().is_empty() {
            format!("{} {rec_id}", if kind == "video" { "Videó" } else { "Meeting" })
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

    // Meet CC feliratok (bridge puffer) → captions.json a felvétel mappájába.
    // Relatív másodpercre igazítva a felvétel indulásához; a transzkripció-fúzió
    // (szerver) ebből rendel valódi neveket a whisper-szegmensekhez.
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
                // A captions.json elvesztése némán rontaná a fúziót (névtelen
                // szegmensek), ezért az írás-hibát legalább logoljuk — a meeting
                // maga már mentve van, ezért itt nem bukunk el, csak jelezünk.
                if let Err(e) = std::fs::write(rec_dir.join("captions.json"), json) {
                    dbg(&format!("CAPTIONS_WRITE_FAIL: {e}"));
                }
            }
        }
    }

    // JSON-t adunk vissza — a frontend ebből tudja, mi került mentésre.
    Ok(serde_json::json!({
        "mic": mic_path.to_string_lossy(),
        "system": system_path,
        "video": video_path,
    })
    .to_string())
}

// ---- M5: REMOTE TRANSZKRIPCIÓ + DIARIZÁCIÓ (beszélő-azonosítás) ----

#[tauri::command]
fn get_server_config() -> remote::ServerConfig {
    remote::load_config()
}

#[tauri::command]
fn set_server_config(config: remote::ServerConfig) -> Result<(), String> {
    remote::save_config(&config)
}

/// A webapp onboardingjában megjelenő pároztató kód beváltása. Sikeres
/// beváltás után a szerver-konfig automatikusan a Lavox-felhőre áll
/// (url+api_key+workspace mentve) — a frontendnek nem kell külön
/// set_server_config-ot hívnia, csak a visszakapott configgal frissítenie
/// a saját state-jét.
#[tauri::command]
async fn hub_pair_claim(code: String) -> Result<remote::ServerConfig, String> {
    let cfg = remote::pair_claim(&code).await?;
    remote::save_config(&cfg)?;
    Ok(cfg)
}

/// A lezárt felvételek listája (a meetings/index.json tartalma, legfrissebb elöl).
#[tauri::command]
fn list_meetings() -> Vec<MeetingRecordEntry> {
    let mut v = read_meetings_index();
    v.sort_by(|a, b| b.created_at.cmp(&a.created_at));
    v
}

/// Egy felvétel kész átirata (capture.json), ha már transzkribálva lett.
#[tauri::command]
fn load_capture(rec_id: String) -> Result<Option<CaptureResult>, String> {
    let p = meetings_dir().join(&rec_id).join("capture.json");
    if !p.exists() {
        return Ok(None);
    }
    let s = std::fs::read_to_string(&p).map_err(|e| e.to_string())?;
    serde_json::from_str(&s).map(Some).map_err(|e| e.to_string())
}

/// A (pl. AI-összefoglalóval bővített) capture visszaírása a felvétel mappájába.
#[tauri::command]
fn save_capture(rec_id: String, capture: CaptureResult) -> Result<(), String> {
    let dir = meetings_dir().join(&rec_id);
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let json = serde_json::to_string_pretty(&capture).map_err(|e| e.to_string())?;
    std::fs::write(dir.join("capture.json"), json).map_err(|e| e.to_string())
}

/// Lezárt meeting-felvétel diarizált transzkripciója: a mic + rendszerhang
/// sávot egy 16 kHz mono fájlba keverjük, a szerver átírja és beszélőkhöz
/// rendeli, az eredmény CaptureResult-ként jön vissza (+ capture.json a
/// felvétel mappájában).
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
        .ok_or_else(|| format!("Nincs ilyen felvétel: {rec_id}"))?;

    let rec_dir = meetings_dir().join(&rec_id);
    let mixed_path = rec_dir.join("mixed_16k.wav").to_string_lossy().to_string();
    let mic = entry.mic.clone();
    let system = entry.system.clone();

    // KÉT-SÁVOS mód, ha mindkét sáv megvan: a sávok KÜLÖN mennek fel (16 kHz-re
    // tömörítve) — az "én vs. ők" szétválasztás determinisztikus marad. A kevert
    // mixed_16k.wav továbbra is elkészül, de csak a lejátszáshoz.
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
    // Meet CC feliratok (ha a bridge rögzítette) — a szerver fúzióhoz használja.
    let captions_file = rec_dir.join("captions.json");
    let captions_path = captions_file
        .exists()
        .then(|| captions_file.to_string_lossy().to_string());
    // Két-sávos módban a "file" = a többiek sávja, a "mic_file" = a felvevőé.
    let (audio_arg, mic_arg) = if two_track {
        (sys16_path.clone(), Some(mic16_path.clone()))
    } else {
        (mixed_path.clone(), None)
    };

    // Naptár-résztvevők mint jelölt-pool a név-következtetéshez. A meghívott ≠
    // jelenlévő — a szerver csak megerősítő evidenciával oszt ki nevet ebből.
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

    // A feltöltési másolatok törlése (a mixed_16k.wav marad a lejátszáshoz),
    // és a lejátszási hivatkozás a KEVERT fájlra állítása — a media.audio_path
    // különben a most törölt system_16k.wav-ra mutatna.
    let mut result = result;
    if two_track {
        let _ = std::fs::remove_file(&mic16_path);
        let _ = std::fs::remove_file(&sys16_path);
        result.media.audio_path = mixed_path.clone();
    }

    // A capture.json a KÉSZ átirat egyetlen perzisztens példánya — ha ez az
    // írás némán elbukik, az UI "átirat kész"-t jelez, de a load_capture később
    // üresen tér vissza (eltűnt átirat). Ezért az írás-hibát HIBAKÉNT
    // propagáljuk, hogy a frontend "ÁTIRAT HIBA" értesítést adjon.
    let json = serde_json::to_string_pretty(&result)
        .map_err(|e| format!("capture.json szerializálás: {e}"))?;
    std::fs::write(rec_dir.join("capture.json"), json)
        .map_err(|e| format!("capture.json írás sikertelen ({}): {e}", rec_dir.display()))?;
    Ok(result)
}

/// Beszélő-enrollment: a `record_mic`-kel felvett (vagy tetszőleges) WAV
/// mintát névvel a szerverre töltjük. Azonos név → új minta a profilhoz.
#[tauri::command]
async fn enroll_speaker(wav_path: String, name: String, is_me: bool) -> Result<String, String> {
    let cfg = remote::load_config();
    remote::enroll_speaker(&cfg, &wav_path, &name, is_me).await
}

/// Beszélő ÁTNEVEZÉSE a kész átiratban + opcionális hangtanulás ("tag-once"
/// flow, Otter-minta): a klaszter leghosszabb szegmenseit kivágjuk a kevert
/// hangból, és hangmintaként feltöltjük az új névvel — a következő felvételen
/// az illető már felirat és átnevezés nélkül is felismerhető.
#[tauri::command]
async fn rename_speaker(
    rec_id: String,
    speaker_id: String,
    new_name: String,
    learn: Option<bool>,
) -> Result<CaptureResult, String> {
    let name = new_name.trim().to_string();
    if name.is_empty() {
        return Err("Üres név".to_string());
    }
    let rec_dir = meetings_dir().join(&rec_id);
    let capture_path = rec_dir.join("capture.json");
    let raw = std::fs::read_to_string(&capture_path)
        .map_err(|e| format!("capture.json olvasás: {e}"))?;
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
        return Err(format!("Nincs ilyen beszélő: {speaker_id}"));
    }
    let json = serde_json::to_string_pretty(&capture)
        .map_err(|e| format!("szerializálás: {e}"))?;
    std::fs::write(&capture_path, json).map_err(|e| format!("capture.json írás: {e}"))?;

    // Hangtanulás az átnevezett klaszter beszédéből (kikapcsolható).
    if learn.unwrap_or(true) {
        let audio = capture.media.audio_path.clone();
        // A klaszter szegmensei hossz szerint, a leghosszabbakból max ~30s.
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
                // A tanulási hiba NEM rontja el az átnevezést — csak logoljuk.
                if let Err(e) = remote::enroll_speaker(&cfg, &sample_path, &name, false).await {
                    dbg(&format!("RENAME_HARVEST_FAIL: {e}"));
                }
                let _ = std::fs::remove_file(&sample_path);
            }
            Ok(false) => dbg("RENAME_HARVEST_SKIP: nincs elég beszéd a tanuláshoz"),
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

/// A bar mic-választója: a kiválasztott mikrofon (üres/None → alapértelmezett).
#[tauri::command]
fn set_recording_mic(name: Option<String>) {
    if let Ok(mut g) = SELECTED_MIC.lock() {
        *g = name.filter(|s| !s.trim().is_empty());
    }
}

/// A jelenlegi felvételi mikrofon (None = alapértelmezett).
#[tauri::command]
fn get_recording_mic() -> Option<String> {
    selected_mic()
}

/// Az elérhető kijelzők — "Kijelző N (SZÉLESSÉG×MAGASSÁG)". Az index a syscap
/// `--display` argumentumához illik. Szálbiztos (CoreGraphics, nem main-thread).
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
                    let main = if d.is_main() { " — fő" } else { "" };
                    format!(
                        "Kijelző {} ({}×{}){}",
                        i + 1,
                        b.size.width as i64,
                        b.size.height as i64,
                        main
                    )
                })
                .collect(),
            Err(_) => vec!["Kijelző 1".to_string()],
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        vec!["Kijelző 1".to_string()]
    }
}

/// A bar képernyő-választója: a rögzítendő kijelző indexe (0 = első).
#[tauri::command]
fn set_recording_display(index: usize) {
    SELECTED_DISPLAY.store(index, std::sync::atomic::Ordering::Relaxed);
}

/// A jelenlegi felvételi kijelző indexe.
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

// ---- NYELV-BEÁLLÍTÁS (engedélyezett nyelvek; perzisztens) ----
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
    vec!["en".to_string()] // alapértelmezés: angol (EN-first termék)
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

/// Az engedélyezett nyelvek (ISO kódok, pl. ["hu","en"]).
#[tauri::command]
fn get_languages() -> Vec<String> {
    let mut g = ENABLED_LANGS.lock().unwrap();
    if g.is_empty() {
        *g = load_langs();
    }
    g.clone()
}
/// Az engedélyezett nyelvek beállítása + perzisztálás.
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

// ---- HOTKEY-BEÁLLÍTÁS (diktálás-trigger kombó; perzisztens) ----
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

/// A jelenlegi diktálás-trigger kombó.
#[tauri::command]
fn get_hotkey() -> HotkeyCombo {
    let mut g = HOTKEY_COMBO.lock().unwrap();
    if g.is_none() {
        *g = Some(load_hotkey());
    }
    g.clone().unwrap_or_default()
}

/// A diktálás-trigger kombó beállítása + perzisztálás + ÉLŐ újrakonfigurálás.
#[tauri::command]
fn set_hotkey(combo: HotkeyCombo) {
    save_hotkey(&combo);
    if let Ok(mut g) = HOTKEY_COMBO.lock() {
        *g = Some(combo.clone());
    }
    crate::hotkey::set_active_combo(combo); // a CGEventTap innen olvassa (Task 3-ban él)
}

/// A CGEventTap induló betöltése — a persistált kombó (Fn default), parancs-kontextus nélkül.
pub(crate) fn hotkey_load_for_tap() -> crate::hotkey::HotkeyCombo {
    let mut g = HOTKEY_COMBO.lock().unwrap();
    if g.is_none() {
        *g = Some(load_hotkey());
    }
    g.clone().unwrap_or_default()
}

// ---- SCRATCHPAD (gyors jegyzetek; perzisztens) ----
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
/// A jegyzetek (legújabb elöl).
#[tauri::command]
fn get_notes() -> Vec<Note> {
    let mut g = NOTES.lock().unwrap();
    if g.is_none() {
        *g = Some(load_notes());
    }
    g.clone().unwrap_or_default()
}
/// Minden ablaknak jelez, hogy a jegyzetek változtak (a notebook frissít).
fn emit_notes_changed(app: &tauri::AppHandle) {
    use tauri::Emitter;
    let _ = app.emit("notes-changed", ());
}
/// Új jegyzet hozzáadása (a lista elejére). Üres szöveget nem ment.
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
/// Meglévő jegyzet szövegének frissítése (a notebook szerkesztőjéből).
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
/// Jegyzet kiemelése / kiemelés levétele (pin) — a kiemeltek a lista tetejére.
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
/// Jegyzet törlése id alapján.
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

/// A notebook (jegyzetfüzet) ablak nyitva van-e.
static NOTEBOOK_OPEN: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
/// A notebook a FÓKUSZÁLT ablak-e — EZ dönti el a diktálás-routingot (csak akkor
/// megy jegyzetbe, ha tényleg a notebookban vagyunk; egyébként a kurzorhoz paste).
static NOTEBOOK_FOCUSED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// A notebook-ablak üveg-témáját állítja (világos / sötét / ultra-átlátszó).
/// FŐ SZÁLON kell hívni (NSVisualEffectView + appearance). clear → re-apply.
#[cfg(target_os = "macos")]
fn apply_notebook_glass(win: &tauri::WebviewWindow, theme: &str) {
    use window_vibrancy::{
        apply_vibrancy, clear_vibrancy, NSVisualEffectMaterial, NSVisualEffectState,
    };
    // Korábbi vibrancy-réteg eltávolítása, hogy ne ragadjon be a régi anyag.
    let _ = clear_vibrancy(win);
    let (material, state, appearance) = match theme {
        // Sötét: a Sidebar-anyag sötét megjelenésben → sötét frosted üveg.
        "dark" => (
            NSVisualEffectMaterial::Sidebar,
            NSVisualEffectState::Active,
            tauri::Theme::Dark,
        ),
        // Ultra-átlátszó: HudWindow = valóban áttetsző (a háttér átdereng,
        // sötétítve), fókusz-követő (elkattintáskor világosodik). NEM ad fehér
        // fátylat, mint a világos ablak-háttér anyagok.
        "ultra" => (
            NSVisualEffectMaterial::HudWindow,
            NSVisualEffectState::FollowsWindowActiveState,
            tauri::Theme::Dark,
        ),
        // Világos (alapértelmezett): vibráló, frosted, világos.
        _ => (
            NSVisualEffectMaterial::Sidebar,
            NSVisualEffectState::Active,
            tauri::Theme::Light,
        ),
    };
    let _ = win.set_theme(Some(appearance));
    let _ = apply_vibrancy(win, material, Some(state), Some(10.0));
}

/// A jegyzetfüzet üveg-témájának váltása futásidőben (a ⋯ menüből).
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

/// A bar 📝 gombja ezt hívja: megnyitja (vagy előtérbe hozza) a kis
/// jegyzetfüzet-ablakot (Wispr-stílus). Ugyanazt a scratchpad.json-t használja,
/// így a bar diktálása azonnal megjelenik benne.
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
    // FONTOS: az OS fájl-dropot a webview kezelje (HTML5 onDrop), ne a Tauri-ablak
    // nyelje el → a jegyzetbe húzott kép/fájl beszúrása működjön.
    .disable_drag_drop_handler()
    .build();
    if let Ok(win) = built {
        NOTEBOOK_OPEN.store(true, Ordering::SeqCst);
        NOTEBOOK_FOCUSED.store(true, Ordering::SeqCst);
        // A vibrancy-t NEM itt alkalmazzuk, hanem a frontend hívja a perzisztált
        // témával (set_notebook_glass) mount-kor → egyetlen réteg, nincs beragadás.
        let app2 = app.clone();
        win.on_window_event(move |ev| {
            use tauri::Emitter;
            match ev {
                tauri::WindowEvent::CloseRequested { .. } => {
                    NOTEBOOK_OPEN.store(false, Ordering::SeqCst);
                    NOTEBOOK_FOCUSED.store(false, Ordering::SeqCst);
                    let _ = app2.emit("notebook-closed", ());
                }
                // EZ a kulcs a diktálás-routinghoz: ha a notebook elveszti a
                // fókuszt (másik appba kattintasz), a diktálás ODA megy, nem ide.
                tauri::WindowEvent::Focused(focused) => {
                    NOTEBOOK_FOCUSED.store(*focused, Ordering::SeqCst);
                    let _ = app2.emit("notebook-focus", *focused);
                }
                _ => {}
            }
        });
    }
}
/// A diktálás-routinghoz: nyitva van-e a notebook.
#[tauri::command]
fn is_notebook_open() -> bool {
    NOTEBOOK_OPEN.load(std::sync::atomic::Ordering::SeqCst)
}
/// A diktálás-routinghoz: a notebook a fókuszált ablak-e.
#[tauri::command]
fn is_notebook_focused() -> bool {
    NOTEBOOK_FOCUSED.load(std::sync::atomic::Ordering::SeqCst)
}

/// A kamera-buborék ablak megnyitása a fő kijelző jobb alsó sarkában.
/// Kör alakú, keret nélküli, mindig-felül webview — a getUserMedia kameráját
/// mutatja, amit a képernyőfelvétel PiP-ként rögzít (nincs videó-kompozit).
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

/// A buborékot a fő (primary) kijelző jobb alsó sarkába teszi, 40 px margóval.
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

/// A kamera-buborék ablak bezárása (a kamera-track felszabadul a webview unmountkor).
#[tauri::command]
fn hide_camera_bubble(app: tauri::AppHandle) {
    use tauri::Manager;
    if let Some(w) = app.get_webview_window("camera-bubble") {
        let _ = w.close();
    }
}

/// A Lavox Notes-ban beszúrt kép/fájl megnyitása: a data-URL-t (`data:<mime>;base64,<...>`)
/// egy ideiglenes fájlba dekódoljuk, majd a rendszer alapértelmezett appjával nyitjuk
/// (Előnézet / Finder-alkalmazás / stb.) — az `opener` pluginon keresztül.
#[tauri::command]
fn open_attachment(app: tauri::AppHandle, data_url: String, filename: String) -> Result<(), String> {
    use base64::Engine;
    let comma = data_url.find(',').ok_or("érvénytelen data-URL")?;
    let b64 = &data_url[comma + 1..];
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(b64)
        .map_err(|e| e.to_string())?;
    let dir = std::env::temp_dir().join("lavox-notes-attachments");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    // Egyedi alkönyvtár, hogy azonos nevű fájlok ne írják felül egymást.
    let unique_dir = dir.join(format!("{}", now_millis()));
    std::fs::create_dir_all(&unique_dir).map_err(|e| e.to_string())?;
    let safe_name = if filename.trim().is_empty() { "csatolmány".to_string() } else { filename };
    let path = unique_dir.join(safe_name);
    std::fs::write(&path, bytes).map_err(|e| e.to_string())?;
    use tauri_plugin_opener::OpenerExt;
    app.opener()
        .open_path(path.to_string_lossy().to_string(), None::<&str>)
        .map_err(|e| e.to_string())
}

/// Transcribe a WAV file using whisper.cpp. `model_path` = path to GGML model.
/// Nyelv a beállításból: 1 engedélyezett → azt; több → auto-detect. Ha az
/// angolra-fordítás be van kapcsolva → a forrást auto-detektáljuk + angolra fordít.
#[tauri::command]
fn transcribe_wav(wav_path: String, model_path: String) -> Result<TranscriptResult, String> {
    let langs = get_languages();
    // NINCS fordítás (a turbo modell nem fordít — külön funkció lesz). A diktálás a
    // beállított nyelvre RÖGZÍT: 1 nyelv → arra kényszerít (más nyelvet nem fogad el
    // helyesen); több → auto-detect a beállítottak közül (gyakorlatban en/hu).
    let lang: Option<&str> = if langs.len() == 1 {
        Some(langs[0].as_str())
    } else {
        None
    };
    dbg(&format!("TRANSCRIBE langs={:?} lang={:?}", langs, lang));
    transcribe::transcribe_wav(&wav_path, &model_path, lang, false)
}

// ---- MODELL-LETÖLTÉS (first-run: whisper GGML modell HuggingFace-ről) ----
// A termék-default a large-v3-turbo (jó magyar minőség); ~1,6 GB.
const MODEL_URL: &str =
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin";
const MODEL_FILENAME: &str = "ggml-large-v3-turbo.bin";

/// A felhasználói modell-mappa (app-support) — ide tölt a download_model, és a
/// find_model is keresi. Így a modell NEM az iCloud-szinkronizált repóban él.
fn models_data_dir() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    std::path::PathBuf::from(home)
        .join("Library/Application Support/live.plansmart.hangar/models")
}

/// A whisper-modell letöltése (streaming, progress-eseményekkel).
/// Esemény: "model-download-progress" {downloaded, total, done?}.
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
        .map_err(|e| format!("kapcsolat: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("letöltés hiba: HTTP {}", resp.status()));
    }
    let total = resp.content_length().unwrap_or(0);
    let mut file = tokio::fs::File::create(&tmp).await.map_err(|e| e.to_string())?;
    let mut downloaded: u64 = 0;
    let mut last_emit = std::time::Instant::now();
    while let Some(chunk) = resp
        .chunk()
        .await
        .map_err(|e| format!("letöltés megszakadt: {e}"))?
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
/// Checks: app-support (letöltött), project root (dev), next to exe (bundled),
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
                // A tauri.conf.json "resources": ["../models/*.bin"] a ".."-t NEM
                // tudja simán a Resources/ alá tenni, ezért egy "_up_" almappát
                // hoz létre (Resources/_up_/models/...) — ezt is nézzük.
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
        None => Err("Nem található GGML modell a models/ mappában".to_string()),
    }
}

/// M2: az overlay előhívásához használt globális gyorsbillentyű.
///
/// Alapértelmezés: `CmdOrCtrl+Shift+Space`. A Fn billentyű önmagában NEM köthető
/// a standard global-shortcut API-val macOS-en (hardveres modifier, a
/// `tauri-plugin-global-shortcut` nem látja).
// TODO(M2.1): valódi Fn-key detektálás CGEventTap-pel (natív IOKit/CGEventTap kell).
const OVERLAY_SHORTCUT: &str = "CmdOrCtrl+Shift+Space";

// Az always-on pill kezdő (idle) mérete — pici vonal, hogy ne zavarja a kijelzőt
// (Wispr-stílus). Minden további méretet a frontend ad meg a `set_pill_size`-zal.
const PILL_LINE_W: f64 = 200.0;
const PILL_LINE_H: f64 = 24.0;
// Távolság a képernyő tetejétől. A notch-os MacBookokon a menüsor/notch ~37px
// magas → a pillt EZ ALÁ kell tenni, különben a bevágásban tűnik el.
const PILL_TOP_MARGIN: f64 = 26.0;

/// A kurzor alatti monitort adja vissza (multi-monitor). A Tauri `monitor_from_point`
/// API-ját használjuk, ami BELÜL helyesen kezeli a koordináta-teret — a kézi
/// bounds-matching mixed-scale multi-monitoron hibás (a monitor-pozíciók
/// inkonzisztensen skálázottak).
fn monitor_under_cursor(window: &tauri::WebviewWindow) -> Option<tauri::Monitor> {
    let cursor = window.cursor_position().ok()?;
    window.monitor_from_point(cursor.x, cursor.y).ok().flatten()
}

/// A pill ablakot a FŐ monitor tetejére, vízszintesen KÖZÉPRE pozicionálja a megadott
/// (logikai) szélességgel + magassággal. A felső él fix marad, így a tartalom lefelé nő.
///
/// MEGJEGYZÉS: a fő (primary) monitort használjuk, mert a Tauri a multi-monitor
/// koordinátákat mixed-scale setupon INKONZISZTENSEN skálázza (a non-primary monitor
/// pozíciója a primary scale-jével szorzódik) → a kurzor-monitorra centerelés elcsúszik.
/// A primary (0,0) monitoron a matek helyes → stabil, középre igazított pill.
/// A valódi multi-monitor követés natív (NSScreen) kódot igényel — később.
fn position_pill(window: &tauri::WebviewWindow, width_logical: f64, height_logical: f64) {
    let ni = NOTCH_INFO.lock().ok().and_then(|g| g.clone());
    // IDEIGLENES DEBUG TESZT: kényszerített fallback-pozíció (notch-mód kikapcsolva),
    // hogy kiderüljön, a notch-sáv (y=0, menüsorral átfedő terület) a hibás-e.
    if std::env::var("LAVOX_FORCE_FALLBACK_POS").is_ok() {
        dbg("POSITION_PILL forced fallback (debug)");
    } else
    // NOTCH-MÓD: a notch KÖZEPÉRE igazítunk a friss NSScreen-adatból (NOTCH_INFO),
    // NEM a Tauri primary_monitor()-ára — az elavul monitor le/felcsatoláskor (a régi
    // monitor mérete/offsetje beragad → a pill elcsúszik a notch alól). A notch a
    // primary (built-in) képernyő tetején van, origó (0,0).
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
    // FALLBACK (nincs notch): a monitor közepére, a menüsor alá.
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

/// Műszerezés: időbélyeges sor a /tmp/lavox-ptt.log-ba (a push-to-talk pipeline
/// elemzéséhez — keydown/keyup, felvétel, transzkripció, beillesztés latenciája).
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

/// A frontend ezzel logol pipeline-lépéseket (start/stop/transzkripció/beillesztés).
#[tauri::command]
fn dbg_log(msg: String) {
    dbg(&msg);
}

/// M2.2: az átirat beillesztése az aktív app kurzorához (vágólap + Cmd+V). A frontend
/// hívja, amikor kész a diktálás. Az overlay nem lopja a fókuszt, így a célapp kapja.
#[tauri::command]
fn insert_text(text: String) -> Result<(), String> {
    let target = DICTATION_TARGET_APP
        .lock()
        .ok()
        .and_then(|t| t.clone());
    inject::insert_text(&text, target.as_deref())
}

/// A pill ablak átméretezése a frontend által megadott (logikai) mérethez, a
/// képernyő tetejéhez igazítva. A frontend ezzel vezérli a pici-vonal ↔ hover ↔
/// kinyitott állapotokat — így minden további méret-finomítás hot-reload, nincs
/// több Rust build.
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

    // CmdOrCtrl+Shift+Space — macOS-en Super (Cmd), máshol Ctrl.
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
                    // A diktálás-trigger a hotkey.rs CGEventTap-jén fut (konfigurálható,
                    // Fn default). Ez a handler MÁR NEM indít diktálást — csak
                    // diagnosztikai log marad, hogy ne legyen dupla/ütköző trigger.
                    if shortcut == &overlay_shortcut {
                        dbg(&format!("LEGACY_GS_EVENT state={:?}", event.state));
                    }
                })
                .build(),
        )
        .setup(move |app| {
            use tauri::Manager;
            // PUSH-TO-TALK: a megbízható billentyű-figyelő (CGEventTap) indítása —
            // ez látja közvetlenül a ⌘⇧Space le/felmenetelét (Discord/Wispr-módszer),
            // a Global Shortcut megbízhatatlan Released-je helyett. Input Monitoring kell.
            hotkey::start(app.handle().clone());
            // NOTCH-DETEKTÁLÁS (a fő szálon — NSScreen main-thread-only). A frontend
            // ehhez igazítja a Dynamic Island-stílusú compact layoutot.
            let ni = notch::detect();
            dbg(&format!(
                "NOTCH has={} h={:.0} left={:.0} right={:.0} screenW={:.0} scale={:.1}",
                ni.has_notch, ni.notch_height, ni.notch_left, ni.notch_right, ni.screen_width, ni.scale
            ));
            if let Ok(mut g) = NOTCH_INFO.lock() {
                *g = Some(ni);
            }
            // ESEMÉNY-VEZÉRELT notch-frissítés: a kijelző-átkonfigurálás (monitor
            // csatlakozik/lecsatlakozik, felbontás/skálázás vált) callbackjét regisztráljuk
            // → a bar magától korrigál, NINCS polling.
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
            // ALWAYS-INTERACTIVE OVERLAY (Wispr-réteg): az overlay ablakot
            // non-activating NSPanel-lé alakítjuk → hover-re kinyílik AKKOR IS, ha
            // másik app van fókuszban, és nem lopja el a fókuszt. Minden téren látszik.
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
                    // A menüsor (level 24) FÖLÉ, hogy a pill mindig látszódjon.
                    #[allow(non_upper_case_globals)]
                    const NSStatusWindowLevel: i32 = 25;
                    panel.set_level(NSStatusWindowLevel);
                    // NonActivatingPanel → fogadja az egér-hovert/kattintást anélkül,
                    // hogy aktiválná az appot (nem lopja el a fókuszt).
                    #[allow(non_upper_case_globals)]
                    const NSWindowStyleMaskNonActivatingPanel: i32 = 1 << 7;
                    panel.set_style_mask(NSWindowStyleMaskNonActivatingPanel);
                    // Minden space-en + fullscreen app fölött is látszik.
                    panel.set_collection_behaviour(
                        NSWindowCollectionBehavior::NSWindowCollectionBehaviorCanJoinAllSpaces
                            | NSWindowCollectionBehavior::NSWindowCollectionBehaviorFullScreenAuxiliary,
                    );
                    // KULCS: az NSPanel alapból ELREJTŐZIK, ha az app háttérbe kerül
                    // → ki kell kapcsolni, hogy a pill MINDIG látszódjon (mint a Wispr).
                    panel.set_hides_on_deactivate(false);
                    // Hover-események akkor is, ha másik app van fókuszban.
                    panel.set_accepts_mouse_moved_events(true);
                    // Ne ragadja meg a billentyű-fókuszt, csak ha tényleg kell.
                    panel.set_becomes_key_only_if_needed(true);
                    // NINCS ablak-árnyék → eltűnik a csúnya téglalap-„keret" a
                    // glass körül (az NSPanel alapból vet egy ablak-árnyékot).
                    panel.set_has_shadow(false);
                    panel.show();
                    dbg(&format!("PANEL_VISIBLE={}", panel.is_visible()));
                } else {
                    dbg("PANEL_CONVERT_FAIL");
                }
            }
            position_pill(&overlay, PILL_LINE_W, PILL_LINE_H);
            // M4: Calendar poller indítása háttérben.
            calendar::start_polling(app.handle().clone(), cal_state.clone());
            // Felhő-párosítás: heartbeat-loop indítása háttérben. Csendben
            // kihagyja magát, amíg nincs eszköz-token (self-hosted mód).
            remote::start_heartbeat_loop();
            // LAVOX Meet Bridge: a Chrome extension jelzi ide (127.0.0.1:5192),
            // amikor a user belép/kilép egy Google Meet-be → pill felvétel-prompt.
            bridge::start(app.handle().clone());

            // HOVER FÓKUSZ NÉLKÜL (Wispr-élmény): a WKWebView háttérben nem kapja a
            // DOM hover-eseményeket, ezért NATÍVAN pollozzuk a globális kurzor-pozíciót
            // az overlay ablak keretéhez képest. Be-/kilépéskor "pill-hover" eseményt
            // küldünk → a frontend kinyit/csuk, akkor is, ha másik app van fókuszban.
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
