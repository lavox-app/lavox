//! Google OAuth natív (desktop) alkalmazáshoz — RFC 8252 loopback + PKCE.
//!
//! MIÉRT NEM a böngészős Google Identity Services (GIS):
//! a korábbi megoldás az `index.html`-ből töltötte a `accounts.google.com/gsi/client`
//! scriptet, amit a Tauri CSP (`script-src 'self'`) csomagolt appban BLOKKOL →
//! `window.google` undefined, a bejelentkezés némán elhasalt. Ráadásul a GIS
//! token-kliense csak 1 órás access tokent ad, refresh token NÉLKÜL — vagyis a
//! naptár-poller óránként némán leállt.
//!
//! EZ A MEGOLDÁS: a rendszer-böngészőben nyitjuk a Google hozzájárulási oldalát,
//! egy eldobható `127.0.0.1:<port>` HTTP-figyelő fogja el a redirectet, majd a
//! kódot access+refresh tokenre váltjuk. A refresh tokent lemezre mentjük, így a
//! naptár lejárat után is magától újraéled.
//!
//! Beállítás: Google Cloud Console → a projekt → OAuth kliens, típus **Desktop
//! app**. Az ID/secret ide kerül (a desktop-kliens secretje az RFC 8252 szerint
//! NEM bizalmas — a natív appból úgysem tartható titokban).

use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::net::TcpListener;

const AUTH_ENDPOINT: &str = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_ENDPOINT: &str = "https://oauth2.googleapis.com/token";
const SCOPES: &str = "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/userinfo.email";

/// A Desktop-app OAuth kliens adatai. A nevek egyeznek az Infisicalban
/// tárolt kulcsokkal (`LAVOX_DESKTOP_CLIENT_ID` / `..._SECRET`), így a build
/// `infisical run -- ./build-and-install.command` alakban közvetlenül megkapja
/// őket — a titok sosem kerül a repóba és a shell-előzménybe.
///
/// `option_env!` = fordításkor beégetve (a csomagolt app így önállóan működik),
/// `std::env::var` = futásidejű felülbírálás fejlesztéshez. A build.rs
/// `rerun-if-env-changed` direktívái gondoskodnak róla, hogy a cargo NE
/// használjon állott cache-t, ha a kulcs változik.
fn client_id() -> Option<String> {
    option_env!("LAVOX_DESKTOP_CLIENT_ID")
        .map(|s| s.to_string())
        .or_else(|| std::env::var("LAVOX_DESKTOP_CLIENT_ID").ok())
        .filter(|s| !s.trim().is_empty())
}

fn client_secret() -> Option<String> {
    option_env!("LAVOX_DESKTOP_CLIENT_SECRET")
        .map(|s| s.to_string())
        .or_else(|| std::env::var("LAVOX_DESKTOP_CLIENT_SECRET").ok())
        .filter(|s| !s.trim().is_empty())
}

pub fn configured() -> bool {
    client_id().is_some() && client_secret().is_some()
}

// ---------------------------------------------------------------------------
// Perzisztens token-tár (refresh token itt él túl az újraindítást)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct StoredTokens {
    pub refresh_token: String,
    pub access_token: String,
    /// epoch millis
    pub expires_at: i64,
    pub email: Option<String>,
}

fn token_file() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    std::path::PathBuf::from(home)
        .join("Library/Application Support/live.plansmart.hangar/google-oauth.json")
}

pub fn load_tokens() -> Option<StoredTokens> {
    std::fs::read_to_string(token_file())
        .ok()
        .and_then(|s| serde_json::from_str::<StoredTokens>(&s).ok())
        .filter(|t| !t.refresh_token.is_empty())
}

pub fn save_tokens(t: &StoredTokens) -> Result<(), String> {
    let path = token_file();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let json = serde_json::to_string_pretty(t).map_err(|e| e.to_string())?;
    std::fs::write(&path, json).map_err(|e| e.to_string())?;
    // A refresh token hosszú életű hitelesítő adat — csak a tulajdonos olvashassa.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

pub fn clear_tokens() {
    let _ = std::fs::remove_file(token_file());
}

// ---------------------------------------------------------------------------
// PKCE
// ---------------------------------------------------------------------------

/// Kriptográfiai minőségű véletlen bájtok az OS-től (nincs külön rand crate).
fn random_bytes(n: usize) -> Vec<u8> {
    use std::fs::File;
    let mut buf = vec![0u8; n];
    if let Ok(mut f) = File::open("/dev/urandom") {
        if f.read_exact(&mut buf).is_ok() {
            return buf;
        }
    }
    // Elvi fallback: /dev/urandom macOS-en mindig elérhető, ide nem jutunk el.
    buf.iter_mut()
        .enumerate()
        .for_each(|(i, b)| *b = (i as u8).wrapping_mul(31));
    buf
}

fn b64url(data: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(data)
}

/// SHA-256 a `security` CLI-vel? Nem — saját, függőségmentes implementáció,
/// hogy ne kelljen új crate a fába csak a PKCE-challenge miatt.
fn sha256(data: &[u8]) -> [u8; 32] {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let mut msg = data.to_vec();
    let bit_len = (data.len() as u64) * 8;
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());

    for chunk in msg.chunks(64) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([chunk[i * 4], chunk[i * 4 + 1], chunk[i * 4 + 2], chunk[i * 4 + 3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let (mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh) =
            (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]);
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }
    let mut out = [0u8; 32];
    for (i, v) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&v.to_be_bytes());
    }
    out
}

