// Left sidebar for the main window — the visual style of the web dashboard's
// Sidebar (logo + title on top, mode nav items with icons + active state with teal background).
// NOTE: t() keys are Hungarian source strings (gettext-style, see lib/i18n.ts).
import { Settings } from "lucide-react";
import { MODES, type ModeId } from "../modes";
import { t } from "../lib/i18n";

interface Props {
  // the active mode, or "settings" when the Settings view is active
  current: ModeId | "settings";
  onSelect: (mode: ModeId | "settings") => void;
  // the loaded model's file name (small caption at the bottom), if any
  modelName?: string | null;
}

// The Lavox brand mark: descending lines (light→deep green) + surface dot.
// Consistent with the landing page and the store icon.
function Logo() {
  return (
    <svg
      width="26"
      height="26"
      viewBox="0 0 120 120"
      aria-hidden="true"
      style={{ filter: "drop-shadow(0 0 5px rgba(63,174,114,0.45))" }}
    >
      <rect x="20" y="26" width="70" height="12" rx="6" fill="#7fe3a6" />
      <rect x="27" y="49" width="63" height="12" rx="6" fill="#4fbf84" />
      <rect x="34" y="72" width="53" height="12" rx="6" fill="#3fae72" />
      <circle cx="61" cy="101" r="8.5" fill="#7fe3a6" />
    </svg>
  );
}

export function Sidebar({ current, onSelect, modelName }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-logo">
        <Logo />
        <span className="sidebar-title">Lavox</span>
      </div>
      <p className="sidebar-subtitle">Capture → Transcribe → Second Brain</p>

      {/* 4 modes as nav items — the type's own color is the dot on the right. */}
      <div className="nav-section-label">{t("Módok")}</div>
      <nav className="sidebar-nav sidebar-nav-main">
        {MODES.map((mode) => {
          const Icon = mode.icon;
          const active = current === mode.id;
          return (
            <button
              key={mode.id}
              type="button"
              className={`nav-item ${active ? "nav-item-active" : ""}`}
              onClick={() => onSelect(mode.id)}
              title={t(mode.description)}
            >
              <span className="nav-item-icon">
                <Icon size={18} strokeWidth={1.75} />
              </span>
              <span>{t(mode.label)}</span>
              {/* mode dot in the type's accent color */}
              <span
                className="nav-mode-dot"
                style={{ ["--mode-color" as string]: mode.color }}
              />
            </button>
          );
        })}
      </nav>

      {/* Settings + model info */}
      <div className="sidebar-footer">
        <nav className="sidebar-nav">
          <button
            type="button"
            className={`nav-item ${current === "settings" ? "nav-item-active" : ""}`}
            onClick={() => onSelect("settings")}
          >
            <span className="nav-item-icon">
              <Settings size={18} strokeWidth={1.75} />
            </span>
            <span>{t("Beállítások")}</span>
          </button>
        </nav>
        {modelName && (
          <p className="sidebar-model" title={modelName}>
            {t("Modell:")} {modelName}
          </p>
        )}
      </div>
    </aside>
  );
}
