/**
 * The Scheduler store is a VIEW over the server now, not an engine.
 *
 * WHERE THE OLD TESTS WENT. This file used to guard four defects in a run
 * loop that lived in the browser tab -- a page reload forking the sweep,
 * a fast Stop being dropped, giving up on a job orphaning it, and "no
 * usable session" being reported as a clean sweep. That loop is gone: it
 * could not run at two in the morning with the tab closed, which is the
 * whole point of a scheduler. The guarantees it protected did not go
 * anywhere, they moved to where the loop now is, and are asserted in
 * backend/tests/test_scheduler_engine.py.
 *
 * Reload-forking specifically is no longer possible to have: there is no
 * loop in this tab to kill, and the server never starts a second sweep of
 * a client it is already sweeping.
 *
 * WHAT IS LEFT TO GET WRONG HERE, and is asserted below:
 *   1. QUEUE ORDER IS WHAT GETS SENT. The order an analyst drags is the
 *      order clients are swept in. A reorder that writes the old order, or
 *      writes it sorted, is silent -- the queue looks right on screen and
 *      the server sweeps something else.
 *   2. A DEAD BACKEND MUST SAY SO. The server owns the schedule now, so if
 *      it cannot be reached nothing will fire. Continuing to render the
 *      last good queue as though it were current is the one lie an analyst
 *      would believe.
 *   3. "UNKNOWN" COVERAGE MUST NOT BECOME "NOTHING OWED". -1 is not 0.
 *   4. THE CLIENT BEING SWEPT CANNOT BE REMOVED from under the run.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Client } from "../api/types";

const getState = vi.fn();
const setQueue = vi.fn();
const runNow = vi.fn();
const stop = vi.fn();
const setSchedule = vi.fn();

vi.mock("../api/schedulerApi", async () => {
  const actual = await vi.importActual<typeof import("../api/schedulerApi")>(
    "../api/schedulerApi",
  );
  return {
    ...actual,
    browserTimezone: () => "Asia/Kolkata",
    schedulerApi: {
      getState: (...a: unknown[]) => getState(...a),
      setQueue: (...a: unknown[]) => setQueue(...a),
      runNow: (...a: unknown[]) => runNow(...a),
      stop: (...a: unknown[]) => stop(...a),
      setSchedule: (...a: unknown[]) => setSchedule(...a),
      preview: vi.fn(),
      listRuns: vi.fn(),
    },
  };
});

type Store = typeof import("./scheduleRunner");

const SCHEDULE = {
  enabled: false, mode: "daily" as const, at: "02:00",
  on_date: "", weekdays: [], tz: "Asia/Kolkata",
};

function serverState(over: Record<string, unknown> = {}) {
  return {
    schedule: SCHEDULE,
    queue: [],
    queue_names: {},
    running: false,
    next_run_at: null,
    upcoming: [],
    last_fired_at: null,
    wall_clock_shift: null,
    catch_up_grace_minutes: 360,
    run: null,
    ...over,
  };
}

function entry(clientId: string, over: Record<string, unknown> = {}) {
  return {
    client_id: clientId, name: clientId, status: "done", job_id: "j1",
    message: "", found: 0, new_profiles: 0, platforms: {},
    platform_details: {}, owed: 0, closing_gaps: false,
    started_at: null, finished_at: null, ...over,
  };
}

function client(clientId: string): Client {
  return {
    client_id: clientId, name: clientId.toUpperCase(),
    name_keywords: ["acme"], domain_keywords: [],
    domain: "",
    platform_limits_individual: {},
    platform_limits_domain: {},
    platform_tab_limits: {},
  };
}

let store: Store;
// Keeps the poller alive for the duration of a test; without a subscriber
// the store deliberately stops polling.
let unsubscribe: () => void;

async function settle(times = 4) {
  for (let i = 0; i < times; i += 1) await Promise.resolve();
}

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  getState.mockReset();
  setQueue.mockReset();
  runNow.mockReset();
  stop.mockReset();
  setSchedule.mockReset();
  getState.mockResolvedValue(serverState());
  setQueue.mockResolvedValue(serverState());
  runNow.mockResolvedValue({ run_id: "r1", message: "ok" });
  stop.mockResolvedValue({ stopping: true, message: "ok" });
  store = await import("./scheduleRunner");
  unsubscribe = store.subscribe(() => {});
  await settle();
});

afterEach(() => {
  unsubscribe?.();
  vi.useRealTimers();
});

describe("reading the server's queue", () => {
  it("renders the queue in the order the server has it", async () => {
    getState.mockResolvedValue(
      serverState({ queue: ["c3", "c1", "c2"], queue_names: { c3: "Three", c1: "One", c2: "Two" } }),
    );
    await store.refresh();
    await settle();
    expect(store.getSnapshot().entries.map((e) => e.client_id)).toEqual(["c3", "c1", "c2"]);
  });

  it("orders by the QUEUE, not by the order the run reports its entries", async () => {
    // The run's entry list can legitimately be in a different order (it is
    // whatever the server last wrote). The queue is the ordering authority.
    getState.mockResolvedValue(serverState({
      queue: ["c1", "c2"],
      run: {
        run_id: "r1", trigger: "manual", status: "done", message: "", current_id: "",
        stopping: false, started_at: null, finished_at: null, due_at: null, late_seconds: 0,
        entries: [entry("c2"), entry("c1")],
      },
    }));
    await store.refresh();
    await settle();
    expect(store.getSnapshot().entries.map((e) => e.client_id)).toEqual(["c1", "c2"]);
  });

  it("shows a client queued after the last run as freshly queued", async () => {
    getState.mockResolvedValue(serverState({
      queue: ["c1", "c_new"],
      run: {
        run_id: "r1", trigger: "manual", status: "done", message: "", current_id: "",
        stopping: false, started_at: null, finished_at: null, due_at: null, late_seconds: 0,
        entries: [entry("c1", { status: "done", found: 9 })],
      },
    }));
    await store.refresh();
    await settle();
    const [, fresh] = store.getSnapshot().entries;
    expect(fresh.client_id).toBe("c_new");
    expect(fresh.status).toBe("pending");
    expect(fresh.found).toBe(0);
  });
});

describe("the coverage ledger's unknown", () => {
  it("keeps -1 as -1 and never rounds it to nothing-owed", async () => {
    // 0 is the clean bill of health. A read that failed must not be able
    // to impersonate one -- that is the false all-clear that looks exactly
    // like success.
    getState.mockResolvedValue(serverState({
      queue: ["c1"],
      run: {
        run_id: "r1", trigger: "scheduled", status: "done", message: "", current_id: "",
        stopping: false, started_at: null, finished_at: null, due_at: null, late_seconds: 0,
        entries: [entry("c1", { owed: -1 })],
      },
    }));
    await store.refresh();
    await settle();
    expect(store.getSnapshot().entries[0].owed).toBe(-1);
    expect(store.getSnapshot().entries[0].owed).not.toBe(0);
  });
});

describe("writing the queue", () => {
  it("sends the full list, in order, when a client is added", async () => {
    getState.mockResolvedValue(serverState({ queue: ["c1"] }));
    await store.refresh();
    await settle();
    store.setDirectory([client("c1"), client("c2")]);
    store.enqueue(client("c2"));
    await settle(8);
    expect(setQueue).toHaveBeenCalledWith(["c1", "c2"]);
  });

  it("sends the NEW order after a reorder, not the old one", async () => {
    getState.mockResolvedValue(serverState({ queue: ["a", "b", "c"] }));
    await store.refresh();
    await settle();
    store.reorder("c", "a");        // move c to where a is
    await settle(8);
    expect(setQueue).toHaveBeenCalledWith(["c", "a", "b"]);
  });

  it("refuses to remove the client being swept right now", async () => {
    getState.mockResolvedValue(serverState({
      queue: ["c1", "c2"],
      running: true,
      run: {
        run_id: "r1", trigger: "scheduled", status: "running", message: "",
        current_id: "c1", stopping: false, started_at: null, finished_at: null,
        due_at: null, late_seconds: 0,
        entries: [entry("c1", { status: "running" }), entry("c2", { status: "pending" })],
      },
    }));
    await store.refresh();
    await settle();
    store.dequeue("c1");
    await settle(4);
    // Removing it would leave its sweep running with nothing tracking it.
    expect(setQueue).not.toHaveBeenCalled();
  });

  it("will not clear the queue mid-run", async () => {
    getState.mockResolvedValue(serverState({ queue: ["c1"], running: true }));
    await store.refresh();
    await settle();
    store.clearQueue();
    await settle(4);
    expect(setQueue).not.toHaveBeenCalled();
  });
});

describe("running", () => {
  it("asks the server rather than looping here", async () => {
    await store.start();
    await settle();
    expect(runNow).toHaveBeenCalledTimes(1);
  });

  it("lets a refusal through instead of swallowing it", async () => {
    // A 409 means a run is ALREADY going -- most likely the scheduled one.
    // Silently doing nothing would make the button look broken.
    runNow.mockRejectedValue(new Error("a run is already in progress"));
    await expect(store.start()).rejects.toThrow("already in progress");
  });

  it("stop asks the server too", async () => {
    await store.stop();
    await settle();
    expect(stop).toHaveBeenCalledTimes(1);
  });
});

describe("when the backend cannot be reached", () => {
  it("says so rather than showing the last queue as current", async () => {
    getState.mockResolvedValue(serverState({ queue: ["c1"] }));
    await store.refresh();
    await settle();
    expect(store.getSnapshot().error).toBe("");

    getState.mockRejectedValue(new Error("Failed to fetch"));
    await store.refresh();
    await settle();

    const snap = store.getSnapshot();
    // The server owns the schedule now: if it is unreachable, nothing is
    // going to fire, and the page has to say that.
    expect(snap.error).toContain("Failed to fetch");
    // The rows stay -- stale data plus a warning beats a blank page --
    // but they are no longer claimed to be current.
    expect(snap.entries).toHaveLength(1);
  });

  it("clears the error once the backend answers again", async () => {
    getState.mockRejectedValue(new Error("Failed to fetch"));
    await store.refresh();
    await settle();
    expect(store.getSnapshot().error).not.toBe("");

    getState.mockResolvedValue(serverState({ queue: ["c1"] }));
    await store.refresh();
    await settle();
    expect(store.getSnapshot().error).toBe("");
  });
});

describe("the schedule", () => {
  it("passes a rejection through so 'saved' never means 'will never run'", async () => {
    setSchedule.mockRejectedValue(
      new Error("a weekly schedule needs at least one weekday selected"),
    );
    await expect(
      store.saveSchedule({ ...SCHEDULE, enabled: true, mode: "weekly", weekdays: [] }),
    ).rejects.toThrow("weekday");
  });

  it("reports a null next run as exactly that", async () => {
    // NULL MEANS NEVER -- off, or a one-time schedule already past. The
    // panel renders it as "not scheduled"; what it must never become is a
    // countdown to nothing.
    getState.mockResolvedValue(serverState({ next_run_at: null, upcoming: [] }));
    await store.refresh();
    await settle();
    expect(store.getSnapshot().nextRunAt).toBeNull();
  });
});
