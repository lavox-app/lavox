import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import {
  Bold,
  Italic,
  Underline,
  Strikethrough,
  List,
  ListOrdered,
  Quote,
  Code,
  Link as LinkIcon,
  Eraser,
  Copy,
  X,
  Plus,
  Paperclip,
  Image as ImageIcon,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Trash2,
  Heading1,
  Heading2,
  Heading3,
  Type,
  Check,
  Pin,
} from "lucide-react";
// NOTE: t() keys are Hungarian source strings (gettext-style, see lib/i18n.ts)
// — keep them byte-identical; the English UI copy lives in lib/i18n-en.ts.
import { t } from "./lib/i18n";
import "./Notebook.css";

// Lavox Notes — a real notebook. Left: search + all notes. Top:
// open note tabs. Right: formatting toolbar (rich text, contentEditable).
// Uses the bar's SHARED store (scratchpad.json, Tauri) → dictation lands here instantly.

interface Note {
  id: string;
  text: string;
  created: number;
  updated: number;
  pinned?: boolean;
}

const HTML_RE = /<[a-z][\s\S]*>/i;
function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
// Stored text → the editor's HTML (legacy plain text: \n → <br>).
function toEditorHtml(stored: string): string {
  if (HTML_RE.test(stored)) return stored;
  return escapeHtml(stored).replace(/\n/g, "<br>");
}
function stripHtml(html: string): string {
  if (!HTML_RE.test(html)) return html;
  const d = document.createElement("div");
  d.innerHTML = html;
  return d.textContent || "";
}
function titleOf(text: string): string {
  const plain = stripHtml(text);
  const line = plain.split("\n").find((l) => l.trim().length > 0) || plain.trim();
  return line ? line.slice(0, 50) : t("Névtelen");
}
function previewOf(text: string): string {
  const plain = stripHtml(text).replace(/\s+/g, " ").trim();
  return plain.slice(0, 90);
}
function timeAgo(ts: number): string {
  const d = Math.max(0, Date.now() - ts);
  const m = Math.floor(d / 60000);
  if (m < 1) return t("most");
  if (m < 60) return t("{n} perce").replace("{n}", String(m));
  const h = Math.floor(m / 60);
  if (h < 24) return t("{n} órája").replace("{n}", String(h));
  return t("{n} napja").replace("{n}", String(Math.floor(h / 24)));
}

