// Meeting-összefoglaló (M5 második fele): a diarizált átiratból LLM-mel
// summary + action_items. Az OpenRouter-kulcsot az llm.ts tárolja
// (Beállítások → AI); ugyanazt a llmCall-t használjuk.
import { llmCall, loadApiKey } from "./llm";
import type { CaptureResult } from "./types";

export interface MeetingSummary {
  summary: string;
  action_items: string[];
}

const SYSTEM_PROMPT = `Te egy meeting-jegyzetelő asszisztens vagy. A kapott,
beszélőkre bontott átiratból írj tömör, tényszerű összefoglalót és gyűjtsd ki
a konkrét teendőket (felelőssel, ha elhangzott). A VÁLASZ NYELVE egyezzen meg
az átirat nyelvével. KIZÁRÓLAG valid JSON-nal válaszolj, pontosan ebben az
alakban: {"summary": "...", "action_items": ["...", "..."]}.
A summary 3-6 mondat; az action_items üres lista is lehet, ha nincs teendő.
Ne találj ki semmit, ami nem hangzott el.`;

/** A diarizált capture szegmenseiből beszélő-címkés átirat-szöveg. */
export function captureToTranscriptText(c: CaptureResult): string {
  const labelOf = new Map(c.speakers.map((s) => [s.id, s.label]));
  return c.segments
    .map((seg) => `${labelOf.get(seg.speaker) ?? seg.speaker}: ${seg.text.trim()}`)
    .join("\n");
}

/** LLM-összefoglaló generálása. Hibát dob, ha nincs API-kulcs vagy a hívás elbukik. */
export async function summarizeMeeting(capture: CaptureResult): Promise<MeetingSummary> {
  const apiKey = loadApiKey();
  if (!apiKey) {
    throw new Error("Nincs OpenRouter API kulcs — add meg a Beállításokban.");
  }
  const transcript = captureToTranscriptText(capture);
  if (!transcript.trim()) {
    throw new Error("Üres átirat — nincs mit összefoglalni.");
  }
  // A nagyon hosszú átiratot levágjuk (a Haiku kontextusa bőven elég, de a
  // költséget is kordában tartjuk); a vége felé lévő tartalom jellemzően
  // fontosabb (döntések, teendők), ezért az ELEJÉT vágjuk.
  const MAX_CHARS = 60_000;
  const clipped =
    transcript.length > MAX_CHARS
      ? "…(az átirat eleje levágva)…\n" + transcript.slice(-MAX_CHARS)
      : transcript;

  const raw = await llmCall(apiKey, SYSTEM_PROMPT, clipped, true);
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("Az AI válasza nem volt érvényes JSON.");
  }
  const obj = parsed as { summary?: unknown; action_items?: unknown };
  const summary = typeof obj.summary === "string" ? obj.summary.trim() : "";
  const items = Array.isArray(obj.action_items)
    ? obj.action_items.filter((x): x is string => typeof x === "string")
    : [];
  if (!summary) throw new Error("Az AI nem adott összefoglalót.");
  return { summary, action_items: items };
}
