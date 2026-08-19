// Always-on pill (a képernyő tetején, Wispr Flow-elmélet, saját animáció).
// Az ablak ÁTLÁTSZÓ; a méretét a frontend állítja: invoke("set_pill_size",{width,height}),
// a Rust a képernyő tetejére, középre igazítja.
//
// Három fő állapot:
//   1) IDLE = pici, alig látható sötét vonal középen felül (nem zavar).
//   2) HOVER = a vonalra húzva smooth kibomlik a vezérlősáv (mód-menü + felirat + mic).
//   3) DIKTÁLÁS = látványos „figyel" visszajelzés (waveform + piros pont + glow),
//      hover nélkül is — a ⌃⇧Space (trigger-dictation) eseményre is aktiválódik.
//   + Transzkripció: shimmer + „Átirat…". Kész: lefelé kinő, mutatja az átiratot.
import { useState, useEffect, useCallback, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen, emit } from "@tauri-apps/api/event";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { RotateCcw, Square } from "lucide-react";
import { isPermissionGranted, requestPermission, sendNotification } from "@tauri-apps/plugin-notification";
import { BarContent, BarPanel, type BarContentProps, type BarPanelKind } from "./components/BarContent";
import { WAVE_BARS } from "./components/Waveform";
import { DEFAULT_MODE, type ModeId } from "./modes";
import { loadAutoRecord } from "./lib/calendar";
import { t } from "./lib/i18n";
import type { TranscriptResult } from "./lib/types";
import "./Overlay.css";

// A Meet Bridge (extension → Rust → ide) esemény-payloadja.
type MeetInfo = { meetCode: string; title: string; participants: string[] };

/** Lavox márkajel — alászálló sorok + felszíni pont. A zárt pill és a notch brand-pöttye.
 *  Egységes a landinggel, a sidebar-ral és a store-ikonnal. */
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

// A Rust `get_notch_info` által visszaadott notch-adatok (logikai pont = CSS px).
interface NotchInfo {
  has_notch: boolean;
  notch_height: number;
  notch_left: number;
  notch_right: number;
  screen_width: number;
  scale: number;
}

// A notch két oldali sávja (CSS px) — szűk, csak a dropdownnak hagy helyet.
const NOTCH_SIDE_W = 78;

// Választható nyelvek (whisper ISO kódok). Több engedélyezett → auto-detect.
const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "hu", label: "Hungarian (Magyar)" },
];

// ── Animáció-paraméterek (egy helyen hangolva) ──────────────────────────────
// Lágyabb, „selymes" springek a méret/alak morphhoz: alacsonyabb stiffness +
// magasabb damping → nem ugrik, nem pattog, folyékonyan átúszik (Wispr-érzet).
// A layout-morph (pici vonal ↔ sáv ↔ kinyílás) saját, kicsit lágyabb springje.
export const LAYOUT_SPRING = { type: "spring" as const, stiffness: 230, damping: 26, mass: 0.9 };
// A kinyíló átirat-panel kicsit ruganyosabb (érezhető „kinő" mozdulat).
const PANEL_SPRING = { type: "spring" as const, stiffness: 240, damping: 28, mass: 0.95 };
// A középső tartalom (wave/shimmer/label) gyors, sima cross-fade-je.
const CONTENT_FADE = { duration: 0.14, ease: [0.22, 0.61, 0.36, 1] as const };

// Az egyes állapotok logikai ablakmérete (a set_pill_size ezzel hív).
// A window NAGYOBB, mint a látható pill — a körüllévő árnyék/glow, és a lefelé
// nyíló menü ELFÉR benne (különben a window széle levágja őket). A pill a window
// tetejétől kicsit lejjebb, középen ül (lásd .pill margin-top + .pill-wrap).
const SIZES = {
  idle: { width: 200, height: 30 },
  hover: { width: 360, height: 82 },
  recording: { width: 360, height: 82 },
  transcribing: { width: 360, height: 82 },
  done: { width: 380, height: 240 },
  menu: { width: 400, height: 330 },
  // Meeting REC kapszula: hover nélkül is látható piros pill + timer.
  meetrec: { width: 210, height: 56 },
} as const;

