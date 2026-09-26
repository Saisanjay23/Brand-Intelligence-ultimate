import { useEffect, useState } from "react";

export function formatElapsed(seconds?: number | null): string {
  if (seconds === undefined || seconds === null || isNaN(seconds) || seconds < 0) {
    return "00m 00s";
  }
  const s = Math.floor(seconds);
  const mins = Math.floor(s / 60);
  const secs = s % 60;
  return `${String(mins).padStart(2, "0")}m ${String(secs).padStart(2, "0")}s`;
}

export function formatSeconds(seconds?: number | null): string {
  if (seconds === undefined || seconds === null || isNaN(seconds) || seconds < 0) {
    return "0.0s";
  }
  if (seconds >= 60) {
    return formatElapsed(seconds);
  }
  return `${seconds.toFixed(1)}s`;
}

/**
 * High-precision drift-free live timer hook based on Unix timestamp.
 */
export function useLiveTimer(startedAtTs?: number | null, isRunning?: boolean, staticElapsed?: number | null): number {
  const [elapsed, setElapsed] = useState<number>(() => {
    if (startedAtTs && isRunning) {
      return Math.max(0, Date.now() / 1000 - startedAtTs);
    }
    return staticElapsed ?? 0;
  });

  useEffect(() => {
    if (!isRunning || !startedAtTs) {
      if (staticElapsed !== undefined && staticElapsed !== null) {
        setElapsed(staticElapsed);
      }
      return;
    }

    // ONE RENDER PER DISPLAYED SECOND, not five. Every consumer shows whole
    // seconds (formatElapsed), and the 200ms interval this replaced
    // re-rendered the whole Analysis / Live Results view five times a second
    // for four identical frames. Waking on the next second boundary shows
    // the same numbers -- and turns over on time instead of up to 200ms late.
    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      const secs = Math.max(0, Date.now() / 1000 - startedAtTs);
      setElapsed(secs);
      const msToNext = 1000 - ((secs * 1000) % 1000);
      timer = setTimeout(tick, msToNext + 5);
    };

    tick();
    return () => clearTimeout(timer);
  }, [startedAtTs, isRunning, staticElapsed]);

  return elapsed;
}
