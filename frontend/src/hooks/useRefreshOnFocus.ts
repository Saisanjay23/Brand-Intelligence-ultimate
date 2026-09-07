// Re-read server data when the analyst comes back to the tab.
//
// THE PROBLEM. Every list in this app fetches on mount and on its own
// filter changes, and nothing else. That is correct for changes THIS tab
// makes -- they update local state as they happen -- but it is blind to
// changes made anywhere else: a second browser tab, another analyst, the
// same person on a different machine. Validate a profile in one tab and
// the other keeps showing it as pending indefinitely, with no error and
// nothing to suggest it is out of date.
//
// Focus is the right moment to fix that, and polling is the wrong one. The
// analyst returning to the tab is the exact instant a stale list starts
// being looked at, and it costs one request per return rather than one
// every N seconds forever, on every open tab, whether or not anyone is
// reading it.
//
// WHY BOTH EVENTS. `visibilitychange` covers switching browser tabs and
// minimising; `focus` covers moving between windows/apps while this tab
// stays "visible". Neither alone catches both, and they routinely fire
// together for one return -- which is what `minIntervalMs` is really for.

import { useEffect, useRef } from "react";

export interface RefreshOnFocusOptions {
  /**
   * Floor between refreshes. Its first job is de-duping: the two listeners
   * below usually both fire for a single return, milliseconds apart, and
   * without this that is two identical requests every time. Its second is
   * to stop a rapid alt-tabber issuing a fetch per flick.
   *
   * Deliberately short. The case this feature exists for -- act in one tab,
   * switch to another -- can happen in a couple of seconds, so a long floor
   * would reintroduce exactly the staleness it is meant to remove.
   */
  minIntervalMs?: number;
  /**
   * Skip the refresh while this is true. For work whose result is not
   * written yet -- a bulk action mid-flight -- where refetching would
   * briefly render the pre-action state over the top of it.
   */
  paused?: boolean;
  /** Off entirely (e.g. a panel that is mounted but not shown). */
  enabled?: boolean;
}

export function useRefreshOnFocus(
  refresh: () => void,
  { minIntervalMs = 3000, paused = false, enabled = true }: RefreshOnFocusOptions = {},
): void {
  // Refs, not deps: re-subscribing the listeners every time a caller's
  // `refresh` identity changes (which for a useCallback over filter state
  // is most renders) would add and remove them constantly for no benefit,
  // and re-running the effect must not be able to trigger a fetch.
  const refreshRef = useRef(refresh);
  const pausedRef = useRef(paused);
  const lastRunRef = useRef(0);

  refreshRef.current = refresh;
  pausedRef.current = paused;

  useEffect(() => {
    if (!enabled) return;

    const onReturn = () => {
      // `focus` fires on a tab that is already visible; visibilitychange
      // fires for both directions. Only a visible tab is worth refreshing.
      if (document.visibilityState !== "visible") return;
      if (pausedRef.current) return;
      const now = Date.now();
      if (now - lastRunRef.current < minIntervalMs) return;
      lastRunRef.current = now;
      refreshRef.current();
    };

    document.addEventListener("visibilitychange", onReturn);
    window.addEventListener("focus", onReturn);
    return () => {
      document.removeEventListener("visibilitychange", onReturn);
      window.removeEventListener("focus", onReturn);
    };
  }, [enabled, minIntervalMs]);
}
