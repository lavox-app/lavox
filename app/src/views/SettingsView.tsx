import { useState, useEffect } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { Settings as SettingsIcon, Calendar, Mic, Sparkles, Globe } from "lucide-react";
import { t, getUiLang, setUiLang, type UiLang } from "../lib/i18n";
import {
  getHotkey, setHotkey, comboFromEvent, formatCombo,
  HOTKEY_PRESETS, type HotkeyCombo,
} from "../lib/hotkey";
import {
  calendarStatus, clearAuth, googleLogin, loadAutoRecord, saveAutoRecord,
  syncTokenToBackend, listenMeetingStarting, listenAutoRecordStart,
  type CalendarStatus, type MeetingEvent,
} from "../lib/calendar";
import { loadApiKey, saveApiKey } from "../lib/llm";
import { SpeakersPanel } from "../components/SpeakersPanel";

interface Props {
  modelPath: string | null;
  mics: string[];
}

export function SettingsView({ modelPath, mics }: Props) {
  // A naptár-állapot a Rust token-tárból jön (nem localStorage) — a refresh
  // token ott él, és a poller magától frissít.
  const [cal, setCal] = useState<CalendarStatus>({ configured: false, connected: false, email: null });
  const [autoRecord, setAutoRecord] = useState(loadAutoRecord);
  // Hangtanulás (harvest): alapból BE; a kapcsoló kikapcsolja a profil-építést.
  const [harvestOn, setHarvestOn] = useState(() => localStorage.getItem("lavox-harvest") !== "false");
  const [loggingIn, setLoggingIn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastMeeting, setLastMeeting] = useState<MeetingEvent | null>(null);
  const [recording, setRecording] = useState(false);
  // A Meet-bővítmény kapcsolat-állapota (5 mp-enként frissül).
  const [bridge, setBridge] = useState<{ lastEventMs: number; lastEventKind: string }>({
    lastEventMs: 0,
    lastEventKind: "",
  });
  const [extHelp, setExtHelp] = useState(false);
  const [apiKey, setApiKey] = useState(loadApiKey);
  const [apiKeySaved, setApiKeySaved] = useState(false);
  const [hotkey, setHotkeyState] = useState<HotkeyCombo | null>(null);
  const [capturing, setCapturing] = useState(false);

  // Modell-letöltés (first-run) — a Rust "model-download-progress" eseményét követi.
  const [dlActive, setDlActive] = useState(false);
  const [dlPercent, setDlPercent] = useState(0);
  const [dlError, setDlError] = useState("");

  useEffect(() => {
    const un = listen<{ downloaded: number; total: number; done?: boolean }>(
      "model-download-progress",
      (e) => {
        const { downloaded, total, done } = e.payload;
        if (total > 0) setDlPercent(Math.min(100, Math.round((downloaded / total) * 100)));
        if (done) window.location.reload(); // az App újra futtatja a find_model-t
      },
    );
    return () => {
      un.then((f) => f()).catch(() => {});
    };
  }, []);

  const startModelDownload = async () => {
    setDlActive(true);
    setDlError("");
    setDlPercent(0);
    try {
      await invoke<string>("download_model");
      window.location.reload();
    } catch (e) {
      setDlError(String(e));
      setDlActive(false);
    }
  };

  useEffect(() => {
    syncTokenToBackend();
    calendarStatus().then(setCal);
    getHotkey().then(setHotkeyState);
    // Az auto-record kezdőértéke a BACKENDBŐL (perzisztens, túléli az újratelepítést).
    invoke<boolean>("get_auto_record").then(setAutoRecord).catch(() => {});
    // Meeting-bővítmény kapcsolat-állapot pollozása (a felhasználó lássa, él-e).
    const pollBridge = () =>
      invoke<{ lastEventMs: number; lastEventKind: string }>("get_bridge_status")
        .then(setBridge)
        .catch(() => {});
    pollBridge();
    const bridgeTimer = setInterval(pollBridge, 5000);
    const unsub1 = listenMeetingStarting((m) => setLastMeeting(m));
    const unsub2 = listenAutoRecordStart((m) => {
      setRecording(true);
      setLastMeeting(m);
    });
    return () => {
      clearInterval(bridgeTimer);
      unsub1.then((fn) => fn());
      unsub2.then((fn) => fn());
    };
  }, []);

  const applyHotkey = (combo: HotkeyCombo) => {
    setHotkeyState(combo);
    setHotkey(combo).catch(() => {});
  };

  useEffect(() => {
    if (!capturing) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      const combo = comboFromEvent(e);
      if (combo) {
        applyHotkey(combo);
        setCapturing(false);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [capturing]);

  const handleLogin = async () => {
    setLoggingIn(true);
    setError(null);
    try {
      // A rendszer-böngésző nyílik meg; a hívás addig vár, míg a user végez.
      await googleLogin();
      setCal(await calendarStatus());
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
    setLoggingIn(false);
  };

  const handleLogout = async () => {
    await clearAuth();
    setCal(await calendarStatus());
  };

  const toggleAutoRecord = () => {
    const next = !autoRecord;
    setAutoRecord(next);
    saveAutoRecord(next);
  };

  return (
    <section className="view">
      <header className="view-header">
        <div className="view-title-row">
          <span className="view-badge">
            <SettingsIcon size={18} strokeWidth={1.75} />
          </span>
          <h1 className="view-title">{t("Beállítások")}</h1>
        </div>
        <p className="view-subtitle">{t("Rendszer-állapot, naptár és felvételi beállítások.")}</p>
      </header>

      {/* --- Felület nyelve / Interface language --- */}
      <h2 className="section-label">
        <Globe size={16} strokeWidth={1.75} style={{ marginRight: 6, verticalAlign: -2 }} />
        {t("Felület nyelve")} / Interface language
      </h2>
      <div className="setting-row">
        <div className="setting-info">
          <span>{t("A felület nyelve (újratöltéssel vált)")}</span>
        </div>
        <select
          value={getUiLang()}
          onChange={(e) => setUiLang(e.target.value as UiLang)}
        >
          <option value="en">English</option>
          <option value="hu">Magyar</option>
        </select>
      </div>

      {/* --- Google Calendar --- */}
      <h2 className="section-label">
        <Calendar size={16} strokeWidth={1.75} style={{ marginRight: 6, verticalAlign: -2 }} />
        Google Calendar
      </h2>
      {!cal.configured ? (
        // Nincs Google desktop-kliens a buildben → a gomb nem működne. Ne
        // mutassunk hamis bejelentkezés-lehetőséget (ez volt a régi hiba).
        <p className="setting-hint">
          {t("Ebben a buildben nincs beállítva a Google-kliens — a naptár-összekötés nem elérhető.")}
        </p>
      ) : cal.connected ? (
        <div className="setting-row">
          <div className="setting-info">
            <span className="setting-dot setting-dot-green" />
            <span>{t("Csatlakozva:")} <strong>{cal.email || t("Google fiók")}</strong></span>
          </div>
          <button className="btn-sm btn-outline" onClick={handleLogout} type="button">
            {t("Leválasztás")}
          </button>
        </div>
      ) : (
        <div className="setting-row">
          <span className="setting-info">
            <span className="setting-dot setting-dot-gray" />
            {t("Nincs csatlakoztatva")}
          </span>
          <button className="btn-sm btn-teal" onClick={handleLogin} disabled={loggingIn} type="button">
            {loggingIn ? "…" : t("Google bejelentkezés")}
          </button>
        </div>
      )}
      {loggingIn && (
        <p className="setting-hint">{t("A böngészőben fejezd be a bejelentkezést, majd térj vissza ide.")}</p>
      )}
      {error && <p className="setting-error">{error}</p>}

      {/* --- Auto-record toggle --- */}
      <div className="setting-row" style={{ marginTop: 12 }}>
        <div className="setting-info">
          <Mic size={16} strokeWidth={1.75} style={{ marginRight: 6, opacity: 0.6 }} />
          <span>{t("Automatikus rögzítés meeting-eknél")}</span>
        </div>
        <label className="toggle">
          <input type="checkbox" checked={autoRecord} onChange={toggleAutoRecord} />
          <span className="toggle-slider" />
        </label>
      </div>
      <p className="setting-hint">
        {autoRecord
          ? t("A meeting indulásánál automatikusan elindul a mikrofon rögzítés.")
          : t("Értesítés érkezik meeting előtt — manuálisan indíthatod a rögzítést.")}
      </p>

      {/* --- Meet-bővítmény kapcsolat-állapot (diagnosztika) --- */}
      {(() => {
        const now = Date.now();
        const seenMs = bridge.lastEventMs > 0 ? now - bridge.lastEventMs : -1;
        // "Aktív" ha 10 percen belül volt esemény (belépés/kilépés/heartbeat).
        const connected = seenMs >= 0 && seenMs < 10 * 60 * 1000;
        return (
          <div className={`bridge-status ${connected ? "ok" : "warn"}`}>
            <span className="bridge-dot" />
            {connected ? (
              <span>
                {t("Meet-bővítmény csatlakozva")} —{" "}
                {bridge.lastEventKind === "joined"
                  ? t("épp egy meetingben")
                  : t("utolsó jelzés {n} perce").replace("{n}", String(Math.max(1, Math.round(seenMs / 60000))))}
              </span>
            ) : (
              <span>
                {t("A Meet-bővítmény még nem jelzett.")}{" "}
                <button className="link-btn" type="button" onClick={() => setExtHelp((v) => !v)}>
                  {t("Hogyan töltsem be?")}
                </button>
              </span>
            )}
          </div>
        );
      })()}
      {extHelp && (
        <div className="ext-help">
          <ol>
            <li>{t("Nyisd meg a Meet-böngésződ bővítmény-oldalát (chrome://extensions vagy arc://extensions).")}</li>
            <li>{t("Kapcsold BE a „Fejlesztői mód”-ot (jobb felül).")}</li>
            <li>{t("„Kicsomagolt bővítmény betöltése” → válaszd a ~/lavox-extension mappát.")}</li>
            <li>{t("Nyiss egy Google Meetet — a bővítmény ikonján ● badge jelenik meg, és ez az állapot zöldre vált.")}</li>
          </ol>
        </div>
      )}

      {/* --- Aktuális meeting állapot --- */}
      {lastMeeting && (
        <div className="setting-meeting-card">
          <strong>{lastMeeting.title}</strong>
          <span>{t("{n} perces meeting").replace("{n}", String(lastMeeting.duration_minutes))}</span>
          {recording && <span className="recording-badge">{t("● Rögzítés...")}</span>}
        </div>
      )}

      {/* --- AI (meeting-összefoglaló) ---
          A diktálás-átíró profilok és a Prompter 2026-08-04-én kikerültek
          (félkészek voltak). Az OpenRouter-kulcs marad: a meeting-összefoglaló
          (lib/summarize.ts) használja. */}
      <h2 className="section-label" style={{ marginTop: "2rem" }}>
        <Sparkles size={16} strokeWidth={1.75} style={{ marginRight: 6, verticalAlign: -2 }} />
        {t("AI")}
      </h2>
      <p className="setting-hint">{t("A meeting-összefoglalóhoz kell. Saját kulcs, lokálisan tárolva.")}</p>

      {/* API kulcs */}
      <p className="setting-hint" style={{ marginTop: "0.875rem", marginBottom: "0.375rem" }}>{t("OpenRouter API kulcs")}</p>
      <div className="setting-row">
        <input
          type="password"
          className="api-key-input"
          placeholder="sk-or-..."
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
        <button
          className="btn-sm btn-teal"
          type="button"
          onClick={() => {
            saveApiKey(apiKey.trim());
            setApiKeySaved(true);
            setTimeout(() => setApiKeySaved(false), 1800);
          }}
        >
          {apiKeySaved ? t("Mentve ✓") : t("Mentés")}
        </button>
      </div>
      <p className="setting-hint">{t("Lokálisan tárolódik, nem kerül szerverre.")}</p>

      {/* --- Hangtanulás (harvest) kapcsoló --- */}
      <div className="setting-row" style={{ marginTop: 12 }}>
        <div className="setting-info">
          <span>{t("Automatikus hangtanulás")}</span>
        </div>
        <label className="toggle">
          <input
            type="checkbox"
            checked={harvestOn}
            onChange={() => {
              const next = !harvestOn;
              setHarvestOn(next);
              localStorage.setItem("lavox-harvest", String(next));
            }}
          />
          <span className="toggle-slider" />
        </label>
      </div>
      <p className="setting-hint">
        {t("A megnevezett beszélők hangját a rendszer megjegyzi, és a következő felvételen felirat nélkül is felismeri. A tárolt profilok lent kezelhetők/törölhetők.")}
      </p>

      {/* --- Beszélők (diarizáció-enrollment) --- */}
      <SpeakersPanel />

      {/* --- Modell --- */}
      <h2 className="section-label" style={{ marginTop: "2rem" }}>{t("Modell")}</h2>
      {modelPath ? (
        <p className="full-text">{modelPath}</p>
      ) : (
        <>
          <div className="setting-row">
            <div className="setting-info">
              <span>{t("Nincs beszédfelismerő modell — töltsd le egy kattintással.")}</span>
            </div>
            <button
              className="btn-sm btn-teal"
              type="button"
              onClick={startModelDownload}
              disabled={dlActive}
            >
              {dlActive ? `${dlPercent}%` : t("Letöltés (1,6 GB)")}
            </button>
          </div>
          {dlActive && (
            <div className="model-dl-bar">
              <div className="model-dl-fill" style={{ width: `${dlPercent}%` }} />
            </div>
          )}
          {dlError && <p className="setting-error">{dlError}</p>}
        </>
      )}

      <details className="mic-list" open={mics.length > 0}>
        <summary>{t("Mikrofonok ({n})").replace("{n}", String(mics.length))}</summary>
        <ul>
          {mics.length > 0 ? (
            mics.map((m) => <li key={m}>{m}</li>)
          ) : (
            <li>{t("Nincs észlelt bemeneti eszköz.")}</li>
          )}
        </ul>
      </details>

      <h2 className="section-label" style={{ marginTop: "1.5rem" }}>{t("Gyorsbillentyűk")}</h2>
      <div className="setting-row">
        <div className="setting-info">
          <Mic size={16} strokeWidth={1.75} style={{ marginRight: 6, opacity: 0.6 }} />
          <span>{t("Diktálás triggere (nyomva tartás)")}</span>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <code className="hotkey-current">{hotkey ? formatCombo(hotkey) : "…"}</code>
          <button
            className={`btn-sm ${capturing ? "btn-teal" : "btn-outline"}`}
            type="button"
            onClick={() => setCapturing((c) => !c)}
          >
            {capturing ? t("Nyomd le a kombót…") : t("Rögzítés")}
          </button>
        </div>
      </div>
      <div className="polish-chips" style={{ marginTop: 8 }}>
        {HOTKEY_PRESETS.map((p) => (
          <button
            key={p.label}
            className={`polish-chip ${hotkey && formatCombo(hotkey) === formatCombo(p.combo) ? "polish-chip-active" : ""}`}
            type="button"
            onClick={() => applyHotkey(p.combo)}
          >
            {p.label}
          </button>
        ))}
      </div>
      <p className="setting-hint">
        {t("Alapértelmezett: Fn nyomva tartás. Tiszta Fn-diktáláshoz állítsd: Rendszerbeállítások → Billentyűzet → „🌐 gomb megnyomására: Semmi”.")}
      </p>
    </section>
  );
}
