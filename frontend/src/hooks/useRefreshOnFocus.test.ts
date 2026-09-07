/**
 * Re-reading server data when the analyst returns to the tab.
 *
 * THE DEFECT THIS CLOSES: every list fetches on mount and on its own filter
 * changes and nothing else, so a decision made in a second tab -- or by
 * another analyst, or the same person on another machine -- never reached
 * this one. A profile validated elsewhere stayed listed as pending here
 * indefinitely, with nothing to suggest the view was out of date.
 *
 * WHAT THESE GUARD, specifically:
 *
 *   1. THE DOUBLE FIRE. `visibilitychange` and `focus` routinely BOTH fire
 *      for one return, milliseconds apart. Without a floor between runs
 *      that is two identical requests every single time the analyst comes
 *      back -- the feature paying for itself twice over.
 *
 *   2. REFRESHING A HIDDEN TAB. `focus` can arrive while the document is
 *      still hidden. Refetching then spends a request on a list nobody is
 *      looking at, which is the polling behaviour this deliberately avoids.
 *
 *   3. REFRESHING MID-ACTION. A bulk validate writes and then reloads
 *      itself. A refresh landing in between paints the pre-action state
 *      back over the rows the analyst just acted on.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useRefreshOnFocus } from "./useRefreshOnFocus";

let visibility: DocumentVisibilityState = "visible";

function setVisibility(v: DocumentVisibilityState) {
  visibility = v;
  document.dispatchEvent(new Event("visibilitychange"));
}

/** A return to the tab, as the browser actually reports it: both events. */
function returnToTab() {
  visibility = "visible";
  document.dispatchEvent(new Event("visibilitychange"));
  window.dispatchEvent(new Event("focus"));
}

beforeEach(() => {
  visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("returning to the tab", () => {
  it("refreshes once, not once per event", () => {
    const refresh = vi.fn();
    renderHook(() => useRefreshOnFocus(refresh));

    returnToTab();

    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("refreshes again on a later return", () => {
    const refresh = vi.fn();
    renderHook(() => useRefreshOnFocus(refresh));

    returnToTab();
    vi.advanceTimersByTime(10_000);
    returnToTab();

    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("does not fetch on mount -- the list already loaded itself", () => {
    const refresh = vi.fn();
    renderHook(() => useRefreshOnFocus(refresh));
    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("a hidden tab is not refreshed", () => {
  it("ignores focus while the document is hidden", () => {
    const refresh = vi.fn();
    renderHook(() => useRefreshOnFocus(refresh));

    visibility = "hidden";
    window.dispatchEvent(new Event("focus"));

    expect(refresh).not.toHaveBeenCalled();
  });

  it("ignores the visibilitychange that reports GOING hidden", () => {
    const refresh = vi.fn();
    renderHook(() => useRefreshOnFocus(refresh));

    setVisibility("hidden");

    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("pausing", () => {
  it("skips while a bulk action is in flight", () => {
    const refresh = vi.fn();
    const { rerender } = renderHook(
      ({ paused }) => useRefreshOnFocus(refresh, { paused }),
      { initialProps: { paused: true } },
    );

    returnToTab();
    expect(refresh).not.toHaveBeenCalled();

    // ...and resumes once that action finishes.
    rerender({ paused: false });
    vi.advanceTimersByTime(10_000);
    returnToTab();
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("does nothing at all when disabled", () => {
    const refresh = vi.fn();
    renderHook(() => useRefreshOnFocus(refresh, { enabled: false }));
    returnToTab();
    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("it always calls the CURRENT refresh", () => {
  it("does not fire a stale closure after the callback changes", () => {
    /** The realistic shape: `refresh` is a useCallback over filter state, so
     *  its identity changes on most renders. Capturing the first one would
     *  refetch the filters the analyst had when the component mounted. */
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ fn }) => useRefreshOnFocus(fn), {
      initialProps: { fn: first },
    });

    rerender({ fn: second });
    returnToTab();

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});

describe("cleanup", () => {
  it("stops listening once unmounted", () => {
    const refresh = vi.fn();
    const { unmount } = renderHook(() => useRefreshOnFocus(refresh));

    unmount();
    returnToTab();

    expect(refresh).not.toHaveBeenCalled();
  });
});
