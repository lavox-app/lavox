// LAVOX Meet Bridge — localhost HTTP listener for Chrome extension signals.
// The extension POSTs here from the meet.google.com tab when the user joins/
// leaves a meeting → the overlay pill reacts instantly (record prompt / auto-rec).
//
// Security: listens ONLY on 127.0.0.1 and enforces an Origin allowlist:
//  - recording endpoints (/lavox/recordings*) → only the known dashboard origins
//  - /lavox/meeting → dashboard origins + chrome-extension:// (the Meet
//    extension's service worker POSTs from there)
//  - unknown browser Origin → 403 without CORS headers (so JS in another open
//    tab cannot read out the meeting audio recordings)
//  - requests without an Origin (non-browser) pass: a local process can read
//    the files on disk anyway; the vector to defend against is the browser.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use tauri::Emitter;

const PORT: u16 = 5192;

/// Time (unix ms) and kind of the last extension event — Settings uses this to
/// show the user whether the Meet extension is actually talking to the app.
static LAST_EVENT_MS: std::sync::atomic::AtomicI64 = std::sync::atomic::AtomicI64::new(0);
static LAST_EVENT_KIND: std::sync::Mutex<String> = std::sync::Mutex::new(String::new());

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// Read by the Settings meeting-connection panel: when the last event arrived and its kind.
pub fn last_event() -> (i64, String) {
    let kind = LAST_EVENT_KIND.lock().map(|g| g.clone()).unwrap_or_default();
    (LAST_EVENT_MS.load(std::sync::atomic::Ordering::Relaxed), kind)
}

/// Dashboard origins that may also access the recording endpoints.
const WEB_ORIGINS: &[&str] = &["http://localhost:5190", "http://127.0.0.1:5190"];

fn is_web_origin(origin: &str) -> bool {
    WEB_ORIGINS.contains(&origin)
        || origin == "https://lavox.app"
        || (origin.starts_with("https://") && origin.ends_with(".lavox.app"))
}

/// Whether the given Origin may access the given path. `None` = no Origin
/// header (not from a browser) → allow, no CORS headers needed.
fn origin_allowed(origin: Option<&str>, path: &str) -> bool {
    let Some(o) = origin else { return true };
    if is_web_origin(o) {
        return true;
    }
    // The extension's service worker may send meeting events and captions.
    o.starts_with("chrome-extension://") && (path == "/lavox/meeting" || path == "/lavox/captions")
}

/// Live buffer of Meet CC captions — the extension fills it continuously,
/// stop_meeting_record drains it into the recording's folder (for fusion).
#[derive(serde::Deserialize, serde::Serialize, Clone, Debug)]
pub struct CaptionEvent {
    /// Unix epoch ms (per the extension's clock).
    pub t: i64,
    #[serde(default, rename = "type")]
    pub kind: String, // "caption" | "active-speaker"
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub text: String,
}

#[derive(serde::Deserialize, Debug)]
struct CaptionBatch {
    #[serde(default, rename = "meetCode")]
    _meet_code: String,
    #[serde(default)]
    events: Vec<CaptionEvent>,
}

static CAPTION_BUFFER: std::sync::Mutex<Vec<CaptionEvent>> = std::sync::Mutex::new(Vec::new());
const CAPTION_BUFFER_CAP: usize = 40_000;

/// Takes the accumulated captions (the caller filters them to a time window).
pub fn drain_captions() -> Vec<CaptionEvent> {
    CAPTION_BUFFER.lock().map(|mut g| std::mem::take(&mut *g)).unwrap_or_default()
}

#[derive(serde::Deserialize, serde::Serialize, Clone, Debug)]
pub struct MeetBridgeEvent {
    pub event: String, // "joined" | "left"
    #[serde(default)]
    pub meet_code: Option<String>,
    #[serde(rename = "meetCode", default)]
    pub meet_code_camel: Option<String>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub participants: Vec<String>,
}

