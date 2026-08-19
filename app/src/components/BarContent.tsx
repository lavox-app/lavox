// Közös bar-tartalom — a NotchShell és a PillShell IS ezt rendeli, így a gomb-készlet
// és a viselkedés garantáltan azonos (egy forrás, nem tud újra divergálni).
// A meglévő `ni-*` glass-stílusokat használja: a pill háttere ugyanaz a sötét
// füstüveg, mint a notché, tehát a stílusok mindkét héjban helyesek.
import { motion } from "framer-motion";
import {
  Mic, Square, Globe, StickyNote, Users, Video, Check,
  Play, MonitorSmartphone, X, Camera, CameraOff,
} from "lucide-react";
import { Waveform, Shimmer } from "./Waveform";
import { t } from "../lib/i18n";

export type BarPhase = "ready" | "recording" | "transcribing" | "done" | "error";
export type BarPanelKind = "language" | "videomic" | "videoscreen";

export interface BarContentProps {
  phase: BarPhase;
  isDictation: boolean;
  levels: number[];
  panel: BarPanelKind | null;
  langs: string[];
  languages: { code: string; label: string }[];
  meetPrompt: { title: string } | null;
  meetRec: { kind: "meeting" | "video" } | null;
  fmtElapsed: string;
  reduceMotion: boolean | null;
  // Videó-vezérlő menü állapot
  videoMenu: boolean;
  cameraOn: boolean;
  mics: string[];
  displays: string[];
  selectedMic: string;
  selectedDisplay: number;
  // callbacks
  onMic: () => void;
  onNotebook: () => void;
  onTogglePanel: (p: BarPanelKind) => void;
  onToggleLang: (code: string) => void;
  onStartMeet: () => void;
  /** Kézi meeting-indítás a barból (kiegészítő/naptár nélkül is). */
  onStartMeetingManual: () => void;
  onDismissMeet: () => void;
  onStopMeet: () => void;
  // videó-menü callbackek
  onOpenVideoMenu: () => void;
  onCloseVideoMenu: () => void;
  onStartVideo: () => void;
  onToggleCamera: () => void;
  onSelectMic: (name: string) => void;
  onSelectDisplay: (i: number) => void;
}

const TAP = { type: "spring" as const, stiffness: 500, damping: 22 };

function IconBtn({
  onClick, title, active, danger, children,
}: {
  onClick: () => void; title: string; active?: boolean; danger?: boolean; children: React.ReactNode;
}) {
  return (
    <motion.button
      className={`ni-btn${active ? " ni-btn-mic" : ""}`}
      data-recording={danger ? true : undefined}
      onClick={onClick}
      title={title}
      aria-label={title}
      whileHover={{ scale: 1.08 }}
      whileTap={{ scale: 0.86 }}
      transition={TAP}
    >
      {children}
    </motion.button>
  );
}

/** Az 5 direkt akció-gomb — mindkét héjban azonos készlet és stílus. */
function ActionButtons(p: BarContentProps) {
  return (
    <>
      <IconBtn onClick={() => p.onTogglePanel("language")} title={t("Nyelv")}>
        <Globe size={15} strokeWidth={2} />
      </IconBtn>
      <motion.button
        className="ni-btn ni-btn-mic"
        data-recording={p.phase === "recording"}
        onClick={p.onMic}
        title={t("Diktálás")}
        aria-label={t("Diktálás")}
        whileHover={{ scale: 1.1 }}
        whileTap={{ scale: 0.86 }}
        transition={TAP}
      >
        <Mic size={16} strokeWidth={2.2} />
      </motion.button>
      <IconBtn onClick={p.onOpenVideoMenu} title={t("Videó felvétel")}>
        <Video size={15} strokeWidth={2} />
      </IconBtn>
      <IconBtn onClick={p.onStartMeetingManual} title={t("Meeting felvétel")}>
        <Users size={15} strokeWidth={2} />
      </IconBtn>
      <IconBtn onClick={p.onNotebook} title={t("Lavox Notes — jegyzetfüzet")}>
        <StickyNote size={15} strokeWidth={2} />
      </IconBtn>
    </>
  );
}

/** A videó-vezérlő menü sora (a bar footprintje nem változik, csak a tartalom). */
function VideoMenu(p: BarContentProps) {
  const recording = !!p.meetRec && p.meetRec.kind === "video";
  if (recording) {
    // Felvétel alatt: leállítás + kamera-toggle + timer (a pause külön körben jön).
    return (
      <>
        <motion.button
          className="ni-btn"
          data-recording
          onClick={p.onStopMeet}
          title={t("Leállítás")}
          aria-label={t("Leállítás")}
          whileTap={{ scale: 0.86 }}
          transition={TAP}
        >
          <Square size={13} strokeWidth={2.6} fill="currentColor" />
        </motion.button>
        <IconBtn onClick={p.onToggleCamera} title={t("Előlapi kamera ki/be")} active={p.cameraOn}>
          {p.cameraOn ? <Camera size={15} strokeWidth={2} /> : <CameraOff size={15} strokeWidth={2} />}
        </IconBtn>
        <span className="ni-meet-title">{t("Videó")} REC · {p.fmtElapsed}</span>
      </>
    );
  }
  // Felvétel előtt: indítás + kamera + mic + képernyő + vissza.
  return (
    <>
      <IconBtn onClick={p.onStartVideo} title={t("Felvétel indítás")} active>
        <Play size={15} strokeWidth={2.4} fill="currentColor" />
      </IconBtn>
      <IconBtn onClick={p.onToggleCamera} title={t("Előlapi kamera ki/be")} active={p.cameraOn}>
        {p.cameraOn ? <Camera size={15} strokeWidth={2} /> : <CameraOff size={15} strokeWidth={2} />}
      </IconBtn>
      <IconBtn onClick={() => p.onTogglePanel("videomic")} title={t("Mikrofon választás")} active={p.panel === "videomic"}>
        <Mic size={15} strokeWidth={2} />
      </IconBtn>
      <IconBtn onClick={() => p.onTogglePanel("videoscreen")} title={t("Képernyő választás")} active={p.panel === "videoscreen"}>
        <MonitorSmartphone size={15} strokeWidth={2} />
      </IconBtn>
      <IconBtn onClick={p.onCloseVideoMenu} title={t("Vissza")}>
        <X size={15} strokeWidth={2.2} />
      </IconBtn>
    </>
  );
}

