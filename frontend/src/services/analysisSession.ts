// The Analysis workspace's state, and the poll that keeps it current.
//
// WHY THIS IS A MODULE-LEVEL SINGLETON AND NOT COMPONENT STATE: exactly the
// reasoning in scheduleRunner.ts, and for exactly the same symptom. An
// analysis takes minutes, and an analyst is expected to leave the Analysis
// tab while it works (to queue a client on the Scheduler, say). AnalysisView
// is mounted inside LiveResultsView, which App.tsx unmounts the moment
// `page` changes -- so every piece of `useState` in that component, results
// included, was destroyed by the tab switch. Come back and the table is
// empty, the job forgotten, and the inline edits gone, with nothing having
// failed: the state simply never outlived the component.
//
// The poll interval lives here too, and NOT in a useEffect cleanup. It used
// to be cleared on unmount, so switching away mid-run also stopped watching
// the job -- the analysis kept running server-side with nothing collecting
// its results. Now the poll outlives the view: leave, come back, and the
// table has filled in while you were gone.
//
// A RELOAD USED TO EMPTY THE TABLE, and no longer does. Results are now
// saved server-side for 24 hours (see backend/database/repositories/
// analysis_result_repository.py), so `saved` below is repopulated from the
// server on mount and the workspace comes back with the day's work in it.
// What is still RAM-only is everything that is not a result: the URL box,
// the filters, and `edits` -- an analyst's uncommitted inline corrections,
// which have nowhere durable to go because nothing has accepted them yet.
//
// `saved` and `jobData.items` OVERLAP, on purpose, and are merged by
// `result_id` rather than concatenated (see mergedItems). A running job's
// row is the fresher copy of a profile that may already be in `saved` from
// an earlier run, so it has to win -- appending both would show the same
// profile twice with different numbers.

import { useCallback, useSyncExternalStore } from "react";
import toast from "react-hot-toast";
import { analysisApi, type AnalysisItemData, type AnalysisJobResponse } from "../api/analysisApi";

// How long the backend is asked to hold each request open while nothing is
// happening, under its own 25s ceiling so the server ends the wait and the
// client never races it. There is no interval any more: on success the next
// request goes straight back out and the waiting happens server-side, where
// it can end the instant a URL completes. See backend/shared/live_poll.py.
const WAIT_S = 20;

// Only after a FAILED request, so a backend that is down or restarting is
// retried rather than hammered by a loop with no delay in it.
const RETRY_MS = 2000;

// A BACKEND THAT DOES NOT SUPPORT THE LONG POLL MUST NOT BE HAMMERED.
// `rev` is what makes the server hold the next request; a backend older
// than that feature returns no `rev`, every request is then answered
// instantly, and looping straight back out would turn this into an
// unthrottled request flood against exactly the deployment least able to
// absorb it. Missing `rev` therefore falls back to plain interval polling
// -- the behaviour this replaced, which is the right thing to degrade to.
const FALLBACK_POLL_MS = 1000;

// Consecutive failures tolerated before the watch gives up and says so.
//
// It used to give up on the FIRST one, which was defensible when a request
// lasted a moment; a request that is deliberately held open for twenty
// seconds is far more exposed to a blip, a dev-server restart or a proxy
// closing an idle connection, and losing the live view of a running batch
// to one of those is a worse trade than waiting a few more seconds. Three
// failures spaced by RETRY_MS is still under seven seconds before the
// analyst is told something is actually wrong.
const MAX_POLL_FAILURES = 3;