pub fn start(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let listener = match TcpListener::bind(("127.0.0.1", PORT)) {
            Ok(l) => l,
            Err(e) => {
                eprintln!("[bridge] failed to bind port {PORT}: {e}");
                return;
            }
        };
        println!("[bridge] LAVOX meet bridge running on 127.0.0.1:{PORT}");

        for stream in listener.incoming() {
            let Ok(stream) = stream else { continue };
            let app = app.clone();
            // Requests are small and rare — handle each on its own thread, simply.
            std::thread::spawn(move || {
                let _ = handle(stream, &app);
            });
        }
    });
}

fn handle(stream: std::net::TcpStream, app: &tauri::AppHandle) -> std::io::Result<()> {
    stream.set_read_timeout(Some(std::time::Duration::from_secs(5)))?;
    let mut reader = BufReader::new(stream.try_clone()?);

    // Request line: "POST /lavox/meeting HTTP/1.1"
    let mut request_line = String::new();
    reader.read_line(&mut request_line)?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("");
    let path = parts.next().unwrap_or("");

    // Headers: Content-Length + Origin.
    let mut content_length = 0usize;
    let mut origin: Option<String> = None;
    loop {
        let mut line = String::new();
        reader.read_line(&mut line)?;
        let l = line.trim();
        if l.is_empty() {
            break;
        }
        let lower = l.to_ascii_lowercase();
        if let Some(v) = lower.strip_prefix("content-length:") {
            content_length = v.trim().parse().unwrap_or(0);
        }
        if lower.starts_with("origin:") {
            origin = Some(l["origin:".len()..].trim().to_string());
        }
    }

    let mut out = stream;

    // Origin allowlist: an unknown browser origin gets access to nothing.
    if !origin_allowed(origin.as_deref(), path) {
        return respond(&mut out, 403, "forbidden", None);
    }
    // From here on the Origin is allowed → echo it in the CORS header.
    let cors = origin.as_deref();

    // CORS preflight — sent by the content script's / dashboard's fetch.
    if method == "OPTIONS" {
        return respond(&mut out, 204, "", cors);
    }

    // ── Serving recordings to the dashboard ──────────────────────
    // GET /lavox/recordings                     → index.json (list)
    // GET /lavox/recordings/<id>/mic.wav        → mic track
    // GET /lavox/recordings/<id>/system.wav     → system-audio track
    // POST /lavox/recordings/<id>/imported      → mark as imported
    if method == "GET" && path == "/lavox/recordings" {
        let list = crate::read_meetings_index();
        return respond_json(&mut out, &serde_json::to_string(&list).unwrap_or_else(|_| "[]".into()), cors);
    }
    if let Some(rest) = path.strip_prefix("/lavox/recordings/") {
        let mut segs = rest.split('/');
        let id = segs.next().unwrap_or("");
        let file = segs.next().unwrap_or("");
        // Path-traversal protection: the id may only come from the registry.
        let valid = crate::read_meetings_index().into_iter().any(|e| e.id == id);
        if !valid || id.contains("..") || file.contains("..") {
            return respond(&mut out, 404, "not found", cors);
        }
        if method == "GET" && (file == "mic.wav" || file == "system.wav" || file == "screen.mov") {
            let p = crate::meetings_dir().join(id).join(file);
            let ctype = if file == "screen.mov" { "video/quicktime" } else { "audio/wav" };
            return match std::fs::read(&p) {
                Ok(bytes) => respond_bytes(&mut out, ctype, &bytes, cors),
                Err(_) => respond(&mut out, 404, "no such track", cors),
            };
        }
        if method == "POST" && file == "imported" {
            let mut index = crate::read_meetings_index();
            for e in index.iter_mut() {
                if e.id == id {
                    e.imported = true;
                }
            }
            crate::write_meetings_index(&index);
            return respond(&mut out, 200, "ok", cors);
        }
        return respond(&mut out, 404, "not found", cors);
    }

    // ── Meet CC captions from the extension — live buffer for fusion ──
    if method == "POST" && path == "/lavox/captions" {
        let mut body = vec![0u8; content_length.min(262_144)];
        reader.read_exact(&mut body)?;
        match serde_json::from_slice::<CaptionBatch>(&body) {
            Ok(batch) => {
                LAST_EVENT_MS.store(now_ms(), std::sync::atomic::Ordering::Relaxed);
                if let Ok(mut g) = CAPTION_BUFFER.lock() {
                    g.extend(batch.events);
                    let len = g.len();
                    if len > CAPTION_BUFFER_CAP {
                        g.drain(..len - CAPTION_BUFFER_CAP);
                    }
                }
                return respond(&mut out, 200, "ok", cors);
            }
            Err(_) => return respond(&mut out, 400, "bad json", cors),
        }
    }

    if method != "POST" || path != "/lavox/meeting" {
        return respond(&mut out, 404, "not found", cors);
    }

    // Body (max 64 KB — comfortably fits the participants list too).
    let mut body = vec![0u8; content_length.min(65536)];
    reader.read_exact(&mut body)?;

    match serde_json::from_slice::<MeetBridgeEvent>(&body) {
        Ok(ev) => {
            let event_name = match ev.event.as_str() {
                "joined" => "meet-joined",
                "left" => "meet-left",
                _ => return respond(&mut out, 400, "unknown event", cors),
            };
            // Update connection status for the Settings panel.
            LAST_EVENT_MS.store(now_ms(), std::sync::atomic::Ordering::Relaxed);
            if let Ok(mut g) = LAST_EVENT_KIND.lock() {
                *g = ev.event.clone();
            }
            let payload = serde_json::json!({
                "meetCode": ev.meet_code_camel.or(ev.meet_code).unwrap_or_default(),
                "title": ev.title.unwrap_or_default(),
                "participants": ev.participants,
            });
            if let Some(overlay) = tauri::Manager::get_webview_window(app, "overlay") {
                let _ = overlay.emit(event_name, payload);
            }
            respond(&mut out, 200, "ok", cors)
        }
        Err(_) => respond(&mut out, 400, "bad json", cors),
    }
}

