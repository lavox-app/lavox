//! `CaptureResult` — the motor → export data model. See `INTERFACE-CaptureResult.md`.
//!
//! NOTE: M0 skeleton, not yet wired into `lib.rs`. M1 step: add `mod model;`.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CaptureType {
    Meeting,
    Dictation,
    Note,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Status {
    Partial,
    Final,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Media {
    pub audio_path: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub video_url: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Speaker {
    pub id: String,
    pub label: String,
    #[serde(default)]
    pub is_me: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Segment {
    pub start: f64,
    pub end: f64,
    pub speaker: String,
    pub text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CaptureResult {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: CaptureType,
    pub status: Status,
    pub created_at: String,
    pub duration_sec: f64,
    pub language: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_app: Option<String>,
    pub media: Media,
    pub speakers: Vec<Speaker>,
    pub segments: Vec<Segment>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    #[serde(default)]
    pub action_items: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default)]
    pub tags: Vec<String>,
}