export interface AnalysisSession {
  urlInput: string;
  jobId: string | null;
  jobData: AnalysisJobResponse | null;
  loading: boolean;
  cancelling: boolean;
  exporting: boolean;
  formatMode: "incident" | "legacy";
  searchQuery: string;
  platformFilter: string;
  riskFilter: string;
  // Inline cell edits, keyed by analysis item id. Analyst-typed work, so
  // this is the state it would hurt most to lose on a tab switch.
  edits: Record<string, Record<string, string>>;
  // Results the server is holding for 24h -- the day's work, including
  // batches from before this page was loaded.
  saved: AnalysisItemData[];
  savedLoading: boolean;
  // Why the last saved-results load failed, or null. Kept in state rather
  // than only toasted because a failed load leaves `saved` EMPTY, and an
  // empty table with the toast already gone reads exactly like "this client
  // has no results" -- a silent clean zero. The view shows a persistent
  // banner with a Retry for as long as this is set.
  savedError: string | null;
  retentionHours: number;
  // Which rows the analyst has ticked, by result_id. Lives here rather than
  // in the view for the same reason everything else does: a tab switch
  // unmounts the view, and losing a 40-row selection to that is the kind of
  // thing that makes someone stop trusting the checkboxes.
  selected: string[];
  deleting: boolean;
  // WHICH CLIENT THIS WORKSPACE IS SHOWING. Every server call that reads,
  // writes or deletes a result carries it, so the table can only ever hold
  // one client's readings. "" is a scratch run with no client selected --
  // its own bucket, never a wildcard. Switching clients clears the table
  // rather than filtering it, because rows for the previous client have no
  // business being on screen for a moment longer than the request takes.
  orgId: string;
}

function emptySession(): AnalysisSession {
  return {
    urlInput: "",
    jobId: null,
    jobData: null,
    loading: false,
    cancelling: false,
    exporting: false,
    formatMode: "incident",
    searchQuery: "",
    platformFilter: "all",
    riskFilter: "all",
    edits: {},
    saved: [],
    savedLoading: false,
    savedError: null,
    retentionHours: 24,
    selected: [],
    deleting: false,
    orgId: "",
  };
}

let state: AnalysisSession = emptySession();
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getSnapshot(): AnalysisSession {
  return state;
}

// Replaces `state` wholesale but keeps every UNCHANGED field's reference
// identical, so `getSnapshot()[key]` is a stable per-field snapshot and
// useSyncExternalStore re-renders only for the fields that actually moved.
function setField<K extends keyof AnalysisSession>(
  key: K,
  value: AnalysisSession[K] | ((prev: AnalysisSession[K]) => AnalysisSession[K]),
): void {
  // No field's type is a function, so this narrowing is safe -- and it lets
  // the setters keep useState's exact signature, including the functional
  // form `setEdits(prev => ...)` the view already uses.
  const next =
    typeof value === "function"
      ? (value as (prev: AnalysisSession[K]) => AnalysisSession[K])(state[key])
      : value;
  if (Object.is(next, state[key])) return;
  state = { ...state, [key]: next };
  emit();
}

function patch(fields: Partial<AnalysisSession>): void {
  state = { ...state, ...fields };
  emit();
}

export { setField as setSessionField };

// A `useState`-shaped view onto one field of the shared session, so the
// component's call sites read exactly as they did when this was local
// state and only the declarations had to change.
export function useAnalysisField<K extends keyof AnalysisSession>(
  key: K,
): [
  AnalysisSession[K],
  (value: AnalysisSession[K] | ((prev: AnalysisSession[K]) => AnalysisSession[K])) => void,
] {
  const value = useSyncExternalStore(
    subscribe,
    () => getSnapshot()[key],
    () => emptySession()[key],
  );
  const set = useCallback(
    (v: AnalysisSession[K] | ((prev: AnalysisSession[K]) => AnalysisSession[K])) => setField(key, v),
    [key],
  );
  return [value, set];
}

// ----------------------------------------------------------------- polling

let pollTimer: number | null = null;

// The request currently being held open, so it can be cut short instead of
// left to run out its window, and whether a watch loop is running at all --
// which `watchJob` needs, because with no interval there is no longer a
// live `pollTimer` to read that from.
let pollAbort: AbortController | null = null;
let pollActive = false;

// Bumped by anything that makes an in-flight request irrelevant: Clear, or
// a newer job superseding this one. Both `startAnalysis` and `watchJob`
// await the network with the workspace still live underneath them, and
// without this a response arriving late would install itself over whatever
// the analyst did in the meantime -- pressing Clear while the start request
// was in flight left a job id and a running poll attached to a workspace
// that had just been emptied.
let generation = 0;

