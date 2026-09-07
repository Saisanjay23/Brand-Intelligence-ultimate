/**
 * The scheduler runs one sweep at a time. These guard the four ways it
 * used to stop being true.
 *
 * THE DEFECTS THESE GUARD
 *   1. A PAGE RELOAD FORKED THE SWEEP. The run loop is plain JS in the tab,
 *      so F5 killed it -- but not the discovery job it had already handed to
 *      the backend, which kept sweeping. The queue was rehydrated with that
 *      entry back at `pending` and its job id still attached, and the next
 *      Run started a SECOND sweep of the same client alongside the first.
 *      Two sweeps of one client is precisely the platform-session contention
 *      that running the queue serially exists to prevent.
 *
 *   2. STOP WAS DROPPED IF IT WAS FAST. `stop()` cancels the job it can see
 *      on the current entry. Between "Run" and the POST returning there is
 *      no job id there yet, so a Stop pressed in that window cancelled
 *      nothing at all, and the sweep it was meant to stop ran to completion
 *      -- minutes -- with the button stuck on "Stopping...".
 *
 *   3. GIVING UP ON A JOB ORPHANED IT. After five consecutive failed polls
 *      the entry was failed and the loop moved on, leaving the job RUNNING
 *      server-side. It was then holding platform sessions all through the
 *      next client's turn, invisibly, because nothing was left watching it.
 *
 *   4. "NO SESSION" WAS REPORTED AS A CLEAN SWEEP. A request naming only
 *      platforms with no usable session is still accepted (202); the job
 *      settles as `done` having swept nothing. Polling it reported `done, 0
 *      found` -- indistinguishable from a real sweep that found nothing.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ScheduleEntry } from "./scheduleRunner";

const startDiscovery = vi.fn();
const getJob = vi.fn();
const cancelJob = vi.fn();

vi.mock("../api/discoveryApi", () => ({
  discoveryApi: {
    startDiscovery: (...args: unknown[]) => startDiscovery(...args),
    getJob: (...args: unknown[]) => getJob(...args),
    cancelJob: (...args: unknown[]) => cancelJob(...args),
  },
}));

// `runEntry` re-reads the client from the database when its turn comes up.
// These cases are all the "client was deleted after being queued" path, so
// this answers not-found -- explicitly, rather than by letting an unmocked
// fetch fail and relying on the catch.
vi.mock("../api/clientsApi", () => ({
  clientsApi: {
    getClient: () => Promise.reject(new Error("client 'acme' not found")),
  },
}));

const QUEUE_KEY = "bi_schedule_queue";
const POLL_MS = 2500;

const BASE: ScheduleEntry = {
  client_id: "acme",
  name: "Acme",
  keywords: ["acme"],
  individual_keywords: [],
  // Kept on the entry so `liveClientFor` has something to fall back on --
  // no client is saved in these tests, which is the "deleted after being
  // queued" path and the one that needs no localStorage fixture.
  domain_keywords: ["acme"],
  status: "pending",
  job_id: "",
  message: "",
  found: 0,
  new_profiles: 0,
  platforms: {},
  platform_details: {},
  resume: false,
  started_at: null,
  finished_at: null,
};

function seed(...entries: Partial<ScheduleEntry>[]): void {
  localStorage.setItem(
    QUEUE_KEY,
    JSON.stringify(entries.map((e) => ({ ...BASE, ...e }))),
  );
}

// Fresh module per test: the store is a module-level singleton and reads
// localStorage once, at import, so the queue fixture has to be in place
// before the import rather than after it.
async function loadRunner() {
  vi.resetModules();
  return import("./scheduleRunner");
}

const accepted = (over: Record<string, unknown> = {}) => ({
  job_id: "job-new",
  status: "queued",
  poll_url: "/discovery/jobs/job-new",
  platforms_queued: ["facebook"],
  skipped: [],
  ...over,
});

const jobState = (over: Record<string, unknown> = {}) => ({
  job_id: "job-1",
  group_id: "acme",
  status: "running",
  keywords: ["acme"],
  message: "",
  total: 4,
  completed: 1,
  found: 0,
  new: 0,
  started_at: null,
  finished_at: null,
  platforms: [],
  ...over,
});

const DONE = jobState({
  status: "done",
  message: "3 profile(s) found, 2 new",
  found: 3,
  new: 2,
  platforms: [{ platform: "facebook", status: "done" }],
});

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  startDiscovery.mockReset();
  getJob.mockReset();
  cancelJob.mockReset();
  cancelJob.mockResolvedValue({ cancelled: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("a page reload does not fork the sweep", () => {
  it("re-attaches to the job it left running instead of starting a second one", async () => {
    seed({ status: "running", job_id: "job-1" });
    const runner = await loadRunner();

    // Rehydrated as pending-but-resumable, and honest about it in the UI.
    expect(runner.getSnapshot().entries[0].resume).toBe(true);
    expect(runner.getSnapshot().entries[0].status).toBe("pending");

    getJob
      .mockResolvedValueOnce(jobState({ status: "running" })) // still alive
      .mockResolvedValue(DONE);

    const run = runner.start();
    await vi.advanceTimersByTimeAsync(POLL_MS * 2);
    await run;

    expect(startDiscovery).not.toHaveBeenCalled();
    const entry = runner.getSnapshot().entries[0];
    expect(entry.job_id).toBe("job-1");
    expect(entry.status).toBe("done");
    expect(entry.found).toBe(3);
  });

  it("starts a fresh sweep when that job is no longer there", async () => {
    seed({ status: "running", job_id: "job-gone" });
    const runner = await loadRunner();

    // Aged out of the backend's in-memory store: GET 404s.
    getJob
      .mockRejectedValueOnce(new Error("no discovery job 'job-gone'"))
      .mockResolvedValue(DONE);
    startDiscovery.mockResolvedValue(accepted());

    const run = runner.start();
    await vi.advanceTimersByTimeAsync(POLL_MS * 2);
    await run;

    expect(startDiscovery).toHaveBeenCalledTimes(1);
    expect(runner.getSnapshot().entries[0].job_id).toBe("job-new");
  });

  it("keeps that pointer through a Reset", async () => {
    seed({ status: "running", job_id: "job-1" });
    const runner = await loadRunner();

    runner.resetStatuses();

    // Reset means "queue it again", not "forget the sweep still running" --
    // dropping the id here is how the duplicate got started.
    const entry = runner.getSnapshot().entries[0];
    expect(entry.job_id).toBe("job-1");
    expect(entry.resume).toBe(true);
  });
});

describe("Stop reaches the server however early it is pressed", () => {
  it("cancels a job whose id did not exist yet when Stop was pressed", async () => {
    seed({});
    const runner = await loadRunner();

    let release: (value: unknown) => void = () => {};
    startDiscovery.mockReturnValue(new Promise((r) => { release = r; }));
    getJob.mockResolvedValue(jobState({ status: "cancelled", message: "cancelled" }));

    const run = runner.start();
    await Promise.resolve();

    // Mid-POST: the entry is running, but there is no job id on it.
    expect(runner.getSnapshot().entries[0].job_id).toBe("");
    await runner.stop();
    expect(cancelJob).not.toHaveBeenCalled();

    release(accepted({ job_id: "job-9" }));
    await vi.advanceTimersByTimeAsync(POLL_MS * 2);
    await run;

    expect(cancelJob).toHaveBeenCalledWith("job-9");
    // Our own cancel, so it goes back in the queue rather than to a
    // terminal state -- resuming should pick this client up again.
    expect(runner.getSnapshot().entries[0].status).toBe("pending");
  });
});

describe("a job it stops watching is a job it stops", () => {
  it("cancels the sweep before failing the entry on lost polls", async () => {
    seed({});
    const runner = await loadRunner();

    startDiscovery.mockResolvedValue(accepted({ job_id: "job-2" }));
    getJob.mockRejectedValue(new Error("Failed to fetch"));

    const run = runner.start();
    await vi.advanceTimersByTimeAsync(POLL_MS * 7);
    await run;

    expect(cancelJob).toHaveBeenCalledWith("job-2");
    expect(runner.getSnapshot().entries[0].status).toBe("failed");
  });
});

describe("a sweep that swept nothing says so", () => {
  it("reports no usable session as skipped, with the reason, not as done", async () => {
    seed({});
    const runner = await loadRunner();

    startDiscovery.mockResolvedValue(
      accepted({
        job_id: "job-3",
        platforms_queued: [],
        skipped: [{ value: "facebook", reason: "session expired" }],
      }),
    );

    const run = runner.start();
    await vi.advanceTimersByTimeAsync(POLL_MS);
    await run;

    const entry = runner.getSnapshot().entries[0];
    expect(entry.status).toBe("skipped");
    expect(entry.message).toContain("session expired");
    expect(entry.platforms).toEqual({ facebook: "skipped" });
    // Nothing to poll: the job is already finished, and polling it is what
    // used to turn this into a "done, 0 found".
    expect(getJob).not.toHaveBeenCalled();
  });
});

describe("per-platform coverage is recorded, not flattened", () => {
  it("keeps each platform's own outcome and reports what still owes work", async () => {
    seed({});
    const runner = await loadRunner();

    startDiscovery.mockResolvedValue(accepted({ job_id: "job-4" }));
    getJob.mockResolvedValue(
      jobState({
        status: "done",
        message: "9 profile(s) found, 4 new",
        found: 9,
        new: 4,
        platforms: [
          { platform: "facebook", status: "partial" },
          { platform: "instagram", status: "done" },
          { platform: "twitter", status: "skipped" },
        ],
      }),
    );

    const run = runner.start();
    await vi.advanceTimersByTimeAsync(POLL_MS * 2);
    await run;

    const entry = runner.getSnapshot().entries[0];
    expect(entry.status).toBe("done");
    expect(entry.platforms).toEqual({
      facebook: "partial",
      instagram: "done",
      twitter: "skipped",
    });
    // "done" is the only outcome that is finished. A platform skipped for
    // want of a session has not been swept, whatever the aggregate says.
    expect(runner.unfinishedPlatformsOf(entry)).toEqual(["facebook", "twitter"]);
  });
});
