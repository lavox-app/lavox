// Video view. Loom-style personal recordings: screen + face bubble + audio.
// Created by the bar's Video button (🎥); this view plays screen.mov and
// optionally requests a transcript too (captions/search). SEPARATE from
// Meetings: here the VIDEO is the point (player), not the diarized transcript.
// NOTE: t() keys are Hungarian source strings (gettext-style, see lib/i18n.ts).
import { useCallback, useEffect, useState } from "react";
import { invoke, convertFileSrc } from "@tauri-apps/api/core";
import { Video as VideoIcon, FileText, RefreshCw, FolderOpen } from "lucide-react";
import { openPath } from "@tauri-apps/plugin-opener";
import { getMode } from "../modes";
import { t } from "../lib/i18n";
import type { CaptureResult, MeetingEntry } from "../lib/types";

function fmtDuration(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}
function fmtDate(iso: string): string {
  const d = new Date(iso);
  return isNaN(d.getTime())
    ? iso
    : d.toLocaleString("hu-HU", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function VideoView() {
  const mode = getMode("video");
  const Icon = mode.icon;

  const [videos, setVideos] = useState<MeetingEntry[]>([]);
  const [selected, setSelected] = useState<MeetingEntry | null>(null);
  const [capture, setCapture] = useState<CaptureResult | null>(null);
  const [busy, setBusy] = useState<"" | "transcribe">("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const list = await invoke<MeetingEntry[]>("list_meetings");
      // ONLY the video recordings (kind=video), meetings live on the Meeting tab.
      setVideos(list.filter((m) => m.kind === "video"));
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const select = useCallback(async (m: MeetingEntry) => {
    setSelected(m);
    setCapture(null);
    setError("");
    try {
      const c = await invoke<CaptureResult | null>("load_capture", { recId: m.id });
      setCapture(c ?? null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  const transcribe = useCallback(async () => {
    if (!selected || busy) return;
    setBusy("transcribe");
    setError("");
    try {
      // Remote transcript for the video too (your own voice → captions/search).
      const c = await invoke<CaptureResult>("remote_transcribe_meeting", {
        recId: selected.id,
        numSpeakers: 1,
        harvest: localStorage.getItem("lavox-harvest") !== "false",
      });
      setCapture(c);
    } catch (e) {
      setError(
        t("Átirat hiba: {msg} — állítsd be a szervert (Beállítások → Beszélők).").replace(
          "{msg}",
          String(e),
        ),
      );
    } finally {
      setBusy("");
    }
  }, [selected, busy]);

  const videoSrc = selected?.video ? convertFileSrc(selected.video) : null;

  return (
    <section className="view">
      <header className="view-header">
        <div
          className="view-title-row"
          style={{ ["--badge-bg" as string]: mode.bg, ["--badge-fg" as string]: mode.color }}
        >
          <span className="view-badge">
            <Icon size={18} strokeWidth={1.75} />
          </span>
          <h1 className="view-title">{t("Videó")}</h1>
        </div>
        <p className="view-subtitle">
          {t("Loom-stílusú felvételeid: képernyő + arc. Indítás a bár Videó-gombjával.")}
        </p>
      </header>

      <div className="setting-row">
        <div className="setting-info">
          <span>{t("Videók ({n})").replace("{n}", String(videos.length))}</span>
        </div>
        <button className="btn-sm btn-outline" type="button" onClick={refresh}>
          <RefreshCw size={13} strokeWidth={2} style={{ verticalAlign: -2, marginRight: 4 }} />
          {t("Frissítés")}
        </button>
      </div>

      {videos.length === 0 && (
        <p className="setting-hint">
          {t("Még nincs videó — indíts egyet a bár Videó-gombjával (🎥). A felvételben ott lesz az arcod is.")}
        </p>
      )}

      <ul className="meeting-list">
        {videos.map((m) => (
          <li key={m.id}>
            <button
              className={`meeting-row ${selected?.id === m.id ? "meeting-row-active" : ""}`}
              type="button"
              onClick={() => select(m)}
            >
              <span className="meeting-kind">{t("Videó")}</span>
              <span className="meeting-title">{m.title || m.id}</span>
              <span className="meeting-meta">
                {fmtDate(m.created_at)} · {fmtDuration(m.duration_sec)}
              </span>
            </button>
          </li>
        ))}
      </ul>

      {selected && (
        <div className="meeting-detail">
          {videoSrc ? (
            <video className="video-player" src={videoSrc} controls playsInline />
          ) : (
            <p className="setting-hint">
              <VideoIcon size={14} strokeWidth={2} style={{ verticalAlign: -2, marginRight: 4 }} />
              {t("Ehhez a felvételhez nincs videó-fájl (csak hang).")}
            </p>
          )}

          <div className="meeting-actions">
            {selected.video && (
              <button
                className="btn-sm btn-outline"
                type="button"
                onClick={() => selected.video && openPath(selected.video).catch(() => {})}
              >
                <FolderOpen size={13} strokeWidth={2} style={{ verticalAlign: -2, marginRight: 4 }} />
                {t("Megnyitás")}
              </button>
            )}
            {!capture?.segments?.length && (
              <button className="btn-sm btn-teal" type="button" onClick={transcribe} disabled={busy !== ""}>
                <FileText size={13} strokeWidth={2} style={{ verticalAlign: -2, marginRight: 4 }} />
                {busy === "transcribe" ? t("Átirat…") : t("Átirat készítése")}
              </button>
            )}
          </div>

          {capture?.segments && capture.segments.length > 0 && (
            <div className="transcript-segments" style={{ marginTop: 12 }}>
              {capture.segments.map((seg, i) => (
                <div key={i} className="segment">
                  <span className="segment-time">{fmtDuration(seg.start)}</span>
                  <span className="segment-text">{seg.text}</span>
                </div>
              ))}
            </div>
          )}
          {error && <p className="setting-error">{error}</p>}
        </div>
      )}
    </section>
  );
}
