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
export function useDiscoveryJobPoll(onFinish?: () => void) {
  const [job, setJob] = useState<DiscoveryJobState | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const epoch = useRef(0);
  const mounted = useRef(true);
  const finish = useRef(onFinish);
  finish.current = onFinish;
  const [cancelling, setCancelling] = useState(false);

  const stop = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  }, []);

  const watch = useCallback((jobId: string) => {
    stop();
    const myEpoch = (epoch.current += 1);
    const stale = () => !mounted.current || epoch.current !== myEpoch;

    const poll = async () => {
      try {
        const updated = await discoveryApi.getJob(jobId);
        if (stale()) return;
        setJob(updated);
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
