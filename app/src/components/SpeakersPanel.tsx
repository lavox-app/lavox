// Beszélők (diarizáció-enrollment) panel a Beállításokhoz.
// A backend-parancsok készen állnak (enroll_speaker / list_enrolled_speakers /
// delete_enrolled_speaker + get/set_server_config) — ez a UI a STATUS.md
// M5.1 UX-flow-ját valósítja meg: lista (név, mintaszám, „én" jelölés, törlés)
// + „Új beszélő": név + is_me → 20 mp vezetett felvétel (record_mic) → enroll.
import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Users, Mic, Trash2, RefreshCw } from "lucide-react";
import { t } from "../lib/i18n";

interface Speaker {
  id: string;
  name: string;
  is_me: boolean;
  num_samples: number;
}

interface ServerConfig {
  url: string;
  api_key: string;
  workspace: string;
}

/** A 20 mp-es enrollment alatt felolvasandó mintaszöveg (magyar, fonéma-gazdag). */
const PROMPT_SENTENCE =
  "A gyors barna róka átugrik a lusta kutya fölött, miközben a nyári zápor " +
  "kopogása végigfut a bádogtetőn. Kérek egy dupla eszpresszót citromhéjjal, " +
  "és mesélj valamit a legutóbbi utazásodról.";

const RECORD_SECONDS = 20;

