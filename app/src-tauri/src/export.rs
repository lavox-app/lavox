//! M1.4 — ObsidianExporter: `CaptureResult` → structured Obsidian markdown.
//!
//! The engine (STT + later LLM pass) produces a `CaptureResult`; this module
//! turns it into Obsidian-friendly markdown (YAML frontmatter + body). BEFORE
//! the LLM pass, `summary/title/action_items` may be empty — the export handles
//! that without failing (see INTERFACE-CaptureResult.md "Edge cases").
//!
//! The date (`created_at`) is passed in by the caller (frontend) as an ISO-8601
//! string, so no date crate is needed and Cargo.toml stays untouched.

use crate::model::{CaptureResult, CaptureType, Media, Segment, Speaker, Status};
use crate::transcribe::TranscriptResult;

/// Export of one capture: markdown text + the suggested filename (without extension).
pub struct ExportedNote {
    pub filename: String,
    pub markdown: String,
}

/// `CaptureResult` → full Obsidian markdown (frontmatter + body).
pub fn capture_to_markdown(c: &CaptureResult) -> String {
    let mut out = String::new();

    // --- YAML frontmatter ---
    out.push_str("---\n");
    out.push_str(&format!("id: {}\n", c.id));
    out.push_str(&format!("type: {}\n", capture_type_str(&c.kind)));
    out.push_str(&format!("status: {}\n", status_str(&c.status)));
    out.push_str(&format!("created: {}\n", c.created_at));
    out.push_str(&format!("duration_sec: {}\n", c.duration_sec.round() as i64));
    out.push_str(&format!("language: {}\n", c.language));
    if let Some(app) = &c.source_app {
        out.push_str(&format!("source_app: {}\n", app));
    }
    if !c.media.audio_path.is_empty() {
        out.push_str(&format!("audio: {}\n", c.media.audio_path));
    }
    if let Some(url) = &c.media.video_url {
        out.push_str(&format!("video: {}\n", url));
    }
    if !c.tags.is_empty() {
        out.push_str(&format!("tags: [{}]\n", c.tags.join(", ")));
    }
    out.push_str("---\n\n");

    // --- Title ---
    let title = c
        .title
        .clone()
        .filter(|t| !t.trim().is_empty())
        .unwrap_or_else(|| default_title(c));
    out.push_str(&format!("# {}\n\n", title));

    // --- Summary (after the LLM pass) ---
    if let Some(summary) = &c.summary {
        if !summary.trim().is_empty() {
            out.push_str("> [!summary] Summary\n");
            for line in summary.trim().lines() {
                out.push_str(&format!("> {}\n", line));
            }
            out.push('\n');
        }
    }

    // --- Action items ---
    if !c.action_items.is_empty() {
        out.push_str("## Action items\n\n");
        for item in &c.action_items {
            out.push_str(&format!("- [ ] {}\n", item));
        }
        out.push('\n');
    }

    // --- Transcript ---
    out.push_str("## Transcript\n\n");
    out.push_str(&render_transcript(c));

    out
}

/// The transcript body. Meetings (multiple speakers) are grouped under speaker
/// headers; dictation/notes (single speaker) become flowing text with timestamps.
fn render_transcript(c: &CaptureResult) -> String {
    let mut out = String::new();
    if c.segments.is_empty() {
        out.push_str("_(no transcript)_\n");
        return out;
    }

    let multi_speaker = c.speakers.len() > 1;

    if multi_speaker {
        let mut last_speaker: Option<&str> = None;
        for seg in &c.segments {
            if last_speaker != Some(seg.speaker.as_str()) {
                let label = speaker_label(c, &seg.speaker);
                out.push_str(&format!(
                    "\n**{}** [{}]\n",
                    label,
                    fmt_timestamp(seg.start)
                ));
                last_speaker = Some(seg.speaker.as_str());
            }
            out.push_str(seg.text.trim());
            out.push(' ');
        }
        out.push('\n');
    } else {
        // Single speaker: timestamped lines, no speaker headers.
        for seg in &c.segments {
            let text = seg.text.trim();
            if text.is_empty() {
                continue;
            }
            out.push_str(&format!("[{}] {}\n\n", fmt_timestamp(seg.start), text));
        }
    }

    out
}

