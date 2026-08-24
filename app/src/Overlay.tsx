// Always-on pill (top of the screen, Wispr Flow concept, custom animation).
// The window is TRANSPARENT; the frontend sets its size: invoke("set_pill_size",{width,height}),
// Rust aligns it to the top-center of the screen.
//
// Three main states:
//   1) IDLE = tiny, barely visible dark line at the top center (unobtrusive).
//   2) HOVER = hovering the line smoothly unfolds the control bar (mode menu + label + mic).
//   3) DICTATION = prominent "listening" feedback (waveform + red dot + glow),
//      even without hover — also activated by the ⌃⇧Space (trigger-dictation) event.
//   + Transcription: shimmer + "Transcribing…". Done: grows downward, shows the transcript.
import { useState, useEffect, useCallback, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen, emit } from "@tauri-apps/api/event";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Pencil, RotateCcw, Square } from "lucide-react";
import { isPermissionGranted, requestPermission, sendNotification } from "@tauri-apps/plugin-notification";
import { BarContent, BarPanel, type BarContentProps, type BarPanelKind } from "./components/BarContent";
import { WAVE_BARS } from "./components/Waveform";
import { DEFAULT_MODE, type ModeId } from "./modes";
import { loadAutoRecord } from "./lib/calendar";
// NOTE: t() keys are Hungarian source strings (gettext-style, see lib/i18n.ts)
// — keep them byte-identical; the English UI copy lives in lib/i18n-en.ts.
import { t } from "./lib/i18n";
import type { TranscriptResult } from "./lib/types";
import "./Overlay.css";

// Event payload of the Meet Bridge (extension → Rust → here).
type MeetInfo = { meetCode: string; title: string; participants: string[] };

/** Lavox brand mark — descending lines + surface dot. The brand dot of the closed pill and the notch.
 *  Consistent with the landing page, the sidebar and the store icon. */
function LavoxMark({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 120 120" fill="none" aria-hidden>
      <rect x="20" y="26" width="70" height="12" rx="6" fill="#7fe3a6" />
      <rect x="27" y="49" width="63" height="12" rx="6" fill="#4fbf84" />
      <rect x="34" y="72" width="53" height="12" rx="6" fill="#3fae72" />
      <circle cx="61" cy="101" r="8.5" fill="#7fe3a6" />
    </svg>
  );
}

type Phase = "ready" | "recording" | "transcribing" | "done" | "error";

// Notch data returned by Rust `get_notch_info` (logical points = CSS px).
interface NotchInfo {
  has_notch: boolean;
  notch_height: number;
  notch_left: number;
  notch_right: number;
  screen_width: number;
  scale: number;
}

// The strip on each side of the notch (CSS px) — narrow, leaves room only for the dropdown.
const NOTCH_SIDE_W = 78;

// Selectable languages (whisper ISO codes). Multiple enabled → auto-detect.
const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "hu", label: "Hungarian (Magyar)" },
];

// ── Animation parameters (tuned in one place) ───────────────────────────────
// Softer, "silky" springs for the size/shape morph: lower stiffness +
// higher damping → no jumping, no bouncing, glides fluidly (Wispr feel).
// The layout morph (tiny line ↔ bar ↔ expansion) has its own, slightly softer spring.
export const LAYOUT_SPRING = { type: "spring" as const, stiffness: 230, damping: 26, mass: 0.9 };
// The expanding transcript panel is a bit springier (a palpable "growing out" move).
const PANEL_SPRING = { type: "spring" as const, stiffness: 240, damping: 28, mass: 0.95 };
// Fast, smooth cross-fade of the center content (wave/shimmer/label).
const CONTENT_FADE = { duration: 0.14, ease: [0.22, 0.61, 0.36, 1] as const };

// Logical window size for each state (set_pill_size is called with this).
// The window is LARGER than the visible pill — the surrounding shadow/glow and
// the downward-opening menu FIT inside it (otherwise the window edge would clip
// them). The pill sits slightly below the window top, centered (see .pill
// margin-top + .pill-wrap).
const SIZES = {
  idle: { width: 200, height: 30 },
  hover: { width: 360, height: 82 },
  recording: { width: 360, height: 82 },
  transcribing: { width: 360, height: 82 },
  done: { width: 380, height: 240 },
  menu: { width: 400, height: 330 },
  // Meeting REC capsule: red pill + timer visible even without hover.
  meetrec: { width: 210, height: 56 },
} as const;

// 220ms grace period before the control bar collapses back to the line.
// (Was 400ms — too "sticky"; this is enough to avoid closing on an
// accidental mouse-out while keeping the interaction snappy.)
const LEAVE_DELAY_MS = 220;

