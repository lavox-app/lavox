// Meetings view. The product's core loop: list of recordings → diarized
// transcript (remote server) → AI summary (OpenRouter) → Obsidian export.
// Recording itself happens from the bar (Video button / Meet Bridge); this view
// is for processing finished recordings.
// NOTE: t() keys are Hungarian source strings (gettext-style, see lib/i18n.ts).
import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Users, FileDown, Sparkles, RefreshCw, AudioLines } from "lucide-react";
import { getMode } from "../modes";
import { t } from "../lib/i18n";
import type { CaptureResult, MeetingEntry } from "../lib/types";
import { summarizeMeeting } from "../lib/summarize";

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

export function MeetingsView() {
  const mode = getMode("meeting");
  const Icon = mode.icon;

  const [entries, setEntries] = useState<MeetingEntry[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [capture, setCapture] = useState<CaptureResult | null>(null);
  const [busy, setBusy] = useState<"" | "transcribe" | "summary" | "export">("");
  const [error, setError] = useState("");
  const [exportedPath, setExportedPath] = useState("");

  const refresh = useCallback(async () => {
    try {
      const list = await invoke<MeetingEntry[]>("list_meetings");
      // ONLY the meetings (kind=meeting), personal videos live on the Video tab.
      setEntries(list.filter((m) => m.kind !== "video"));
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const select = useCallback(async (id: string) => {
    setSelected(id);
    setCapture(null);
    setError("");
    setExportedPath("");
    try {
      const c = await invoke<CaptureResult | null>("load_capture", { recId: id });
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
      const c = await invoke<CaptureResult>("remote_transcribe_meeting", {
        recId: selected,
        harvest: localStorage.getItem("lavox-harvest") !== "false",
      });
      setCapture(c);
    } catch (e) {
      setError(
        t("Átirat hiba: {msg} — ellenőrizd a szerver-beállítást (Beállítások → Beszélők).").replace(
          "{msg}",
          String(e),
        ),
      );
    } finally {
      setBusy("");
    }
  }, [selected, busy]);

  const summarize = useCallback(async () => {
    if (!capture || !selected || busy) return;
    setBusy("summary");
    setError("");
    try {
      const s = await summarizeMeeting(capture);
      const enriched: CaptureResult = { ...capture, summary: s.summary, action_items: s.action_items };
      setCapture(enriched);
      await invoke("save_capture", { recId: selected, capture: enriched }).catch(() => {});
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  }, [capture, selected, busy]);

  const exportObsidian = useCallback(async () => {
    if (!capture || busy) return;
    setBusy("export");
    setError("");
    try {
      const path = await invoke<string>("export_capture_to_obsidian", {
        capture,
        vaultDir: null,
      });
      setExportedPath(path);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  }, [capture, busy]);

  const speakerLabel = (id: string) =>
    capture?.speakers.find((s) => s.id === id)?.label ?? id;

  // Rename a speaker ("tag-once"): click the label to give a new name, and the
  // system also learns the cluster's voice, recognizing it on its own next time.
  const renameSpeaker = useCallback(
    async (speakerId: string) => {
      if (!capture || !selected || busy) return;
      const current = capture.speakers.find((s) => s.id === speakerId)?.label ?? speakerId;
      const name = window.prompt(t("Beszélő neve:"), current);
      if (!name || name.trim() === "" || name.trim() === current) return;
      try {
        const learn = localStorage.getItem("lavox-harvest") !== "false";
        const c = await invoke<CaptureResult>("rename_speaker", {
          recId: selected,
          speakerId,
          newName: name.trim(),
          learn,
        });
        setCapture(c);
      } catch (e) {
        setError(String(e));
      }
    },
    [capture, selected, busy],
  );

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
          <h1 className="view-title">{t("Meeting")}</h1>
        </div>
        <p className="view-subtitle">
          {t("Rögzített hívások: ki mit mondott (diarizált átirat), AI-összefoglaló, Obsidian-export.")}
        </p>
      </header>

      <div className="setting-row">
        <div className="setting-info">
          <span>{t("Meetingek ({n})").replace("{n}", String(entries.length))}</span>
        </div>
        <button className="btn-sm btn-outline" type="button" onClick={refresh}>
          <RefreshCw size={13} strokeWidth={2} style={{ verticalAlign: -2, marginRight: 4 }} />
          {t("Frissítés")}
        </button>
      </div>

      {entries.length === 0 && (
        <p className="setting-hint">
          {t("Még nincs meeting — lépj be egy Google Meetbe (a bővítmény felajánlja a rögzítést), vagy kapcsold be az auto-rögzítést a Beállításokban.")}
        </p>
      )}

      <ul className="meeting-list">
        {entries.map((m) => (
          <li key={m.id}>
            <button
              className={`meeting-row ${selected === m.id ? "meeting-row-active" : ""}`}
              type="button"
              onClick={() => select(m.id)}
            >
              <span className="meeting-kind">Meeting</span>
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
          {!capture ? (
            <div className="setting-row">
              <div className="setting-info">
                <AudioLines size={16} strokeWidth={1.75} style={{ marginRight: 6, opacity: 0.6 }} />
                <span>{t("Még nincs átirat ehhez a felvételhez.")}</span>
              </div>
              <button
                className="btn-sm btn-teal"
                type="button"
                onClick={transcribe}
                disabled={busy !== ""}
              >
                {busy === "transcribe" ? t("Átirat…") : t("Átirat + beszélők")}
              </button>
            </div>
          ) : (
            <>
              {capture.speakers.length > 0 && (
                <p className="setting-hint">
                  <Users size={13} strokeWidth={2} style={{ verticalAlign: -2, marginRight: 4 }} />
                  {capture.speakers.map((s) => s.label + (s.is_me ? ` (${t("én")})` : "")).join(" · ")}
                </p>
              )}

              {capture.summary && (
                <div className="meeting-summary">
                  <h3>{t("Összefoglaló")}</h3>
                  <p>{capture.summary}</p>
                  {capture.action_items.length > 0 && (
                    <>
                      <h3>{t("Teendők")}</h3>
                      <ul>
                        {capture.action_items.map((a, i) => (
                          <li key={i}>{a}</li>
                        ))}
                      </ul>
                    </>
                  )}
                </div>
              )}

              <div className="meeting-actions">
                {!capture.summary && (
                  <button className="btn-sm btn-teal" type="button" onClick={summarize} disabled={busy !== ""}>
                    <Sparkles size={13} strokeWidth={2} style={{ verticalAlign: -2, marginRight: 4 }} />
                    {busy === "summary" ? "…" : t("AI-összefoglaló")}
                  </button>
                )}
                <button className="btn-sm btn-outline" type="button" onClick={exportObsidian} disabled={busy !== ""}>
                  <FileDown size={13} strokeWidth={2} style={{ verticalAlign: -2, marginRight: 4 }} />
                  {busy === "export" ? "…" : exportedPath ? t("Exportálva ✓") : t("Export Obsidianba")}
                </button>
                <button className="btn-sm btn-outline" type="button" onClick={transcribe} disabled={busy !== ""}>
                  {busy === "transcribe" ? "…" : t("Újra-átirat")}
                </button>
              </div>
              {exportedPath && <p className="setting-hint">{exportedPath}</p>}

              <div className="transcript-segments" style={{ marginTop: 12 }}>
                {capture.segments.map((seg, i) => (
                  <div key={i} className="segment">
                    <span
                      className="segment-time"
                      style={{ cursor: "pointer" }}
                      title={t("Kattints az átnevezéshez — a rendszer a hangot is megtanulja")}
                      onClick={() => renameSpeaker(seg.speaker)}
                    >
                      {speakerLabel(seg.speaker)}
                    </span>
                    <span className="segment-text">{seg.text}</span>
                  </div>
                ))}
              </div>
            </>
          )}
          {error && <p className="setting-error">{error}</p>}
        </div>
      )}
    </section>
  );
}
