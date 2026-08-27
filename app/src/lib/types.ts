// Return types of the transcribe_wav Tauri command (frozen contract).
// Matches the TranscriptResult / TranscriptSegment shapes defined on the Rust side.

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

// Mirror of the Rust CaptureResult (model.rs), used by the meetings loop
// (list → transcript → summary → export). Field names match the serde output.
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

// One entry of meetings/index.json (mirror of the Rust MeetingRecordEntry).
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