function Overlay() {
  const reduceMotion = useReducedMotion();
  const [mode, setMode] = useState<ModeId>(DEFAULT_MODE);
  const [phase, setPhase] = useState<Phase>("ready");
  // The status text is set by setStatus (error/state); the bar now shows buttons,
  // so the getter is currently not rendered (errors show via the data-phase glow).
  const [_status, setStatus] = useState("");
  const [transcript, setTranscript] = useState<TranscriptResult | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  // Dictation correction: the edited text teaches the personal dictionary.
  const [editDraft, setEditDraft] = useState<string | null>(null);
  const editingRef = useRef(false);
  const lastRawRef = useRef<string>("");
  const [hovered, setHovered] = useState(false);
  // Real-time microphone levels for the waveform (rolling buffer, newest at the end).
  const [levels, setLevels] = useState<number[]>(() => new Array(WAVE_BARS).fill(0));
  // Open sub-panel (language / device pickers) — for the Wispr buttons.
  const [panel, setPanel] = useState<BarPanelKind | null>(null);
  // Video control menu (opened by the 🎥 button; the bar footprint does not change).
  const [videoMenu, setVideoMenu] = useState(false);
  const [cameraOn, setCameraOn] = useState(true);
  const [barMics, setBarMics] = useState<string[]>([]);
  const [barDisplays, setBarDisplays] = useState<string[]>([]);
  const [selectedMic, setSelectedMic] = useState("");
  const [selectedDisplay, setSelectedDisplay] = useState(0);
  // Whether the notebook window is FOCUSED — THIS decides the dictation routing. If
  // you click into another app, the notebook loses focus → dictation goes there, not here.
  const [notebookFocused, setNotebookFocused] = useState(false);
  const notebookFocusedRef = useRef(false);
  notebookFocusedRef.current = notebookFocused;
  // The bar's 📝 button: opens the small Lavox Notes notebook window.
  const openNotebook = useCallback(() => {
    invoke("show_notebook").catch(() => {});
    setNotebookFocused(true); // gains focus when opened
  }, []);
  useEffect(() => {
    invoke<boolean>("is_notebook_focused").then(setNotebookFocused).catch(() => {});
    const unFocus = listen<boolean>("notebook-focus", (e) => setNotebookFocused(!!e.payload));
    const unClosed = listen("notebook-closed", () => setNotebookFocused(false));
    return () => {
      unFocus.then((f) => f()).catch(() => {});
      unClosed.then((f) => f()).catch(() => {});
    };
  }, []);
  // Enabled languages (ISO codes) — stored by the backend, used by transcribe.
  const [langs, setLangs] = useState<string[]>([]);
  useEffect(() => {
    invoke<string[]>("get_languages").then(setLangs).catch(() => {});
  }, []);
  const toggleLang = useCallback((code: string) => {
    setLangs((prev) => {
      const next = prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code];
      const final = next.length ? next : [code]; // at least 1 language
      invoke("set_languages", { langs: final }).catch(() => {});
      return final;
    });
  }, []);

  // ── MEETING RECORDING (Meet Bridge) ────────────────────────────────────────
  // The extension signals joining → prompt ("Record this?") or auto-start.
  // While recording, the pill becomes a red REC capsule; click = stop.
  const [meetPrompt, setMeetPrompt] = useState<MeetInfo | null>(null);
  const [meetRec, setMeetRec] = useState<{ title: string; startedAt: number; kind: "meeting" | "video" } | null>(null);
  const [meetElapsed, setMeetElapsed] = useState(0);
  const meetRecRef = useRef<{ title: string; startedAt: number; kind: "meeting" | "video" } | null>(null);
  meetRecRef.current = meetRec;

  // Auto-record from the BACKEND (auto_record.json) — survives a reinstall,
  // unlike localStorage. The meet-joined decision reads this ref.
  const autoRecordRef = useRef<boolean>(loadAutoRecord());
  useEffect(() => {
    invoke<boolean>("get_auto_record").then((v) => (autoRecordRef.current = v)).catch(() => {});
    const un = listen<boolean>("auto-record-changed", (e) => {
      autoRecordRef.current = !!e.payload;
    });
    return () => {
      un.then((f) => f()).catch(() => {});
    };
  }, []);

  const notify = useCallback(async (title: string, body: string) => {
    try {
      let granted = await isPermissionGranted();
      if (!granted) granted = (await requestPermission()) === "granted";
      if (granted) sendNotification({ title, body });
    } catch { /* non-critical */ }
  }, []);

  const startMeetRec = useCallback(async (info: MeetInfo) => {
    setMeetPrompt(null);
    try {
      await invoke("start_meeting_record");
      setMeetRec({ title: info.title || info.meetCode || "Meeting", startedAt: Date.now(), kind: "meeting" });
    } catch (e) {
      const msg = String(e);
      // "already running" — error string from the Rust
      // backend, matched verbatim; do not translate.
      if (msg.includes("already running")) {
        // desync guard: the backend is already recording → adopt its state
        setMeetRec({ title: info.title || "Meeting", startedAt: Date.now(), kind: "meeting" });
      } else {
        notify("LAVOX", t("Felvétel indítás sikertelen: {msg}").replace("{msg}", msg));
      }
    }
  }, [notify]);

  // Manual VIDEO recording from the bar — anytime, even without a meeting
  // (screen + system audio + microphone, same machinery).
  const cameraOnRef = useRef(true);
  cameraOnRef.current = cameraOn;
  const startVideoRec = useCallback(async () => {
    if (meetRecRef.current) return;
    const now = new Date();
    const title = `${t("Videó")} ${now.toLocaleDateString("hu-HU", { month: "short", day: "numeric" })} ${now.toLocaleTimeString("hu-HU", { hour: "2-digit", minute: "2-digit" })}`;
    try {
      // Open the camera bubble (PiP) BEFORE recording, and ONLY if the face
      // is enabled. Without a camera/permission, the bubble closes itself.
      if (cameraOnRef.current) {
        await invoke("show_camera_bubble").catch(() => {});
      }
      await invoke("start_video_record");
      setMeetRec({ title, startedAt: Date.now(), kind: "video" });
    } catch (e) {
      const msg = String(e);
      // "already running" (Rust backend error, matched verbatim)
      if (msg.includes("already running")) {
        setMeetRec({ title, startedAt: Date.now(), kind: "video" });
      } else {
        notify("LAVOX", t("Videó indítás sikertelen: {msg}").replace("{msg}", msg));
      }
    }
  }, [notify]);

  // Manual MEETING recording from the bar — even WITHOUT the extension and calendar.
  // (Previously a meeting only started on the Meet extension's signal or via
  // calendar auto-record; without either, Meeting mode couldn't be started.)
  const startMeetingManual = useCallback(async () => {
    if (meetRecRef.current) return;
    const now = new Date();
    const title = `Meeting ${now.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
    try {
      await invoke("start_meeting_record");
      setMeetRec({ title, startedAt: Date.now(), kind: "meeting" });
    } catch (e) {
      const msg = String(e);
      // "already running" (Rust backend error, matched verbatim)
      if (msg.includes("already running")) {
        setMeetRec({ title, startedAt: Date.now(), kind: "meeting" });
      } else {
        notify("LAVOX", t("Felvétel indítás sikertelen: {msg}").replace("{msg}", msg));
      }
    }
  }, [notify]);

  // ── VIDEO CONTROL MENU (opened by the 🎥 button) ───────────────────────────
  const openVideoMenu = useCallback(() => {
    setVideoMenu(true);
    setPanel(null);
    invoke<string[]>("list_mics").then(setBarMics).catch(() => {});
    invoke<string[]>("list_displays").then(setBarDisplays).catch(() => {});
    invoke<string | null>("get_recording_mic").then((m) => setSelectedMic(m ?? "")).catch(() => {});
    invoke<number>("get_recording_display").then(setSelectedDisplay).catch(() => {});
  }, []);
  const closeVideoMenu = useCallback(() => {
    setVideoMenu(false);
    setPanel(null);
  }, []);
  const selectMic = useCallback((name: string) => {
    setSelectedMic(name);
    invoke("set_recording_mic", { name: name || null }).catch(() => {});
    setPanel(null);
  }, []);
  const selectDisplay = useCallback((i: number) => {
    setSelectedDisplay(i);
    invoke("set_recording_display", { index: i }).catch(() => {});
    setPanel(null);
  }, []);
  // Face (camera bubble) on/off — shows/hides it live during recording.
  const toggleCamera = useCallback(() => {
    setCameraOn((prev) => {
      const next = !prev;
      if (meetRecRef.current?.kind === "video") {
        if (next) invoke("show_camera_bubble").catch(() => {});
        else invoke("hide_camera_bubble").catch(() => {});
      }
      return next;
    });
  }, []);

  const stopMeetRec = useCallback(async () => {
    const rec = meetRecRef.current;
    if (!rec) return;
    setMeetRec(null);
    try {
      const raw = await invoke<string>("stop_meeting_record", { title: rec.title });
      // Close the bubble AFTER the screen stop (keep it in until the last frame).
      await invoke("hide_camera_bubble").catch(() => {});
      // JSON: { mic: path, system: path|null } — the old format (plain path) is also accepted
      let mic = raw;
      let system: string | null = null;
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object" && parsed.mic) {
          mic = parsed.mic;
          system = parsed.system ?? null;
        }
      } catch { /* plain path — old backend */ }
      if (system) {
        notify(t("LAVOX — meeting mentve (2 sáv)"), `${t("Mikrofon + rendszerhang:")}\n${mic}\n${system}`);
      } else {
        notify(
          t("LAVOX — meeting mentve (csak mikrofon!)"),
          `${mic}\n${t("A többiek hangjához engedélyezd: Rendszerbeállítások → Adatvédelem → Képernyő- és rendszerhang-felvétel → Lavox Hub")}`,
        );
      }
      // Automatic transcript after saving — on error, a LOUD notification; it
      // must never die silently. rec_id is the recording folder name from the mic path.
      const recId = mic.split("/").slice(-2, -1)[0];
      if (recId) {
        invoke("remote_transcribe_meeting", { recId, harvest: localStorage.getItem("lavox-harvest") !== "false" })
          .then(() => notify(t("LAVOX — átirat kész"), rec.title || recId))
          .catch((e) =>
            notify(
              t("LAVOX — ÁTIRAT HIBA"),
              `${String(e)}\n${t("Ellenőrizd a szerver-beállítást (Beállítások → Beszélők).")}`,
            ),
          );
      }
    } catch (e) {
      notify("LAVOX", t("Mentés hiba: {msg}").replace("{msg}", String(e)));
    }
  }, [notify]);

  // Bridge events from the extension (forwarded by Rust bridge.rs).
  useEffect(() => {
    const unJoin = listen<MeetInfo>("meet-joined", (e) => {
      if (meetRecRef.current) return; // already recording
      if (autoRecordRef.current) {
        startMeetRec(e.payload);
      } else {
        setMeetPrompt(e.payload);
      }
    });
    const unLeft = listen<MeetInfo>("meet-left", () => {
      setMeetPrompt(null);
      // Only stop the MEETING recording on leave — a manual video keeps going.
      if (meetRecRef.current?.kind === "meeting") stopMeetRec();
    });
    return () => {
      unJoin.then((f) => f()).catch(() => {});
      unLeft.then((f) => f()).catch(() => {});
    };
  }, [startMeetRec, stopMeetRec]);

  // REC timer (mm:ss in the capsule).
  useEffect(() => {
    if (!meetRec) { setMeetElapsed(0); return; }
    const iv = window.setInterval(() => {
      setMeetElapsed(Math.floor((Date.now() - meetRec.startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(iv);
  }, [meetRec]);

  const fmtElapsed = `${Math.floor(meetElapsed / 60)}:${String(meetElapsed % 60).padStart(2, "0")}`;

  // Notch info (Dynamic Island-style compact layout). Fetched at startup; then
  // updated EVENT-DRIVEN: the backend's display-reconfiguration callback (monitor
  // connects/disconnects, resolution/scaling changes) sends "notch-refreshed" —
  // no polling.
  const [notch, setNotch] = useState<NotchInfo | null>(null);
  useEffect(() => {
    invoke<NotchInfo>("get_notch_info").then(setNotch).catch(() => {});
    const un = listen<NotchInfo>("notch-refreshed", (e) => {
      const n = e.payload;
      setNotch((prev) =>
        prev &&
        prev.has_notch === n.has_notch &&
        prev.notch_left === n.notch_left &&
        prev.notch_right === n.notch_right &&
        prev.notch_height === n.notch_height &&
        prev.screen_width === n.screen_width
          ? prev
          : n,
      );
    });
    return () => {
      un.then((f) => f()).catch(() => {});
    };
  }, []);
  const modelPathRef = useRef<string | null>(null);
  const runningRef = useRef(false);
  const recordingRef = useRef(false); // whether currently recording (toggle: 1st ⌃⇧Space start, 2nd stop)
  const modeRef = useRef<ModeId>(DEFAULT_MODE);
  const leaveTimer = useRef<number | null>(null);
  // For window sizing: the previous area (for the grow/shrink decision) +
  // the delayed-shrink timer.
  const prevAreaRef = useRef(0);
  const resizeTimer = useRef<number | null>(null);

  const busy = phase === "recording" || phase === "transcribing";
  const doneOpen = (phase === "done" && !!transcript) || editDraft !== null;
  // The control bar is visible when: hover OR menu open OR dictating/transcribing
  // OR a finished transcript OR an error OR a meeting prompt awaiting an answer.
  const controlsVisible = hovered || menuOpen || busy || doneOpen || phase === "error" || !!panel || !!meetPrompt || videoMenu;

  // NOTCH MODE (Dynamic Island): the window goes to the top of the notch (y=0, Rust),
  // the content sits on the two sides of the notch (compact), and when open it blooms BELOW the notch.
  const notched = !!notch?.has_notch;
  const notchH = notched ? notch!.notch_height : 0;
  const notchW = notched ? notch!.notch_right - notch!.notch_left : 0;
  const niWindowW = notchW + 2 * NOTCH_SIDE_W;
  // Sub-panel height: the device lists grow with the item count (small top+bottom
  // margins), the language panel is fixed.
  const panelH =
    panel === "language" ? 188
    : panel === "videomic" ? Math.min(220, 60 + (barMics.length + 1) * 34)
    : panel === "videoscreen" ? Math.min(220, 60 + Math.max(1, barDisplays.length) * 34)
    : 0;

  // Derive the current window size from state (priority from top to bottom).
  let size: { width: number; height: number };
  if (doneOpen) size = SIZES.done;
  else if (menuOpen) size = SIZES.menu;
  else if (phase === "recording" || phase === "transcribing") size = SIZES.recording;
  else if (controlsVisible) size = SIZES.hover;
  else if (meetRec) size = SIZES.meetrec;
  else size = SIZES.idle;

  if (notched) {
    // The window width is CONSTANT (no jump on open → no horizontal glitch),
    // only the height grows. The glass animates within it.
    size = {
      width: niWindowW,
      height: notchH + (size.height - 12) + (panel ? panelH + 16 : 0),
    };
  } else if (panel && !doneOpen) {
    // PILL: when the panel opens, the window grows downward to fit it
    // (the notch shell already handles panelH on its own branch).
    size = { width: size.width, height: SIZES.hover.height + panelH + 16 };
  }

  // The frontend sizes the window (Rust aligns it top-center). We also signal
  // whether we are idle — only then does the pill follow the cursor across
  // screens (in an active state, size-positioning puts it on the cursor's monitor anyway).
  useEffect(() => {
    const apply = () =>
      invoke("set_pill_size", { width: size.width, height: size.height }).catch(() => {});

    // SMOOTH EXPANSION: the window must always be large enough for the content
    // CURRENTLY animating.
    // - GROWING (opening): enlarge the window immediately → the pill has room to grow.
    // - SHRINKING (closing): WAIT for the content animation (~320ms), otherwise the
    //   window clips the still-animating pill → it looks "choppy".
    const area = size.width * size.height;
    const growing = area >= prevAreaRef.current;
    prevAreaRef.current = area;
    if (resizeTimer.current !== null) {
      window.clearTimeout(resizeTimer.current);
      resizeTimer.current = null;
    }
    if (growing) {
      apply();
    } else {
      resizeTimer.current = window.setTimeout(() => {
        apply();
        resizeTimer.current = null;
      }, 320);
    }

    const isIdle =
      size.width === SIZES.idle.width && size.height === SIZES.idle.height;
    invoke("set_follow_enabled", { enabled: isIdle }).catch(() => {});
  }, [size.width, size.height]);

  // The overlay window's body: no margin/scrolling, TRANSPARENT background so
  // the desktop shows through around the pill. Set at runtime (because of the
  // Vite global CSS bundle we can't put it in CSS — it would affect the main window too).
  useEffect(() => {
    const b = document.body.style;
    const h = document.documentElement.style; // the <html> (:root) — App.css paints it white!
    const prev = { m: b.margin, o: b.overflow, bg: b.background, hbg: h.background };
    b.margin = "0";
    b.overflow = "hidden";
    b.background = "transparent";
    // The white square's cause: App.css `:root { background: #fff }` applies globally.
    // The <html> background must also be transparent, otherwise it stays white around the pill.
    h.background = "transparent";
    return () => {
      b.margin = prev.m;
      b.overflow = prev.o;
      b.background = prev.bg;
      h.background = prev.hbg;
    };
  }, []);

  // Dictation TOGGLE: 1st ⌃⇧Space → recording starts (unlimited time),
  // 2nd ⌃⇧Space → stop + transcript + insert at the cursor.
  // (StreamingRecorder = mic, no fixed time limit.)
  const startDictation = useCallback(async () => {
    if (recordingRef.current || runningRef.current) return;
    try {
      if (!modelPathRef.current) {
        modelPathRef.current = await invoke<string>("find_model");
      }
      setLevels(new Array(WAVE_BARS).fill(0)); // clean flat wave at start
      await invoke("start_dictation_record");
      recordingRef.current = true;
      invoke("dbg_log", { msg: "REC_START" }).catch(() => {});
      setTranscript(null);
      setPhase("recording");
      setStatus(t("Hallgatlak… engedd el a leállításhoz"));
    } catch (e) {
      const msg = String(e);
      // Stuck-state guard: if the backend is already recording (frontend-backend
      // desync, e.g. after a hot reload), treat it as recording → the next
      // ⌃⇧Space stops it instead of getting stuck. "Már fut" = the backend's
      // Hungarian "already running" error, matched verbatim.
      if (msg.includes("already running") || msg.toLowerCase().includes("already")) {
        recordingRef.current = true;
        setPhase("recording");
        setStatus(t("Felvétel… (engedd el a triggert a leállításhoz)"));
      } else {
        recordingRef.current = false;
        setPhase("error");
        setStatus(t("Hiba: {msg}").replace("{msg}", msg));
      }
    }
  }, []);

  const stopAndTranscribe = useCallback(async () => {
    if (!recordingRef.current || runningRef.current) return;
    recordingRef.current = false;
    runningRef.current = true;
    const t0 = performance.now();
    invoke("dbg_log", { msg: "STOP_REQ" }).catch(() => {});
    setPhase("transcribing");
    setStatus(t("Átirat…"));
    try {
      const wavPath = await invoke<string>("stop_dictation_record");
      const t1 = performance.now();
      invoke("dbg_log", { msg: `WAV_SAVED +${Math.round(t1 - t0)}ms` }).catch(() => {});
      const modelPath = modelPathRef.current as string;
      const result = await invoke<TranscriptResult>("transcribe_wav", { wavPath, modelPath });
      const t2 = performance.now();
      const words = result.full_text.trim() ? result.full_text.trim().split(/\s+/).length : 0;
      invoke("dbg_log", { msg: `TRANSCRIBED +${Math.round(t2 - t1)}ms words=${words}` }).catch(() => {});
      setTranscript(result);
      lastRawRef.current = result.full_text.trim();
      setPhase("done");
      setStatus("");
      // NOTEBOOK ROUTING: dictated text goes into a NOTE only if Lavox Notes is
      // the FOCUSED window. If you clicked into another app, it is pasted there.
      if (result.full_text.trim()) {
        if (notebookFocusedRef.current) {
          // The notebook is the focused window → dictated text goes to the
          // editor's CURSOR (into the active note), not as a new note.
          emit("notebook-dictate", result.full_text.trim()).catch(() => {});
        } else {
          await invoke("insert_text", { text: result.full_text }).catch(() => {});
          // Lavox Memory: the finished dictation flows into memory (fire-and-forget,
          // never slows the insert — silently no-ops without the server too).
          invoke("memory_ingest_dictation", { text: result.full_text }).catch(() => {});
          const t3 = performance.now();
          invoke("dbg_log", {
            msg: `INSERTED +${Math.round(t3 - t2)}ms · e2e(stop→insert)=${Math.round(t3 - t0)}ms`,
          }).catch(() => {});
        }
      }
      // IMPORTANT (fix for the 2nd-dictation bug): after inserting, the pill
      // AUTOMATICALLY collapses back to the line after ~1.3s. This way every
      // dictation starts from clean IDLE (like the 1st, working round), and the
      // open pill doesn't steal focus on the next Cmd+V. (Wispr style: the text
      // is at the cursor, the pill doesn't stay open.)
      window.setTimeout(() => {
        if (!recordingRef.current && !runningRef.current && !editingRef.current) {
          setPhase((p) => (p === "done" ? "ready" : p));
          setTranscript(null);
          setMenuOpen(false);
          setHovered(false);
        }
      }, 2200);
    } catch (e) {
      setPhase("error");
      setStatus(t("Hiba: {msg}").replace("{msg}", String(e)));
    } finally {
      runningRef.current = false;
    }
  }, []);

  const startCorrection = useCallback(() => {
    editingRef.current = true;
    setEditDraft((transcript?.full_text ?? lastRawRef.current).trim());
  }, [transcript]);

  const closeCorrection = useCallback(() => {
    editingRef.current = false;
    setEditDraft(null);
    setTranscript(null);
    setPhase((p) => (p === "done" ? "ready" : p));
  }, []);

  const saveCorrection = useCallback(() => {
    const corrected = (editDraft ?? "").trim();
    const raw = lastRawRef.current;
    closeCorrection();
    if (corrected && raw && corrected !== raw) {
      invoke("dictation_learn", { raw, corrected }).catch(() => {});
      setStatus(t("Szótár frissítve"));
      window.setTimeout(() => setStatus(""), 1800);
    }
  }, [editDraft, closeCorrection]);

  // ⌃⇧Space / mic button: if recording → stop+transcribe, otherwise → start recording.
  const recordAndTranscribe = useCallback(() => {
    if (recordingRef.current) stopAndTranscribe();
    else startDictation();
  }, [startDictation, stopAndTranscribe]);

  // Mode switch from the radial menu.
  const switchMode = useCallback(
    (next: ModeId) => {
      setMode(next);
      modeRef.current = next;
      setTranscript(null);
      setStatus("");
      setPhase("ready");
      if (next === "dictation") recordAndTranscribe();
    },
    [recordAndTranscribe]
  );

  // PUSH-TO-TALK: ⌃⇧Space held → recording; released → stop + transcript + insert.
  // Rust sends two events: "dictation-start" (Pressed) and "dictation-stop" (Released).
  useEffect(() => {
    const unStart = listen("dictation-start", () => {
      invoke("dbg_log", { msg: `JS_GOT_DICTATION_START mode=${modeRef.current}` }).catch(() => {});
      if (modeRef.current === "dictation") startDictation();
    });
    const unStop = listen("dictation-stop", () => {
      invoke("dbg_log", { msg: `JS_GOT_DICTATION_STOP mode=${modeRef.current}` }).catch(() => {});
      if (modeRef.current === "dictation") stopAndTranscribe();
    });
    return () => {
      unStart.then((fn) => fn()).catch(() => {});
      unStop.then((fn) => fn()).catch(() => {});
    };
  }, [startDictation, stopAndTranscribe]);

  // REAL-TIME WAVEFORM: Rust sends the microphone RMS level every ~50ms.
  // We normalize it (silence → ~0, speech → ~1) and push it into a rolling buffer:
  // the new sample enters on the right, old ones slide out left → a live, speech-reactive wave.
  useEffect(() => {
    const un = listen<number>("mic-level", (e) => {
      const rms = typeof e.payload === "number" ? e.payload : 0;
      // Subtract the noise floor + square-root curve → quiet speech shows, silence stays flat.
      const norm = Math.min(1, Math.sqrt(Math.max(0, rms - 0.004) * 7));
      setLevels((prev) => [...prev.slice(1), norm]);
    });
    return () => {
      un.then((fn) => fn()).catch(() => {});
    };
  }, []);

  // Esc → close the menu/transcript (the pill doesn't disappear, just returns to the line).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setMenuOpen(false);
        setHovered(false);
        setPanel(null);
        setVideoMenu(false);
        // Finished transcript and error states also reset → the pill collapses to the line.
        if (phase === "done" || phase === "error") setPhase("ready");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [phase]);

  // Hover handling: expand immediately on enter; collapse with a delay on leave,
  // but only if no dictation/transcription/transcript/menu started meanwhile
  // (controlsVisible keeps those open regardless of the hover flag anyway).
  const clearLeaveTimer = () => {
    if (leaveTimer.current !== null) {
      window.clearTimeout(leaveTimer.current);
      leaveTimer.current = null;
    }
  };
  const onEnter = () => {
    clearLeaveTimer();
    setHovered(true);
  };
  const onLeave = () => {
    clearLeaveTimer();
    leaveTimer.current = window.setTimeout(() => {
      setHovered(false);
      leaveTimer.current = null;
    }, LEAVE_DELAY_MS);
  };
  // Clear the timer on unmount.
  useEffect(() => () => clearLeaveTimer(), []);

  // HOVER WITHOUT FOCUS: Rust natively tracks the cursor relative to the window
  // frame (WKWebView gives no DOM hover in the background) and sends a
  // "pill-hover" event → we open/close exactly like on mouse-enter. So whichever
  // app has focus, the pill opens on hover (Wispr experience).
  useEffect(() => {
    const un = listen<boolean>("pill-hover", (e) => {
      if (e.payload) {
        clearLeaveTimer();
        setHovered(true);
      } else {
        clearLeaveTimer();
        leaveTimer.current = window.setTimeout(() => {
          setHovered(false);
          leaveTimer.current = null;
        }, LEAVE_DELAY_MS);
      }
    });
    return () => {
      un.then((fn) => fn()).catch(() => {});
    };
  }, []);

  const isDictation = mode === "dictation";

  // Props of the shared bar content (5 buttons + center state + panel) — BOTH shells get this.
  const barProps: BarContentProps = {
    phase,
    isDictation,
    levels,
    panel,
    langs,
    languages: LANGUAGES,
    meetPrompt: meetPrompt ? { title: meetPrompt.title } : null,
    meetRec: meetRec ? { kind: meetRec.kind } : null,
    fmtElapsed,
    reduceMotion,
    videoMenu,
    cameraOn,
    mics: barMics,
    displays: barDisplays,
    selectedMic,
    selectedDisplay,
    onMic: () => (isDictation ? recordAndTranscribe() : switchMode("dictation")),
    onNotebook: openNotebook,
    onTogglePanel: (p) => setPanel((cur) => (cur === p ? null : p)),
    onToggleLang: toggleLang,
    onStartMeet: () => meetPrompt && startMeetRec(meetPrompt),
    onStartMeetingManual: () => startMeetingManual(),
    onDismissMeet: () => setMeetPrompt(null),
    onStopMeet: stopMeetRec,
    onOpenVideoMenu: openVideoMenu,
    onCloseVideoMenu: closeVideoMenu,
    onStartVideo: () => startVideoRec(),
    onToggleCamera: toggleCamera,
    onSelectMic: selectMic,
    onSelectDisplay: selectDisplay,
  };

  return (
    // The wrapper catches the hover events; transparent, fills the window, and
    // aligns the pill to top-center. The pill itself is the floating element.
    <div className="pill-wrap" onMouseEnter={onEnter} onMouseLeave={onLeave}>
      {/* A SINGLE shell that is always present. On hover it first NARROWS with a
          KEYFRAME (anticipation), then EXPANDS into the pill (widens + thickens),
          making room for the content — which floats in only after the expansion. */}
      {/* NOTCH GLASS (mockup): a unified Liquid Glass capsule around the notch.
          Top: two teal pills on the TWO sides of the notch. Bottom (open): waveform. */}
      {notched && (() => {
        // NARROW: the glass width is the same CLOSED = OPEN (notch + two beads, with
        // a small margin so the beads stay INSIDE the rounded corner). Open, it only grows DOWNWARD.
        const glassW = notchW + 108;
        const glassH = controlsVisible ? notchH + 66 + panelH : notchH + 6;
        return (
          <motion.div
            className="pill ni-glass"
            data-recording={phase === "recording"}
            data-phase={phase}
            style={{ marginTop: 0 }}
            animate={{ width: glassW, height: glassH }}
            transition={
              reduceMotion ? { duration: 0 } : { type: "spring", stiffness: 220, damping: 24, mass: 0.9 }
            }
          >
            {/* Top row (notch level): two icon beads on the two sides of the notch —
                both closed AND open. The glass is slightly wider + smaller corner, so
                the beads sit INSIDE the rounded corner and don't stick out. */}
            {/* Top row (notch level): STATUS on the two sides of the notch (NOT a button →
                no duplication with the function buttons below). Left = status dot, right = brand. */}
            <div className="ni-toprow" style={{ height: notchH }}>
              <div className="ni-tpill ni-tpill-left">
                <span className="ni-dot" data-recording={phase === "recording" || !!meetRec} />
              </div>
              <div className="ni-notchgap" style={{ width: notchW }} />
              <div className="ni-tpill ni-tpill-right">
                {meetRec ? (
                  <span className="ni-rec-time">{fmtElapsed}</span>
                ) : (
                  <LavoxMark size={13} />
                )}
              </div>
            </div>
            {/* Bottom row (open): waveform + STOP while recording; otherwise the 4 Wispr
                buttons (language · mic · scratchpad) — each a SEPARATE function, no duplication. */}
            <AnimatePresence>
              {controlsVisible && (
                <motion.div
                  className="ni-bar"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={reduceMotion ? { duration: 0 } : { duration: 0.18, delay: 0.05, ease: "easeOut" }}
                >
                  <BarContent {...barProps} />
                </motion.div>
              )}
            </AnimatePresence>
            {/* Sub-panel (language / device pickers) — the shared BarPanel, identical in both shells. */}
            <AnimatePresence>
              {panel && <BarPanel {...barProps} />}
            </AnimatePresence>
          </motion.div>
        );
      })()}
      {!notched && (() => {
        const expandedW = doneOpen ? 340 : 330;
        // NOTCH MODE: idle, the glass is a THIN STRIP below the notch (the green tabs
        // beside it); on hover it blooms out from UNDER the notch. Non-notch: thin bar ↔ pill.
        // Meeting REC collapsed: not a thin line but a visible red capsule.
        const meetRecCompact = !!meetRec && !controlsVisible;
        const shellW = controlsVisible ? expandedW : notched ? notchW + 24 : meetRecCompact ? 190 : 130;
        // Explicit height + overflow:hidden → the ALWAYS-mounted content is
        // clipped when collapsed (it doesn't leave the DOM). APPLE PRINCIPLE:
        // don't remove/re-add elements mid-morph, only animate them.
        // Collapsed: 18px — fits the Lavox logo mark (brand presence even closed).
        // Panel (language/device) open: the pill grows downward to fit the panel.
        const shellH = doneOpen
          ? 200
          : panel && controlsVisible
            ? 46 + panelH + 16
            : controlsVisible
              ? 46
              : notched
                ? 6
                : meetRecCompact
                  ? 40
                  : 18;
        const idleLogoVisible = !controlsVisible && !meetRecCompact && !notched;
        return (
          <motion.div
            className="pill"
            data-collapsed={!controlsVisible && !meetRecCompact}
            data-recording={phase === "recording"}
            data-meetrec={meetRecCompact}
            data-phase={phase}
            data-tauri-drag-region
            style={{ overflow: menuOpen || panel ? "visible" : "hidden", marginTop: notched ? notchH : 12 }}
            animate={{ width: shellW, height: shellH }}
            transition={
              reduceMotion
                ? { duration: 0 }
                : {
                    // APPLE-LIKE "liquid" spring: the glass drips fluidly from under
                    // the notch, settling gently — one continuous motion.
                    type: "spring",
                    stiffness: 220,
                    damping: 24,
                    mass: 0.9,
                  }
            }
          >
            {/* Lavox logo mark in the CLOSED pill — brand presence at a barely-visible size */}
            <AnimatePresence>
              {idleLogoVisible && (
                <motion.div
                  className="pill-idle-logo"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 0.92 }}
                  exit={{ opacity: 0 }}
                  transition={reduceMotion ? { duration: 0 } : CONTENT_FADE}
                >
                  <LavoxMark size={13} />
                </motion.div>
              )}
            </AnimatePresence>
            {/* Meeting REC mini capsule — in the collapsed state, INSTEAD of the line:
                a red pulsing pill + timer. Click = stop + save. */}
            <AnimatePresence>
              {meetRecCompact && (
                <motion.button
                  className="pill-meetrec-mini"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={reduceMotion ? { duration: 0 } : CONTENT_FADE}
                  onClick={stopMeetRec}
                  title={t("Meeting felvétel — kattints a leállításhoz")}
                  aria-label={t("Meeting felvétel leállítása")}
                >
                  <span className="pill-rec-dot" />
                  <span>REC {fmtElapsed}</span>
                  <Square size={9} strokeWidth={2.6} fill="currentColor" />
                </motion.button>
              )}
            </AnimatePresence>
            {/* Control bar — ALWAYS in the DOM (Apple principle), only the OPACITY
                animates along with the size → one coordinated move, no mount/unmount gap.
                When collapsed, overflow:hidden clips it and opacity:0 hides it. */}
            <motion.div
              className="pill-bar"
              data-tauri-drag-region
              animate={{ opacity: controlsVisible ? 1 : 0 }}
              transition={
                reduceMotion
                  ? { duration: 0 }
                  : { duration: 0.2, ease: "easeOut", delay: controlsVisible ? 0.1 : 0 }
              }
            >
              {/* The shared bar content (5 direct buttons / center state) — the SAME as
                  in the notch shell. The pill now shows the 5 buttons when idle (not the
                  old CircleMenu + label), so the two bars are functionally identical. */}
              <BarContent {...barProps} />
            </motion.div>
            {/* Language/Polish sub-panel — the shared BarPanel, identical to the notch shell. */}
            <AnimatePresence>
              {panel && <BarPanel {...barProps} />}
            </AnimatePresence>

            {/* Expanding area — finished transcript. Height animation + content fade-in. */}
            <AnimatePresence>
              {doneOpen && (
                <motion.div
                  className="pill-body"
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={reduceMotion ? { duration: 0 } : PANEL_SPRING}
                >
                  <motion.div
                    className="pill-body-inner"
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={reduceMotion ? { duration: 0 } : { delay: 0.06, duration: 0.22, ease: "easeOut" }}
                  >
                    {editDraft !== null ? (
                      <>
                        <textarea
                          className="pill-edit"
                          value={editDraft}
                          onChange={(e) => setEditDraft(e.target.value)}
                          rows={3}
                          autoFocus
                        />
                        <div className="pill-actions">
                          <button className="pill-action" onClick={saveCorrection}>
                            {t("Mentés")}
                          </button>
                          <button className="pill-action" onClick={closeCorrection}>
                            {t("Mégse")}
                          </button>
                        </div>
                      </>
                    ) : (
                      <>
                        <p className="pill-transcript">{transcript?.full_text || t("(üres)")}</p>
                        <div className="pill-actions">
                          <button className="pill-action" onClick={() => recordAndTranscribe()}>
                            <RotateCcw size={13} strokeWidth={2.2} />
                            {t("Újra")}
                          </button>
                          <button className="pill-action" onClick={startCorrection}>
                            <Pencil size={13} strokeWidth={2.2} />
                            {t("Javítás")}
                          </button>
                        </div>
                      </>
                    )}
                  </motion.div>
                </motion.div>
              )}
            </AnimatePresence>
          </motion.div>
        );
      })()}
    </div>
  );
}

export default Overlay;
