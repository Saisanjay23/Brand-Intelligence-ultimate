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
// It also remembers that entry's JOB id, and the next run RE-ATTACHES to it
// when it is still in flight rather than starting a second sweep of the
// same client. Without that, the one guarantee this file exists to provide
// -- one sweep at a time, so no two sweeps fight over the same platform
// session -- was broken by nothing more than pressing F5.
//
// There is no /scheduler backend on the rebuilt API (that route group was
// deleted along with /clients and /jobs), which is why this is sequenced
// client-side out of the one route that does exist: POST /discovery/jobs
// plus its poll. One job in flight at a time, by design -- "one by one" is
// the actual requirement, and it also means one client's sweep never
// competes with another's for the same platform session pool.

import { discoveryApi, type DiscoveryJobState } from "../api/discoveryApi";
import type { Client } from "../api/types";
import { clientsApi } from "../api/clientsApi";
import { findClient } from "./clientDirectory";

const KEY = "bi_schedule_queue";
const POLL_MS = 2500;
// A poll failing once is a blip (dev server restart, a dropped request);
// failing this many times in a row means the job is genuinely unreachable
// and the entry is failed rather than polled forever.
const MAX_POLL_ERRORS = 5;
// Breathing room between clients so a long queue doesn't slam straight from
// one platform sweep into the next.
//
// 1.5s was breathing room in name only. Every client in the queue is swept
// through the SAME pooled accounts, so a four-client queue was four sweeps
// of the same Facebook login inside a couple of minutes -- and this pool has
// since had a Facebook account disabled. A minute between clients is
// invisible next to a sweep that already takes minutes (a 15-keyword client
// is several), and it breaks up the one pattern that reads as automation:
// sustained, evenly-spaced, back-to-back searching on one account.
//
// The per-KEYWORD gap is the other half of this and lives server-side, where
// the sweeping actually happens -- see _PLATFORM_INTER_KEYWORD_DELAY in
// backend/discovery/runner.py.
const GAP_MS = 60_000;

export type EntryStatus = "pending" | "running" | "done" | "failed" | "skipped" | "cancelled";

// Exactly the discovery job's own per-platform sweep states
// (DiscoveryJobState.platforms[].status), carried through unchanged so the
// coverage view reports what the sweep itself reported rather than a
// re-derivation of it.
export type PlatformOutcome =
  | "pending" | "running" | "done" | "partial" | "failed" | "skipped";

// Per-platform detail: how many profiles were scraped, how many were new,
// and any note the sweep reported (e.g. the reason a platform failed).
// Keyed by platform id, same as `platforms`.
export interface PlatformDetail {
  found: number;
  new: number;
  note: string;
}

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
  // Per-platform outcome of the most recent sweep, keyed by platform id.
  // Empty until this entry has run once. The aggregate status cannot
  // express the case that matters most -- Instagram and X finished but
  // Facebook lost its session halfway -- which is neither a success nor a
  // failure; this is what says which. Read by Live Activity's Client
  // Coverage tab.
  platforms: Record<string, PlatformOutcome>;
  // Per-platform scraped counts and error notes. Populated from the job's
  // PlatformSweepState each poll tick so the Scheduler UI can show exactly
  // how many profiles each platform found and what went wrong if it failed.
  platform_details: Record<string, PlatformDetail>;
  // Set by `rehydrate` when a page reload interrupted this entry mid-sweep,
  // so `job_id` points at a job that is very probably STILL RUNNING on the
  // server. Cleared as soon as the next run has decided what to do with it.
  resume: boolean;
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
        // Default for a queue persisted by a build older than this field:
        // an entry stored before it existed still has to load.
        platforms: e.platforms ?? {},
        platform_details: e.platform_details ?? {},
        // The loop that owned this died with the previous page. Saying
        // "running" here would be a lie that never resolves.
        status: e.status === "running" ? "pending" : e.status,
        // ...but the SWEEP it started did not die with the page, and
        // `job_id` is kept precisely so the next run can re-attach to it
        // instead of starting a duplicate alongside it.
        resume: e.status === "running" && Boolean(e.job_id),
        message:
          e.status === "running"
            ? e.job_id
              ? "interrupted by a page reload -- will pick that sweep back up"
              : "interrupted by a page reload -- re-queued"
            : e.message,
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
      platforms: {},
      platform_details: {},
      resume: false,
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
      // A reset does NOT throw away a live job pointer. An entry a reload
      // caught mid-sweep still has that sweep running on the server, and
      // forgetting it here would start a second one alongside it -- the
      // exact overlap the whole queue exists to prevent.
      job_id: e.resume ? e.job_id : "",
      resume: e.resume,
      message: e.resume ? e.message : "",
      found: 0,
      new_profiles: 0,
      platforms: {},
      platform_details: {},
      started_at: null,
      finished_at: null,
    })),
  );
}