// The same guard, for the saved-results load specifically. A client switch
// fires its own request, and two switches in quick succession can return out
// of order -- without this, client A's slower response would repopulate a
// workspace already showing client B, which is precisely the cross-client
// leak the scoping is here to close.
let savedGeneration = 0;

// WHICH CLIENT EACH JOB WAS STARTED FOR. The job payload does not carry its
// org, so it is remembered here at the moment the job is first seen -- by
// `startAnalysis`, or by the first `watchJob` (which App fires for a job it
// just started under the client that is open). Needed because App never
// clears `resumeAnalysisJobId`: every remount of the view re-offers the same
// id, and after a client switch that would quietly re-attach client A's job
// -- its rows, and with them its exports and deletes -- under client B.
const jobOrg = new Map<string, string>();

// A job that was still RUNNING when the analyst switched away from its
// client, by org. Switching back re-attaches it so its progress is not lost;
// a job that had already settled needs no parking, its rows are in `saved`.
const parkedJob = new Map<string, string>();

// Rows deleted while the CURRENT job is on screen, by result_id. The server
// drops them from the 24h store, but the running job still holds them in
// memory and lists them on every poll -- without this filter a deleted row
// reappeared a second later, which reads as the delete having failed.
// Scoped to one job on purpose: a later job that re-analyses the same URL
// produces a new reading under the same result_id, and that must show.
let deletedInJob = new Set<string>();

function withoutDeleted(data: AnalysisJobResponse): AnalysisJobResponse {
  if (!deletedInJob.size) return data;
  return { ...data, items: data.items.filter((i) => !deletedInJob.has(i.result_id ?? "")) };
}

// The edits that survive replacing the current job. Edits are keyed by item
// id, and a settled job row is saved under that SAME id -- so once a job's
// rows are in `saved` they stay on screen after the job is replaced, and the
// analyst's corrections on them must stay too. Only ids that are about to
// leave the screen (rows of the old job with no saved copy) are dropped.
function editsSurvivingJob(job: AnalysisJobResponse | null): AnalysisSession["edits"] {
  if (!job || !job.items.length) return state.edits;
  const onScreen = new Set(state.saved.map((r) => r.id));
  const drop = job.items.map((i) => i.id).filter((id) => !onScreen.has(id) && id in state.edits);
  if (!drop.length) return state.edits;
  const next = { ...state.edits };
  for (const id of drop) delete next[id];
  return next;
}

export function stopPolling(): void {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  // A request can now be held open for twenty seconds, so tearing the watch
  // down has to CUT IT SHORT rather than just ignore what it returns --
  // otherwise every Clear or client switch leaves a connection hanging
  // around for the rest of its window.
  pollAbort?.abort();
  pollAbort = null;
  pollActive = false;
}

// Returns whether this poll SETTLED the job -- terminal status, an error,
// or a response that is no longer wanted -- along with the revision to wait
// on next. The caller uses `settled` to decide whether to keep watching: a
// job that was already finished on its first poll must not be.
//
// `rev`/`wait` turn this into a long poll. Passing neither (the first
// request, which has nothing to wait for) is the plain immediate snapshot
// it has always been.
async function pollJob(
  id: string,
  gen: number,
  opts: { rev?: string; wait?: number; quiet?: boolean } = {},
): Promise<{ settled: boolean; rev: string; failed?: boolean }> {
  const ac = new AbortController();
  pollAbort = ac;
  try {
    const data = await analysisApi.getJob(id, {
      rev: opts.rev, wait: opts.wait, signal: ac.signal,
    });
    // A poll that lands after the analyst started a different job (or hit
    // Clear) must not overwrite the current one with a stale payload.
    if (gen !== generation || state.jobId !== id) return { settled: true, rev: "" };
    patch({ jobData: withoutDeleted(data) });
    if (data.status === "done" || data.status === "cancelled" || data.status === "failed") {
      stopPolling();
      patch({ loading: false, cancelling: false });
      // The job wrote each row to the 24h store as it settled; pull the
      // authoritative set back so what is on screen is what would survive a
      // reload. Also picks up a CANCELLED job's partial work, which is real
      // and saved even though the batch did not finish.
      void loadSaved();
      if (data.status === "done") {
        toast.success(`Analysis completed for ${data.completed}/${data.total} URLs`);
      } else if (data.status === "cancelled") {
        toast.error("Analysis stopped by user");
      }
      return { settled: true, rev: data.rev || "" };
    }
    return { settled: false, rev: data.rev || "" };
  } catch (e) {
    if (gen !== generation || state.jobId !== id) return { settled: true, rev: "" };
    // An abort is this watch being torn down, not a failure: reporting it
    // would put an error toast on screen every time the analyst pressed
    // Clear or switched client.
    if ((e as Error)?.name === "AbortError") return { settled: true, rev: "" };
    // `quiet` means a caller that intends to retry -- it decides when a run
    // of failures is worth reporting, so this must not tear the watch down
    // underneath it.
    if (opts.quiet) return { settled: false, rev: opts.rev || "", failed: true };
    stopPolling();
    patch({ loading: false, cancelling: false });
    toast.error((e as Error).message || "Failed to update job status");
    return { settled: true, rev: "" };
  }
}