fn urlencode(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (b as char).to_string()
            }
            _ => format!("%{:02X}", b),
        })
        .collect()
}

// ---------------------------------------------------------------------------
// A bejelentkezési folyamat
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
    #[serde(default)]
    refresh_token: Option<String>,
    expires_in: i64,
}

/// A böngészőből visszaérkező redirect elfogása egy eldobható loopback-figyelővel.
/// Blokkoló hívás — a hívó `spawn_blocking`-ban futtatja.
fn wait_for_code(listener: TcpListener, expected_state: &str) -> Result<String, String> {
    // Ne lógjon örökké, ha a user bezárja a böngészőt: 3 perc türelmi idő.
    listener
        .set_nonblocking(false)
        .map_err(|e| format!("listener: {e}"))?;
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(180);

    for stream in listener.incoming() {
        if std::time::Instant::now() > deadline {
            return Err("Időtúllépés — a bejelentkezés nem fejeződött be.".into());
        }
        let mut stream = match stream {
            Ok(s) => s,
            Err(_) => continue,
        };
        let mut buf = [0u8; 4096];
        let n = stream.read(&mut buf).map_err(|e| format!("olvasás: {e}"))?;
        let req = String::from_utf8_lossy(&buf[..n]);
        // "GET /?code=...&state=... HTTP/1.1"
        let first = req.lines().next().unwrap_or_default();
        let path = first.split_whitespace().nth(1).unwrap_or_default();
        let query = path.split('?').nth(1).unwrap_or_default();

        let mut code = String::new();
        let mut state = String::new();
        let mut err = String::new();
        for pair in query.split('&') {
            let mut it = pair.splitn(2, '=');
            let k = it.next().unwrap_or_default();
            let v = it.next().unwrap_or_default();
            match k {
                "code" => code = v.to_string(),
                "state" => state = v.to_string(),
                "error" => err = v.to_string(),
                _ => {}
            }
        }

        let body = if !err.is_empty() {
            "<h2>A bejelentkezes megszakadt.</h2><p>Visszaterhetsz a Lavox Hubhoz.</p>"
        } else if code.is_empty() {
            "<h2>Hianyzo kod.</h2><p>Probald ujra a Lavox Hubbol.</p>"
        } else {
            "<h2>Kesz!</h2><p>Visszaterhetsz a Lavox Hubhoz — ez az ablak bezarhato.</p>"
        };
        let resp = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        );
        let _ = stream.write_all(resp.as_bytes());
        let _ = stream.flush();

        if !err.is_empty() {
            return Err(format!("Google hiba: {err}"));
        }
        if code.is_empty() {
            continue;
        }
        // CSRF-védelem: a visszakapott state egyezzen azzal, amit küldtünk.
        if state != expected_state {
            return Err("A state nem egyezik — a válasz nem tőlünk származik.".into());
        }
        return Ok(code);
    }
    Err("A bejelentkezés nem fejeződött be.".into())
}

