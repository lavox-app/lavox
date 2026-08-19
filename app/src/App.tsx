// Fő ablak — dashboard-szerű layout: bal sidebar (4 mód + Beállítások) + jobb
// main-content az aktív mód nézetével. A megosztott állapotot (modell, mikrofonok)
// itt töltjük be egyszer, és átadjuk a nézeteknek.
import { useState, useEffect } from "react";
import { invoke } from "@tauri-apps/api/core";
import "./App.css";
import { DEFAULT_MODE, type ModeId } from "./modes";
import { Sidebar } from "./components/Sidebar";
import { DictationView } from "./views/DictationView";
import { MeetingsView } from "./views/MeetingsView";
import { VideoView } from "./views/VideoView";
import { NotesView } from "./views/NotesView";
import { SettingsView } from "./views/SettingsView";

type Route = ModeId | "settings";

function App() {
  const [route, setRoute] = useState<Route>(DEFAULT_MODE);
  const [mics, setMics] = useState<string[]>([]);
  const [modelPath, setModelPath] = useState<string | null>(null);

  // Megosztott állapot betöltése egyszer (a befagyasztott parancsokkal).
  useEffect(() => {
    invoke<string>("find_model")
      .then(setModelPath)
      .catch(() => setModelPath(null));
    invoke<string[]>("list_mics")
      .then(setMics)
      .catch(() => {});
  }, []);

  const modelName = modelPath ? modelPath.split("/").pop() ?? null : null;

  function renderRoute() {
    switch (route) {
      case "dictation":
        return <DictationView modelPath={modelPath} mics={mics} />;
      case "meeting":
        return <MeetingsView />;
      case "video":
        return <VideoView />;
      case "note":
        return <NotesView />;
      case "settings":
        return <SettingsView modelPath={modelPath} mics={mics} />;
    }
  }

  return (
    <div className="app-layout">
      <Sidebar current={route} onSelect={setRoute} modelName={modelName} />
      <main className="main-content">{renderRoute()}</main>
    </div>
  );
}

export default App;