/// CORS headers: only for an allowed Origin, echoing it — no wildcard.
fn cors_headers(cors: Option<&str>) -> String {
    match cors {
        Some(o) => format!(
            "Access-Control-Allow-Origin: {o}\r\n\
             Vary: Origin\r\n\
             Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n\
             Access-Control-Allow-Headers: Content-Type\r\n"
        ),
        None => String::new(),
    }
}

fn respond_json(stream: &mut std::net::TcpStream, json: &str, cors: Option<&str>) -> std::io::Result<()> {
    let resp = format!(
        "HTTP/1.1 200 OK\r\n\
         {}Content-Type: application/json\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\r\n{json}",
        cors_headers(cors),
        json.len()
    );
    stream.write_all(resp.as_bytes())
}

fn respond_bytes(stream: &mut std::net::TcpStream, content_type: &str, bytes: &[u8], cors: Option<&str>) -> std::io::Result<()> {
    let header = format!(
        "HTTP/1.1 200 OK\r\n\
         {}Content-Type: {content_type}\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\r\n",
        cors_headers(cors),
        bytes.len()
    );
    stream.write_all(header.as_bytes())?;
    stream.write_all(bytes)
}

fn respond(stream: &mut std::net::TcpStream, code: u16, body: &str, cors: Option<&str>) -> std::io::Result<()> {
    let status = match code {
        200 => "200 OK",
        204 => "204 No Content",
        400 => "400 Bad Request",
        403 => "403 Forbidden",
        _ => "404 Not Found",
    };
    let resp = format!(
        "HTTP/1.1 {status}\r\n\
         {}Content-Type: text/plain\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\r\n{body}",
        cors_headers(cors),
        body.len()
    );
    stream.write_all(resp.as_bytes())
}