/// Teljes bejelentkezés: böngésző-megnyitás → kód elfogása → token-csere.
pub async fn login(app: &tauri::AppHandle) -> Result<StoredTokens, String> {
    let cid = client_id().ok_or(
        "Nincs beállítva a Google Desktop OAuth kliens (LAVOX_GOOGLE_CLIENT_ID).",
    )?;
    let csecret = client_secret().ok_or(
        "Nincs beállítva a Google Desktop OAuth kliens titka (LAVOX_GOOGLE_CLIENT_SECRET).",
    )?;

    // A loopback figyelő EFEMER porton — a Google desktop-kliensnél bármelyik
    // 127.0.0.1 port elfogadott, nem kell előre regisztrálni.
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| format!("port: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("port: {e}"))?
        .port();
    let redirect_uri = format!("http://127.0.0.1:{port}");

    let verifier = b64url(&random_bytes(64));
    let challenge = b64url(&sha256(verifier.as_bytes()));
    let state = b64url(&random_bytes(24));

    let auth_url = format!(
        "{AUTH_ENDPOINT}?client_id={}&redirect_uri={}&response_type=code&scope={}&code_challenge={}&code_challenge_method=S256&state={}&access_type=offline&prompt=consent",
        urlencode(&cid),
        urlencode(&redirect_uri),
        urlencode(SCOPES),
        urlencode(&challenge),
        urlencode(&state),
    );

    // A rendszer-böngészőben nyitjuk (nem beágyazott webview — a Google tiltja).
    {
        use tauri_plugin_opener::OpenerExt;
        app.opener()
            .open_url(auth_url.clone(), None::<&str>)
            .map_err(|e| format!("böngésző-megnyitás: {e}"))?;
    }

    let state_for_wait = state.clone();
    let code = tauri::async_runtime::spawn_blocking(move || wait_for_code(listener, &state_for_wait))
        .await
        .map_err(|e| format!("join: {e}"))??;

    // Kód → access + refresh token
    let client = reqwest::Client::new();
    let resp = client
        .post(TOKEN_ENDPOINT)
        .form(&[
            ("client_id", cid.as_str()),
            ("client_secret", csecret.as_str()),
            ("code", code.as_str()),
            ("code_verifier", verifier.as_str()),
            ("grant_type", "authorization_code"),
            ("redirect_uri", redirect_uri.as_str()),
        ])
        .send()
        .await
        .map_err(|e| format!("token-csere: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("token-csere sikertelen ({status}): {body}"));
    }
    let tr: TokenResponse = resp
        .json()
        .await
        .map_err(|e| format!("token-válasz parse: {e}"))?;

    let email = fetch_email(&tr.access_token).await;
    let tokens = StoredTokens {
        refresh_token: tr.refresh_token.unwrap_or_default(),
        access_token: tr.access_token,
        expires_at: chrono::Utc::now().timestamp_millis() + tr.expires_in * 1000,
        email,
    };
    save_tokens(&tokens)?;
    Ok(tokens)
}

/// Lejárt access token megújítása a refresh tokennel. Ez az, ami eddig hiányzott:
/// enélkül a naptár-poller 1 óra után némán leállt.
pub async fn refresh(stored: &StoredTokens) -> Result<StoredTokens, String> {
    let cid = client_id().ok_or("Nincs Google kliens-azonosító.")?;
    let csecret = client_secret().ok_or("Nincs Google kliens-titok.")?;
    if stored.refresh_token.is_empty() {
        return Err("Nincs refresh token — újra be kell jelentkezni.".into());
    }

    let client = reqwest::Client::new();
    let resp = client
        .post(TOKEN_ENDPOINT)
        .form(&[
            ("client_id", cid.as_str()),
            ("client_secret", csecret.as_str()),
            ("refresh_token", stored.refresh_token.as_str()),
            ("grant_type", "refresh_token"),
        ])
        .send()
        .await
        .map_err(|e| format!("frissítés: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        // 400/401 = visszavont hozzáférés → a tárolt token értéktelen.
        if status.as_u16() == 400 || status.as_u16() == 401 {
            clear_tokens();
        }
        return Err(format!("token-frissítés sikertelen ({status}): {body}"));
    }
    let tr: TokenResponse = resp
        .json()
        .await
        .map_err(|e| format!("frissítés-válasz parse: {e}"))?;

    let updated = StoredTokens {
        // A Google a frissítésnél általában NEM küld új refresh tokent — a régit tartjuk.
        refresh_token: tr.refresh_token.unwrap_or_else(|| stored.refresh_token.clone()),
        access_token: tr.access_token,
        expires_at: chrono::Utc::now().timestamp_millis() + tr.expires_in * 1000,
        email: stored.email.clone(),
    };
    save_tokens(&updated)?;
    Ok(updated)
}

/// Érvényes access token: a tároltat adja vissza, vagy frissít, ha lejárt
/// (60 mp ráhagyással, hogy ne a hívás közben járjon le).
pub async fn valid_access_token() -> Option<String> {
    let stored = load_tokens()?;
    if stored.expires_at > chrono::Utc::now().timestamp_millis() + 60_000 {
        return Some(stored.access_token);
    }
    refresh(&stored).await.ok().map(|t| t.access_token)
}

async fn fetch_email(access_token: &str) -> Option<String> {
    let client = reqwest::Client::new();
    let resp = client
        .get("https://www.googleapis.com/oauth2/v2/userinfo")
        .bearer_auth(access_token)
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let v: serde_json::Value = resp.json().await.ok()?;
    v.get("email").and_then(|e| e.as_str()).map(|s| s.to_string())
}
