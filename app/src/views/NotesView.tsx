// Notes view: the WORKING Lavox Notes surface in the main window.
// Notes are stored by the backend (get_notes); editing happens in the floating
// notebook window (show_notebook), this view is a list + entry point.
// NOTE: t() keys are Hungarian source strings (gettext-style, see lib/i18n.ts).
import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { NotebookPen, Pin } from "lucide-react";
import { getMode } from "../modes";
import { t } from "../lib/i18n";

interface Note {
  id: string;
  text: string;
  created: number;
  updated: number;
  pinned: boolean;
}

/** The first non-empty line = title; the rest = preview. */
function splitNote(text: string): { title: string; preview: string } {
  const lines = text.split("\n").map((l) => l.trim());
  const title = lines.find((l) => l.length > 0) ?? "";
  const rest = lines.slice(lines.indexOf(title) + 1).filter((l) => l.length > 0);
  return { title: title || t("Névtelen"), preview: rest.join(" · ").slice(0, 140) };
}

function timeAgo(ms: number): string {
  const d = Date.now() - ms;
  const min = Math.floor(d / 60000);
  if (min < 1) return t("most");
  if (min < 60) return t("{n} perce").replace("{n}", String(min));
  const h = Math.floor(min / 60);
  if (h < 24) return t("{n} órája").replace("{n}", String(h));
  return t("{n} napja").replace("{n}", String(Math.floor(h / 24)));
}

export function NotesView() {
  const mode = getMode("note");
  const Icon = mode.icon;
  const [notes, setNotes] = useState<Note[]>([]);

  useEffect(() => {
    const load = () => invoke<Note[]>("get_notes").then(setNotes).catch(() => {});
    load();
    // The notebook window signals on every save, we refresh live.
    const un = listen("notes-changed", load);
    return () => {
      un.then((f) => f()).catch(() => {});
    };
  }, []);

  const openNotebook = () => invoke("show_notebook").catch(() => {});

  const sorted = [...notes].sort(
    (a, b) => Number(b.pinned) - Number(a.pinned) || (b.updated || b.created) - (a.updated || a.created),
  );

  return (
    <div className="view">
      <header className="view-header">
        <div className="view-title-row">
          <span className="view-badge" style={{ ["--badge-bg" as string]: mode.bg, ["--badge-fg" as string]: mode.color }}>
            <Icon size={18} strokeWidth={1.75} />
          </span>
          <h1 className="view-title">{t("Jegyzetek")}</h1>
        </div>
        <p className="view-subtitle">
          {t("Diktálj a barból a jegyzetfüzetbe, vagy írj kézzel — minden itt gyűlik.")}
        </p>
      </header>

      <button className="btn-teal btn-open-notebook" type="button" onClick={openNotebook}>
        <NotebookPen size={15} strokeWidth={1.75} />
        {t("Jegyzetfüzet megnyitása")}
      </button>

      {sorted.length === 0 ? (
        <p className="notes-empty">{t("Még nincs jegyzet — nyisd meg a jegyzetfüzetet, vagy diktálj a barból (📝).")}</p>
      ) : (
        <ul className="notes-list">
          {sorted.map((n) => {
            const { title, preview } = splitNote(n.text);
            return (
              <li key={n.id}>
                <button type="button" className="note-row" onClick={openNotebook}>
                  <div className="note-row-main">
                    <span className="note-row-title">
                      {n.pinned && <Pin size={12} strokeWidth={2} className="note-pin" />}
                      {title}
                    </span>
                    {preview && <span className="note-row-preview">{preview}</span>}
                  </div>
                  <span className="note-row-time">{timeAgo(n.updated || n.created)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