// ----------------------------------------------------------------- runner

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// The job a Stop has already asked the server to cancel. Module-level
// because two places need to know: `stop()` cancels immediately when it has
// the id, and `runEntry` covers the window where there ISN'T one yet -- a
// Stop pressed while POST /discovery/jobs was still in flight used to be
// dropped on the floor, and the sweep it was meant to stop then ran to
// completion, minutes of it, with the button stuck on "Stopping...".
let cancelRequestedFor = "";

async function cancelNow(jobId: string): Promise<void> {
  if (!jobId || cancelRequestedFor === jobId) return;
  cancelRequestedFor = jobId;
  try {
    await discoveryApi.cancelJob(jobId);
  } catch {
    // Server unreachable, or the job finished between the click and this
    // call. Clearing the marker lets the poll loop try again on its next
    // tick rather than assuming a cancel that never landed.
    cancelRequestedFor = "";
  }
}

// Platform id -> outcome, straight off the job. Reported as-is: `partial`
// and `failed` are the sweep's own words for what happened to that
// platform, and flattening them into the entry's single status is exactly
// what loses the detail the coverage view exists to show.
function platformOutcomes(job: DiscoveryJobState): Record<string, PlatformOutcome> {
  const out: Record<string, PlatformOutcome> = {};
  for (const p of job.platforms || []) out[p.platform] = p.status;
  return out;
}

// Per-platform scraped counts and notes, populated from the job's own
// PlatformSweepState array each poll tick.
function platformDetails(job: DiscoveryJobState): Record<string, PlatformDetail> {
  const out: Record<string, PlatformDetail> = {};
  for (const p of job.platforms || []) {
    out[p.platform] = { found: p.found ?? 0, new: p.new ?? 0, note: p.note ?? "" };
  }
  return out;
}

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

// Is this job still in flight? Asked only to decide whether an entry a page
// reload caught mid-sweep should re-attach or start over. Any error --
// including the 404 of a job that has aged out of the backend's in-memory
// store -- answers "no": starting a fresh sweep is always safe, and
// polling a job that is not there is not.
async function stillRunning(jobId: string): Promise<boolean> {
  try {
    const job = await discoveryApi.getJob(jobId);
    return job.status === "queued" || job.status === "running";
  } catch {
    return false;
  }
}

// The client's current record (keywords AND caps), read from the database
// at the moment this client's turn actually comes up, so an edit made after
// queueing -- including a changed scrape cap -- is respected. A queue can
// sit for an hour before reaching an entry; the cached copy from when the
// panel last rendered is not good enough for deciding what to sweep.
//
// undefined when the client has since been deleted, or when the server
// cannot be reached: callers fall back to the entry's own snapshot for
// keywords and run uncapped (no caps to fall back to -- see ScheduleEntry's
// own comment). The in-memory directory is the second try rather than the
// first, so a brief network blip degrades to a slightly stale config
// instead of to an uncapped sweep.
async function liveClientFor(entry: ScheduleEntry): Promise<Client | undefined> {
  try {
    return await clientsApi.getClient(entry.client_id);
  } catch {
    return findClient(entry.client_id);
  }
}

// The scope this entry will run under, from the cached directory. Only used
// for the up-front "would this sweep nothing?" check -- the run itself reads
// the client fresh, see runEntry.
function liveScopeFor(entry: ScheduleEntry): string {
  return findClient(entry.client_id)?.scheduler_keyword_scope || "";
}

