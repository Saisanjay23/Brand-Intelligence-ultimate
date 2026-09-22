// The Scheduler's view of the run queue. A VIEW -- the queue and the run
// loop itself now live on the server (backend/services/scheduler_service.py).
//
// WHAT CHANGED AND WHY. This file used to BE the engine: a JS loop in this
// tab that POSTed one discovery job per client and polled it to completion.
// That worked for a run an analyst sat and watched, and it could not work
// for a run scheduled at two in the morning. Closing the tab ended it.
// Closing the browser ended it. Locking the laptop ended it. And it ended
// SILENTLY -- the queue came back looking exactly like one that had never
// been told to run, which is indistinguishable from a sweep that found
// nothing.
//
// So the loop moved to the backend, which is up whether or not anyone is
// looking, and this module became the client for it. The exported surface
// is deliberately unchanged (`subscribe`/`getSnapshot`/`enqueue`/`dequeue`/
// `reorder`/`start`/`stop`), so the panel is still a view over a store --
// it just polls that store from the server instead of owning it.
//
// ONE OWNER. There is no run loop in this file any more, at all. Two loops
// -- one here, one on the server -- could run at once, and two concurrent
// sweeps means two clients competing for the same platform sessions, which
// is the one thing the queue exists to prevent. The manual Run button and
// the 02:00 fire are now literally the same server code path.
//
// A FAILED POLL IS REPORTED, NEVER SMOOTHED OVER. When the backend cannot
// be reached this store says so (`error`) rather than continuing to render
// the last good queue as though it were current. A stale-but-happy queue
// is the worst of the available lies: it is the one an analyst believes.

import type { Client } from "../api/types";
import {
  browserTimezone,
  schedulerApi,
  type PlatformDetail,
  type PlatformOutcome,
  type Schedule,
  type SchedulerRun,
  type WallClockShift,
} from "../api/schedulerApi";

export type { PlatformDetail, PlatformOutcome, Schedule, SchedulerRun, WallClockShift };

// How often to ask the server what is happening. Fast while a sweep is in
// flight (an analyst is watching a progress line move), lazy when idle.
const POLL_RUNNING_MS = 2_000;
const POLL_IDLE_MS = 10_000;

export type EntryStatus =
  | "pending" | "running" | "done" | "failed" | "skipped"
  | "cancelled" | "stopped" | "interrupted";

export interface ScheduleEntry {
  client_id: string;
  name: string;
  // From the client directory, for the "N keywords" line on a queued row.
  // Display only: what actually gets swept is read server-side from the
  // client record at the moment its turn comes up, so an edit made after
  // queueing still takes effect on an overnight run.
  keywords: string[];
  status: EntryStatus;
  job_id: string;
  message: string;
  found: number;
  new_profiles: number;
  // Per-platform outcome of the most recent sweep, keyed by platform id.
  // Empty until this entry has run once. The aggregate status cannot
  // express the case that matters most -- Instagram and X finished but
  // Facebook lost its session halfway -- which is neither a success nor a
  // failure; this is what says which.
  platforms: Record<string, PlatformOutcome>;
  platform_details: Record<string, PlatformDetail>;
  // Searches this client still owes on the durable coverage ledger.
  //
  // -1 MEANS NOT KNOWN, deliberately not 0: zero is the clean bill of
  // health, and a coverage read that failed must never be able to
  // impersonate one.
  owed: number;
  closingGaps: boolean;
  started_at: number | null;
  finished_at: number | null;
}

export interface ScheduleState {
  entries: ScheduleEntry[];
  running: boolean;
  // client_id of the entry being swept right now, "" when idle.
  currentId: string;
  stopping: boolean;
  // False until the first successful read. The panel shows "loading"
  // rather than an empty queue, because an empty queue is a claim.
  loaded: boolean;
  // Why the server could not be reached, "" when it could. Rendered.
  error: string;
  schedule: Schedule;
  // When the schedule next fires, UTC ISO. NULL MEANS NEVER -- off, or a
  // one-time schedule whose moment has passed. Render that as "not
  // scheduled", never as "not yet".
  nextRunAt: string | null;
  upcoming: string[];
  lastFiredAt: string | null;
  wallClockShift: WallClockShift | null;
  catchUpGraceMinutes: number;
  // The run in flight, or the most recent finished one when idle.
  lastRun: SchedulerRun | null;
}

const DEFAULT_SCHEDULE: Schedule = {
  enabled: false,
  mode: "daily",
  at: "02:00",
  on_date: "",
  weekdays: [],
  tz: browserTimezone(),
};

