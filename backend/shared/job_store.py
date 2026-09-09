"""The bounded, TTL'd, in-memory job table `backend/discovery/runner.py`
and `backend/analysis/runner.py` both sit on.

Before this existed, each runner defined its own `MAX_JOBS`/`JOB_TTL_SECONDS`
ceiling, its own age/evict-oldest logic, its own lookup-that-expires-on-read,
and its own "which pooled session is this job holding right now" tracking --
four pieces of bookkeeping, identical in both files, maintained twice.

This is NOT a job-running framework: discovery sweeps keywords and analysis
scrapes URLs, and how a job actually does its work stays entirely in each
runner. This only owns what happens to a job once it exists: how long it
lives in memory, what evicts it, and which session it currently holds.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Generic, Optional, Protocol, TypeVar, runtime_checkable


@runtime_checkable
class TrackedJob(Protocol):
    """The minimum shape a job needs to live in a `JobStore`. Both
    `DiscoveryJob` and `AnalysisJob` satisfy this already -- structurally,
    via their own dataclass fields, not by inheriting from anything here."""

    id: str
    created_at: float
    status: str
    cancel: "asyncio.Event"


# How long a cancelled job is given to stop cleanly on its own before its
# task is cancelled outright. Short enough that "Stop" means what an analyst
# reads it as, long enough that the cooperative path -- which saves what it
# has read and releases its session on the way out -- wins in the ordinary
# case. See `JobStore.cancel`.
HARD_CANCEL_GRACE_S = 5.0

J = TypeVar("J", bound=TrackedJob)


class JobStore(Generic[J]):
    """One domain's (discovery's, or analysis's) live jobs. Process-wide
    per domain -- each runner constructs and owns exactly one."""

    def __init__(
        self, *, max_jobs: int, ttl_seconds: float, terminal_statuses: frozenset[str],
        on_evict: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.max_jobs = max_jobs
        self.ttl_seconds = ttl_seconds
        self._terminal = terminal_statuses
        # Called with a job's id whenever THIS store drops it (TTL expiry
        # or pressure eviction) -- never on an explicit external delete,
        # there is none. Analysis uses this to drop that job's screenshots
        # (keyed "job_id:item_id", so JobStore itself has no way to find
        # them); discovery has nothing extra to clean up and leaves it unset.
        self._on_evict = on_evict
        self._jobs: dict[str, J] = {}
        self._lock = asyncio.Lock()
        # (platform_id, session_id) a job is holding at this exact moment.
        # Read by sessions/manager.py::_session_in_use to show "currently
        # running" in an operational view; released in the runner's own
        # `finally` so a crash can never leave a session stuck "busy".
        self._sessions_in_use: set[tuple[str, str]] = set()
        # Live cancel watchdogs (see `cancel`), held so the event loop's
        # weak task references cannot collect one before its deadline.
        self._watchdogs: set["asyncio.Task"] = set()

    def age_seconds(self, job: J) -> float:
        return time.time() - job.created_at

    async def put(self, job: J) -> None:
        """Register a newly-created job, evicting first so the table never
        grows past `max_jobs` even under a burst of creations."""
        async with self._lock:
            self._evict_locked(reserve=1)
            self._jobs[job.id] = job

    async def get(self, job_id: str) -> Optional[J]:
        """None for an unknown id OR one that has aged past its TTL -- a
        caller does not need to tell those apart, both mean "nothing here
        for you to read"."""
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if self.age_seconds(job) >= self.ttl_seconds:
                self._drop_locked(job_id)
                return None
            return job

    async def cancel(self, job_id: str) -> bool:
        """Stop this job now. False means nothing to cancel -- unknown id,
        or already terminal -- never an error; a caller can treat this as
        idempotent.

        TWO MECHANISMS, BECAUSE THE COOPERATIVE ONE ALONE IS NOT "NOW".
        Setting `job.cancel` asks the runner to put the work down at its
        next checkpoint, which is the clean stop: whatever has been read is
        saved, sessions are released, browsers are closed by their own
        `finally`. Every checkpoint is inside a loop, though, so how fast
        that lands depends entirely on how long the CURRENT step takes --
        and a Facebook sweep can hold one step for its whole `max_seconds`
        ceiling plus a reconciliation phase. Pressing Stop was therefore
        accepted instantly, shown as "cancelling", and then did nothing
        observable for up to a quarter of an hour.

        So the flag is followed by a deadline. If the job has not reached a
        terminal status within `HARD_CANCEL_GRACE_S`, its task is cancelled
        outright, which unwinds it through whatever `await` it is parked on
        -- a page load, a sleep, a gather -- and runs every `finally` on the
        way out. The grace period is what lets the cooperative path win
        whenever it can, so the hard cancel is the backstop and not the
        normal route.
        """
        job = await self.get(job_id)
        if job is None or job.status in self._terminal:
            return False
        job.cancel.set()
        task = getattr(job, "task", None)
        if task is not None and not task.done():
            # HELD IN A SET, not fire-and-forget. asyncio keeps only a weak
            # reference to a task, so a watchdog with no strong reference
            # can be collected before its deadline -- which would silently
            # remove the backstop under exactly the load that needs it.
            watchdog = asyncio.create_task(self._enforce_cancel(job, task))
            self._watchdogs.add(watchdog)
            watchdog.add_done_callback(self._watchdogs.discard)
        return True

    async def _enforce_cancel(self, job: J, task: "asyncio.Task") -> None:
        """The deadline behind `cancel`. Waits out the grace period and,
        only if the job is still going, cancels its task.

        `asyncio.wait` is used rather than `wait_for` on purpose: it does
        NOT cancel the thing it is waiting on when the timeout expires, so
        the decision to cancel stays here and explicit rather than being a
        side effect of the wait.
        """
        try:
            done, _ = await asyncio.wait({task}, timeout=HARD_CANCEL_GRACE_S)
            if done or job.status in self._terminal:
                # The cooperative path got there first, which is the
                # outcome this grace period exists to allow.
                return
            task.cancel()
        except asyncio.CancelledError:
            # The watchdog itself was cancelled (process shutting down).
            # Nothing to enforce, and re-raising would be noise.
            return
        except Exception:                        # noqa: BLE001 - never fatal
            # A stop that fails must never take the process with it; the
            # cooperative flag is still set and still honoured.
            return

    def _drop_locked(self, job_id: str) -> None:
        if self._jobs.pop(job_id, None) is not None and self._on_evict is not None:
            self._on_evict(job_id)

    def _evict_locked(self, *, reserve: int = 0) -> None:
        """Expired jobs first (free to lose), then oldest-first once still
        over the ceiling -- but ONLY among TERMINAL jobs, and only a still-
        running one if there are truly no terminal jobs left to reclaim.

        THE BUG THIS GUARDS. The ceiling-eviction loop used to pick the
        globally oldest job by `created_at` with no regard for `status`.
        That is exactly backwards for which job is safest to forget: a
        terminal job (done/failed/cancelled) has nothing left to lose by
        being dropped from this table, while a job that is still RUNNING
        has a live `asyncio.Task` depending on this table being the thing
        that keeps it reachable -- `job.task = asyncio.create_task(...)` is
        the ONLY strong reference to that task once it is created (see
        discovery/runner.py and analysis/runner.py, both of which do
        exactly this and never read `.task` again). Per asyncio's own
        documented behaviour, "the event loop only keeps weak references to
        tasks. A task that isn't referenced elsewhere may get garbage
        collected at any time, even before it's done." Evicting a running
        job's entry here breaks that one reference, which does two things
        at once: every poller (a client's own poll loop, or the Scheduler's
        queue runner watching a discovery sweep) starts getting "job not
        found" the instant this table forgets it, AND the sweep itself
        becomes eligible for outright garbage collection mid-keyword, with
        nothing left to log that it happened. A long-running server
        accumulates terminal jobs from ordinary use far faster than it
        accumulates genuinely still-running ones, so without this
        ordering, ceiling pressure was disproportionately likely to pick
        exactly the wrong job -- an outlier that has been running a long
        time (and is therefore often the globally oldest) over any of the
        many short-lived terminal jobs sitting right next to it.

        This module already applies the identical reasoning one level up
        (job.avatar_tasks' own docstring: "a task with no live reference
        can be garbage collected mid-flight, which would cache nothing
        under exactly the load where it matters") -- this is the same fix
        for the job's own task.

        `reserve` leaves room for a job `put()` is about to insert right
        after this call -- without it, evicting down to exactly `max_jobs`
        and then inserting leaves the table at `max_jobs + 1` until the
        NEXT `put()`, one permanently-late eviction behind the ceiling."""
        for jid in [j for j, job in self._jobs.items() if self.age_seconds(job) >= self.ttl_seconds]:
            self._drop_locked(jid)
        while len(self._jobs) > self.max_jobs - reserve:
            terminal = [j for j, job in self._jobs.items() if job.status in self._terminal]
            pool = terminal or list(self._jobs)
            oldest = min(pool, key=lambda j: self._jobs[j].created_at)
            self._drop_locked(oldest)

    # ------------------------------------------------------- session tracking

    def holds_session(self, platform_id: str, session_id: str) -> bool:
        return (platform_id, session_id) in self._sessions_in_use

    def hold_session(self, platform_id: str, session_id: str) -> Optional[tuple[str, str]]:
        """Record that a job is about to use this session. Returns the key
        to hand back to `release_session` in a `finally` -- None when
        `session_id` is blank (a key/MTProto-authed platform has nothing to
        track), so the caller can pass the result straight through without
        an extra branch."""
        if not session_id:
            return None
        key = (platform_id, session_id)
        self._sessions_in_use.add(key)
        return key

    def release_session(self, key: Optional[tuple[str, str]]) -> None:
        if key is not None:
            self._sessions_in_use.discard(key)

    # ------------------------------------------------------------- observability

    async def stats(self) -> dict:
        async with self._lock:
            return {
                "jobs": len(self._jobs),
                "max_jobs": self.max_jobs,
                "ttl_seconds": self.ttl_seconds,
                "running": sum(1 for j in self._jobs.values() if j.status not in self._terminal),
            }
