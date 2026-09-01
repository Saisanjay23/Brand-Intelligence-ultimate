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

    const tick = () => {
      const nowSec = Date.now() / 1000;
      setElapsed(Math.max(0, nowSec - startedAtTs));
    };

    tick();
    const interval = setInterval(tick, 200);
    return () => clearInterval(interval);
  }, [startedAtTs, isRunning, staticElapsed]);

  return elapsed;
}