function emptyState(): ScheduleState {
  return {
    entries: [], running: false, currentId: "", stopping: false,
    loaded: false, error: "",
    schedule: DEFAULT_SCHEDULE,
    nextRunAt: null, upcoming: [], lastFiredAt: null,
    wallClockShift: null, catchUpGraceMinutes: 0, lastRun: null,
  };
}

// ------------------------------------------------------------------ store

let state: ScheduleState = emptyState();
const listeners = new Set<() => void>();

// The client directory, mirrored in so a row can show its keyword count.
// Set by the panel via `setDirectory`.
let directory: Client[] = [];

// A run whose per-client results the analyst has dismissed with Reset.
// Kept because the server keeps that run as history -- Reset is a display
// choice here, not a rewriting of what happened.
let hiddenRunId = "";

// The queue as this tab last set it, held while a write is in flight so
// drag-to-reorder stays responsive instead of snapping back on every poll.
let optimisticQueue: string[] | null = null;

function emit(): void {
  for (const l of listeners) l();
}

// useSyncExternalStore compares snapshots with Object.is, so a poll that
// changed nothing must not produce a new object -- it would re-render the
// panel every two seconds for ever.
let lastSerialised = "";

function commit(next: ScheduleState): void {
  const serialised = JSON.stringify(next);
  if (serialised === lastSerialised) return;
  lastSerialised = serialised;
  state = next;
  emit();
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  startPolling();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) stopPolling();
  };
}

export function getSnapshot(): ScheduleState {
  return state;
}

export function keywordsOf(client: Client): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const kw of [...(client.name_keywords || []), ...(client.domain_keywords || [])]) {
    const key = (kw || "").trim().toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(kw.trim());
  }
  return out;
}

// Lets the panel keep this store's keyword counts current without this
// module having to own a second copy of the client directory.
export function setDirectory(clients: Client[]): void {
  directory = clients;
}

// --------------------------------------------------------------- deriving

function iso(value: string | null): number | null {
  if (!value) return null;
  const t = Date.parse(value);
  return Number.isNaN(t) ? null : t;
}

function blankEntry(clientId: string, name: string): ScheduleEntry {
  return {
    client_id: clientId, name, keywords: [], status: "pending", job_id: "",
    message: "", found: 0, new_profiles: 0, platforms: {},
    platform_details: {}, owed: -1, closingGaps: false,
    started_at: null, finished_at: null,
  };
}

// The queue is the ORDER; the run carries the per-client outcomes. They
// are merged here rather than server-side because the queue can be edited
// between runs, and a row for a client added after the last run has to
// render as freshly queued rather than inheriting nothing at all.
function deriveEntries(
  queue: string[], names: Record<string, string>, run: SchedulerRun | null,
): ScheduleEntry[] {
  const results = new Map<string, ScheduleEntry>();
  if (run && run.run_id !== hiddenRunId) {
    for (const e of run.entries || []) {
      results.set(e.client_id, {
        client_id: e.client_id,
        name: e.name || e.client_id,
        keywords: [],
        status: e.status,
        job_id: e.job_id,
        message: e.message,
        found: e.found,
        new_profiles: e.new_profiles,
        platforms: e.platforms || {},
        platform_details: e.platform_details || {},
        owed: typeof e.owed === "number" ? e.owed : -1,
        closingGaps: Boolean(e.closing_gaps),
        started_at: iso(e.started_at),
        finished_at: iso(e.finished_at),
      });
    }
  }

  return queue.map((cid) => {
    const client = directory.find((c) => c.client_id === cid);
    const base = results.get(cid) ?? blankEntry(cid, names[cid] || cid);
    return {
      ...base,
      name: client?.name || base.name || cid,
      keywords: client ? keywordsOf(client) : base.keywords,
    };
  });
}

// ------------------------------------------------------------- the polling

let timer: ReturnType<typeof setTimeout> | null = null;
let inFlight = false;

function schedulePoll(delay: number): void {
  if (timer) clearTimeout(timer);
  if (listeners.size === 0) return;
  timer = setTimeout(() => void poll(), delay);
}

function startPolling(): void {
  if (timer || inFlight) return;
  void poll();
}

function stopPolling(): void {
  if (timer) clearTimeout(timer);
  timer = null;
}

export async function refresh(): Promise<void> {
  await poll();
}

