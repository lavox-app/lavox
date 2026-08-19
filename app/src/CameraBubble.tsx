import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import "./CameraBubble.css";

// Kör alakú kamera-előnézet, amit a képernyőfelvétel PiP-ként rögzít.
// Ha a getUserMedia hibázik (nincs kamera / engedély megtagadva), jelez a
// Rustnak (hide_camera_bubble) → a felvétel csak képernyővel folytatódik.
export default function CameraBubble() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let stream: MediaStream | null = null;
    let cancelled = false;
    navigator.mediaDevices
      .getUserMedia({ video: { width: 640, height: 640 }, audio: false })
      .then((s) => {
        if (cancelled) {
          s.getTracks().forEach((t) => t.stop());
          return;
        }
        stream = s;
        if (videoRef.current) {
          videoRef.current.srcObject = s;
          videoRef.current.play().catch(() => {});
        }
      })
      .catch((e) => {
        setErr(String(e));
        // Nincs kamera/engedély → a Rust zárja be a buborékot (screen-only felvétel).
        invoke("hide_camera_bubble").catch(() => {});
      });
    return () => {
      cancelled = true;
      stream?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  return (
    <div className="cam-wrap">
      <div className="cam-circle">
        {err ? (
          <div className="cam-err">Nincs kamera</div>
        ) : (
          <video ref={videoRef} autoPlay playsInline muted />
        )}
      </div>
    </div>
  );
}