// A SELF-RESCHEDULING LOOP, NOT AN INTERVAL. A held request can outlast any
// interval worth setting, and an interval would then stack a second request
// on top of the one still waiting. Each pass starts only once the last has
// answered, so exactly one request is ever in flight for a job.
function watch(id: string, gen: number, rev: string): void {
  stopPolling();
  pollActive = true;
  let failures = 0;
  const loop = async () => {
    if (gen !== generation || state.jobId !== id) {
      pollActive = false;
      return;
    }
    const { settled, rev: next, failed } = await pollJob(id, gen, {
      rev, wait: WAIT_S, quiet: failures + 1 < MAX_POLL_FAILURES,
    });
    if (settled) {
      pollActive = false;
      return;
    }
    if (gen !== generation || state.jobId !== id) {
      pollActive = false;
      return;
    }
    if (failed) {
      failures += 1;
      pollTimer = window.setTimeout(() => void loop(), RETRY_MS);
      return;
    }
    failures = 0;
    if (!next) {
      rev = "";
      pollTimer = window.setTimeout(() => void loop(), FALLBACK_POLL_MS);
      return;
    }
    rev = next;
    void loop();
  };
  void loop();
}

// ----------------------------------------------------------------- actions

// Picks up a job started elsewhere (Home's "Analyse", Live Results'
// "Analyse Validated").
//
// THE GUARD IS THE WHOLE POINT. `resumeJobId` stays set on App's state, so
// the effect that calls this re-fires on every remount -- i.e. every time
// the analyst comes back from another tab. Without the guard, coming back
// would blank `jobData` and `edits` and re-poll from scratch: precisely the
// disappearing-results bug this store exists to fix, reintroduced one layer
// up. So: already have this job's results, or already polling it? Do
// nothing. Only a genuinely different job id starts over.
//
// The `jobData === null && no poll` case is deliberately NOT skipped -- that
// is a first watch whose poll failed, and it should be retried, not left
// showing an empty table forever.
//
// A job that belongs to ANOTHER client is ignored outright -- see `jobOrg`.
// A job seen for the first time is taken to belong to the client that is
// open now, which is the one it was just started for.
export function watchJob(id: string): void {
  const owner = jobOrg.get(id);
  if (owner !== undefined && owner !== state.orgId) return;
  if (state.jobId === id && (state.jobData !== null || pollActive)) return;
  jobOrg.set(id, state.orgId);
  const gen = ++generation;
  stopPolling();
  if (state.jobId !== id) deletedInJob = new Set();
  patch({ jobId: id, jobData: null, loading: true, edits: editsSurvivingJob(state.jobData) });
  void (async () => {
    const first = await pollJob(id, gen);
    if (!first.settled) watch(id, gen, first.rev);
  })();
}

