// The Scheduler's engine: a queue of clients, run through discovery one at
// a time, in order.
//
// WHY THIS IS A MODULE-LEVEL SINGLETON AND NOT COMPONENT STATE: a run takes
// minutes per client, and an analyst is expected to leave the Scheduler tab
// while it works (to watch Live Results, say). React state lives and dies
// with the component, so the loop would be killed the moment they navigate
// away. This store outlives every component; the panel subscribes to it via
// useSyncExternalStore and is purely a view over it.
//
// WHAT IT DOES NOT SURVIVE: a page reload. The loop is plain JS in this tab,
// so a refresh ends it -- the discovery job already handed to the backend
// keeps running server-side, but nothing here is left watching it. The
// QUEUE itself is persisted (localStorage), so the list comes back; any
// entry caught mid-run is rehydrated as `pending` with a note saying so,
// rather than lying that it is still running. See `rehydrate()`.
//
// There is no /scheduler backend on the rebuilt API (that route group was
// deleted along with /clients and /jobs), which is why this is sequenced
// client-side out of the one route that does exist: POST /discovery/jobs
// plus its poll. One job in flight at a time, by design -- "one by one" is
// the actual requirement, and it also means one client's sweep never
// competes with another's for the same platform session pool.

import { discoveryApi } from "../api/discoveryApi";
import type { Client } from "../api/types";
import { listSavedClients } from "./savedClients";

const KEY = "bi_schedule_queue";
const POLL_MS = 2500;
// A poll failing once is a blip (dev server restart, a dropped request);
// failing this many times in a row means the job is genuinely unreachable
// and the entry is failed rather than polled forever.
const MAX_POLL_ERRORS = 5;
// Breathing room between clients so a long queue doesn't slam straight from
// one platform sweep into the next.
const GAP_MS = 1500;

export type EntryStatus = "pending" | "running" | "done" | "failed" | "skipped" | "cancelled";

export interface ScheduleEntry {
  client_id: string;
  name: string;
  // Snapshot taken when the client was queued. Only a FALLBACK: the run
  // re-reads the client's current keywords AND caps at the moment its turn
  // comes up (see `liveClientFor`), so editing a client after queueing it
  // still takes effect. The snapshot covers the case where the client was
  // deleted from the directory after being queued -- caps have no fallback
  // in that case (an ad-hoc sweep with no client behind it runs uncapped,
  // same as before caps existed at all).
  keywords: string[];
  individual_keywords: string[];
  domain_keywords: string[];
  status: EntryStatus;
  job_id: string;
  message: string;
  found: number;
  new_profiles: number;
  started_at: number | null;
  finished_at: number | null;
}

export interface ScheduleState {
  entries: ScheduleEntry[];
  running: boolean;
  // client_id of the entry being swept right now, "" when idle.
  currentId: string;
  // Set between "Stop" being pressed and the loop actually unwinding, so
  // the button can show "Stopping…" instead of looking like it did nothing.
  stopping: boolean;
}

function dedupe(keywords: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const kw of keywords) {
    const key = kw.trim().toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(kw.trim());
  }
  return out;
}

export function keywordsOf(client: Client): string[] {
  return dedupe([...(client.name_keywords || []), ...(client.domain_keywords || [])]);
}

// ------------------------------------------------------------------ store

let state: ScheduleState = { entries: [], running: false, currentId: "", stopping: false };
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

// Replaces `state` wholesale rather than mutating it: useSyncExternalStore
// compares snapshots with Object.is, so an in-place mutation would not
// re-render.
function setState(patch: Partial<ScheduleState>): void {
  state = { ...state, ...patch };
  persist();
  emit();
}

function setEntries(fn: (entries: ScheduleEntry[]) => ScheduleEntry[]): void {
  setState({ entries: fn(state.entries) });
}

function patchEntry(clientId: string, patch: Partial<ScheduleEntry>): void {
  setEntries((entries) =>
    entries.map((e) => (e.client_id === clientId ? { ...e, ...patch } : e)),
  );
}