/** A bar közép-sávjának állapotfüggő tartalma (felvétel/átirat/meeting/vezérlők). */
export function BarContent(props: BarContentProps) {
  const { phase, levels, meetPrompt, meetRec, fmtElapsed, videoMenu, onMic, onStartMeet, onDismissMeet, onStopMeet } = props;

  // A videó-menü (ha nyitva) MINDENT visz — a start/stop/kamera/mic/képernyő itt van.
  if (videoMenu) {
    return <VideoMenu {...props} />;
  }

  if (phase === "recording") {
    return (
      <>
        <div className="ni-wave">
          <Waveform levels={levels} />
        </div>
        <motion.button
          className="ni-btn ni-btn-mic"
          data-recording
          onClick={onMic}
          aria-label={t("Leállítás")}
          whileTap={{ scale: 0.86 }}
          transition={TAP}
        >
          <Square size={12} strokeWidth={2.6} fill="currentColor" />
        </motion.button>
      </>
    );
  }
  if (phase === "transcribing") {
    return (
      <div className="ni-wave">
        <Shimmer />
      </div>
    );
  }
  if (meetPrompt) {
    return (
      <>
        <Users size={13} strokeWidth={2.2} />
        <span className="ni-meet-title" title={meetPrompt.title}>
          {meetPrompt.title || "Meeting"}
        </span>
        <button className="pill-meet-btn pill-meet-btn-rec" onClick={onStartMeet}>
          {t("Felvétel")}
        </button>
        <button className="pill-meet-btn" onClick={onDismissMeet}>
          {t("Kihagy")}
        </button>
      </>
    );
  }
  if (meetRec) {
    return (
      <>
        <span className="pill-rec-dot" />
        <span className="ni-meet-title">
          {meetRec.kind === "video" ? t("Videó") : "Meeting"} REC · {fmtElapsed}
        </span>
        <button className="pill-meet-btn pill-meet-btn-stop" onClick={onStopMeet} aria-label={t("Felvétel leállítása")}>
          <Square size={10} strokeWidth={2.6} fill="currentColor" />
        </button>
      </>
    );
  }
  return <ActionButtons {...props} />;
}

/** A nyelv/mic/képernyő al-panel — mindkét héj alá kerülhet. */
export function BarPanel(props: BarContentProps) {
  const {
    panel, langs, languages, mics, displays, selectedMic, selectedDisplay,
    onToggleLang, onSelectMic, onSelectDisplay, reduceMotion,
  } = props;
  if (!panel) return null;

  const title =
    panel === "language" ? t("Nyelv")
    : panel === "videomic" ? t("Mikrofon")
    : panel === "videoscreen" ? t("Képernyő")
    : "Polish / Prompt";

  return (
    <motion.div
      className="ni-panel"
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={reduceMotion ? { duration: 0 } : { duration: 0.18, ease: "easeOut" }}
    >
      <div className="ni-panel-title">{title}</div>

      {panel === "language" && (
        <div className="ni-langs">
          {languages.map((l) => {
            const on = langs.includes(l.code);
            return (
              <button key={l.code} className="ni-langrow" data-on={on} onClick={() => onToggleLang(l.code)}>
                <span>{l.label}</span>
                {on && <Check size={14} strokeWidth={2.6} />}
              </button>
            );
          })}
          <div className="ni-panel-hint">
            {t("Egy nyelv → arra rögzít · több nyelv → auto-felismerés")}
          </div>
        </div>
      )}

      {panel === "videomic" && (
        <div className="ni-langs">
          <button className="ni-langrow" data-on={!selectedMic} onClick={() => onSelectMic("")}>
            <span>{t("Alapértelmezett mikrofon")}</span>
            {!selectedMic && <Check size={14} strokeWidth={2.6} />}
          </button>
          {mics.map((m) => (
            <button key={m} className="ni-langrow" data-on={selectedMic === m} onClick={() => onSelectMic(m)}>
              <span>{m}</span>
              {selectedMic === m && <Check size={14} strokeWidth={2.6} />}
            </button>
          ))}
        </div>
      )}

      {panel === "videoscreen" && (
        <div className="ni-langs">
          {displays.map((d, i) => (
            <button key={d} className="ni-langrow" data-on={selectedDisplay === i} onClick={() => onSelectDisplay(i)}>
              <span>{d}</span>
              {selectedDisplay === i && <Check size={14} strokeWidth={2.6} />}
            </button>
          ))}
        </div>
      )}
    </motion.div>
  );
}
