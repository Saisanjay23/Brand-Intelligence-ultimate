import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "react-hot-toast";
import { discoveryApi } from "../api/discoveryApi";
import type { DiscoveryJobState } from "../api/discoveryApi";

const TERMINAL = new Set(["done", "failed", "cancelled"]);

// How long the backend is asked to hold each request open while nothing is
// happening. Under its own 25s ceiling, so the server is what ends the wait
// and the client never races it. See backend/shared/live_poll.py.
const WAIT_S = 20;

// Only after a FAILED request. The success path has no delay at all -- the
// next request goes straight back out, and the waiting happens server-side
// where it can end the instant something changes. This gap exists purely so
// a backend that is down or restarting is retried rather than hammered.
const RETRY_MS = 2000;

// A BACKEND THAT DOES NOT SUPPORT THE LONG POLL MUST NOT BE HAMMERED.
// `rev` is what makes the server hold the next request; a backend older
// than that feature returns no `rev`, every request is then answered
// instantly, and looping straight back out would turn this into an
// unthrottled request flood against exactly the deployment least able to
// absorb it. Missing `rev` therefore falls back to plain interval polling
// -- the behaviour this replaced, which is the right thing to degrade to.
const FALLBACK_POLL_MS = 1000;

function notifyFinished(job: DiscoveryJobState): void {
  const message = `Discovery sweep ${
    job.status === "done" ? "finished" : job.status === "failed" ? "failed" : "was cancelled"
  }: ${job.found} found${job.new ? ` (${job.new} new)` : ""}`;
  if (job.status === "failed") toast.error(message);
  else if (job.status === "cancelled") toast(message, { icon: "⚠️" });
  else toast.success(message);
}

// GET /discovery/jobs/{id} is a plain snapshot -- status, per-platform
// counts, a message string -- there is no event log to tail (that whole
// concept, jobsApi.jobEvents/last_seq, belonged to the old /jobs route
// group and has no backend equivalent any more).
//
// IT IS NOW A LONG POLL, WHICH IS WHY THERE IS NO INTERVAL LEFT. This used
// to re-fetch the snapshot every two seconds, and that interval was the
// floor on how fast anything could appear: discovery saves its profiles per
// completed sweep, so a row could sit written, readable and invisible for
// two seconds longer than it took to save. Sending the previous `rev` back
// with a `wait` moves the waiting to the server, which answers the moment
// the job actually moves -- results land in about a tenth of a second, and
// a sweep that is grinding through one slow keyword costs one held request
// instead of ten pointless ones. The epoch guard, the visibility handling
// and the "never setState after unmount" structure are unchanged; only what
// makes the next request due has changed.
//
// `onProgress` fires whenever the running job's counts MOVE, not only when
// it ends. Without it the grid had no idea results were landing: the rows
// were in Mongo the whole time, and the only way to see them was to leave
// the tab and come back, which remounted the grid and forced a fetch.
export function useDiscoveryJobPoll(onFinish?: () => void, onProgress?: () => void) {
  const [job, setJob] = useState<DiscoveryJobState | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The in-flight long poll, so it can be cut short rather than left to
  // run out its window after an unmount or a switch to another job.
  const abort = useRef<AbortController | null>(null);
  const epoch = useRef(0);
  const mounted = useRef(true);
  const finish = useRef(onFinish);
  finish.current = onFinish;
  const progress = useRef(onProgress);
  progress.current = onProgress;
  // Last counts we told the caller about. Compared rather than fired on
  // every response so a job whose telemetry moved (a new current keyword,
  // say) without finding anything does not re-query the grid and its four
  // facet aggregates for rows that cannot have changed.
  const lastCounts = useRef("");
  const [cancelling, setCancelling] = useState(false);

  const stop = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    abort.current?.abort();
    abort.current = null;
  }, []);

  const watch = useCallback((jobId: string) => {
    stop();
    const myEpoch = (epoch.current += 1);
    lastCounts.current = "";
    const stale = () => !mounted.current || epoch.current !== myEpoch;
    // The rev we are waiting to see change. Empty on the first request, so
    // that one is answered immediately with the state as it stands.
    let rev = "";

    const poll = async () => {
      // A request can now be in flight for twenty seconds, so unmounting or
      // watching a different job has to ABORT it rather than just ignore
      // what it returns -- otherwise every restart leaves a held connection
      // behind for the rest of its window.
      const ac = new AbortController();
      abort.current = ac;
      try {
        const updated = await discoveryApi.getJob(jobId, {
          rev, wait: WAIT_S, signal: ac.signal,
        });
        if (stale()) return;
        rev = updated.rev || "";
        setJob(updated);
        // Counts moved -> new rows are already saved and readable.
        const counts = `${updated.found}/${updated.new}/${updated.completed}`;
        if (counts !== lastCounts.current) {
          lastCounts.current = counts;
          progress.current?.();
        }
        if (TERMINAL.has(updated.status)) {
          timer.current = null;
          notifyFinished(updated);
          finish.current?.();
          return;
        }
        if (stale()) return;
        if (!updated.rev) {
          timer.current = setTimeout(poll, FALLBACK_POLL_MS);
          return;
        }
        // Straight back out: the server does the waiting.
        void poll();
        return;
      } catch {
        // An abort is this hook being torn down, not a failure -- `stale()`
        // covers it below. Anything else is a transient fetch failure, and
        // backing off briefly is what stops a restarting backend being
        // hammered by a loop with no delay in it.
      }
      if (stale()) return;
      timer.current = setTimeout(poll, RETRY_MS);
    };
    void poll();
  }, [stop]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      stop();
    };
  }, [stop]);

  const running = !!job && !TERMINAL.has(job.status);

  useEffect(() => {
    if (!running) setCancelling(false);
  }, [running]);

  const cancel = async () => {
    if (!job || cancelling) return;
    setCancelling(true);
    try {
      await discoveryApi.cancelJob(job.job_id);
      toast("Stopping -- this can take a few seconds", { icon: "⏹" });
    } catch (e) {
      setCancelling(false);
      toast.error(`Could not stop: ${(e as Error).message}`);
    }
  };

  return { job, watch, running, cancelling, cancel };
}
