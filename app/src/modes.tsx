// A 4 működési mód központi definíciója — egyetlen forrás, amit a Sidebar,
// az App fő nézete és az overlay kör-menüje is használ.
// A capture-típus színek a dashboarddal egyeznek:
//   diktálás = indigo (#6366f1), meeting = teal (#0d9488), jegyzet = amber (#d97706).
import type { ComponentType } from "react";
import { Mic, Video, Users, NotebookPen } from "lucide-react";

export type ModeId = "dictation" | "video" | "meeting" | "note";

export interface ModeDef {
  id: ModeId;
  label: string;
  // lucide-react ikon (props: size, strokeWidth, color...)
  icon: ComponentType<{ size?: number; strokeWidth?: number }>;
  // a mód accent-színe (CSS-érték) — pötty / badge ehhez igazodik
  color: string;
  // halvány háttér a badge-hez (a típus saját light-bg-je)
  bg: string;
  // rövid leírás a nézet fejlécében / a shell placeholderben
  description: string;
  // true = működő funkció; false = UI-shell (backend külön jön)
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