async function poll(): Promise<void> {
  if (inFlight) return;
  inFlight = true;
  try {
    const s = await schedulerApi.getState();
    // A write of ours has landed (the server's queue now matches what we
    // asked for), so stop preferring the local copy.
    if (optimisticQueue && sameOrder(optimisticQueue, s.queue)) optimisticQueue = null;
    const queue = optimisticQueue ?? s.queue;

    commit({
      entries: deriveEntries(queue, s.queue_names || {}, s.run),
      running: s.running,
      currentId: s.run?.current_id || "",
      stopping: Boolean(s.run?.stopping),
      loaded: true,
      error: "",
      schedule: { ...DEFAULT_SCHEDULE, ...s.schedule },
      nextRunAt: s.next_run_at,
      upcoming: s.upcoming || [],
      lastFiredAt: s.last_fired_at,
      wallClockShift: s.wall_clock_shift,
      catchUpGraceMinutes: s.catch_up_grace_minutes,
      lastRun: s.run,
    });
  } catch (e) {
    // SAYS SO rather than leaving the last good queue on screen looking
    // current. `loaded` stays as it was: a queue read successfully ten
    // seconds ago is still worth showing, as long as the banner says it
    // may be stale.
    commit({ ...state, error: (e as Error).message || "the backend is unreachable" });
  } finally {
    inFlight = false;
    schedulePoll(state.running ? POLL_RUNNING_MS : POLL_IDLE_MS);
  }
}

function sameOrder(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

// ------------------------------------------------------------ queue edits

export function isQueued(clientId: string): boolean {
  return state.entries.some((e) => e.client_id === clientId);
}

// Applies the change locally first so dragging feels immediate, then
// writes the whole list through. A failed write reverts to whatever the
// server actually has on the next poll, and says why.
async function writeQueue(ids: string[]): Promise<void> {
  optimisticQueue = ids;
  commit({
    ...state,
    entries: deriveEntries(ids, Object.fromEntries(
      state.entries.map((e) => [e.client_id, e.name])), state.lastRun),
  });
  try {
    await schedulerApi.setQueue(ids);
    optimisticQueue = null;
  } catch (e) {
    optimisticQueue = null;
    commit({ ...state, error: (e as Error).message });
  }
  await poll();
}

export function enqueue(client: Client): void {
  if (isQueued(client.client_id)) return;
  void writeQueue([...state.entries.map((e) => e.client_id), client.client_id]);
}

// Removing the entry that is mid-sweep would leave its job running with
// nothing tracking it, so that one is refused -- Stop first.
export function dequeue(clientId: string): void {
  if (state.currentId === clientId) return;
  void writeQueue(state.entries.map((e) => e.client_id).filter((id) => id !== clientId));
}

// Drag-to-reorder: moves `draggedId` to sit where `targetId` currently is.
export function reorder(draggedId: string, targetId: string): void {
  if (draggedId === targetId) return;
  const ids = state.entries.map((e) => e.client_id);
  const from = ids.indexOf(draggedId);
  const to = ids.indexOf(targetId);
  if (from < 0 || to < 0) return;
  const next = [...ids];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  void writeQueue(next);
}

export function clearQueue(): void {
  if (state.running) return;
  void writeQueue([]);
}

// Clears the LAST RUN'S RESULTS FROM THIS VIEW so a finished queue reads
// as ready again. It does not delete anything: the run stays in the
// server's history, because what happened last night happened whether or
// not this tab is still showing it.
export function resetStatuses(): void {
  if (state.running) return;
  hiddenRunId = state.lastRun?.run_id || "";
  commit({
    ...state,
    entries: deriveEntries(
      state.entries.map((e) => e.client_id),
      Object.fromEntries(state.entries.map((e) => [e.client_id, e.name])),
      state.lastRun),
  });
}

// ---------------------------------------------------------------- running

// Starts the queue NOW, through the same server path a scheduled fire
// takes. Rejects (with the backend's own reason) when a run is already
// going -- declined rather than queued behind it, because a second full
// sweep of every client starting whenever the first ends is never what the
// press meant.
export async function start(): Promise<void> {
  hiddenRunId = "";
  try {
    await schedulerApi.runNow();
  } finally {
    await poll();
  }
}

export async function stop(): Promise<void> {
  try {
    await schedulerApi.stop();
  } finally {
    await poll();
  }
}

// --------------------------------------------------------------- schedule

// 422 when the schedule could never fire -- a weekly with no days, a
// one-time date already in the past. The error carries the backend's own
// reason and is thrown on, because "saved" and "will never run" must not
// look the same to whoever set it.
export async function saveSchedule(schedule: Schedule): Promise<void> {
  try {
    await schedulerApi.setSchedule(schedule);
  } finally {
    await poll();
  }
}

// ------------------------------------------------------------------ reads

// Platforms that did not finish on this entry's last sweep -- everything
// the job did not report as `done`. `skipped` counts: a platform skipped
// for want of a session has not been swept, and a coverage view that says
// otherwise is worse than no coverage view at all.
export function unfinishedPlatformsOf(entry: ScheduleEntry): string[] {
  return Object.entries(entry.platforms ?? {})
    .filter(([, outcome]) => outcome !== "done")
    .map(([platform]) => platform)
    .sort();
}
