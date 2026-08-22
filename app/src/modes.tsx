// Central definition of the 4 operating modes — a single source used by the
// Sidebar, the App's main view and the overlay's radial menu.
// Capture-type colors match the dashboard:
//   dictation = indigo (#6366f1), meeting = teal (#0d9488), note = amber (#d97706).
//
// NOTE: `label` and `description` are rendered through t() (see lib/i18n.ts),
// which uses the Hungarian source string as the dictionary key — keep these
// Hungarian values byte-identical; the English text lives in lib/i18n-en.ts.
import type { ComponentType } from "react";
import { Mic, Video, Users, NotebookPen } from "lucide-react";

export type ModeId = "dictation" | "video" | "meeting" | "note";

export interface ModeDef {
  id: ModeId;
  label: string;
  // lucide-react icon (props: size, strokeWidth, color...)
  icon: ComponentType<{ size?: number; strokeWidth?: number }>;
  // the mode's accent color (CSS value) — dot / badge follows it
  color: string;
  // faint background for the badge (the type's own light bg)
  bg: string;
  // short description in the view header / the shell placeholder
  description: string;
  // true = working feature; false = UI shell (backend comes separately)
  ready: boolean;
}

export const MODES: ModeDef[] = [
  {
    id: "dictation",
    label: "Diktálás",
    icon: Mic,
    color: "var(--indigo)",
    bg: "var(--indigo-bg)",
    description: "Gyors hangfelvétel és azonnali átirat (whisper.cpp helyben).",
    ready: true,
  },
  {
    id: "video",
    label: "Videó",
    icon: Video,
    color: "var(--teal)",
    bg: "var(--teal-bg)",
    description: "Loom-stílusú saját felvétel: képernyő + arc-buborék + hang, megosztható videó.",
    ready: true,
  },
  {
    id: "meeting",
    label: "Meeting",
    icon: Users,
    color: "var(--teal)",
    bg: "var(--teal-bg)",
    description: "Hívás rögzítése: rendszerhang + mikrofon → diarizált átirat (ki mit mondott) + összefoglaló.",
    ready: true,
  },
  {
    id: "note",
    label: "Jegyzetek",
    icon: NotebookPen,
    color: "var(--amber)",
    bg: "var(--amber-bg)",
    description: "Gyors jegyzetek — diktálj a barból, vagy írj a lebegő jegyzetfüzetben.",
    ready: true,
  },
];

export const DEFAULT_MODE: ModeId = "dictation";

export function getMode(id: ModeId): ModeDef {
  return MODES.find((m) => m.id === id) ?? MODES[0];
}
