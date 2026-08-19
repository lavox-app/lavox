import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import Overlay from "./Overlay";
import Notebook from "./Notebook";
import CameraBubble from "./CameraBubble";
import "./App.css";

// ── EGYSZERI MIGRÁCIÓ: a régi "hangar-*" localStorage-kulcsok átvitele
// "lavox-*"-ra (Lavox Hub átnevezés, 2026-07-23). A régi kulcs törlődik,
// az érték megmarad — API-kulcs, téma, naptár-auth, minden beállítás él.
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
  /* localStorage nem elérhető — nincs teendő */
}

// Az ablakokat URL-paraméter alapján különböztetjük meg:
//   ?window=overlay        → a pill overlay (bar)
//   ?window=notebook       → a Lavox Notes jegyzetfüzet-ablak
//   ?window=camera-bubble  → a webkamera-buborék (videó PiP)
//   (egyéb)                → a fő app
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
