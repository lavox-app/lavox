// A transcribe_wav Tauri-parancs visszatérési típusai (befagyasztott szerződés).
// Megegyezik a Rust oldalon definiált TranscriptResult / TranscriptSegment alakkal.

export interface TranscriptSegment {
  start_ms: number;
  end_ms: number;
  text: string;
}

export interface TranscriptResult {
  language: string;
  segments: TranscriptSegment[];
  full_text: string;
}

// A Rust CaptureResult tükre (model.rs) — a meetings-loop (lista → átirat →
// összefoglaló → export) használja. A mezőnevek a serde-kimenettel egyeznek.
export interface CaptureSpeaker {
  id: string;
  label: string;
  is_me: boolean;
}

export interface CaptureSegment {
  start: number;
  end: number;
  speaker: string;
  text: string;
}

export interface CaptureMedia {
  audio_path: string;
  video_url?: string | null;
}

export interface CaptureResult {
  id: string;
  type: "meeting" | "dictation" | "note";
  status: string;
  created_at: string;
  duration_sec: number;
  language: string;
  source_app?: string | null;
  media: CaptureMedia;
  speakers: CaptureSpeaker[];
  segments: CaptureSegment[];
  summary?: string | null;
  action_items: string[];
  title?: string | null;
  tags: string[];
}

// A meetings/index.json egy bejegyzése (Rust MeetingRecordEntry tükre).
export interface MeetingEntry {
  id: string;
  title: string;
  created_at: string;
  duration_sec: number;
  mic: string | null;
  system: string | null;
  video: string | null;
  kind: "meeting" | "video";
  imported: boolean;
}