// 220ms türelmi idő, mielőtt a vezérlősáv visszacsukódik a vonalra.
// (Korábban 400ms — túl „ragadós" volt; ennyi elég, hogy ne csukódjon be
// véletlen kilépéskor, de snappy maradjon az interakció.)
const LEAVE_DELAY_MS = 220;

function Overlay() {
  const reduceMotion = useReducedMotion();
  const [mode, setMode] = useState<ModeId>(DEFAULT_MODE);
  const [phase, setPhase] = useState<Phase>("ready");
  // A státusz-szöveget a setStatus állítja (hiba/állapot); a bar most gombokat mutat,
  // ezért a getter jelenleg nem jelenik meg (a hiba a data-phase glow-val látszik).
  const [_status, setStatus] = useState("");
  const [transcript, setTranscript] = useState<TranscriptResult | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [hovered, setHovered] = useState(false);
  // Valós idejű mikrofon-szintek a waveformhoz (gördülő puffer, új a végén).
  const [levels, setLevels] = useState<number[]>(() => new Array(WAVE_BARS).fill(0));
  // Megnyitott al-panel (nyelv / eszköz-választók) — a Wispr-gombokhoz.
  const [panel, setPanel] = useState<BarPanelKind | null>(null);
  // Videó-vezérlő menü (a 🎥 gomb nyitja; a bar footprintje nem változik).
  const [videoMenu, setVideoMenu] = useState(false);
  const [cameraOn, setCameraOn] = useState(true);
  const [barMics, setBarMics] = useState<string[]>([]);
  const [barDisplays, setBarDisplays] = useState<string[]>([]);
  const [selectedMic, setSelectedMic] = useState("");
  const [selectedDisplay, setSelectedDisplay] = useState(0);
  // A jegyzetfüzet-ablak FÓKUSZÁLT-e — EZ dönti el a diktálás-routingot. Ha másik
  // appba kattintasz, a notebook elveszti a fókuszt → a diktálás oda megy, nem ide.
  const [notebookFocused, setNotebookFocused] = useState(false);
  const notebookFocusedRef = useRef(false);
  notebookFocusedRef.current = notebookFocused;
  // A bar 📝 gombja: megnyitja a kis Lavox Notes jegyzetfüzet-ablakot.
  const openNotebook = useCallback(() => {
    invoke("show_notebook").catch(() => {});
    setNotebookFocused(true); // megnyitáskor fókuszba kerül
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
  // Engedélyezett nyelvek (ISO kódok) — a backend tárolja, a transcribe ezt használja.
  const [langs, setLangs] = useState<string[]>([]);
  useEffect(() => {
    invoke<string[]>("get_languages").then(setLangs).catch(() => {});
  }, []);
  const toggleLang = useCallback((code: string) => {
    setLangs((prev) => {
      const next = prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code];
      const final = next.length ? next : [code]; // legalább 1 nyelv
      invoke("set_languages", { langs: final }).catch(() => {});
      return final;
    });
  }, []);

  // ── MEETING FELVÉTEL (Meet Bridge) ─────────────────────────────────────────
  // Az extension jelzi a belépést → prompt ("Felvegyem?") vagy auto-indítás.
  // Felvétel alatt a pill piros REC kapszulává válik; kattintás = stop.
  const [meetPrompt, setMeetPrompt] = useState<MeetInfo | null>(null);
  const [meetRec, setMeetRec] = useState<{ title: string; startedAt: number; kind: "meeting" | "video" } | null>(null);
  const [meetElapsed, setMeetElapsed] = useState(0);
  const meetRecRef = useRef<{ title: string; startedAt: number; kind: "meeting" | "video" } | null>(null);
  meetRecRef.current = meetRec;

  // Az auto-record BACKENDBŐL (auto_record.json) — túléli az újratelepítést,
  // szemben a localStorage-dzsal. A meet-joined döntés ezt a refet olvassa.
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
    } catch { /* nem kritikus */ }
  }, []);

  const startMeetRec = useCallback(async (info: MeetInfo) => {
    setMeetPrompt(null);
    try {
      await invoke("start_meeting_record");
      setMeetRec({ title: info.title || info.meetCode || "Meeting", startedAt: Date.now(), kind: "meeting" });
    } catch (e) {
      const msg = String(e);
      if (msg.includes("Már fut")) {
        // desync-védelem: a backend már rögzít → vegyük át az állapotot
        setMeetRec({ title: info.title || "Meeting", startedAt: Date.now(), kind: "meeting" });
      } else {
        notify("LAVOX", t("Felvétel indítás sikertelen: {msg}").replace("{msg}", msg));
      }
    }
  }, [notify]);

  // Kézi VIDEÓ-felvétel a bárból — bármikor, meeting nélkül is
  // (képernyő + rendszerhang + mikrofon, ugyanaz a gépezet).
  const cameraOnRef = useRef(true);
  cameraOnRef.current = cameraOn;
  const startVideoRec = useCallback(async () => {
    if (meetRecRef.current) return;
    const now = new Date();
    const title = `${t("Videó")} ${now.toLocaleDateString("hu-HU", { month: "short", day: "numeric" })} ${now.toLocaleTimeString("hu-HU", { hour: "2-digit", minute: "2-digit" })}`;
    try {
      // A kamera-buborékot CSAK ha be van kapcsolva az arc, a felvétel ELŐTT
      // nyitjuk meg (PiP). Ha nincs kamera/engedély, a buborék magát zárja be.
      if (cameraOnRef.current) {
        await invoke("show_camera_bubble").catch(() => {});
      }
      await invoke("start_video_record");
      setMeetRec({ title, startedAt: Date.now(), kind: "video" });
    } catch (e) {
      const msg = String(e);
      if (msg.includes("Már fut")) {
        setMeetRec({ title, startedAt: Date.now(), kind: "video" });
      } else {
        notify("LAVOX", t("Videó indítás sikertelen: {msg}").replace("{msg}", msg));
      }
    }
  }, [notify]);

  // Kézi MEETING-felvétel a bárból — kiegészítő és naptár NÉLKÜL is.
  // (Eddig a meeting csak a Meet-bővítmény jelzésére vagy naptár-auto-recordra
  // indult; ha egyik sincs, a Meeting mód elindíthatatlan volt.)
  const startMeetingManual = useCallback(async () => {
    if (meetRecRef.current) return;
    const now = new Date();
    const title = `Meeting ${now.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
    try {
      await invoke("start_meeting_record");
      setMeetRec({ title, startedAt: Date.now(), kind: "meeting" });
    } catch (e) {
      const msg = String(e);
      if (msg.includes("Már fut")) {
        setMeetRec({ title, startedAt: Date.now(), kind: "meeting" });
      } else {
        notify("LAVOX", t("Felvétel indítás sikertelen: {msg}").replace("{msg}", msg));
      }
    }
  }, [notify]);

  // ── VIDEÓ-VEZÉRLŐ MENÜ (a 🎥 gomb nyitja) ──────────────────────────────────
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
  // Arc (kamera-buborék) ki/be — felvétel alatt élőben mutatja/rejti.
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
      // A képernyő-stop UTÁN zárjuk a buborékot (az utolsó frame-ig benne legyen).
      await invoke("hide_camera_bubble").catch(() => {});
      // JSON: { mic: path, system: path|null } — régi formátum (sima path) is elfogadott
      let mic = raw;
      let system: string | null = null;
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object" && parsed.mic) {
          mic = parsed.mic;
          system = parsed.system ?? null;
        }
      } catch { /* sima path — régi backend */ }
      if (system) {
        notify(t("LAVOX — meeting mentve (2 sáv)"), `${t("Mikrofon + rendszerhang:")}\n${mic}\n${system}`);
      } else {
        notify(
          t("LAVOX — meeting mentve (csak mikrofon!)"),
          `${mic}\n${t("A többiek hangjához engedélyezd: Rendszerbeállítások → Adatvédelem → Képernyő- és rendszerhang-felvétel → Lavox Hub")}`,
        );
      }
      // Automatikus átirat a mentés után — hibánál HANGOS értesítés, sosem
      // halhat el csendben. A rec_id a felvétel-mappa neve a mic útvonalából.
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

  // Bridge események az extensionből (Rust bridge.rs továbbítja).
  useEffect(() => {
    const unJoin = listen<MeetInfo>("meet-joined", (e) => {
      if (meetRecRef.current) return; // már rögzítünk
      if (autoRecordRef.current) {
        startMeetRec(e.payload);
      } else {
        setMeetPrompt(e.payload);
      }
    });
    const unLeft = listen<MeetInfo>("meet-left", () => {
      setMeetPrompt(null);
      // Csak a MEETING-felvételt állítjuk le kilépéskor — a kézi videó megy tovább.
      if (meetRecRef.current?.kind === "meeting") stopMeetRec();
    });
    return () => {
      unJoin.then((f) => f()).catch(() => {});
      unLeft.then((f) => f()).catch(() => {});
    };
  }, [startMeetRec, stopMeetRec]);

  // REC időzítő (mm:ss a kapszulában).
  useEffect(() => {
    if (!meetRec) { setMeetElapsed(0); return; }
    const iv = window.setInterval(() => {
      setMeetElapsed(Math.floor((Date.now() - meetRec.startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(iv);
  }, [meetRec]);

  const fmtElapsed = `${Math.floor(meetElapsed / 60)}:${String(meetElapsed % 60).padStart(2, "0")}`;

  // Notch-infó (Dynamic Island-stílusú compact layout). Induláskor lekérjük; utána
  // ESEMÉNY-VEZÉRELTEN frissül: a backend kijelző-átkonfigurálás (monitor csatlakozik/
  // lecsatlakozik, felbontás/skálázás vált) callbackje küldi a "notch-refreshed"-et —
  // nincs polling.
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
  const recordingRef = useRef(false); // épp felvesz-e (toggle: 1. ⌃⇧Space start, 2. stop)
  const modeRef = useRef<ModeId>(DEFAULT_MODE);
  const leaveTimer = useRef<number | null>(null);
  // A window-méretezéshez: az előző terület (növekszik/zsugorodik döntéshez) +
  // a késleltetett zsugorítás timere.
  const prevAreaRef = useRef(0);
  const resizeTimer = useRef<number | null>(null);

  const busy = phase === "recording" || phase === "transcribing";
  const doneOpen = phase === "done" && !!transcript;
  // A vezérlősáv akkor látszik, ha: hover VAGY menü nyitva VAGY diktál/transzkribál
  // VAGY kész átirat VAGY hiba VAGY meeting-prompt vár válaszra.
  const controlsVisible = hovered || menuOpen || busy || doneOpen || phase === "error" || !!panel || !!meetPrompt || videoMenu;

  // NOTCH-MÓD (Dynamic Island): a window a notch tetejére kerül (y=0, Rust), a
  // tartalom a notch két oldalára (compact), kinyitva pedig a notch ALÁ bloomol.
  const notched = !!notch?.has_notch;
  const notchH = notched ? notch!.notch_height : 0;
  const notchW = notched ? notch!.notch_right - notch!.notch_left : 0;
  const niWindowW = notchW + 2 * NOTCH_SIDE_W;
  // Az al-panel magassága: a device-listák a tételszám szerint nőnek (kis felső+alsó
  // margóval), a nyelv fix.
  const panelH =
    panel === "language" ? 188
    : panel === "videomic" ? Math.min(220, 60 + (barMics.length + 1) * 34)
    : panel === "videoscreen" ? Math.min(220, 60 + Math.max(1, barDisplays.length) * 34)
    : 0;

  // Az aktuális ablakméret levezetése az állapotból (prioritás fentről lefelé).
  let size: { width: number; height: number };
  if (doneOpen) size = SIZES.done;
  else if (menuOpen) size = SIZES.menu;
  else if (phase === "recording" || phase === "transcribing") size = SIZES.recording;
  else if (controlsVisible) size = SIZES.hover;
  else if (meetRec) size = SIZES.meetrec;
  else size = SIZES.idle;

  if (notched) {
    // A window szélessége KONSTANS (nem ugrik nyitáskor → nincs vízszintes glitch),
    // csak a magasság nő. A glass ezen belül animálódik.
    size = {
      width: niWindowW,
      height: notchH + (size.height - 12) + (panel ? panelH + 16 : 0),
    };
  } else if (panel && !doneOpen) {
    // PILL: a panel megnyitásakor az ablak lefelé nő, hogy elférjen
    // (a notch-héj a saját ágán már kezeli a panelH-t).
    size = { width: size.width, height: SIZES.hover.height + panelH + 16 };
  }

  // Az ablakot a frontend méretezi (a Rust a tetejére, középre igazítja).
  // Egyúttal jelezzük, hogy idle-ben vagyunk-e — csak akkor követi a pill a kurzort
  // a képernyők közt (aktív állapotban a méret-pozícionálás úgyis a kurzor monitorára tesz).
  useEffect(() => {
    const apply = () =>
      invoke("set_pill_size", { width: size.width, height: size.height }).catch(() => {});

    // SMOOTH KINYÍLÁS: a window legyen mindig elég nagy az ÉPP animálódó tartalomhoz.
    // - NÖVEKEDÉS (kinyílás): azonnal nagyobb window → a pillnek van helye kinőni.
    // - ZSUGORODÁS (záródás): VÁRJUK MEG a tartalom-animációt (~320ms), különben a
    //   window levágja a még kifelé animálódó pillt → ettől „töredezett".
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

  // Az overlay ablak body-ja: margó/görgetés nélkül, ÁTLÁTSZÓ háttér, hogy a
  // pill körül átlátszódjon a desktop. Futásidőben állítjuk (a Vite globális
  // CSS-bundle miatt nem tehetjük a CSS-be — a fő ablakot is érintené).
  useEffect(() => {
    const b = document.body.style;
    const h = document.documentElement.style; // a <html> (:root) — az App.css fehérre festi!
    const prev = { m: b.margin, o: b.overflow, bg: b.background, hbg: h.background };
    b.margin = "0";
    b.overflow = "hidden";
    b.background = "transparent";
    // A fehér négyzet oka: az App.css `:root { background: #fff }` globálisan hat.
    // A <html> hátterét is átlátszóra kell tenni, különben fehér marad a pill körül.
    h.background = "transparent";
    return () => {
      b.margin = prev.m;
      b.overflow = prev.o;
      b.background = prev.bg;
      h.background = prev.hbg;
    };
  }, []);

  // Diktálás TOGGLE: 1. ⌃⇧Space → felvétel indul (korlátlan idő),
  // 2. ⌃⇧Space → stop + átirat + beillesztés a kurzorhoz.
  // (StreamingRecorder = mic, nincs fix időkorlát.)
  const startDictation = useCallback(async () => {
    if (recordingRef.current || runningRef.current) return;
    try {
      if (!modelPathRef.current) {
        modelPathRef.current = await invoke<string>("find_model");
      }
      setLevels(new Array(WAVE_BARS).fill(0)); // tiszta lapos hullám induláskor
      await invoke("start_dictation_record");
      recordingRef.current = true;
      invoke("dbg_log", { msg: "REC_START" }).catch(() => {});
      setTranscript(null);
      setPhase("recording");
      setStatus(t("Hallgatlak… engedd el a leállításhoz"));
    } catch (e) {
      const msg = String(e);
      // Védelem a beragadás ellen: ha a backend már rögzít (frontend-backend
      // desync, pl. hot-reload után), kezeljük rögzítésként → a következő
      // ⌃⇧Space leállítja, nem ragad be.
      if (msg.includes("Már fut") || msg.toLowerCase().includes("already")) {
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
      setPhase("done");
      setStatus("");
      // NOTEBOOK-ROUTING: csak ha a Lavox Notes a FÓKUSZÁLT ablak, akkor megy a
      // diktált szöveg JEGYZETBE. Ha másik appba kattintottál, oda paste-elődik.
      if (result.full_text.trim()) {
        if (notebookFocusedRef.current) {
          // A notebook a fókuszált ablak → a diktált szöveg a szerkesztő
          // KURZORÁHOZ kerül (az aktív jegyzetbe), nem új jegyzetként.
          emit("notebook-dictate", result.full_text.trim()).catch(() => {});
        } else {
          await invoke("insert_text", { text: result.full_text }).catch(() => {});
          // Lavox Memory: a kész diktátum a memóriába folyik (fire-and-forget,
          // a beillesztést sosem lassítja — a szerver nélkül is némán elmegy).
          invoke("memory_ingest_dictation", { text: result.full_text }).catch(() => {});
          const t3 = performance.now();
          invoke("dbg_log", {
            msg: `INSERTED +${Math.round(t3 - t2)}ms · e2e(stop→insert)=${Math.round(t3 - t0)}ms`,
          }).catch(() => {});
        }
      }
      // FONTOS (a 2. diktálás bug fixe): beillesztés után a pill AUTOMATIKUSAN
      // visszacsukódik a vonalra ~1.3mp múlva. Így minden diktálás tiszta IDLE-ből
      // indul (mint az 1., működő kör), és a kinyitott pill nem rántja magához a
      // fókuszt a következő Cmd+V-nél. (Wispr-stílus: a szöveg a kurzornál van,
      // a pill nem marad kinyitva.)
      window.setTimeout(() => {
        if (!recordingRef.current && !runningRef.current) {
          setPhase((p) => (p === "done" ? "ready" : p));
          setTranscript(null);
          setMenuOpen(false);
          setHovered(false);
        }
      }, 1300);
    } catch (e) {
      setPhase("error");
      setStatus(t("Hiba: {msg}").replace("{msg}", String(e)));
    } finally {
      runningRef.current = false;
    }
  }, []);

  // ⌃⇧Space / mic gomb: ha épp felvesz → stop+átirat, különben → felvétel indul.
  const recordAndTranscribe = useCallback(() => {
    if (recordingRef.current) stopAndTranscribe();
    else startDictation();
  }, [startDictation, stopAndTranscribe]);

  // Mód-váltás a kör-menüből.
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

  // PUSH-TO-TALK: ⌃⇧Space lenyomva → felvétel; elengedve → stop + átirat + beillesztés.
  // A Rust két eseményt küld: "dictation-start" (Pressed) és "dictation-stop" (Released).
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

  // VALÓS IDEJŰ WAVEFORM: a Rust ~50ms-enként küldi a mikrofon RMS-szintjét.
  // Normalizáljuk (csend → ~0, beszéd → ~1) és gördülő pufferbe toljuk: az új
  // minta a jobb szélre kerül, a régiek balra kicsúsznak → élő, beszéd-reaktív hullám.
  useEffect(() => {
    const un = listen<number>("mic-level", (e) => {
      const rms = typeof e.payload === "number" ? e.payload : 0;
      // Zajküszöb levonása + gyök-görbe → a halk beszéd is látszik, a csend lapos.
      const norm = Math.min(1, Math.sqrt(Math.max(0, rms - 0.004) * 7));
      setLevels((prev) => [...prev.slice(1), norm]);
    });
    return () => {
      un.then((fn) => fn()).catch(() => {});
    };
  }, []);

  // Esc → menü/átirat bezárása (a pill nem tűnik el, csak vissza a vonalra).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setMenuOpen(false);
        setHovered(false);
        setPanel(null);
        setVideoMenu(false);
        // Kész átirat és hiba is visszaáll → a pill összecsukódik a vonalra.
        if (phase === "done" || phase === "error") setPhase("ready");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [phase]);

  // Hover-kezelés: belépéskor azonnal kibont; kilépéskor késleltetve csuk vissza,
  // de csak ha közben nem indult diktálás/transzkripció/átirat/menü (azt a
  // controlsVisible úgyis nyitva tartja a hover-flagtől függetlenül).
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
  // Timer takarítása unmountkor.
  useEffect(() => () => clearLeaveTimer(), []);

  // HOVER FÓKUSZ NÉLKÜL: a Rust natívan figyeli a kurzort az ablak keretéhez
  // képest (a WKWebView háttérben nem ad DOM hover-t), és "pill-hover" eseményt
  // küld → ugyanúgy nyitunk/csukunk, mint az egér-belépésnél. Így bármelyik app
  // van fókuszban, a pill hoverre kinyílik (Wispr-élmény).
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

  // A közös bar-tartalom (5 gomb + center-state + panel) propjai — MINDKÉT héj ezt kapja.
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
    // A wrapper fogja a hover-eseményeket; átlátszó, kitölti az ablakot, és
    // középre/felülre igazítja a pillt. A pill maga a lebegő elem.
    <div className="pill-wrap" onMouseEnter={onEnter} onMouseLeave={onLeave}>
      {/* EGYETLEN shell, ami mindig jelen van. Hoverre KEYFRAME-mel előbb
          ÖSSZESZŰKÜL (anticipáció), majd KITÁGUL a pillé (szélesedik + vastagodik),
          helyet csinálva a tartalomnak — ami csak a tágulás után úszik be. */}
      {/* NOTCH GLASS (mockup): egységes Liquid Glass kapszula a notch körül.
          Felül: két teal pill a notch KÉT oldalán. Alul (nyitva): waveform. */}
      {notched && (() => {
        // SZŰK: a glass szélessége ZÁRT = NYITOTT (notch + két bogyó, kis ráhagyással
        // hogy a bogyók a lekerekített sarkon BELÜL legyenek). Nyitva csak LEFELÉ nő.
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
            {/* Felső sor (notch szint): két ikon-bogyó a notch két oldalán — zárva ÉS
                nyitva is. A glass kicsit szélesebb + kisebb sarok, így a bogyók a
                lekerekített sarkon BELÜL vannak, nem lógnak ki. */}
            {/* Felső sor (notch szint): STÁTUSZ a notch két oldalán (NEM gomb → nincs
                duplikáció a lenti funkció-gombokkal). Bal = állapot-pont, jobb = brand. */}
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
            {/* Alsó sor (nyitva): felvételkor waveform + STOP; egyébként a 4 Wispr-gomb
                (nyelv · mic · scratchpad) — mind KÜLÖN funkció, nincs duplikáció. */}
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
            {/* Al-panel (nyelv / eszköz-választók) — a közös BarPanel, mindkét héjban azonos. */}
            <AnimatePresence>
              {panel && <BarPanel {...barProps} />}
            </AnimatePresence>
          </motion.div>
        );
      })()}
      {!notched && (() => {
        const expandedW = doneOpen ? 340 : 330;
        // NOTCH-MÓD: idle-ben a glass egy VÉKONY CSÍK a notch alatt (a zöld fülek
        // mellette), hoverre a notch ALÓL lebloomol. Nem-notch: vékony bar ↔ pill.
        // Meeting REC collapsed: nem vékony vonal, hanem látható piros kapszula.
        const meetRecCompact = !!meetRec && !controlsVisible;
        const shellW = controlsVisible ? expandedW : notched ? notchW + 24 : meetRecCompact ? 190 : 130;
        // Explicit magasság + overflow:hidden → a MINDIG mountolt tartalom
        // collapsed-ben le van vágva (nem tűnik el a DOM-ból). APPLE-ELV: ne
        // remováljuk/re-addeljük az elemeket morph közben, csak animáljuk őket.
        // Collapsed: 18px — elfér benne a Lavox logó-jel (brand jelenlét zárva is).
        // Panel (nyelv/eszköz) nyitva: a pill lefelé nő, hogy elférjen a panel.
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
                    // APPLE-LIKE „liquid" spring: a glass folyékonyan csöppen le a
                    // notch alól, enyhe beállással — egy összefüggő mozdulat.
                    type: "spring",
                    stiffness: 220,
                    damping: 24,
                    mass: 0.9,
                  }
            }
          >
            {/* Lavox logó-jel a ZÁRT pillben — brand jelenlét, alig-látható méretben */}
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
            {/* Meeting REC mini-kapszula — collapsed állapotban a vonal HELYETT
                piros pulzáló pill + timer. Kattintás = stop + mentés. */}
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
            {/* Vezérlősáv — MINDIG a DOM-ban (Apple-elv), csak az OPACITY animálódik
                a mérettel együtt → egy koordinált mozdulat, nincs mount/unmount-hézag.
                Collapsed-ben az overflow:hidden levágja, az opacity:0 elrejti. */}
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
              {/* A közös bar-tartalom (5 direkt gomb / center-state) — UGYANAZ, mint a
                  notch-héjban. A pill idle-ben mostantól az 5 gombot mutatja (nem a
                  régi CircleMenu + felirat), így a két bar funkciója azonos. */}
              <BarContent {...barProps} />
            </motion.div>
            {/* Nyelv/Polish al-panel — a közös BarPanel, a notch-héjjal azonos. */}
            <AnimatePresence>
              {panel && <BarPanel {...barProps} />}
            </AnimatePresence>

            {/* Kinyíló terület — kész átirat. Magasság-anim + tartalom fade-in. */}
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
                    <p className="pill-transcript">{transcript?.full_text || t("(üres)")}</p>
                    <div className="pill-actions">
                      <button className="pill-action" onClick={() => recordAndTranscribe()}>
                        <RotateCcw size={13} strokeWidth={2.2} />
                        {t("Újra")}
                      </button>
                    </div>
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
