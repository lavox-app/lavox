// LAVOX Meet Bridge — localhost HTTP listener a Chrome extension jelzéseihez.
// Az extension a meet.google.com tabról POST-ol ide, amikor a user belép/kilép
// egy meetingből → az overlay pill azonnal reagál (felvétel-prompt / auto-rec).
//
// Biztonság: CSAK 127.0.0.1-en hallgat, és Origin-allowlistet kényszerít:
//  - felvétel-végpontok (/lavox/recordings*) → csak az ismert dashboard-originek
//  - /lavox/meeting → dashboard-originek + chrome-extension:// (a Meet-extension
//    service workere innen POST-ol)
//  - ismeretlen browser-Origin → 403, CORS-header nélkül (más nyitott tab JS-e
//    így nem tudja kiolvasni a meeting-hangfelvételeket)
//  - Origin nélküli (nem-böngésző) kérés átmegy: lokális process amúgy is eléri
//    a fájlokat a lemezen, a védendő vektor a böngésző.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use tauri::Emitter;

const PORT: u16 = 5192;

/// Az utolsó bővítmény-esemény ideje (unix ms) és típusa — a Settings ebből
/// mutatja a felhasználónak, hogy a Meet-bővítmény tényleg beszél-e az appal.
static LAST_EVENT_MS: std::sync::atomic::AtomicI64 = std::sync::atomic::AtomicI64::new(0);
static LAST_EVENT_KIND: std::sync::Mutex<String> = std::sync::Mutex::new(String::new());

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// A Settings meeting-kapcsolat panele olvassa: mikor jött utoljára esemény és milyen.
pub fn last_event() -> (i64, String) {
    let kind = LAST_EVENT_KIND.lock().map(|g| g.clone()).unwrap_or_default();
    (LAST_EVENT_MS.load(std::sync::atomic::Ordering::Relaxed), kind)
}

/// Dashboard-originek, amelyek a felvétel-végpontokat is elérhetik.
const WEB_ORIGINS: &[&str] = &["http://localhost:5190", "http://127.0.0.1:5190"];

fn is_web_origin(origin: &str) -> bool {
    WEB_ORIGINS.contains(&origin)
        || origin == "https://lavox.app"
        || (origin.starts_with("https://") && origin.ends_with(".lavox.app"))
}

/// Az adott Origin elérheti-e az adott útvonalat. `None` = nincs Origin header
/// (nem böngészőből jön) → engedjük, CORS-header nem kell.
fn origin_allowed(origin: Option<&str>, path: &str) -> bool {
    let Some(o) = origin else { return true };
    if is_web_origin(o) {
        return true;
    }
    // Az extension service workere meeting-eseményt és feliratokat küldhet.
    o.starts_with("chrome-extension://") && (path == "/lavox/meeting" || path == "/lavox/captions")
}

/// A Meet CC feliratok élő puffere — az extension folyamatosan tölti,
/// a stop_meeting_record üríti és írja a felvétel mappájába (fúzióhoz).
#[derive(serde::Deserialize, serde::Serialize, Clone, Debug)]
pub struct CaptionEvent {
    /// Unix epoch ms (az extension órája szerint).
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

/// A felgyűlt feliratok kivétele (a hívó szűri idő-ablakra).
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
                eprintln!("[bridge] nem sikerült bindolni a {PORT} portra: {e}");
                return;
            }
        };
        println!("[bridge] LAVOX meet bridge fut: 127.0.0.1:{PORT}");

        for stream in listener.incoming() {
            let Ok(stream) = stream else { continue };
            let app = app.clone();
            // Egy-egy kérés kicsi és ritka — szálanként kezeljük, egyszerűen.
            std::thread::spawn(move || {
                let _ = handle(stream, &app);
            });
        }
    });
}

fn handle(stream: std::net::TcpStream, app: &tauri::AppHandle) -> std::io::Result<()> {
    stream.set_read_timeout(Some(std::time::Duration::from_secs(5)))?;
    let mut reader = BufReader::new(stream.try_clone()?);

    // Kérő sor: "POST /lavox/meeting HTTP/1.1"
    let mut request_line = String::new();
    reader.read_line(&mut request_line)?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("");
    let path = parts.next().unwrap_or("");

    // Fejlécek: Content-Length + Origin.
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

    // Origin-allowlist: ismeretlen böngésző-origin semmit nem érhet el.
    if !origin_allowed(origin.as_deref(), path) {
        return respond(&mut out, 403, "forbidden", None);
    }
    // Innentől az Origin engedélyezett → ezt echózzuk a CORS-headerben.
    let cors = origin.as_deref();

    // CORS preflight — a content script / dashboard fetch-e küldi.
    if method == "OPTIONS" {
        return respond(&mut out, 204, "", cors);
    }

    // ── Felvétel-kiszolgálás a dashboardnak ──────────────────────
    // GET /lavox/recordings                     → index.json (lista)
    // GET /lavox/recordings/<id>/mic.wav        → mic sáv
    // GET /lavox/recordings/<id>/system.wav     → rendszerhang sáv
    // POST /lavox/recordings/<id>/imported      → importáltnak jelölés
    if method == "GET" && path == "/lavox/recordings" {
        let list = crate::read_meetings_index();
        return respond_json(&mut out, &serde_json::to_string(&list).unwrap_or_else(|_| "[]".into()), cors);
    }
    if let Some(rest) = path.strip_prefix("/lavox/recordings/") {
        let mut segs = rest.split('/');
        let id = segs.next().unwrap_or("");
        let file = segs.next().unwrap_or("");
        // Path-traversal védelem: az id csak a registry-ből jöhet.
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

    // ── Meet CC feliratok az extensionből — élő puffer a fúzióhoz ──
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

    // Test (max 64 KB — a participants lista is belefér bőven).
    let mut body = vec![0u8; content_length.min(65536)];
    reader.read_exact(&mut body)?;

    match serde_json::from_slice::<MeetBridgeEvent>(&body) {
        Ok(ev) => {
            let event_name = match ev.event.as_str() {
                "joined" => "meet-joined",
                "left" => "meet-left",
                _ => return respond(&mut out, 400, "unknown event", cors),
            };
            // Kapcsolat-állapot frissítése a Settings-panelhez.
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

/// CORS-headerek: csak engedélyezett Origin esetén, azt echózva — wildcard nincs.
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