async function runEntry(entry: ScheduleEntry): Promise<void> {
  const client = await liveClientFor(entry);
  const individual = client ? dedupe(client.name_keywords || []) : entry.individual_keywords;
  const domain = client ? dedupe(client.domain_keywords || []) : entry.domain_keywords;
  if (!individual.length && !domain.length) {
    patchEntry(entry.client_id, {
      status: "skipped",
      message: "no keywords configured -- add some on the Clients page",
      resume: false,
      finished_at: Date.now(),
    });
    return;
  }

  // A keyword scope that excludes everything this client actually has would
  // POST an empty sweep, which settles as `done, 0 found` -- indistinguishable
  // in the queue from a real sweep that genuinely found nothing. It is a
  // configuration mistake, and it says so.
  const chosenScope = liveScopeFor(entry);
  if (chosenScope === "individual" && !individual.length) {
    patchEntry(entry.client_id, {
      status: "skipped",
      message: "set to individual keywords only, but this client has none",
      resume: false, finished_at: Date.now(),
    });
    return;
  }
  if (chosenScope === "domain" && !domain.length) {
    patchEntry(entry.client_id, {
      status: "skipped",
      message: "set to domain keywords only, but this client has none",
      resume: false, finished_at: Date.now(),
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

  // A reload left a sweep of THIS client running on the server. Re-attach
  // to it instead of starting a second one: two sweeps of one client at
  // once means both competing for the same platform sessions, which is
  // precisely what running the queue serially is meant to prevent.
  let jobId = "";
  if (entry.resume && entry.job_id && (await stillRunning(entry.job_id))) {
    jobId = entry.job_id;
    patchEntry(entry.client_id, {
      resume: false,
      message: "re-attached to the sweep this client already had running…",
    });
  } else {
    patchEntry(entry.client_id, { resume: false, job_id: "" });
  }

  if (!jobId) {
    try {
      // WHAT THIS CLIENT ASKED FOR, read from the client record at the
      // moment its turn comes up -- not from the queue entry -- so a change
      // made in the Scheduler after queueing still takes effect, and the
      // choice survives the queue being cleared or the browser closed.
      //
      // An empty `scheduler_platforms` means every ready platform, which is
      // exactly what omitting `platforms` means to the API: one spelling of
      // "all", so a never-configured client and a deliberately-all one
      // behave identically.
      const wanted = client?.scheduler_platforms || [];
      const scope = client?.scheduler_keyword_scope || "";
      // Narrowed by sending an EMPTY list for the excluded type rather than
      // filtering afterwards, so the per-type caps below only ever apply to
      // keywords genuinely part of this sweep -- the same thing the Clients
      // page's own runner does.
      const res = await discoveryApi.startDiscovery({
        group_id: entry.client_id,
        individual_keywords: scope === "domain" ? [] : individual,
        domain_keywords: scope === "individual" ? [] : domain,
        platforms: wanted.length ? wanted : undefined,
        platform_limits_individual: client?.platform_limits_individual,
        platform_limits_domain: client?.platform_limits_domain,
        platform_tab_limits: client?.platform_tab_limits,
      });
      jobId = res.job_id;
      patchEntry(entry.client_id, { job_id: jobId, message: "sweeping…" });

      // Accepted (202) with nothing to sweep: every platform was skipped
      // for want of a usable session. The job settles as `done` a moment
      // later, so polling it reports a clean sweep that found nothing --
      // indistinguishable, in this list, from a real sweep that genuinely
      // found nothing. It is a skip, and it now says so, with the reason.
      if (!res.platforms_queued.length) {
        patchEntry(entry.client_id, {
          status: "skipped",
          message: res.skipped.length
            ? `nothing swept -- ${res.skipped.map((s) => `${s.value}: ${s.reason}`).join(", ")}`
            : "nothing swept -- no platform has a usable session",
          platforms: Object.fromEntries(
            res.skipped.map((s) => [s.value, "skipped" as PlatformOutcome]),
          ),
          finished_at: Date.now(),
        });
        return;
      }
    } catch (e) {
      patchEntry(entry.client_id, {
        status: "failed",
        message: `could not start: ${(e as Error).message}`,
        finished_at: Date.now(),
      });
      return;
    }
  }

  // Stop can be pressed while the POST above is still in flight, when
  // `stop()` has no job id to cancel. This is where that cancel finally
  // reaches the server.
  if (state.stopping) await cancelNow(jobId);

  let pollErrors = 0;
  for (;;) {
    await sleep(POLL_MS);
    // Covers a cancel request that never landed (the server was briefly
    // unreachable): cancelNow clears its own marker on failure, so this
    // retries until one gets through.
    if (state.stopping) await cancelNow(jobId);
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
          platforms: platformOutcomes(job),
          platform_details: platformDetails(job),
          finished_at: Date.now(),
        });
        return;
      }
      patchEntry(entry.client_id, {
        message: job.message || `${job.completed}/${job.total} sweeps`,
        found: job.found,
        new_profiles: job.new,
        platforms: platformOutcomes(job),
        platform_details: platformDetails(job),
      });
    } catch (e) {
      pollErrors += 1;
      if (pollErrors >= MAX_POLL_ERRORS) {
        // Give up WATCHING it, but do not leave it RUNNING. An untracked
        // sweep is still holding platform sessions when the next client's
        // turn begins -- the overlap this queue exists to prevent, and
        // invisible with it, because nothing is left reporting it. Best
        // effort: if the server is unreachable this fails too, and there is
        // nothing further this tab can do about it.
        await cancelNow(jobId);
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

  // Auto-reset every entry to a clean slate before each new run so old
  // results, counts and failure notes never bleed into a fresh sweep.
  // Entries with a `resume` pointer are preserved (a reload-interrupted
  // sweep that is still running server-side).
  setEntries((entries) =>
    entries.map((e) => ({
      ...e,
      status: "pending" as EntryStatus,
      job_id: e.resume ? e.job_id : "",
      resume: e.resume,
      message: e.resume ? e.message : "",
      found: 0,
      new_profiles: 0,
      platforms: {},
      platform_details: {},
      started_at: null,
      finished_at: null,
    })),
  );

  if (!state.entries.some((e) => e.status === "pending")) return;

  setState({ running: true, stopping: false });
  cancelRequestedFor = "";
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
  // No job id yet means the POST that creates it is still in flight;
  // `runEntry` sends the cancel the moment that id lands. Either way the
  // poll loop is what resolves the entry.
  if (current?.job_id) await cancelNow(current.job_id);
}