/** Point the workspace at a client and repopulate `saved` for it.
 *
 *  Called on mount and whenever the selected client changes. The table is
 *  emptied BEFORE the request goes out, not after it returns: leaving the
 *  previous client's rows up while the new client's load is in flight is
 *  showing one client another's work, briefly, which is the exact thing
 *  this scoping exists to prevent.
 *
 *  `gen` guards against two client switches overlapping -- a slow response
 *  for client A must never repopulate a workspace that has since moved to
 *  client B.
 *
 *  A REAL SWITCH ALSO DROPS THE LIVE JOB. Clearing only `saved` used to
 *  leave client A's running job attached -- still polled, its rows still
 *  laid over B's table, and so inside B's exports and B's Delete All. Now
 *  the poll is cut, the job and its edits go, and a job that was still
 *  running is parked so that switching back to A picks it up again.
 */
export async function setOrg(orgId: string): Promise<void> {
  const next = orgId || "";
  if (state.orgId === next && (state.saved.length > 0 || state.savedLoading)) return;
  if (state.orgId !== next) {
    const running = state.jobId !== null
      && (state.loading || !state.jobData
        || !["done", "cancelled", "failed"].includes(state.jobData.status));
    if (running && state.jobId) parkedJob.set(state.orgId, state.jobId);
    generation++;
    stopPolling();
    deletedInJob = new Set();
    patch({
      orgId: next, saved: [], selected: [], savedError: null,
      jobId: null, jobData: null, edits: {}, loading: false, cancelling: false,
    });
    const back = parkedJob.get(next);
    if (back) {
      parkedJob.delete(next);
      watchJob(back);
    }
  } else {
    patch({ orgId: next, saved: [], selected: [], savedError: null });
  }
  await loadSaved();
}

/** Repopulate `saved` for the current client. Safe to call at any time. */
export async function loadSaved(): Promise<void> {
  const org = state.orgId;
  const gen = ++savedGeneration;
  patch({ savedLoading: true });
  try {
    const res = await analysisApi.listResults(org);
    // The client changed (or another load started) while this was in
    // flight -- these rows belong to a workspace that no longer exists.
    if (gen !== savedGeneration || state.orgId !== org) return;
    patch({
      // A delete that landed while this load was in flight is filtered
      // here too, or the stale list would put the row straight back.
      saved: deletedInJob.size
        ? res.items.filter((r) => !deletedInJob.has(r.result_id ?? ""))
        : res.items,
      retentionHours: res.retention_hours,
      savedLoading: false,
      savedError: null,
      // Drop ticks for rows that are no longer there (expired, or deleted
      // from another tab). A selection that outlives its rows would make
      // "Delete Selected (3)" act on one.
      selected: state.selected.filter((id) => res.items.some((i) => i.result_id === id)),
    });
  } catch (e) {
    if (gen !== savedGeneration) return;
    const msg = (e as Error).message || "Could not load saved results";
    patch({ savedLoading: false, savedError: msg });
    toast.error(msg);
  }
}

/** Every row on screen: the saved set, with a live job's rows laid over it.
 *
 *  Merged by `result_id`, which is stable across runs, so re-analysing a
 *  URL updates its row in place instead of adding a second one that
 *  disagrees with the first. The live copy wins because it is the newer
 *  reading -- and because a row still `running` has no saved counterpart
 *  yet and must appear anyway, or the table would look frozen mid-job.
 *
 *  Job rows keep their job order at the top (that is the batch the analyst
 *  is watching); everything else follows, newest first.
 */
export function mergedItems(session: AnalysisSession): AnalysisItemData[] {
  const jobItems = session.jobData?.items ?? [];
  const inJob = new Set(jobItems.map((i) => i.result_id).filter(Boolean));
  const rest = session.saved
    .filter((r) => !r.result_id || !inJob.has(r.result_id))
    .sort((a, b) => String(b.analysed_at ?? "").localeCompare(String(a.analysed_at ?? "")));
  return [...jobItems, ...rest];
}

export function toggleSelected(resultId: string): void {
  setField("selected", (prev) =>
    prev.includes(resultId) ? prev.filter((i) => i !== resultId) : [...prev, resultId]);
}

export function setSelected(ids: string[]): void {
  setField("selected", ids);
}