/// Builds a `CaptureResult` from a raw `TranscriptResult` (STT output, no
/// diarization/LLM pass) so the real recording flow is exportable already today.
/// `created_at` comes from the caller (ISO-8601). `summary/title/action_items` are empty.
pub fn transcript_to_capture(
    t: &TranscriptResult,
    created_at: &str,
    audio_path: &str,
    title: Option<String>,
) -> CaptureResult {
    let duration_sec = t
        .segments
        .last()
        .map(|s| s.end_ms as f64 / 1000.0)
        .unwrap_or(0.0);

    let segments = t
        .segments
        .iter()
        .map(|s| Segment {
            start: s.start_ms as f64 / 1000.0,
            end: s.end_ms as f64 / 1000.0,
            speaker: "S1".to_string(),
            text: s.text.clone(),
        })
        .collect();

    CaptureResult {
        // Deterministic, time-based id (no uuid crate); collision-insensitive
        // in a single-user context.
        id: format!("lavox-{}", slugify(created_at)),
        kind: CaptureType::Note,
        status: Status::Final,
        created_at: created_at.to_string(),
        duration_sec,
        language: t.language.clone(),
        source_app: Some("mic".to_string()),
        media: Media {
            audio_path: audio_path.to_string(),
            video_url: None,
        },
        speakers: vec![Speaker {
            id: "S1".to_string(),
            label: "Me".to_string(),
            is_me: true,
        }],
        segments,
        summary: None,
        action_items: vec![],
        title,
        tags: vec![],
    }
}

/// Produces the markdown + a filename suggestion for a capture.
pub fn export_note(c: &CaptureResult) -> ExportedNote {
    let date = date_prefix(&c.created_at);
    let title = c
        .title
        .clone()
        .filter(|t| !t.trim().is_empty())
        .unwrap_or_else(|| default_title(c));
    let filename = format!("{}-{}", date, slugify(&title));
    ExportedNote {
        filename,
        markdown: capture_to_markdown(c),
    }
}

// --- Tauri commands ---

/// Writes the markdown into the target directory. Returns the full path.
/// Without `vault_dir`: `$HOME/Documents/Lavox`. Creates the directory.
fn write_note(note: &ExportedNote, vault_dir: Option<String>) -> Result<String, String> {
    let dir = match vault_dir {
        Some(d) if !d.trim().is_empty() => std::path::PathBuf::from(d),
        _ => {
            let home = std::env::var("HOME").map_err(|_| "HOME is not set".to_string())?;
            std::path::Path::new(&home).join("Documents").join("Lavox")
        }
    };
    std::fs::create_dir_all(&dir).map_err(|e| format!("failed to create directory: {e}"))?;
    let path = dir.join(format!("{}.md", note.filename));
    std::fs::write(&path, &note.markdown).map_err(|e| format!("write failed: {e}"))?;
    Ok(path.to_string_lossy().to_string())
}

/// Raw transcript → Obsidian note. `created_at` comes from the frontend
/// (`new Date().toISOString()`). Returns the path of the written file.
#[tauri::command]
pub fn export_transcript_to_obsidian(
    transcript: TranscriptResult,
    created_at: String,
    audio_path: String,
    title: Option<String>,
    vault_dir: Option<String>,
) -> Result<String, String> {
    let capture = transcript_to_capture(&transcript, &created_at, &audio_path, title);
    let note = export_note(&capture);
    write_note(&note, vault_dir)
}

/// Finished `CaptureResult` (e.g. after the LLM pass) → Obsidian note.
#[tauri::command]
pub fn export_capture_to_obsidian(
    capture: CaptureResult,
    vault_dir: Option<String>,
) -> Result<String, String> {
    let note = export_note(&capture);
    write_note(&note, vault_dir)
}

// --- Helpers ---

fn capture_type_str(t: &CaptureType) -> &'static str {
    match t {
        CaptureType::Meeting => "meeting",
        CaptureType::Dictation => "dictation",
        CaptureType::Note => "note",
    }
}

fn status_str(s: &Status) -> &'static str {
    match s {
        Status::Partial => "partial",
        Status::Final => "final",
    }
}

fn speaker_label(c: &CaptureResult, id: &str) -> String {
    c.speakers
        .iter()
        .find(|s| s.id == id)
        .map(|s| s.label.clone())
        .unwrap_or_else(|| id.to_string())
}

fn default_title(c: &CaptureResult) -> String {
    let kind = match c.kind {
        CaptureType::Meeting => "Meeting",
        CaptureType::Dictation => "Dictation",
        CaptureType::Note => "Note",
    };
    format!("{} – {}", kind, date_prefix(&c.created_at))
}

/// First 10 characters of an ISO-8601 string (YYYY-MM-DD); "undated" if shorter.
fn date_prefix(iso: &str) -> String {
    if iso.len() >= 10 {
        iso[..10].to_string()
    } else {
        "undated".to_string()
    }
}

/// Seconds → `m:ss` (or `h:mm:ss` above one hour).
fn fmt_timestamp(sec: f64) -> String {
    let total = sec.max(0.0) as i64;
    let h = total / 3600;
    let m = (total % 3600) / 60;
    let s = total % 60;
    if h > 0 {
        format!("{}:{:02}:{:02}", h, m, s)
    } else {
        format!("{}:{:02}", m, s)
    }
}

