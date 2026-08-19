use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::sync::Mutex;

const CALENDAR_API: &str = "https://www.googleapis.com/calendar/v3";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CalendarToken {
    pub access_token: String,
    pub expires_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpcomingMeeting {
    pub id: String,
    pub title: String,
    pub start: String,
    pub end: String,
    pub seconds_until: i64,
    pub duration_minutes: i64,
}

pub struct CalendarState {
    pub token: Option<CalendarToken>,
    pub auto_record: bool,
    pub notified_ids: Vec<String>,
}

impl Default for CalendarState {
    fn default() -> Self {
        Self {
            token: None,
            auto_record: false,
            notified_ids: Vec::new(),
        }
    }
}

pub type SharedCalendarState = Arc<Mutex<CalendarState>>;

pub fn new_state() -> SharedCalendarState {
    Arc::new(Mutex::new(CalendarState::default()))
}

pub async fn check_upcoming(token: &str) -> Result<Vec<UpcomingMeeting>, String> {
    let now = Utc::now();
    let time_min = now.to_rfc3339();
    let time_max = (now + chrono::Duration::minutes(3)).to_rfc3339();

    let client = reqwest::Client::new();
    let res = client
        .get(format!("{CALENDAR_API}/calendars/primary/events"))
        .bearer_auth(token)
        .query(&[
            ("timeMin", time_min.as_str()),
            ("timeMax", time_max.as_str()),
            ("singleEvents", "true"),
            ("orderBy", "startTime"),
            ("maxResults", "5"),
        ])
        .send()
        .await
        .map_err(|e| format!("HTTP hiba: {e}"))?;

    if !res.status().is_success() {
        let status = res.status();
        let body = res.text().await.unwrap_or_default();
        return Err(format!("Google API {status}: {body}"));
    }

    let data: serde_json::Value = res.json().await.map_err(|e| e.to_string())?;
    let empty = vec![];
    let items = data["items"].as_array().unwrap_or(&empty);

    let meetings: Vec<UpcomingMeeting> = items
        .iter()
        .filter_map(|item| {
            let start_str = item["start"]["dateTime"].as_str()?;
            let end_str = item["end"]["dateTime"].as_str()?;
            let start = DateTime::parse_from_rfc3339(start_str).ok()?;
            let end = DateTime::parse_from_rfc3339(end_str).ok()?;
            let diff = start.signed_duration_since(now);
            let dur = end.signed_duration_since(start);

            Some(UpcomingMeeting {
                id: item["id"].as_str()?.to_string(),
                title: item["summary"]
                    .as_str()
                    .unwrap_or("(Nincs cím)")
                    .to_string(),
                start: start_str.to_string(),
                end: end_str.to_string(),
                seconds_until: diff.num_seconds(),
                duration_minutes: dur.num_minutes(),
            })
        })
        .collect();

    Ok(meetings)
}

/// A felvétel idősávjával átfedő naptár-esemény RÉSZTVEVŐ-NEVEI — a
/// beszélő-azonosítás jelölt-poolja. FIGYELEM: a meghívott ≠ jelenlévő; a
/// szerver ezt a listát csak megerősítő evidenciával (önbemutatkozás,
/// megszólítás) együtt használhatja név-kiosztásra.
pub async fn attendees_for_window(
    token: &str,
    start_rfc3339: &str,
    end_rfc3339: &str,
) -> Result<Vec<String>, String> {
    let client = reqwest::Client::new();
    let res = client
        .get(format!("{CALENDAR_API}/calendars/primary/events"))
        .bearer_auth(token)
        .query(&[
            ("timeMin", start_rfc3339),
            ("timeMax", end_rfc3339),
            ("singleEvents", "true"),
            ("orderBy", "startTime"),
            ("maxResults", "3"),
        ])
        .send()
        .await
        .map_err(|e| format!("HTTP hiba: {e}"))?;
    if !res.status().is_success() {
        return Err(format!("Google API {}", res.status()));
    }
    let data: serde_json::Value = res.json().await.map_err(|e| e.to_string())?;
    let empty = vec![];
    let mut names: Vec<String> = Vec::new();
    for item in data["items"].as_array().unwrap_or(&empty) {
        for a in item["attendees"].as_array().unwrap_or(&empty) {
            // displayName ha van; az email local-partja fallback.
            let name = a["displayName"]
                .as_str()
                .map(String::from)
                .or_else(|| {
                    a["email"].as_str().map(|e| {
                        e.split('@').next().unwrap_or(e).replace(['.', '_'], " ")
                    })
                });
            if let Some(n) = name {
                let n = n.trim().to_string();
                if !n.is_empty() && !names.contains(&n) {
                    names.push(n);
                }
            }
        }
    }
    Ok(names)
}

pub fn start_polling(app: tauri::AppHandle, state: SharedCalendarState) {
    tauri::async_runtime::spawn(async move {
        let mut interval = tokio::time::interval(tokio::time::Duration::from_secs(30));
        loop {
            interval.tick().await;

            let auto_record = { state.lock().await.auto_record };

            // A tokent a gauth adja: ha lejárt, MAGÁTÓL frissíti a refresh
            // tokennel. (Korábban itt csak a memóriában tárolt, 1 órás GIS-token
            // volt — lejárat után a poller némán, jelzés nélkül leállt.)
            let token = match crate::gauth::valid_access_token().await {
                Some(t) => {
                    // A megosztott állapot is kövesse, hogy a UI konzisztens legyen.
                    let mut s = state.lock().await;
                    s.token = Some(CalendarToken {
                        access_token: t.clone(),
                        expires_at: Utc::now().timestamp_millis() + 55 * 60 * 1000,
                    });
                    t
                }
                None => continue,
            };

            match check_upcoming(&token).await {
                Ok(meetings) => {
                    for meeting in meetings {
                        if meeting.seconds_until > 90 || meeting.seconds_until < -60 {
                            continue;
                        }

                        let already = {
                            let s = state.lock().await;
                            s.notified_ids.contains(&meeting.id)
                        };
                        if already {
                            continue;
                        }

                        {
                            let mut s = state.lock().await;
                            s.notified_ids.push(meeting.id.clone());
                            if s.notified_ids.len() > 50 {
                                s.notified_ids.drain(0..25);
                            }
                        }

                        use tauri::Emitter;
                        let _ = app.emit("meeting-starting", &meeting);

                        if auto_record {
                            let _ = app.emit("auto-record-start", &meeting);
                        }
                    }
                }
                Err(e) => {
                    eprintln!("[calendar] Polling hiba: {e}");
                }
            }
        }
    });
}
