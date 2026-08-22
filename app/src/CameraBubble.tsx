import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import "./CameraBubble.css";

// Circular camera preview that the screen recording captures as PiP.
// If getUserMedia fails (no camera / permission denied), it signals Rust
// (hide_camera_bubble) → the recording continues with screen only.
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
        // No camera/permission → Rust closes the bubble (screen-only recording).
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
          <div className="cam-err">No camera</div>
        ) : (
          <video ref={videoRef} autoPlay playsInline muted />
        )}
      </div>
    </div>
  );
}
