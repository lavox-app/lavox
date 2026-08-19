// A pill felvétel-szignója: VALÓS IDEJŰ waveform. A sávok magassága a tényleges
// mikrofon-szintből (RMS, a Rust ~50ms-enként küldi) jön → a vonalak CSAK akkor
// emelkednek, ha tényleg beszélsz; csendben laposak. Tisztán CSS scaleY-ből,
// framer-motion-nal (GPU-gyorsított).
import { motion } from "framer-motion";

// Ennyi sáv = ennyi minta gördül át a hullámon (új jobbra, balra kiscrollozik).
export const WAVE_BARS = 14;

interface WaveformProps {
  // 0..1 normalizált szintek sávonként (a legfrissebb a tömb végén). Ha üres
  // vagy nincs, a sávok laposak (csend).
  levels?: number[];
}

export function Waveform({ levels }: WaveformProps) {
  const data =
    levels && levels.length ? levels : new Array(WAVE_BARS).fill(0);
  return (
    <div className="pill-wave" aria-hidden>
      {data.map((lv, i) => {
        const clamped = Math.min(1, Math.max(0, lv));
        // 0.10 = lapos alap (csend), 1.0 = teli (hangos beszéd).
        const scaleY = 0.1 + clamped * 0.9;
        return (
          <motion.span
            key={i}
            className="pill-wave-bar"
            animate={{ scaleY }}
            // Rövid, lágy követés → folyamatos, nem ugráló hullám.
            transition={{ duration: 0.12, ease: "easeOut" }}
          />
        );
      })}
    </div>
  );
}

// Transzkripció: a hullám helyett finom „futó” shimmer-pontok — jelzi, hogy
// dolgozik, de már nem hangot vesz fel. Három pont, egymás után felizzik.
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