export function SpeakersPanel() {
  const [cfg, setCfg] = useState<ServerConfig>({ url: "", api_key: "", workspace: "default" });
  const [cfgSaved, setCfgSaved] = useState(false);
  const [pairCode, setPairCode] = useState("");
  const [pairing, setPairing] = useState(false);
  const [pairMsg, setPairMsg] = useState("");
  const [speakers, setSpeakers] = useState<Speaker[]>([]);
  const [listErr, setListErr] = useState("");
  const [loading, setLoading] = useState(false);

  // Új beszélő űrlap
  const [newName, setNewName] = useState("");
  const [newIsMe, setNewIsMe] = useState(false);
  const [recording, setRecording] = useState(false);
  const [countdown, setCountdown] = useState(0);
  const [enrollMsg, setEnrollMsg] = useState("");
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    invoke<ServerConfig>("get_server_config").then(setCfg).catch(() => {});
    return () => {
      if (timerRef.current !== null) window.clearInterval(timerRef.current);
    };
  }, []);

  const refresh = useCallback(async () => {
    if (!cfg.url.trim()) return;
    setLoading(true);
    setListErr("");
    try {
      const raw = await invoke<string>("list_enrolled_speakers");
      const parsed = JSON.parse(raw);
      setSpeakers(Array.isArray(parsed?.speakers) ? parsed.speakers : []);
    } catch (e) {
      setListErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [cfg.url]);

  // A lista első betöltése, amint van szerver-URL.
  useEffect(() => {
    if (cfg.url.trim()) refresh();
  }, [cfg.url, refresh]);

  const saveConfig = useCallback(async () => {
    try {
      await invoke("set_server_config", { config: cfg });
      setCfgSaved(true);
      setTimeout(() => setCfgSaved(false), 1800);
      refresh();
    } catch (e) {
      setListErr(String(e));
    }
  }, [cfg, refresh]);

  /** A webapp onboardingjában megjelenő kód beváltása — a szerver-config
   * (url/api_key/workspace) automatikusan a Lavox-felhőre áll, kézi
   * beállítás nélkül. A háttér-heartbeat innentől magától indul. */
  const doPair = useCallback(async () => {
    const code = pairCode.trim();
    if (!code || pairing) return;
    setPairing(true);
    setPairMsg("");
    try {
      const newCfg = await invoke<ServerConfig>("hub_pair_claim", { code });
      setCfg(newCfg);
      setPairCode("");
      setPairMsg(t("Párosítva ✓ — a Hub mostantól a felhős fiókodhoz kapcsolódik"));
    } catch (e) {
      setPairMsg(t("Hiba: {msg}").replace("{msg}", String(e)));
    } finally {
      setPairing(false);
    }
  }, [pairCode, pairing]);

  const startEnroll = useCallback(async () => {
    const name = newName.trim();
    if (!name || recording) return;
    setEnrollMsg("");
    setRecording(true);
    setCountdown(RECORD_SECONDS);
    // Kliens-oldali visszaszámláló, amíg a szinkron record_mic fut.
    timerRef.current = window.setInterval(() => {
      setCountdown((c) => (c > 0 ? c - 1 : 0));
    }, 1000);
    try {
      const wavPath = await invoke<string>("record_mic", { seconds: RECORD_SECONDS });
      setEnrollMsg(t("Feltöltés…"));
      await invoke<string>("enroll_speaker", { wavPath, name, isMe: newIsMe });
      setEnrollMsg(t("Mentve ✓ — új minta ugyanazzal a névvel = pontosítás"));
      setNewName("");
      setNewIsMe(false);
      refresh();
    } catch (e) {
      setEnrollMsg(t("Hiba: {msg}").replace("{msg}", String(e)));
    } finally {
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
      setRecording(false);
      setCountdown(0);
    }
  }, [newName, newIsMe, recording, refresh]);

  const remove = useCallback(
    async (id: string) => {
      try {
        await invoke("delete_enrolled_speaker", { speakerId: id });
        refresh();
      } catch (e) {
        setListErr(String(e));
      }
    },
    [refresh],
  );

  return (
    <>
      <h2 className="section-label" style={{ marginTop: "2rem" }}>
        <Users size={16} strokeWidth={1.75} style={{ marginRight: 6, verticalAlign: -2 }} />
        {t("Beszélők (meeting-felismerés)")}
      </h2>
      <p className="setting-hint">
        {t("A meeting-átiratban név szerint azonosítja, ki beszél. Rögzíts 20 mp hangmintát beszélőnként.")}
      </p>

      {/* Felhő-párosítás: a webapp onboardingja mutat egy kódot, azt kell ide
          beírni. Sikeres párosítás után a szerver-mezők maguktól kitöltődnek. */}
      <div className="setting-row" style={{ gap: 8 }}>
        <input
          className="api-key-input"
          placeholder={t("Párosító kód a webappból (pl. E9MK-G3YD)")}
          value={pairCode}
          onChange={(e) => setPairCode(e.target.value)}
          disabled={pairing}
          style={{ flex: 2 }}
        />
        <button
          className="btn-sm btn-teal"
          type="button"
          onClick={doPair}
          disabled={!pairCode.trim() || pairing}
        >
          {pairing ? "…" : t("Párosítás")}
        </button>
      </div>
      {pairMsg && <p className="setting-hint">{pairMsg}</p>}

      {/* Szerver-beállítás (a diarizáció-szerver; lokál vagy cloud) —
          párosítás után magától kitöltődik, kézzel csak self-hostinghoz kell. */}
      <div className="setting-row" style={{ gap: 8 }}>
        <input
          className="api-key-input"
          placeholder="http://127.0.0.1:8000"
          value={cfg.url}
          onChange={(e) => setCfg((c) => ({ ...c, url: e.target.value }))}
          style={{ flex: 2 }}
        />
        <input
          type="password"
          className="api-key-input"
          placeholder={t("API kulcs (opcionális)")}
          value={cfg.api_key}
          onChange={(e) => setCfg((c) => ({ ...c, api_key: e.target.value }))}
          style={{ flex: 1 }}
        />
        <button className="btn-sm btn-teal" type="button" onClick={saveConfig}>
          {cfgSaved ? t("Mentve ✓") : t("Mentés")}
        </button>
      </div>

      {!cfg.url.trim() ? (
        <p className="setting-hint">{t("Add meg a transzkripciós szerver címét a beszélő-profilokhoz.")}</p>
      ) : (
        <>
          {/* Profil-lista */}
          <div className="setting-row" style={{ marginTop: 10 }}>
            <div className="setting-info">
              <span>{t("Profilok ({n})").replace("{n}", String(speakers.length))}</span>
            </div>
            <button className="btn-sm btn-outline" type="button" onClick={refresh} disabled={loading}>
              <RefreshCw size={13} strokeWidth={2} style={{ verticalAlign: -2, marginRight: 4 }} />
              {loading ? "…" : t("Frissítés")}
            </button>
          </div>
          {listErr && <p className="setting-error">{listErr}</p>}
          {speakers.length > 0 && (
            <ul className="custom-doc-list">
              {speakers.map((s) => (
                <li key={s.id} className="custom-doc-item">
                  <span className="custom-doc-name">
                    {s.name}
                    {s.is_me && <span className="speaker-me-badge">{t("én")}</span>}
                  </span>
                  <span className="custom-doc-chars">
                    {t("{n} minta").replace("{n}", String(s.num_samples))}
                  </span>
                  <button
                    className="btn-sm btn-feedback btn-feedback-reject"
                    type="button"
                    onClick={() => remove(s.id)}
                    aria-label={t("Törlés")}
                  >
                    <Trash2 size={12} strokeWidth={2} />
                  </button>
                </li>
              ))}
            </ul>
          )}

          {/* Új beszélő */}
          <div className="setting-row" style={{ marginTop: 12, gap: 8 }}>
            <input
              className="api-key-input"
              placeholder={t("Név (pl. Dávid)")}
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              disabled={recording}
              style={{ flex: 1 }}
            />
            <label style={{ display: "flex", alignItems: "center", gap: 5, fontSize: "0.78rem", whiteSpace: "nowrap" }}>
              <input
                type="checkbox"
                checked={newIsMe}
                onChange={(e) => setNewIsMe(e.target.checked)}
                disabled={recording}
              />
              {t("ez én vagyok")}
            </label>
            <button
              className="btn-sm btn-teal"
              type="button"
              onClick={startEnroll}
              disabled={!newName.trim() || recording}
            >
              <Mic size={13} strokeWidth={2} style={{ verticalAlign: -2, marginRight: 4 }} />
              {recording ? t("Felvétel… {n}s").replace("{n}", String(countdown)) : t("20 mp felvétel")}
            </button>
          </div>
          {recording && (
            <div className="speaker-prompt">
              <strong>{t("Olvasd fel hangosan:")}</strong>
              <p>{PROMPT_SENTENCE}</p>
            </div>
          )}
          {enrollMsg && <p className="setting-hint">{enrollMsg}</p>}
        </>
      )}
    </>
  );
}
