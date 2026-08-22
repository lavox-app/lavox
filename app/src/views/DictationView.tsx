// Dictation view — recording + transcription + Obsidian export.
//
// The AI "polish" flow (rewrite profiles + Prompter) was removed on 2026-08-04:
// it was half-finished and required the user's own OpenRouter key. The LLM
// plumbing stays (lib/llm.ts) because the meeting summary builds on it.
//
// NOTE: t() keys are Hungarian source strings (gettext-style, see lib/i18n.ts)
// — keep them byte-identical; the English UI copy lives in lib/i18n-en.ts.
import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Mic, FileDown } from "lucide-react";
import { getMode } from "../modes";
import type { TranscriptResult } from "../lib/types";
import { t } from "../lib/i18n";

interface Props {
  modelPath: string | null;
  mics: string[];
}

type Phase = "idle" | "recording" | "transcribing";

function formatTime(ms: number): string {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

export function DictationView({ modelPath, mics }: Props) {
  const mode = getMode("dictation");

  // Transcription state
  const [status, setStatus] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [seconds, setSeconds] = useState(5);
  const [transcript, setTranscript] = useState<TranscriptResult | null>(null);

  // Obsidian export (M1.4 — the backend's export_transcript_to_obsidian command).
  const [exporting, setExporting] = useState(false);
  const [exportedPath, setExportedPath] = useState("");

  async function exportToObsidian() {
    if (!transcript || exporting) return;
    setExporting(true);
    setExportedPath("");
    try {
      const path = await invoke<string>("export_transcript_to_obsidian", {
        transcript,
        createdAt: new Date().toISOString(),
        audioPath: "",
        title: null,
        vaultDir: null, // default: ~/Documents/Lavox
      });
      setExportedPath(path);
    } catch (e) {
      setStatus(t("Hiba: {msg}").replace("{msg}", String(e)));
    } finally {
      setExporting(false);
    }
  }

  // Recording + transcription — the existing, working flow.
  async function recordAndTranscribe() {
    if (!modelPath) {
      setStatus(t("Nincs modell — tedd a .bin fájlt a models/ mappába"));
      return;
    }

    setTranscript(null);
    setExportedPath("");
    setPhase("recording");
    setStatus(t("Felvétel {n} másodpercig...").replace("{n}", String(seconds)));

    try {
      const wavPath = await invoke<string>("record_mic", { seconds });

      setPhase("transcribing");
      setStatus(t("Transzkripció folyamatban..."));

      const result = await invoke<TranscriptResult>("transcribe_wav", {
        wavPath,
        modelPath,
      });

      setTranscript(result);
      setStatus(
        t("Kész — {n} szegmens, nyelv: {lang}")
          .replace("{n}", String(result.segments.length))
          .replace("{lang}", result.language)
      );
    } catch (e) {
      setStatus(t("Hiba: {msg}").replace("{msg}", String(e)));
    }

    setPhase("idle");
  }

  const busy = phase !== "idle";

  return (
    <section className="view">
      <header className="view-header">
        <div
          className="view-title-row"
          style={{
            ["--badge-bg" as string]: mode.bg,
            ["--badge-fg" as string]: mode.color,
          }}
        >
          <span className="view-badge">
            <Mic size={18} strokeWidth={1.75} />
          </span>
          <h1 className="view-title">{t("Diktálás")}</h1>
        </div>
        <p className="view-subtitle">{t(mode.description)}</p>
      </header>

      <div className="controls">
        <label>
          {t("Hossz:")}
          <select
            value={seconds}
            onChange={(e) => setSeconds(Number(e.target.value))}
            disabled={busy}
          >
            <option value={5}>{t("5 mp")}</option>
            <option value={10}>{t("10 mp")}</option>
            <option value={30}>{t("30 mp")}</option>
            <option value={60}>{t("1 perc")}</option>
            <option value={120}>{t("2 perc")}</option>
          </select>
        </label>

        <button
          onClick={recordAndTranscribe}
          disabled={busy}
          className={`btn-primary ${phase === "recording" ? "btn-recording" : ""}`}
        >
          {phase === "recording"
            ? t("Felvétel...")
            : phase === "transcribing"
            ? t("Transzkripció...")
            : t("Felvétel + Transzkripció")}
        </button>
      </div>

      {status && (
        <p className="status">
          <span className="status-dot" data-phase={phase} />
          {status}
        </p>
      )}

      {transcript && (
        <div className="transcript">
          <h2>{t("Átirat")}</h2>
          <div className="transcript-segments">
            {transcript.segments.map((seg, i) => (
              <div key={i} className="segment">
                <span className="segment-time">
                  {formatTime(seg.start_ms)}–{formatTime(seg.end_ms)}
                </span>
                <span className="segment-text">{seg.text}</span>
              </div>
            ))}
          </div>
          <details>
            <summary>{t("Teljes szöveg")}</summary>
            <p className="full-text">{transcript.full_text}</p>
          </details>

          <button
            className="btn-sm btn-outline"
            style={{ marginTop: 8 }}
            type="button"
            onClick={exportToObsidian}
            disabled={exporting}
          >
            <FileDown size={14} strokeWidth={1.75} style={{ verticalAlign: -2, marginRight: 4 }} />
            {exporting ? "…" : exportedPath ? t("Exportálva ✓") : t("Export Obsidianba")}
          </button>
          {exportedPath && <p className="setting-hint">{exportedPath}</p>}
        </div>
      )}

      {mics.length > 0 && (
        <details className="mic-list">
          <summary>{t("Mikrofonok ({n})").replace("{n}", String(mics.length))}</summary>
          <ul>
            {mics.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
