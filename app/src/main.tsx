import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import Overlay from "./Overlay";
import Notebook from "./Notebook";
import CameraBubble from "./CameraBubble";
import "./App.css";

// ── ONE-TIME MIGRATION: move the old "hangar-*" localStorage keys to
// "lavox-*" (Lavox Hub rename, 2026-07-23). The old key is deleted, the
// value is kept — API key, theme, calendar auth, every setting survives.
try {
  for (const key of Object.keys(localStorage)) {
    if (key.startsWith("hangar-")) {
      const newKey = "lavox-" + key.slice("hangar-".length);
      if (localStorage.getItem(newKey) === null) {
        localStorage.setItem(newKey, localStorage.getItem(key) ?? "");
      }
      localStorage.removeItem(key);
    }
  }
} catch {
  /* localStorage unavailable — nothing to do */
}

// Windows are distinguished by URL parameter:
//   ?window=overlay        → the pill overlay (bar)
//   ?window=notebook       → the Lavox Notes notebook window
//   ?window=camera-bubble  → the webcam bubble (video PiP)
//   (anything else)        → the main app
const search = window.location.search;
const which = search.includes("camera-bubble")
  ? "camera-bubble"
  : search.includes("notebook")
    ? "notebook"
    : search.includes("overlay")
      ? "overlay"
      : "app";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    {which === "camera-bubble" ? (
      <CameraBubble />
    ) : which === "notebook" ? (
      <Notebook />
    ) : which === "overlay" ? (
      <Overlay />
    ) : (
      <App />
    )}
  </React.StrictMode>,
);
