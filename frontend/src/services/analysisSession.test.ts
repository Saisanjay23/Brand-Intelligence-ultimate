/**
 * Analysis results have to outlive the Analysis view.
 *
 * THE DEFECT THESE GUARD: AnalysisView is mounted inside LiveResultsView,
 * which App.tsx unmounts the moment `page` changes. Every piece of the
 * workspace -- the job id, the results table, the analyst's inline edits --
 * was component `useState`, so switching to the Scheduler tab destroyed all
 * of it. Coming back showed an empty paste box, with nothing having failed.
 * The poll interval was cleared in an unmount cleanup on top of that, so
 * leaving mid-run also stopped collecting results from a job that was still
 * running server-side.
 *
 * The store is in-memory on purpose: results must survive a TAB SWITCH, and
 * are still expected to be gone after a page REFRESH.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const start = vi.fn();
const getJob = vi.fn();
const cancelJob = vi.fn();

vi.mock("../api/analysisApi", () => ({
  analysisApi: {
    start: (...args: unknown[]) => start(...args),
    getJob: (...args: unknown[]) => getJob(...args),
    cancelJob: (...args: unknown[]) => cancelJob(...args),
  },
}));

vi.mock("react-hot-toast", () => {
  const toast = Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() });
  return { default: toast, toast };
});

const POLL_MS = 1500;

function job(over: Record<string, unknown> = {}) {
  return {
    job_id: "job-1",
    status: "running",
    total: 2,
    completed: 1,
    items: [{ id: "item-a", url: "u", platform: "instagram", status: "done" }],
    platform_progress: {},
    ...over,
  };
}

// A fresh module registry per test, so each one gets its own singleton --
// otherwise the store under test is literally shared between cases.
async function loadStore() {
  vi.resetModules();
  return await import("./analysisSession");
}

beforeEach(() => {
  vi.useFakeTimers();
  start.mockReset();
  getJob.mockReset();
  cancelJob.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("results survive leaving the tab", () => {
  it("keeps the job, its rows and the analyst's edits after the view unmounts", async () => {
    const store = await loadStore();
    start.mockResolvedValue({ job_id: "job-1", skipped: [] });
    getJob.mockResolvedValue(job({ status: "done" }));

    await store.startAnalysis(["https://instagram.com/x"]);
    // The analyst types into a cell (what the view's setEdits does).
    store.setSessionField("edits", { "item-a": { AssetName: "typed by hand" } });

    // Unmount. Every subscriber goes away with the component -- which for a
    // module singleton changes nothing, and that is the entire fix.
    const unsubscribe = store.subscribe(() => {});
    unsubscribe();

    expect(store.getSnapshot().jobId).toBe("job-1");
    expect(store.getSnapshot().jobData?.items).toHaveLength(1);
    expect(store.getSnapshot().jobData?.status).toBe("done");
    expect(store.getSnapshot().edits["item-a"].AssetName).toBe("typed by hand");
  });

  it("does not blank a finished job when the view remounts and re-watches it", async () => {
    const store = await loadStore();
    getJob.mockResolvedValue(job({ status: "done" }));

    // First mount: picks up a job started from Live Results.
    store.watchJob("job-1");
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getSnapshot().jobData?.status).toBe("done");

    const callsAfterFirstWatch = getJob.mock.calls.length;

    // Remount. `resumeJobId` is still set on App's state, so the effect
    // fires again with the SAME id. This must be a no-op, not a reset --
    // the job is finished, so its poll is already stopped.
    store.watchJob("job-1");
    await vi.advanceTimersByTimeAsync(0);

    expect(store.getSnapshot().jobData).not.toBeNull();
    expect(store.getSnapshot().jobData?.status).toBe("done");
    expect(getJob.mock.calls.length).toBe(callsAfterFirstWatch);
  });

  it("starts over when a genuinely different job is handed to it", async () => {
    const store = await loadStore();
    getJob.mockResolvedValue(job({ status: "done" }));

    store.watchJob("job-1");
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getSnapshot().jobId).toBe("job-1");

    getJob.mockResolvedValue(job({ job_id: "job-2", status: "done" }));
    store.watchJob("job-2");
    await vi.advanceTimersByTimeAsync(0);

    expect(store.getSnapshot().jobId).toBe("job-2");
    expect(store.getSnapshot().jobData?.job_id).toBe("job-2");
  });
});

describe("the poll outlives the view", () => {
  it("keeps collecting results while the analyst is on another tab", async () => {
    const store = await loadStore();
    start.mockResolvedValue({ job_id: "job-1", skipped: [] });
    getJob.mockResolvedValue(job({ status: "running", completed: 1 }));

    await store.startAnalysis(["https://instagram.com/x"]);
    expect(store.getSnapshot().jobData?.completed).toBe(1);

    // The view unmounts here. Nothing cancels the interval -- it belongs to
    // the store, not to a useEffect cleanup.
    getJob.mockResolvedValue(job({ status: "done", completed: 2 }));
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(store.getSnapshot().jobData?.completed).toBe(2);
    expect(store.getSnapshot().loading).toBe(false);
  });

  it("stops polling once the job reaches a terminal status", async () => {
    const store = await loadStore();
    start.mockResolvedValue({ job_id: "job-1", skipped: [] });
    getJob.mockResolvedValue(job({ status: "done" }));

    await store.startAnalysis(["https://instagram.com/x"]);
    const settled = getJob.mock.calls.length;

    await vi.advanceTimersByTimeAsync(POLL_MS * 4);
    expect(getJob.mock.calls.length).toBe(settled);
  });
});

describe("a stale poll cannot overwrite the current job", () => {
  it("drops a response that lands after the analyst cleared the workspace", async () => {
    const store = await loadStore();
    start.mockResolvedValue({ job_id: "job-1", skipped: [] });

    let release: (v: unknown) => void = () => {};
    start.mockReturnValue(new Promise((r) => { release = r; }));
    getJob.mockResolvedValue(job({ status: "done" }));

    const started = store.startAnalysis(["https://instagram.com/x"]);
    // Clear lands while POST /analysis/jobs is still in flight -- so there
    // is no job id to forget yet, and the response is about to hand one to
    // a workspace the analyst has just emptied.
    store.clearSession();
    release({ job_id: "job-1", skipped: [] });
    await started;
    await vi.advanceTimersByTimeAsync(POLL_MS * 2);

    expect(store.getSnapshot().jobId).toBeNull();
    expect(store.getSnapshot().jobData).toBeNull();
    expect(store.getSnapshot().loading).toBe(false);
    // ...and nothing is left polling for it either.
    expect(getJob).not.toHaveBeenCalled();
  });
});

describe("clearing the workspace clears the work attached to it", () => {
  it("drops the edits keyed to the results it just threw away", async () => {
    const store = await loadStore();
    start.mockResolvedValue({ job_id: "job-1", skipped: [] });
    getJob.mockResolvedValue(job({ status: "done" }));

    await store.startAnalysis(["https://instagram.com/x"]);
    expect(store.getSnapshot().jobData).not.toBeNull();

    store.clearSession();

    expect(store.getSnapshot().jobData).toBeNull();
    expect(store.getSnapshot().edits).toEqual({});
    expect(store.getSnapshot().urlInput).toBe("");
  });
});
