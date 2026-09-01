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
        """False means nothing to cancel -- unknown id, or already
        terminal -- never an error; a caller can treat this as idempotent."""
        job = await self.get(job_id)
        if job is None or job.status in self._terminal:
            return False
        job.cancel.set()
        return True

    def _drop_locked(self, job_id: str) -> None:
        if self._jobs.pop(job_id, None) is not None and self._on_evict is not None:
            self._on_evict(job_id)

    def _evict_locked(self, *, reserve: int = 0) -> None:
        """Expired jobs first (free to lose), then oldest-first once still
        over the ceiling. Insertion order isn't relied on for age -- this
        reads each job's own `created_at`, so eviction stays correct even
        if a caller ever re-inserts an existing id.

        `reserve` leaves room for a job `put()` is about to insert right
        after this call -- without it, evicting down to exactly `max_jobs`
        and then inserting leaves the table at `max_jobs + 1` until the
        NEXT `put()`, one permanently-late eviction behind the ceiling."""
        for jid in [j for j, job in self._jobs.items() if self.age_seconds(job) >= self.ttl_seconds]:
            self._drop_locked(jid)
        while len(self._jobs) > self.max_jobs - reserve:
            oldest = min(self._jobs, key=lambda j: self._jobs[j].created_at)
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