export default function Notebook() {
  const [notes, setNotes] = useState<Note[]>([]);
  const [openTabs, setOpenTabs] = useState<string[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [wordCount, setWordCount] = useState(0);
  // Draggable note-list width + collapsibility (persistent).
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const v = Number(localStorage.getItem("lavox-nb-sidebar-w"));
    return v >= 180 && v <= 460 ? v : 264;
  });
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("lavox-nb-collapsed") === "1");
  const resizing = useRef(false);
  const editorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    localStorage.setItem("lavox-nb-sidebar-w", String(sidebarWidth));
  }, [sidebarWidth]);
  useEffect(() => {
    localStorage.setItem("lavox-nb-collapsed", collapsed ? "1" : "0");
  }, [collapsed]);
  useEffect(() => {
    function onMove(e: MouseEvent) {
      if (!resizing.current) return;
      setSidebarWidth(Math.min(460, Math.max(180, e.clientX)));
    }
    function onUp() {
      if (resizing.current) {
        resizing.current = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      }
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);
  function onResizeStart(e: React.MouseEvent) {
    e.preventDefault();
    resizing.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }

  // Text-style dropdown (Aa → Normal / H1 / H2 / H3)
  const [styleOpen, setStyleOpen] = useState(false);
  const styleRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!styleOpen) return;
    function onDown(e: MouseEvent) {
      if (styleRef.current && !styleRef.current.contains(e.target as Node)) {
        setStyleOpen(false);
      }
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [styleOpen]);

  // Glass theme (light / dark / ultra-transparent) — persistent, from the ⋯ menu.
  const [theme, setTheme] = useState<string>(() => localStorage.getItem("lavox-nb-theme") || "light");
  useEffect(() => {
    localStorage.setItem("lavox-nb-theme", theme);
    invoke("set_notebook_glass", { theme }).catch(() => {});
  }, [theme]);
  const [themeOpen, setThemeOpen] = useState(false);
  const themeRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!themeOpen) return;
    function onDown(e: MouseEvent) {
      if (themeRef.current && !themeRef.current.contains(e.target as Node)) {
        setThemeOpen(false);
      }
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [themeOpen]);
  const THEMES = [
    { key: "light", label: t("Világos") },
    { key: "dark", label: t("Sötét") },
    { key: "ultra", label: t("Ultra-átlátszó") },
  ];
  const saveTimer = useRef<number | null>(null);
  const knownIds = useRef<Set<string>>(new Set());
  const firstLoad = useRef(true);
  const activeIdRef = useRef<string | null>(null);
  activeIdRef.current = activeId;
  const notesRef = useRef<Note[]>([]);
  notesRef.current = notes;

  // Load the active note's content after the editor MOUNTS (the div exists only
  // after the re-render). Runs only on activeId → doesn't overwrite while editing.
  useEffect(() => {
    if (!editorRef.current) return;
    const n = notesRef.current.find((x) => x.id === activeIdRef.current);
    const html = toEditorHtml(n ? n.text : "");
    // BLOCK GUARANTEE: even an empty note gets a <div>, so that formatBlock
    // (heading/quote/code) and the slash commands work on empty notes too.
    editorRef.current.innerHTML = html || "<div><br></div>";
    const t = (editorRef.current.textContent || "").trim();
    editorRef.current.dataset.empty = t ? "false" : "true";
    setWordCount(t ? t.split(/\s+/).length : 0);
    // Auto-focus with the cursor at the end of the note → the caret shows
    // immediately, no click needed (Notion pattern). Also brings the window forward (key).
    if (activeIdRef.current) {
      window.setTimeout(() => {
        const el = editorRef.current;
        if (!el) return;
        el.focus();
        const range = document.createRange();
        range.selectNodeContents(el);
        range.collapse(false);
        const sel = window.getSelection();
        sel?.removeAllRanges();
        sel?.addRange(range);
      }, 30);
    }
  }, [activeId]);

  const refresh = useCallback(async () => {
    try {
      const list = await invoke<Note[]>("get_notes");
      // Detect a new (e.g. dictated) note → open it automatically as a tab.
      const fresh = list.filter((n) => !knownIds.current.has(n.id));
      list.forEach((n) => knownIds.current.add(n.id));
      setNotes(list);
      if (!firstLoad.current && fresh.length > 0) {
        const newest = fresh.reduce((a, b) => (a.updated >= b.updated ? a : b));
        setOpenTabs((prev) => (prev.includes(newest.id) ? prev : [...prev, newest.id]));
        setActiveId(newest.id);
        loadIntoEditor(newest.text);
      }
      firstLoad.current = false;
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    refresh();
    const un = listen("notes-changed", () => refresh());
    return () => {
      un.then((f) => f()).catch(() => {});
    };
  }, [refresh]);

  // When the window becomes active again (e.g. after a file picker/another app),
  // the editor regains focus → the caret blinks again. Only if nothing is focused.
  useEffect(() => {
    const onWinFocus = () => {
      const ae = document.activeElement;
      if (activeIdRef.current && editorRef.current && (!ae || ae === document.body)) {
        window.setTimeout(() => editorRef.current?.focus(), 0);
      }
    };
    window.addEventListener("focus", onWinFocus);
    return () => window.removeEventListener("focus", onWinFocus);
  }, []);

  const active = notes.find((n) => n.id === activeId) ?? null;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = q ? notes.filter((n) => stripHtml(n.text).toLowerCase().includes(q)) : notes;
    // Pinned notes first, then the rest — both groups newest-first.
    return [...list].sort((a, b) => {
      if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
      return b.updated - a.updated;
    });
  }, [notes, query]);

  function loadIntoEditor(text: string) {
    if (editorRef.current) editorRef.current.innerHTML = toEditorHtml(text);
  }

  function openNote(id: string) {
    setOpenTabs((prev) => (prev.includes(id) ? prev : [...prev, id]));
    setActiveId(id);
    const n = notes.find((x) => x.id === id);
    loadIntoEditor(n ? n.text : "");
  }

  function closeTab(id: string, e?: React.MouseEvent) {
    e?.stopPropagation();
    setOpenTabs((prev) => {
      const next = prev.filter((t) => t !== id);
      if (activeIdRef.current === id) {
        const fallback = next[next.length - 1] ?? null;
        setActiveId(fallback);
        const n = fallback ? notes.find((x) => x.id === fallback) : null;
        loadIntoEditor(n ? n.text : "");
      }
      return next;
    });
  }

  async function newNote() {
    const created = await invoke<Note | null>("add_note", { text: "" }).catch(() => null);
    if (created) {
      knownIds.current.add(created.id);
      await refresh();
      setOpenTabs((prev) => [...prev, created.id]);
      setActiveId(created.id);
      loadIntoEditor("");
      setTimeout(() => editorRef.current?.focus(), 30);
    }
  }

  function recountWords() {
    const t = (editorRef.current?.textContent || "").trim();
    setWordCount(t ? t.split(/\s+/).length : 0);
  }
  function scheduleSave() {
    recountWords();
    const id = activeIdRef.current;
    if (!id || !editorRef.current) return;
    const html = editorRef.current.innerHTML;
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      invoke("update_note", { id, text: html }).catch(() => {});
    }, 350);
  }

  function exec(cmd: string, value?: string) {
    editorRef.current?.focus();
    document.execCommand(cmd, false, value);
    scheduleSave();
  }
  function addLink() {
    const url = window.prompt(t("Link URL:"));
    if (url) exec("createLink", url);
  }

  // ---- IMAGE / FILE insertion (paste, drag&drop, file picker) ----
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pickImagesOnlyRef = useRef(true);
  const MAX_ATTACH = 5 * 1024 * 1024; // 5 MB / file (like Wispr)

  function escAttr(s: string) {
    return s.replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function insertHtmlAtCaret(html: string) {
    editorRef.current?.focus();
    document.execCommand("insertHTML", false, html);
    if (editorRef.current) editorRef.current.dataset.empty = "false";
    scheduleSave();
  }
  function fileToDataUrl(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result as string);
      r.onerror = reject;
      r.readAsDataURL(file);
    });
  }
  async function insertFile(file: File) {
    if (file.size > MAX_ATTACH) {
      window.alert(t("„{name}\" túl nagy (max 5 MB).").replace("{name}", file.name));
      return;
    }
    const url = await fileToDataUrl(file).catch(() => "");
    if (!url) return;
    if (file.type.startsWith("image/")) {
      // data-filename: so the system app receives the file with the correct
      // extension on open (src is the data URL itself — opened on click).
      insertHtmlAtCaret(
        `<img src="${url}" alt="${escAttr(file.name)}" data-filename="${escAttr(file.name)}" class="nb-openable" />`,
      );
    } else {
      insertHtmlAtCaret(
        `<a href="${url}" download="${escAttr(file.name)}" data-filename="${escAttr(file.name)}" class="nb-attach">📎 ${escAttr(file.name)}</a>&nbsp;`,
      );
    }
  }
  // Open an image/file with the system default app (data URL → temporary
  // file → opener). Delegated click handler on the editor (the content is raw HTML).
  function onEditorClick(e: React.MouseEvent) {
    const target = e.target as HTMLElement;
    // "img" (not just .nb-openable) → images inserted in older notes should be
    // openable too, not only ones inserted after this change.
    const img = target.closest("img") as HTMLImageElement | null;
    if (img && img.src) {
      e.preventDefault();
      invoke("open_attachment", {
        dataUrl: img.src,
        filename: img.dataset.filename || img.alt || "image.png",
      }).catch(() => {});
      return;
    }
    const link = target.closest("a[download], a.nb-attach") as HTMLAnchorElement | null;
    if (link) {
      e.preventDefault();
      invoke("open_attachment", {
        dataUrl: link.getAttribute("href") || "",
        filename: link.dataset.filename || link.getAttribute("download") || "file",
      }).catch(() => {});
    }
  }
  function onFilePicked(e: React.ChangeEvent<HTMLInputElement>) {
    const files = e.target.files;
    if (files) for (const f of Array.from(files)) insertFile(f);
    e.target.value = ""; // allow picking the same file again
  }
  function pickImage() {
    pickImagesOnlyRef.current = true;
    if (fileInputRef.current) {
      fileInputRef.current.accept = "image/*";
      fileInputRef.current.click();
    }
  }
  function pickFile() {
    pickImagesOnlyRef.current = false;
    if (fileInputRef.current) {
      fileInputRef.current.accept = "";
      fileInputRef.current.click();
    }
  }
  function onEditorPaste(e: React.ClipboardEvent) {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const it of Array.from(items)) {
      if (it.type.startsWith("image/")) {
        const f = it.getAsFile();
        if (f) {
          e.preventDefault();
          insertFile(f);
          return;
        }
      }
    }
    // text paste → keep the default behavior
  }
  function onEditorDrop(e: React.DragEvent) {
    const files = e.dataTransfer?.files;
    if (files && files.length) {
      e.preventDefault();
      for (const f of Array.from(files)) insertFile(f);
    }
  }

  // ---- SLASH COMMANDS (Notion-style): "/" → block-type menu at the cursor ----
  // `keys` are search keywords matched against what the user types after "/" —
  // they mix accentless Hungarian and English terms; functional data, not UI copy.
  const SLASH_COMMANDS = [
    { keys: "normal szoveg text body", label: t("Normál szöveg"), icon: Type, run: () => exec("formatBlock", "<p>") },
    { keys: "cim h1 heading1 nagy", label: t("Cím (H1)"), icon: Heading1, run: () => exec("formatBlock", "<h1>") },
    { keys: "fejlec h2 heading2", label: t("Fejléc (H2)"), icon: Heading2, run: () => exec("formatBlock", "<h2>") },
    { keys: "alcim h3 heading3", label: t("Alcím (H3)"), icon: Heading3, run: () => exec("formatBlock", "<h3>") },
    { keys: "felsorolas bullet lista pont", label: t("Felsorolás"), icon: List, run: () => exec("insertUnorderedList") },
    { keys: "szamozott number lista ol", label: t("Számozott lista"), icon: ListOrdered, run: () => exec("insertOrderedList") },
    { keys: "idezet quote blockquote", label: t("Idézet"), icon: Quote, run: () => exec("formatBlock", "<blockquote>") },
    { keys: "kod code pre", label: t("Kód"), icon: Code, run: () => exec("formatBlock", "<pre>") },
  ];
  const [slashOpen, setSlashOpen] = useState(false);
  const [slashQuery, setSlashQuery] = useState("");
  const [slashIdx, setSlashIdx] = useState(0);
  const [slashPos, setSlashPos] = useState({ top: 0, left: 0 });
  const slashItems = SLASH_COMMANDS.filter((c) => {
    const q = slashQuery.toLowerCase();
    return !q || c.label.toLowerCase().includes(q) || c.keys.includes(q);
  });
  function closeSlash() {
    setSlashOpen(false);
    setSlashQuery("");
    setSlashIdx(0);
  }
  // Inspects the text before the cursor: if "/word" stands at the start of the line
  // or after a space, opens the menu with "word" as the filter.
  function detectSlash() {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return closeSlash();
    const range = sel.getRangeAt(0);
    const node = range.startContainer;
    if (node.nodeType !== Node.TEXT_NODE) return closeSlash();
    const before = (node.textContent || "").slice(0, range.startOffset);
    const m = before.match(/(?:^|\s)\/([\p{L}\w]*)$/u);
    if (!m) return closeSlash();
    setSlashQuery(m[1]);
    setSlashIdx(0);
    let r = range.getBoundingClientRect();
    if (!r || (r.top === 0 && r.left === 0)) {
      const er = editorRef.current?.getBoundingClientRect();
      if (er) r = er;
    }
    setSlashPos({ top: (r.bottom || r.top) + 4, left: Math.max(8, r.left) });
    setSlashOpen(true);
  }
  function onEditorInput() {
    scheduleSave();
    if (editorRef.current) {
      editorRef.current.dataset.empty = (editorRef.current.textContent || "").trim() ? "false" : "true";
    }
    detectSlash();
  }
  // Delete the "/word" + run the command. The deletion length is computed from the
  // ACTUAL DOM text (not from the async, possibly stale slashQuery state).
  function applySlash(cmd: { run: () => void }) {
    const sel = window.getSelection();
    if (sel && sel.rangeCount > 0) {
      const range = sel.getRangeAt(0);
      const node = range.startContainer;
      if (node.nodeType === Node.TEXT_NODE) {
        const before = (node.textContent || "").slice(0, range.startOffset);
        const m = before.match(/\/[\p{L}\w]*$/u); // the actual "/word" at the cursor
        if (m) {
          try {
            range.setStart(node, range.startOffset - m[0].length);
            range.deleteContents();
            sel.removeAllRanges();
            sel.addRange(range);
          } catch {
            /* ignore */
          }
        }
      }
    }
    closeSlash();
    cmd.run();
  }
  function onEditorKeyDown(e: React.KeyboardEvent) {
    if (!slashOpen || slashItems.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSlashIdx((i) => (i + 1) % slashItems.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSlashIdx((i) => (i - 1 + slashItems.length) % slashItems.length);
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      applySlash(slashItems[Math.min(slashIdx, slashItems.length - 1)]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeSlash();
    }
  }

  // Delete a given note (from the left list or the editor). Backend
  // delete_note → notes-changed event → the list refreshes (the note disappears).
  function removeNote(id: string) {
    invoke("delete_note", { id })
      .then(() => refresh())
      .catch(() => {});
    closeTab(id);
  }
  function deleteActive() {
    if (activeId) removeNote(activeId);
  }

  async function copyActive() {
    if (editorRef.current) {
      const txt = editorRef.current.textContent || "";
      try {
        await navigator.clipboard.writeText(txt);
      } catch {
        /* ignore */
      }
    }
  }

  function togglePin(id: string, pinned: boolean) {
    invoke("set_note_pinned", { id, pinned })
      .then(() => refresh())
      .catch(() => {});
  }

  // Dictation goes to the CURSOR in the active note (event from the bar). If there
  // is no active note → a new note with the text.
  function insertDictation(text: string) {
    const t = (text || "").trim();
    if (!t) return;
    const el = editorRef.current;
    if (!activeIdRef.current || !el) {
      invoke("add_note", { text: t }).then(() => refresh()).catch(() => {});
      return;
    }
    el.focus();
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || !el.contains(sel.anchorNode)) {
      const range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
      sel?.removeAllRanges();
      sel?.addRange(range);
    }
    document.execCommand("insertText", false, t);
    scheduleSave();
  }
  useEffect(() => {
    const un = listen<string>("notebook-dictate", (e) => insertDictation(e.payload));
    return () => {
      un.then((f) => f()).catch(() => {});
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const tabNotes = openTabs.map((id) => notes.find((n) => n.id === id)).filter(Boolean) as Note[];

  // Heading levels (Apple Notes-style): Normal + 3 levels.
  const headings: { icon: typeof Bold; label: string; run: () => void }[] = [
    { icon: Type, label: t("Normál szöveg"), run: () => exec("formatBlock", "<p>") },
    { icon: Heading1, label: t("Cím (H1)"), run: () => exec("formatBlock", "<h1>") },
    { icon: Heading2, label: t("Fejléc (H2)"), run: () => exec("formatBlock", "<h2>") },
    { icon: Heading3, label: t("Alcím (H3)"), run: () => exec("formatBlock", "<h3>") },
  ];
  const tools: { icon: typeof Bold; label: string; run: () => void }[] = [
    { icon: Bold, label: t("Félkövér"), run: () => exec("bold") },
    { icon: Italic, label: t("Dőlt"), run: () => exec("italic") },
    { icon: Underline, label: t("Aláhúzott"), run: () => exec("underline") },
    { icon: Strikethrough, label: t("Áthúzott"), run: () => exec("strikeThrough") },
    { icon: List, label: t("Felsorolás"), run: () => exec("insertUnorderedList") },
    { icon: ListOrdered, label: t("Számozott"), run: () => exec("insertOrderedList") },
    { icon: Quote, label: t("Idézet"), run: () => exec("formatBlock", "<blockquote>") },
    { icon: Code, label: t("Kód"), run: () => exec("formatBlock", "<pre>") },
    { icon: LinkIcon, label: "Link", run: addLink },
    { icon: Eraser, label: t("Formázás törlése"), run: () => exec("removeFormat") },
  ];

  return (
    <div className="nb-root" data-theme={theme}>
      {/* Top strip: drag region + note-list toggle */}
      <div className="nb-titlebar" data-tauri-drag-region>
        <button
          className="nb-collapse"
          onClick={() => setCollapsed((c) => !c)}
          title={t("Jegyzetlista be/ki")}
          aria-label={t("Jegyzetlista")}
        >
          {collapsed ? <PanelLeftOpen size={17} strokeWidth={2} /> : <PanelLeftClose size={17} strokeWidth={2} />}
        </button>
        <span className="nb-titlebar-name">Lavox Notes</span>
      </div>

      <div className="nb-body">
        {/* LEFT: search + all notes (draggable width, collapsible) */}
        {!collapsed && (
          <aside className="nb-sidebar" style={{ width: sidebarWidth }}>
            <button className="nb-newbtn" onClick={newNote}>
              <Plus size={15} strokeWidth={2.4} /> {t("Új jegyzet")}
            </button>
        <div className="nb-search">
          <input
            placeholder={t("Keresés…")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="nb-list">
          {filtered.length === 0 ? (
            <div className="nb-empty">
              {query ? t("Nincs találat") : t("Diktálj a barból, vagy kezdj egy újat ✍️")}
            </div>
          ) : (
            filtered.map((n) => (
              <button
                key={n.id}
                className={`nb-item ${n.id === activeId ? "nb-item-active" : ""} ${n.pinned ? "nb-item-pinned" : ""}`}
                onClick={() => openNote(n.id)}
              >
                <span
                  className="nb-item-pin"
                  role="button"
                  data-pinned={!!n.pinned}
                  aria-label={n.pinned ? t("Kiemelés levétele") : t("Kiemelés")}
                  title={n.pinned ? t("Kiemelés levétele") : t("Kiemelés")}
                  onClick={(e) => {
                    e.stopPropagation();
                    togglePin(n.id, !n.pinned);
                  }}
                >
                  <Pin size={12} strokeWidth={1.9} />
                </span>
                <span
                  className="nb-item-del"
                  role="button"
                  aria-label={t("Jegyzet törlése")}
                  title={t("Törlés")}
                  onClick={(e) => {
                    e.stopPropagation();
                    removeNote(n.id);
                  }}
                >
                  <X size={13} strokeWidth={1.8} />
                </span>
                <div className="nb-item-title">{titleOf(n.text)}</div>
                <div className="nb-item-prev">{previewOf(n.text) || t("üres…")}</div>
                <div className="nb-item-time">{timeAgo(n.updated)}</div>
              </button>
            ))
          )}
            </div>
          </aside>
        )}
        {/* Draggable divider between the list and the editor */}
        {!collapsed && (
          <div className="nb-divider" onMouseDown={onResizeStart} title={t("Húzd az átméretezéshez")} />
        )}

        {/* RIGHT MAIN AREA */}
        <div className="nb-main">
        {/* TOP: open note tabs */}
        <div className="nb-tabs">
          {tabNotes.length === 0 ? (
            <div className="nb-tabs-empty">{t("Nyiss meg egy jegyzetet a listából")}</div>
          ) : (
            tabNotes.map((n) => (
              <div
                key={n.id}
                className={`nb-tab ${n.id === activeId ? "nb-tab-active" : ""}`}
                onClick={() => openNote(n.id)}
              >
                <span className="nb-tab-title">{titleOf(n.text)}</span>
                <span className="nb-tab-close" onClick={(e) => closeTab(n.id, e)}>
                  <X size={12} strokeWidth={2.6} />
                </span>
              </div>
            ))
          )}
        </div>

        {/* EDITOR + RIGHT-SIDE FORMATTING TOOLBAR */}
        <div className="nb-editor-wrap">
          {active ? (
            <div className="nb-card">
              <div className="nb-card-body">
                <div
                  ref={editorRef}
                  className="nb-editor"
                  contentEditable
                  suppressContentEditableWarning
                  onInput={onEditorInput}
                  onKeyDown={onEditorKeyDown}
                  onPaste={onEditorPaste}
                  onDrop={onEditorDrop}
                  onClick={onEditorClick}
                  onDragOver={(e) => e.preventDefault()}
                  onFocus={() => {
                    try {
                      document.execCommand("defaultParagraphSeparator", false, "div");
                    } catch {
                      /* ignore */
                    }
                  }}
                  onBlur={closeSlash}
                  data-placeholder={t("Írj, „/” a parancsokhoz, vagy húzz ide / illessz be képet…")}
                />
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  style={{ display: "none" }}
                  onChange={onFilePicked}
                />
                {/* Slash-command menu (Notion-style, at the cursor) */}
                {slashOpen && slashItems.length > 0 && (
                  <div className="nb-slash" style={{ top: slashPos.top, left: slashPos.left }}>
                    <div className="nb-slash-head">{t("Blokk beszúrása")}</div>
                    {slashItems.map((c, i) => (
                      <button
                        key={c.label}
                        className={`nb-slash-item ${i === slashIdx ? "nb-slash-active" : ""}`}
                        onMouseDown={(e) => {
                          e.preventDefault();
                          applySlash(c);
                        }}
                        onMouseEnter={() => setSlashIdx(i)}
                      >
                        <c.icon size={15} strokeWidth={2} />
                        <span>{c.label}</span>
                      </button>
                    ))}
                  </div>
                )}
                <div className="nb-toolbar">
                  {/* Text-style dropdown (Aa → Normal / H1 / H2 / H3) */}
                  <div className="nb-style-wrap" ref={styleRef}>
                    <button
                      className={`nb-tool ${styleOpen ? "nb-tool-on" : ""}`}
                      title={t("Szövegstílus (cím / fejléc / normál)")}
                      aria-label={t("Szövegstílus")}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => setStyleOpen((o) => !o)}
                    >
                      <Type size={16} strokeWidth={2} />
                    </button>
                    {styleOpen && (
                      <div className="nb-style-menu">
                        {headings.map((h, i) => (
                          <button
                            key={i}
                            className="nb-style-item"
                            onMouseDown={(e) => e.preventDefault()}
                            onClick={() => {
                              h.run();
                              setStyleOpen(false);
                            }}
                          >
                            <h.icon size={15} strokeWidth={2} />
                            <span>{h.label}</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  <div className="nb-tool-sep" />
                  {tools.map((t, i) => (
                    <Fragment key={i}>
                      {(i === 4 || i === 9) && <div className="nb-tool-sep" />}
                      <button className="nb-tool" title={t.label} aria-label={t.label} onMouseDown={(e) => e.preventDefault()} onClick={t.run}>
                        <t.icon size={16} strokeWidth={2} />
                      </button>
                    </Fragment>
                  ))}
                  <div className="nb-tool-sep" />
                  <button className="nb-tool" title={t("Másolás")} aria-label={t("Másolás")} onClick={copyActive}>
                    <Copy size={16} strokeWidth={2} />
                  </button>
                  <button className="nb-tool nb-tool-danger" title={t("Jegyzet törlése")} aria-label={t("Törlés")} onClick={deleteActive}>
                    <Trash2 size={16} strokeWidth={2} />
                  </button>
                </div>
              </div>
              <div className="nb-cardbar">
                <span className="nb-count">{t("{n} szó").replace("{n}", String(wordCount))}</span>
                <div className="nb-cardbar-actions">
                  <button className="nb-cbtn" title={t("Fájl csatolása")} aria-label={t("Fájl")} onMouseDown={(e) => e.preventDefault()} onClick={pickFile}>
                    <Paperclip size={16} strokeWidth={2} />
                  </button>
                  <button className="nb-cbtn" title={t("Kép beszúrása")} aria-label={t("Kép")} onMouseDown={(e) => e.preventDefault()} onClick={pickImage}>
                    <ImageIcon size={16} strokeWidth={2} />
                  </button>
                  <div className="nb-theme-wrap" ref={themeRef}>
                    <button
                      className={`nb-cbtn ${themeOpen ? "nb-cbtn-on" : ""}`}
                      title={t("Téma / beállítások")}
                      aria-label={t("Téma")}
                      onClick={() => setThemeOpen((o) => !o)}
                    >
                      <MoreHorizontal size={16} strokeWidth={2} />
                    </button>
                    {themeOpen && (
                      <div className="nb-theme-menu">
                        <div className="nb-theme-head">{t("Üveg-téma")}</div>
                        {THEMES.map((t) => (
                          <button
                            key={t.key}
                            className="nb-style-item"
                            onClick={() => {
                              setTheme(t.key);
                              setThemeOpen(false);
                            }}
                          >
                            <span>{t.label}</span>
                            {theme === t.key && <Check size={15} strokeWidth={2.4} />}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <div className="nb-no-active">
              <div className="nb-no-active-inner">
                <h2>Lavox Notes</h2>
                <p>{t("Nyiss meg egy jegyzetet, diktálj a barból, vagy kezdj egy újat.")}</p>
                <button className="nb-newbtn nb-newbtn-lg" onClick={newNote}>
                  <Plus size={16} strokeWidth={2.4} /> {t("Új jegyzet")}
                </button>
              </div>
            </div>
          )}
          </div>
        </div>
      </div>
    </div>
  );
}
