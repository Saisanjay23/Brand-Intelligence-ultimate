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

// Consecutive failed requests before the sweep is reported as "cannot be
// reached" rather than quietly retried for ever.
const LOST_AFTER_FAILURES = 5;

// A PER-PLATFORM ending that means "not everything was searched".
const UNHEALTHY_PLATFORM = new Set(["failed", "partial", "skipped"]);

function notifyFinished(job: DiscoveryJobState): void {
  const counts = `${job.found} found${job.new ? ` (${job.new} new)` : ""}`;
  if (job.status === "failed") {
    toast.error(`Discovery sweep failed: ${counts}${job.message ? ` -- ${job.message}` : ""}`);
    return;
  }
  if (job.status === "cancelled") {
    toast(`Discovery sweep was cancelled: ${counts}`, { icon: "⚠️" });
    return;
  }
  // THE JOB'S STATUS IS NOT THE VERDICT. The runner marks a job "done" once
  // every platform has ENDED, however it ended -- so a sweep in which every
  // platform failed, was skipped (no ready session) or stopped part-way used
  // to toast a green "finished: 0 found". That is the silent clean zero
  // this tool exists to avoid: an unsearched keyword reads exactly like a
  // searched one that found nothing. The per-platform statuses decide.
  const platforms = job.platforms || [];
  const bad = platforms.filter((p) => UNHEALTHY_PLATFORM.has(p.status));
  const anyDone = platforms.some((p) => p.status === "done");
  const name = (p: { display_name: string; platform: string }) => p.display_name || p.platform;
  const tail = job.message ? ` -- ${job.message}` : "";
  if (bad.length && !anyDone) {
    toast.error(
      `Discovery sweep did not search: ${bad.map((p) => `${name(p)} ${p.status}`).join(", ")}. ` +
        `${counts}${tail}`,
      { duration: 12000 },
    );
  } else if (bad.length) {
    toast(
      `Discovery sweep finished with gaps -- ${bad.map((p) => `${name(p)} ${p.status}`).join(", ")}. ` +
        `${counts}${tail}`,
      { icon: "⚠️", duration: 12000 },
    );
  } else if (!anyDone && job.found === 0) {
    // No platform reports a clean finish and nothing was found: whatever
    // happened, it was not a completed search, so it must not be green.
    toast(`Discovery sweep ended without any platform completing: ${counts}${tail}`, {
      icon: "⚠️", duration: 12000,
    });
  } else {
    toast.success(`Discovery sweep finished: ${counts}`);
  }
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
  // WHY THE SWEEP CAN NO LONGER BE FOLLOWED, or "". Every error used to be
  // retried every two seconds for ever -- including a 404 after a backend
  // restart -- so the banner sat frozen on RUNNING, `busy` kept blocking new
  // sweeps, and nothing ever said the job was gone.
  const [lostState, setLost] = useState<{ message: string; gone: boolean } | null>(null);

  const stop = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    abort.current?.abort();
    abort.current = null;
  }, []);

  const watch = useCallback((jobId: string) => {
    stop();
    setLost(null);
    let failures = 0;
    const myEpoch = (epoch.current += 1);
    lastCounts.current = "";
    const stale = () => !mounted.current || epoch.current !== myEpoch;
    // The rev we are waiting to see change. Empty on the first request, so
    // that one is answered immediately with the state as it stands.
    let rev = "";
    // The finished toast fires once, when the sweep ends -- not again when
    // its picture checks finish settling behind it.
    let announced = false;

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
        failures = 0;
        setLost(null);
        rev = updated.rev || "";
        setJob(updated);
        // Counts moved -> new rows are already saved and readable. A landed
        // picture batch counts too: it can have corrected a card's logo.
        const counts = `${updated.found}/${updated.new}/${updated.completed}/${updated.avatar_updates ?? 0}`;
        if (counts !== lastCounts.current) {
          lastCounts.current = counts;
          progress.current?.();
        }
        if (TERMINAL.has(updated.status)) {
          if (!announced) {
            announced = true;
            notifyFinished(updated);
          }
          // Picture checks still landing after the sweep ended: keep
          // listening, so their logo corrections reach the open grid.
          if (!updated.avatars_settling || !updated.rev) {
            timer.current = null;
            finish.current?.();
            return;
          }
        }
        if (stale()) return;
        if (!updated.rev) {
          timer.current = setTimeout(poll, FALLBACK_POLL_MS);
          return;
        }
        // Straight back out: the server does the waiting.
        void poll();
        return;
      } catch (e) {
        // An abort is this hook being torn down, not a failure -- `stale()`
        // covers it below. Anything else is a transient fetch failure, and
        // backing off briefly is what stops a restarting backend being
        // hammered by a loop with no delay in it.
        if (!stale()) {
          if ((e as { notFound?: boolean }).notFound) {
            setLost({
              gone: true,
              message: "The server no longer knows this sweep (it may have restarted). "
                + "Whatever it found before then is saved -- start it again to continue.",
            });
            timer.current = null;
            finish.current?.();
            return;
          }
          failures += 1;
          if (failures === LOST_AFTER_FAILURES) {
            setLost({
              gone: false,
              message: "Cannot reach the server to follow this sweep. It may still be running -- "
                + "retrying; this message clears when it answers.",
            });
          }
        }
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

  const running = !!job && !TERMINAL.has(job.status) && !lostState?.gone;

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

  return { job, watch, running, cancelling, cancel, lost: lostState?.message ?? "" };
}
