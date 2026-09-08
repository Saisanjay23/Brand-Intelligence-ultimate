import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "react-hot-toast";
import { discoveryApi } from "../api/discoveryApi";
import type { DiscoveryJobState } from "../api/discoveryApi";

const TERMINAL = new Set(["done", "failed", "cancelled"]);
const POLL_MS = 2000;

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
// group and has no backend equivalent any more). So this just re-fetches
// the snapshot on an interval until the job reaches a terminal status,
// keeping the epoch-guard + visibility-aware structure that made the old
// useJobPolling hook safe to unmount/restart mid-poll.
// `onProgress` fires whenever the running job's counts MOVE, not only when
// it ends. Without it the grid had no idea results were landing: the backend
// writes profiles per completed sweep (see discovery/runner.py -- "so a
// caller polling this job sees results within seconds"), but nothing on this
// side re-read them until the job hit a terminal status. The rows were in
// Mongo the whole time; the only way to see them was to leave the tab and
// come back, which remounted the grid and forced a fetch.
export function useDiscoveryJobPoll(onFinish?: () => void, onProgress?: () => void) {
  const [job, setJob] = useState<DiscoveryJobState | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const epoch = useRef(0);
  const mounted = useRef(true);
  const finish = useRef(onFinish);
  finish.current = onFinish;
  const progress = useRef(onProgress);
  progress.current = onProgress;
  // Last counts we told the caller about. Compared rather than fired on
  // every tick so a 2s poll on an idle platform does not re-query the grid
  // (and its four facet aggregates) for nothing.
  const lastCounts = useRef("");
  const [cancelling, setCancelling] = useState(false);

  const stop = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  }, []);

  const watch = useCallback((jobId: string) => {
    stop();
    const myEpoch = (epoch.current += 1);
    lastCounts.current = "";
    const stale = () => !mounted.current || epoch.current !== myEpoch;

    const poll = async () => {
      try {
        const updated = await discoveryApi.getJob(jobId);
        if (stale()) return;
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
      } catch {
        // transient fetch failure, keep polling rather than giving up
      }
      if (stale()) return;
      timer.current = setTimeout(poll, POLL_MS);
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
