// The pill's recording signature: REAL-TIME waveform. Bar heights come from the
// actual microphone level (RMS, sent by Rust every ~50ms) → the lines rise ONLY
// when you actually speak; flat in silence. Pure CSS scaleY with
// framer-motion (GPU-accelerated).
import { motion } from "framer-motion";

// This many bars = this many samples roll through the wave (new on the right, scrolls out left).
export const WAVE_BARS = 14;

interface WaveformProps {
  // 0..1 normalized levels per bar (the newest at the end of the array). If empty
  // or missing, the bars are flat (silence).
  levels?: number[];
}

export function Waveform({ levels }: WaveformProps) {
  const data =
    levels && levels.length ? levels : new Array(WAVE_BARS).fill(0);
  return (
    <div className="pill-wave" aria-hidden>
      {data.map((lv, i) => {
        const clamped = Math.min(1, Math.max(0, lv));
        // 0.10 = flat baseline (silence), 1.0 = full (loud speech).
        const scaleY = 0.1 + clamped * 0.9;
        return (
          <motion.span
            key={i}
            className="pill-wave-bar"
            animate={{ scaleY }}
            // Short, soft follow → a continuous, non-jumpy wave.
            transition={{ duration: 0.12, ease: "easeOut" }}
          />
        );
      })}
    </div>
  );
}

// Transcription: instead of the wave, subtle "running" shimmer dots — signals
// it's working but no longer recording audio. Three dots, glowing in sequence.
export function Shimmer() {
  return (
    <div className="pill-shimmer" aria-hidden>
      {[0, 1, 2].map((i) => (
        <motion.span
          key={i}
          className="pill-shimmer-dot"
          initial={{ opacity: 0.25 }}
          animate={{ opacity: [0.25, 1, 0.25] }}
          transition={{
            duration: 1.1,
            delay: i * 0.18,
            repeat: Infinity,
            ease: "easeInOut",
          }}
        />
      ))}
    </div>
  );
}
