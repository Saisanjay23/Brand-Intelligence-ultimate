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
// WHAT IT DOES NOT SURVIVE, BY DESIGN: a page reload. This is in RAM only,
// deliberately -- a job's full item set (bios, per-item rows, screenshot
// pointers) is far too big for localStorage, and the requirement is only
// that results last until the analyst refreshes. `resumeJobId` already
// covers picking a job back up after a reload.

import { useCallback, useSyncExternalStore } from "react";
import toast from "react-hot-toast";
import { analysisApi, type AnalysisJobResponse } from "../api/analysisApi";

const POLL_MS = 1500;

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

// Bumped by anything that makes an in-flight request irrelevant: Clear, or
// a newer job superseding this one. Both `startAnalysis` and `watchJob`
// await the network with the workspace still live underneath them, and
// without this a response arriving late would install itself over whatever
// the analyst did in the meantime -- pressing Clear while the start request
// was in flight left a job id and a running poll attached to a workspace
// that had just been emptied.
let generation = 0;

export function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

// Returns true when this poll SETTLED the job -- terminal status, an error,
// or a response that is no longer wanted. The caller uses it to decide
// whether an interval is still needed: a job that was already finished on
// its first poll must not get one.
async function pollJob(id: string, gen: number): Promise<boolean> {
  try {
    const data = await analysisApi.getJob(id);
    // A poll that lands after the analyst started a different job (or hit
    // Clear) must not overwrite the current one with a stale payload.
    if (gen !== generation || state.jobId !== id) return true;
    patch({ jobData: data });
    if (data.status === "done" || data.status === "cancelled" || data.status === "failed") {
      stopPolling();
      patch({ loading: false, cancelling: false });
      if (data.status === "done") {
        toast.success(`Analysis completed for ${data.completed}/${data.total} URLs`);
      } else if (data.status === "cancelled") {
        toast.error("Analysis stopped by user");
      }
      return true;
    }
    return false;
  } catch (e) {
    if (gen !== generation || state.jobId !== id) return true;
    stopPolling();
    patch({ loading: false, cancelling: false });
    toast.error((e as Error).message || "Failed to update job status");
    return true;
  }
}

function watch(id: string, gen: number): void {
  stopPolling();
  pollTimer = window.setInterval(() => void pollJob(id, gen), POLL_MS);
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
export function watchJob(id: string): void {
  if (state.jobId === id && (state.jobData !== null || pollTimer !== null)) return;
  const gen = ++generation;
  stopPolling();
  patch({ jobId: id, jobData: null, loading: true, edits: {} });
  void (async () => {
    if (!(await pollJob(id, gen))) watch(id, gen);
  })();
}

export async function startAnalysis(urls: string[]): Promise<void> {
  const gen = ++generation;
  stopPolling();
  patch({ loading: true, jobId: null, jobData: null, edits: {} });
  try {
    const res = await analysisApi.start(urls, "", "");
    // Clear, or a newer start, landed while this request was in flight.
    // Installing this job now would attach it to a workspace the analyst
    // has already moved on from.
    if (gen !== generation) return;
    patch({ jobId: res.job_id });

    if (res.skipped && res.skipped.length > 0) {
      toast(`Skipped ${res.skipped.length} invalid/duplicate URL(s)`, { icon: "ℹ️" });
    }

    if (!(await pollJob(res.job_id, gen))) watch(res.job_id, gen);
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
// would accumulate for the life of the page.
export function clearSession(): void {
  // Bumping the generation is what makes Clear beat a start or poll that is
  // still in flight -- see `generation`.
  generation++;
  stopPolling();
  patch({
    urlInput: "",
    jobId: null,
    jobData: null,
    searchQuery: "",
    edits: {},
    loading: false,
    cancelling: false,
  });
}
