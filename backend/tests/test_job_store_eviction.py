"""Ceiling eviction must never pick a still-running job over a finished one.

THE BUG THIS GUARDS. `JobStore` is what `discovery/runner.py` and
`analysis/runner.py` both sit on: a bounded, in-memory table of live jobs.
When a new job pushes the table past `max_jobs`, something already in the
table has to go. The eviction loop used to pick the globally OLDEST job by
`created_at` with no regard for whether it was still running -- which is
backwards. A terminal job (done/failed/cancelled) costs nothing to forget.
A running job has a live `asyncio.Task` whose ONLY strong reference is
`job.task = asyncio.create_task(...)` -- both runners set it once and never
read it again (grep confirms this: discovery/runner.py:558,
analysis/runner.py:554). Per asyncio's own documentation, a task with no
reference elsewhere "may get garbage collected at any time, even before
it's done." Evicting a running job's table entry breaks that reference.

A long-uptime server accumulates terminal jobs from ordinary use far faster
than genuinely long-running ones, so under the old ordering, ceiling
pressure was disproportionately likely to pick exactly the wrong job: an
outlier that has been running a long time -- and is therefore often the
globally oldest -- over any of the many short-lived terminal jobs sitting
right next to it in the table.

This is pure logic against the real `JobStore`, no network or Mongo
involved -- a fake job needs only the four fields `TrackedJob` requires.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.shared.job_store import JobStore

RUNNING, DONE, FAILED = "running", "done", "failed"
TERMINAL = frozenset({DONE, FAILED, "cancelled"})


# A base near "now", not epoch zero -- `created_at` is compared against
# real wall-clock time.time() by the TTL check, so a fake job's timestamp
# has to be recent or it reads as decades old and gets reclaimed by TTL
# before the eviction-ORDER logic these tests are pinning ever runs.
_NOW = time.time()


class FakeJob:
    """The minimum shape JobStore needs -- id, created_at, status, cancel."""

    def __init__(self, id: str, created_at: float, status: str = RUNNING):
        self.id = id
        self.created_at = _NOW + created_at
        self.status = status
        self.cancel = asyncio.Event()
        # Stands in for `job.task` -- what this whole test suite is really
        # protecting: something that must not be dropped while `status` is
        # still RUNNING.
        self.task = object()


def _store(max_jobs: int) -> JobStore[FakeJob]:
    return JobStore(max_jobs=max_jobs, ttl_seconds=6 * 3600, terminal_statuses=TERMINAL)


class TestTerminalJobsAreEvictedBeforeRunningOnes:
    @pytest.mark.asyncio
    async def test_a_running_job_survives_while_a_terminal_one_is_available(self):
        """The exact shape of the bug: the RUNNING job is also the OLDEST,
        which is what made the old oldest-first rule pick precisely wrong."""
        store = _store(max_jobs=2)
        running_old = FakeJob("running-old", created_at=1.0, status=RUNNING)
        done_new = FakeJob("done-new", created_at=2.0, status=DONE)
        await store.put(running_old)
        await store.put(done_new)

        # A third job arrives, pushing the table over its ceiling of 2.
        newcomer = FakeJob("newcomer", created_at=3.0, status=RUNNING)
        await store.put(newcomer)

        assert await store.get("running-old") is not None, (
            "the running job must not be the one evicted while a terminal "
            "job was sitting right next to it"
        )
        assert await store.get("done-new") is None, (
            "the terminal job is the one with nothing left to lose -- it "
            "should have been reclaimed instead"
        )
        assert await store.get("newcomer") is not None

    @pytest.mark.asyncio
    async def test_several_terminal_jobs_are_reclaimed_in_age_order_first(self):
        store = _store(max_jobs=3)
        running = FakeJob("running", created_at=0.0, status=RUNNING)
        old_done = FakeJob("old-done", created_at=1.0, status=DONE)
        mid_failed = FakeJob("mid-failed", created_at=2.0, status=FAILED)
        for j in (running, old_done, mid_failed):
            await store.put(j)

        await store.put(FakeJob("newcomer1", created_at=3.0, status=RUNNING))
        # table: running, mid-failed, newcomer1 (old-done reclaimed)
        assert await store.get("old-done") is None
        assert await store.get("running") is not None
        assert await store.get("mid-failed") is not None

        await store.put(FakeJob("newcomer2", created_at=4.0, status=RUNNING))
        # table: running, newcomer1, newcomer2 (mid-failed reclaimed next)
        assert await store.get("mid-failed") is None
        assert await store.get("running") is not None, (
            "still the only running job -- still the last one touched"
        )

    @pytest.mark.asyncio
    async def test_a_running_job_is_evicted_only_once_no_terminal_job_remains(self):
        """The fallback this rule needs: if EVERY job in the table is
        running, ceiling pressure still has to evict something, or the
        table grows without bound and defeats its own purpose."""
        store = _store(max_jobs=1)
        oldest_running = FakeJob("oldest", created_at=1.0, status=RUNNING)
        await store.put(oldest_running)

        await store.put(FakeJob("newer", created_at=2.0, status=RUNNING))

        assert await store.get("oldest") is None, (
            "with nothing terminal to reclaim, the ceiling still has to "
            "hold -- the oldest running job is the last resort, not exempt"
        )
        assert await store.get("newer") is not None


class TestOrdinaryEvictionIsUnaffected:
    """The common case -- a table full of terminal jobs -- must keep
    behaving exactly as before: oldest-first, nothing running involved."""

    @pytest.mark.asyncio
    async def test_oldest_terminal_job_goes_first_when_nothing_is_running(self):
        store = _store(max_jobs=2)
        await store.put(FakeJob("oldest", created_at=1.0, status=DONE))
        await store.put(FakeJob("newer", created_at=2.0, status=FAILED))
        await store.put(FakeJob("newest", created_at=3.0, status=DONE))

        assert await store.get("oldest") is None
        assert await store.get("newer") is not None
        assert await store.get("newest") is not None

    @pytest.mark.asyncio
    async def test_expired_jobs_are_still_reclaimed_before_the_ceiling_runs(self):
        store = JobStore(max_jobs=5, ttl_seconds=100, terminal_statuses=TERMINAL)
        stale = FakeJob("stale", created_at=-1000, status=RUNNING)  # 1000s before _NOW
        await store.put(stale)
        assert await store.get("stale") is None


class TestUnderTheCeilingNothingIsTouched:
    @pytest.mark.asyncio
    async def test_a_running_job_is_never_evicted_while_there_is_room(self):
        store = _store(max_jobs=10)
        await store.put(FakeJob("solo", created_at=1.0, status=RUNNING))
        assert await store.get("solo") is not None
