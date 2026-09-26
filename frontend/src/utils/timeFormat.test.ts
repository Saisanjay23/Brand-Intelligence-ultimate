/**
 * The live elapsed-time clock re-renders the view that uses it -- the whole
 * Analysis page, or the Live Results rail. It used to tick every 200ms for a
 * display that only shows whole seconds, so four of every five renders drew
 * the same thing. It now wakes on each second boundary: same numbers, one
 * render per second.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { formatElapsed, useLiveTimer } from "./timeFormat";

describe("useLiveTimer", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-26T10:00:00.300Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("updates once per displayed second, on time", () => {
    const started = Date.parse("2026-09-26T10:00:00.000Z") / 1000;
    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useLiveTimer(started, true);
    });
    const afterMount = renders;

    // In 100ms steps, each its own act(): one act() batches every update
    // inside it into a single render, which would hide the difference.
    for (let i = 0; i < 50; i++) {
      act(() => { vi.advanceTimersByTime(100); });
    }
    // ~5 seconds -> ~5 renders (the old 200ms tick made ~25).
    expect(renders - afterMount).toBeGreaterThanOrEqual(4);
    expect(renders - afterMount).toBeLessThanOrEqual(6);
    expect(formatElapsed(result.current)).toBe("00m 05s");
  });

  it("stops when the job is not running", () => {
    const { result } = renderHook(() => useLiveTimer(null, false, 42));
    act(() => { vi.advanceTimersByTime(3000); });
    expect(result.current).toBe(42);
  });
});