/** Delete the ticked rows, or every saved row. Returns how many went.
 *
 *  The live job's copy is dropped along with them: deleting a row that is
 *  still listed on `jobData` would put it straight back on screen at the
 *  next poll, which reads as the delete having failed. */
export async function deleteSaved(mode: "selected" | "all"): Promise<number> {
  const ids = state.selected;
  if (mode === "selected" && !ids.length) return 0;
  patch({ deleting: true });
  try {
    const res = mode === "all"
      ? await analysisApi.deleteAllResults(state.orgId)
      : await analysisApi.deleteResults(ids);
    const gone = new Set(mode === "all" ? [] : ids);
    const job = state.jobData;
    // Remembered for the rest of this job -- see `deletedInJob`. "All"
    // covers every saved row and every job row that had SETTLED (and so was
    // saved); a row still running is not saved yet, will be written when it
    // finishes, and is a genuinely new result when it then appears.
    for (const id of gone) deletedInJob.add(id);
    if (mode === "all") {
      for (const r of state.saved) if (r.result_id) deletedInJob.add(r.result_id);
      for (const i of job?.items ?? []) {
        if (i.result_id && (i.status === "done" || i.status === "error")) deletedInJob.add(i.result_id);
      }
    }
    patch({
      deleting: false,
      selected: [],
      saved: mode === "all" ? [] : state.saved.filter((r) => !gone.has(r.result_id ?? "")),
      jobData: job ? withoutDeleted(job) : job,
    });
    return res.deleted;
  } catch (e) {
    patch({ deleting: false });
    toast.error((e as Error).message || "Could not delete results");
    return 0;
  }
}

export async function startAnalysis(urls: string[]): Promise<void> {
  const gen = ++generation;
  const org = state.orgId;
  stopPolling();
  deletedInJob = new Set();
  patch({ loading: true, jobId: null, jobData: null, edits: editsSurvivingJob(state.jobData) });
  try {
    const res = await analysisApi.start(urls, "", "", org);
    // Clear, a newer start, or a client switch landed while this request
    // was in flight. Installing this job now would attach it to a workspace
    // the analyst has already moved on from.
    if (gen !== generation) return;
    jobOrg.set(res.job_id, org);
    patch({ jobId: res.job_id });

    if (res.skipped && res.skipped.length > 0) {
      toast(`Skipped ${res.skipped.length} invalid/duplicate URL(s)`, { icon: "ℹ️" });
    }

    const first = await pollJob(res.job_id, gen);
    if (!first.settled) watch(res.job_id, gen, first.rev);
  } catch (e) {
    if (gen !== generation) return;
    patch({ loading: false });
    toast.error((e as Error).message || "Failed to start analysis");
  }
}

export async function cancelAnalysis(): Promise<void> {
  const id = state.jobId;
  if (!id) return;
  try {
    patch({ cancelling: true });
    await analysisApi.cancelJob(id);
    toast("Stopping analysis...", { icon: "⏳" });
  } catch (e) {
    patch({ cancelling: false });
    toast.error((e as Error).message || "Failed to cancel");
  }
}

// Clears the results AND the edits keyed to them. Edits used to be left
// behind here, which was invisible while the whole component was thrown
// away on every tab switch; now that the session persists, orphaned edits
// would accumulate for the life of the page. Only the edits of rows that
// LEAVE the screen go, though: a job row that already settled is in `saved`
// under the same id and stays visible, and wiping the analyst's corrections
// on a row they can still see is losing their work, not tidying up.
// Clears the WORKSPACE -- the URL box, the current job, the filters and the
// edits keyed to them. It deliberately does NOT touch `saved`: those rows
// are on the server for 24 hours and Clear is not a delete. An analyst who
// wants them gone uses Delete Selected / Delete All, which say so.
export function clearSession(): void {
  // Bumping the generation is what makes Clear beat a start or poll that is
  // still in flight -- see `generation`.
  generation++;
  stopPolling();
  deletedInJob = new Set();
  patch({
    urlInput: "",
    jobId: null,
    jobData: null,
    searchQuery: "",
    edits: editsSurvivingJob(state.jobData),
    loading: false,
    cancelling: false,
  });
}