function persist(): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(state.entries));
  } catch {
    // storage unavailable -- the queue just won't survive a reload
  }
}

function rehydrate(): void {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return;
    const entries = JSON.parse(raw) as ScheduleEntry[];
    if (!Array.isArray(entries)) return;
    state = {
      entries: entries.map((e) => ({
        ...e,
        // The loop that owned this died with the previous page. Saying
        // "running" here would be a lie that never resolves.
        status: e.status === "running" ? "pending" : e.status,
        message: e.status === "running" ? "interrupted by a page reload -- re-queued" : e.message,
      })),
      running: false,
      currentId: "",
      stopping: false,
    };
  } catch {
    // unparseable stored queue -- start empty rather than crash the panel
  }
}

rehydrate();

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getSnapshot(): ScheduleState {
  return state;
}

// ------------------------------------------------------------ queue edits

export function isQueued(clientId: string): boolean {
  return state.entries.some((e) => e.client_id === clientId);
}

export function enqueue(client: Client): void {
  if (isQueued(client.client_id)) return;
  setEntries((entries) => [
    ...entries,
    {
      client_id: client.client_id,
      name: client.name || client.client_id,
      keywords: keywordsOf(client),
      individual_keywords: dedupe(client.name_keywords || []),
      domain_keywords: dedupe(client.domain_keywords || []),
      status: "pending",
      job_id: "",
      message: "",
      found: 0,
      new_profiles: 0,
      started_at: null,
      finished_at: null,
    },
  ]);
}

// Removing the entry that is mid-sweep would leave its job running with
// nothing tracking it, so that one is refused -- Stop first.
export function dequeue(clientId: string): void {
  if (state.currentId === clientId) return;
  setEntries((entries) => entries.filter((e) => e.client_id !== clientId));
}

// Drag-to-reorder: moves `draggedId` to sit where `targetId` currently is.
export function reorder(draggedId: string, targetId: string): void {
  if (draggedId === targetId) return;
  setEntries((entries) => {
    const from = entries.findIndex((e) => e.client_id === draggedId);
    const to = entries.findIndex((e) => e.client_id === targetId);
    if (from < 0 || to < 0) return entries;
    const next = [...entries];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    return next;
  });
}

export function clearQueue(): void {
  if (state.running) return;
  setState({ entries: [] });
}

// Puts every entry back to `pending` so a finished queue can be run again
// without being rebuilt by hand.
export function resetStatuses(): void {
  if (state.running) return;
  setEntries((entries) =>
    entries.map((e) => ({
      ...e,
      status: "pending",
      job_id: "",
      message: "",
      found: 0,
      new_profiles: 0,
      started_at: null,
      finished_at: null,
    })),
  );
}

// ----------------------------------------------------------------- runner

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// The client's current record (keywords AND caps), preferring the live
// saved one over the enqueue-time snapshot so an edit made after queueing
// -- including a changed scrape cap -- is respected. undefined when the
// client has since been deleted from the directory; callers fall back to
// the entry's own snapshot for keywords and run uncapped (no caps to fall
// back to -- see ScheduleEntry's own comment).
function liveClientFor(entry: ScheduleEntry): Client | undefined {
  return listSavedClients().find((c) => c.client_id === entry.client_id);
}

