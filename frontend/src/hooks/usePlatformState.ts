import { useCallback, useEffect, useRef, useState } from "react";
import { discoveryApi } from "../api/discoveryApi";
import { sessionsApi } from "../api/sessionsApi";
import type { PlatformState, SessionInfo } from "../api/types";

// Everything that answers "can this platform actually run right now":
// /discovery/platforms (its `session_state` is what the platform checkboxes
// on the Discovery page render) and /sessions (the pools themselves, which
// drive the Sessions panel and the header's ready count).
//
// These two used to be fetched independently and only once each: platform
// health at mount and never again, sessions only when the Sessions panel
// itself changed something. So re-pasting cookies for a logged-out account
// updated its row in the pool list while the platform rail kept showing
// `exhausted` and the header kept counting it as unusable until a full page
// reload. They're one fact about one platform and are now fetched together,
// from one place, on every path that can change them:
//
//   - refresh(), called by any panel that just mutated a session
//   - a background poll, so changes made ELSEWHERE (a job quarantining a
//     session mid-sweep, the 30-minute health sweep, another operator's
//     browser tab) show up without anyone touching this one
//   - returning to the tab, since a backgrounded tab's poll is suspended
//     and whatever it last saw may be arbitrarily old
//
// This backend deliberately polls rather than pushes, see
// backend/docs/adr/0002-polling-plus-webhook-over-websocket.md.
const POLL_MS = 8000;

// One blip on a poll shouldn't replace a working screen with an error
// banner; the previous data is still on screen and still nearly current.
// Two in a row (~16s of silence) is worth telling someone about.
const FAILURES_BEFORE_ERROR = 2;

interface PlatformStateResult {
  platforms: PlatformState[];
  sessions: SessionInfo[];
  refresh: () => Promise<void>;
  error: string;
  // false until the first fetch has answered. An empty list before then
  // is "loading", not "nothing registered".
  loaded: boolean;
}

// Replace state only when the payload actually differs. Every poll builds
// fresh arrays, and handing those down unconditionally would re-render the
// whole tree (and re-run every effect keyed on `platforms`) every 8 seconds
// for data that changes maybe once an hour.
function keepIfUnchanged<T>(previous: T[], next: T[]): T[] {
  return JSON.stringify(previous) === JSON.stringify(next) ? previous : next;
}

export function usePlatformState(): PlatformStateResult {
  const [platforms, setPlatforms] = useState<PlatformState[]>([]);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [error, setError] = useState("");
  // Has a fetch EVER answered? Until it has, an empty `platforms` means
  // "not known yet", never "none registered" -- see `loaded` below.
  const [loaded, setLoaded] = useState(false);
  const everLoaded = useRef(false);

  const mounted = useRef(true);
  // The fetch currently running, and the ONE follow-up queued behind it.
  const inFlight = useRef<Promise<void> | null>(null);
  const queued = useRef<Promise<void> | null>(null);
  const failures = useRef(0);

  const fetchOnce = useCallback(async () => {
    try {
      const [health, pools] = await Promise.all([
        discoveryApi.platforms(),
        sessionsApi.allSessionStatus(),
      ]);
      if (!mounted.current) return;
      failures.current = 0;
      setPlatforms((prev) => keepIfUnchanged(prev, health.items));
      setSessions((prev) => keepIfUnchanged(prev, pools.items));
      setError("");
      everLoaded.current = true;
      setLoaded(true);
    } catch (e) {
      if (!mounted.current) return;
      failures.current += 1;
      // Before the FIRST answer there is no previous data on screen to
      // keep showing, so the "one blip" grace above does not apply: an
      // empty list there would read as "no platforms", not "unreachable".
      if (failures.current >= FAILURES_BEFORE_ERROR || !everLoaded.current) {
        setError((e as Error).message);
      }
    }
  }, []);

  // Start a fetch unless one is already running; either way, the promise
  // of the fetch that is now running.
  const start = useCallback((): Promise<void> => {
    if (inFlight.current) return inFlight.current;
    const p = fetchOnce().finally(() => {
      if (inFlight.current === p) inFlight.current = null;
    });
    inFlight.current = p;
    return p;
  }, [fetchOnce]);

  // The BACKGROUND poll (timer, focus): a slow request must not stack up
  // behind the poll timer, and a poll firing mid-fetch would only race
  // itself for the same answer -- so it just joins the running fetch.
  const poll = useCallback(() => void start(), [start]);

  // What a panel calls right after it MUTATED something. Joining the
  // running fetch is wrong here: that request left before the mutation
  // landed, so awaiting it hands the caller pre-mutation data and the
  // screen keeps showing the old state as if the change had not taken.
  // Instead ONE follow-up fetch is queued behind it (however many callers
  // ask meanwhile) and the caller resolves only once that has answered.
  const refresh = useCallback((): Promise<void> => {
    if (!inFlight.current) return start();
    if (!queued.current) {
      queued.current = inFlight.current.then(() => {
        queued.current = null;
        return start();
      });
    }
    return queued.current;
  }, [start]);

  useEffect(() => {
    mounted.current = true;
    poll();

    // Polling a tab nobody is looking at is wasted work, but only the
    // TIMER is gated on that. The events below are not: `focus` means the
    // user is looking at this window whatever the visibility API believes,
    // and gating it too would mean a browser that reports `hidden` for a
    // window the user is actually reading (embedded webviews and some
    // remote-desktop setups do) never refreshes at all.
    const tick = setInterval(() => {
      if (document.visibilityState !== "hidden") poll();
    }, POLL_MS);

    const onBackInView = () => poll();
    document.addEventListener("visibilitychange", onBackInView);
    window.addEventListener("focus", onBackInView);

    return () => {
      mounted.current = false;
      clearInterval(tick);
      document.removeEventListener("visibilitychange", onBackInView);
      window.removeEventListener("focus", onBackInView);
    };
  }, [poll]);

  return { platforms, sessions, refresh, error, loaded };
}
