// Meeting summary (second half of M5): summary + action_items from the
// diarized transcript via an LLM. The OpenRouter key is stored by llm.ts
// (Settings → AI); we use the same llmCall.
import { llmCall, loadApiKey } from "./llm";
import type { CaptureResult } from "./types";

export interface MeetingSummary {
  summary: string;
  action_items: string[];
}

const SYSTEM_PROMPT = `You are a meeting note-taking assistant. From the
speaker-separated transcript you receive, write a concise, factual summary and
collect the concrete action items (with owners, if mentioned). The RESPONSE
LANGUAGE must match the language of the transcript. Respond with valid JSON
ONLY, in exactly this shape: {"summary": "...", "action_items": ["...", "..."]}.
The summary is 3-6 sentences; action_items may be an empty list if there are
no to-dos. Do not invent anything that was not said.`;

/** Speaker-labelled transcript text from the diarized capture's segments. */
export function captureToTranscriptText(c: CaptureResult): string {
  const labelOf = new Map(c.speakers.map((s) => [s.id, s.label]));
  return c.segments
    .map((seg) => `${labelOf.get(seg.speaker) ?? seg.speaker}: ${seg.text.trim()}`)
    .join("\n");
}

/** Generate the LLM summary. Throws if there is no API key or the call fails. */
export async function summarizeMeeting(capture: CaptureResult): Promise<MeetingSummary> {
  const apiKey = loadApiKey();
  if (!apiKey) {
    throw new Error("No OpenRouter API key. Add one in Settings.");
  }
  const transcript = captureToTranscriptText(capture);
  if (!transcript.trim()) {
    throw new Error("Empty transcript: nothing to summarize.");
  }
  // Clip very long transcripts (Haiku's context is plenty, but we also keep
  // cost in check); content near the end is usually more important
  // (decisions, action items), so we trim the BEGINNING.
  const MAX_CHARS = 60_000;
  const clipped =
    transcript.length > MAX_CHARS
      ? "…(beginning of transcript trimmed)…\n" + transcript.slice(-MAX_CHARS)
      : transcript;

  const raw = await llmCall(apiKey, SYSTEM_PROMPT, clipped, true);
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("The AI response was not valid JSON.");
  }
  const obj = parsed as { summary?: unknown; action_items?: unknown };
  const summary = typeof obj.summary === "string" ? obj.summary.trim() : "";
  const items = Array.isArray(obj.action_items)
    ? obj.action_items.filter((x): x is string => typeof x === "string")
    : [];
  if (!summary) throw new Error("The AI returned no summary.");
  return { summary, action_items: items };
}