async function runEntry(entry: ScheduleEntry): Promise<void> {
  const client = liveClientFor(entry);
  const individual = client ? dedupe(client.name_keywords || []) : entry.individual_keywords;
  const domain = client ? dedupe(client.domain_keywords || []) : entry.domain_keywords;
  if (!individual.length && !domain.length) {
    patchEntry(entry.client_id, {
      status: "skipped",
      message: "no keywords configured -- add some on the Clients page",
      finished_at: Date.now(),
    });
    return;
  }

  patchEntry(entry.client_id, {
    status: "running",
    message: "starting sweep…",
    started_at: Date.now(),
    finished_at: null,
  });
  setState({ currentId: entry.client_id });

  let jobId = "";
  try {
    // No `platforms`: omitting it sweeps every platform that is ready right
    // now, which is what an unattended scheduled run should do. Caps come
    // from the live client (undefined -- and so uncapped -- when it was
    // deleted after being queued; see liveClientFor).
    const res = await discoveryApi.startDiscovery({
      group_id: entry.client_id,
      individual_keywords: individual,
      domain_keywords: domain,
      platform_limits_individual: client?.platform_limits_individual,
      platform_limits_domain: client?.platform_limits_domain,
      platform_tab_limits: client?.platform_tab_limits,
    });
    jobId = res.job_id;
    patchEntry(entry.client_id, { job_id: jobId, message: "sweeping…" });
  } catch (e) {
    patchEntry(entry.client_id, {
      status: "failed",
      message: `could not start: ${(e as Error).message}`,
      finished_at: Date.now(),
    });
    return;
  }

  let pollErrors = 0;
  for (;;) {
    await sleep(POLL_MS);
    try {
      const job = await discoveryApi.getJob(jobId);
      pollErrors = 0;
      if (job.status === "done" || job.status === "failed" || job.status === "cancelled") {
        // A cancel is only re-queued as `pending` when WE asked for it
        // (Stop was pressed, so `stopping` is set) -- resuming should pick
        // that client up again.
        //
        // A cancel we did NOT ask for -- another tab, a direct API call --
        // must NOT go back to `pending`: the loop below picks the first
        // pending entry each lap, so it would immediately restart the very
        // job that was just cancelled, and keep doing so forever, burning a
        // platform session on every lap. It gets its own terminal status
        // instead, which the loop skips and Reset can clear.
        const cancelStatus: EntryStatus = state.stopping ? "pending" : "cancelled";
        patchEntry(entry.client_id, {
          status: job.status === "done" ? "done" : job.status === "cancelled" ? cancelStatus : "failed",
          message: job.message || job.status,
          found: job.found,
          new_profiles: job.new,
          finished_at: Date.now(),
        });
        return;
      }
      patchEntry(entry.client_id, {
        message: job.message || `${job.completed}/${job.total} sweeps`,
        found: job.found,
        new_profiles: job.new,
      });
    } catch (e) {
      pollErrors += 1;
      if (pollErrors >= MAX_POLL_ERRORS) {
        patchEntry(entry.client_id, {
          status: "failed",
          message: `lost track of the job: ${(e as Error).message}`,
          finished_at: Date.now(),
        });
        return;
      }
    }
  }
}

export async function start(): Promise<void> {
  // Guard against a double-click (or a second panel instance) starting two
  // loops over the same queue.
  if (state.running) return;
  if (!state.entries.some((e) => e.status === "pending")) return;

  setState({ running: true, stopping: false });
  try {
    for (;;) {
      if (state.stopping) break;
      // Re-read each lap instead of iterating a captured array: the queue
      // can be reordered or added to WHILE the run is in progress, and the
      // next pick should respect that.
      const next = state.entries.find((e) => e.status === "pending");
      if (!next) break;
      await runEntry(next);
      setState({ currentId: "" });
      if (state.stopping) break;
      if (state.entries.some((e) => e.status === "pending")) await sleep(GAP_MS);
    }
  } finally {
    setState({ running: false, currentId: "", stopping: false });
  }
}

// Stops after cancelling whatever is in flight, so a Stop is immediate
// rather than "finishes this client first".
export async function stop(): Promise<void> {
  if (!state.running) return;
  setState({ stopping: true });
  const current = state.entries.find((e) => e.client_id === state.currentId);
  if (current?.job_id) {
    try {
      await discoveryApi.cancelJob(current.job_id);
    } catch {
      // The job may have finished between the click and this call -- the
      // poll loop resolves the entry either way.
    }
  }
}