/// Filename-friendly slug with Hungarian accent folding.
fn slugify(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut prev_dash = false;
    for ch in input.chars() {
        let folded = fold_char(ch);
        for fc in folded.chars() {
            if fc.is_ascii_alphanumeric() {
                out.push(fc.to_ascii_lowercase());
                prev_dash = false;
            } else if !prev_dash && !out.is_empty() {
                out.push('-');
                prev_dash = true;
            }
        }
    }
    let trimmed = out.trim_matches('-').to_string();
    if trimmed.is_empty() {
        "note".to_string()
    } else {
        trimmed
    }
}

/// ASCII equivalent of Hungarian (and a few common) accented characters.
fn fold_char(ch: char) -> String {
    match ch {
        'á' | 'à' | 'â' | 'ä' | 'ã' => "a".into(),
        'Á' | 'À' | 'Â' | 'Ä' | 'Ã' => "A".into(),
        'é' | 'è' | 'ê' | 'ë' => "e".into(),
        'É' | 'È' | 'Ê' | 'Ë' => "E".into(),
        'í' | 'ì' | 'î' | 'ï' => "i".into(),
        'Í' | 'Ì' | 'Î' | 'Ï' => "I".into(),
        'ó' | 'ò' | 'ô' | 'ö' | 'õ' | 'ő' => "o".into(),
        'Ó' | 'Ò' | 'Ô' | 'Ö' | 'Õ' | 'Ő' => "O".into(),
        'ú' | 'ù' | 'û' | 'ü' | 'ű' => "u".into(),
        'Ú' | 'Ù' | 'Û' | 'Ü' | 'Ű' => "U".into(),
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> CaptureResult {
        let raw = include_str!("../../mocks/sample-meeting.json");
        serde_json::from_str(raw).expect("mock parse")
    }

    #[test]
    fn frontmatter_has_required_fields() {
        let md = capture_to_markdown(&sample());
        assert!(md.starts_with("---\n"));
        assert!(md.contains("type: meeting"));
        assert!(md.contains("status: final"));
        assert!(md.contains("language: en"));
        assert!(md.contains("tags: [sales, discovery]"));
    }

    #[test]
    fn body_renders_title_summary_and_actions() {
        let md = capture_to_markdown(&sample());
        assert!(md.contains("# Intro call – Client"));
        assert!(md.contains("> [!summary] Summary"));
        assert!(md.contains("- [ ] Send the proposal by Friday"));
    }

    #[test]
    fn multi_speaker_uses_labels() {
        let md = capture_to_markdown(&sample());
        assert!(md.contains("**David** [0:00]"));
        assert!(md.contains("**Client** [0:04]"));
    }

    #[test]
    fn handles_missing_llm_fields() {
        let mut c = sample();
        c.summary = None;
        c.title = None;
        c.action_items.clear();
        let md = capture_to_markdown(&c);
        // Does not panic, and no empty "Action items" / "Summary" sections.
        assert!(!md.contains("## Action items"));
        assert!(!md.contains("[!summary]"));
        assert!(md.contains("# Meeting – 2026-06-22"));
    }

    #[test]
    fn slugify_folds_hungarian_accents() {
        // Hungarian inputs on purpose: they exercise the accent-folding path.
        assert_eq!(slugify("Bevezető hívás – Ügyfél"), "bevezeto-hivas-ugyfel");
        assert_eq!(slugify("  több   szóköz  "), "tobb-szokoz");
        assert_eq!(slugify("!!!"), "note");
    }

    #[test]
    fn timestamp_formats() {
        assert_eq!(fmt_timestamp(0.0), "0:00");
        assert_eq!(fmt_timestamp(65.0), "1:05");
        assert_eq!(fmt_timestamp(3725.0), "1:02:05");
    }

    #[test]
    fn export_note_builds_dated_filename() {
        let note = export_note(&sample());
        assert_eq!(note.filename, "2026-06-22-intro-call-client");
    }

    #[test]
    fn transcript_converts_to_single_speaker_note() {
        let t = TranscriptResult {
            language: "hu".into(),
            segments: vec![crate::transcribe::TranscriptSegment {
                start_ms: 0,
                end_ms: 2000,
                text: "Test sentence.".into(),
            }],
            full_text: "Test sentence.".into(),
        };
        let c = transcript_to_capture(&t, "2026-06-24T10:00:00Z", "captures/a.wav", None);
        assert_eq!(c.speakers.len(), 1);
        assert!(c.speakers[0].is_me);
        assert!((c.duration_sec - 2.0).abs() < 1e-6);
        let md = capture_to_markdown(&c);
        assert!(md.contains("[0:00] Test sentence."));
    }
}
