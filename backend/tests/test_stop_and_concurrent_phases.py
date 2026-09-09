"""Stop means stop, and the two phases can run at the same time.

TWO COMPLAINTS, ONE ROOT SHAPE. Both of these were the consequence of a
decision being taken only BETWEEN units of work:

  "Stop does nothing"        `job.cancel` was checked between keywords,
                             between tabs and between URLs. Every one of
                             those is inside a loop, so how fast a stop
                             landed was set by how long the CURRENT step
                             took -- and one Facebook sweep can hold a step
                             for its whole `max_seconds` ceiling plus a
                             reconciliation phase. The button was accepted
                             instantly and then did nothing observable for
                             up to a quarter of an hour.

  "can't run both phases"    a session is claimed for the whole of a sweep
                             or a batch, and a claim that could not be
                             waited on was an immediate ConflictError. On a
                             one-account platform whichever phase started
                             second failed outright, against a pool that was
                             healthy and about to be free.

The fixes are symmetrical: give the slow thing a way to be interrupted
(a cooperative signal the engine can see, plus a hard deadline behind it),
and give the blocked thing a way to wait instead of failing.

No browser and no Mongo here -- all of this is scheduling, decided against
a discoverer of one method and a pool of five, so fakes over those test the
real decisions.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.platforms.scan_options import DiscoveryOptions, ScanOptions, cancelled
from backend.shared import job_store as JS


# ------------------------------------------------- the signal itself


class TestTheCancelSignalReachesAnAdapter:
    """`cancelled(opts)` is what an engine calls at its own checkpoints. It
    reads through `getattr` because adapters take a "ScanOptions-shaped
    object", not necessarily a ScanOptions."""

    def test_no_signal_configured_is_not_cancelled(self):
        assert cancelled(DiscoveryOptions()) is False
        assert cancelled(ScanOptions()) is False

    def test_an_unset_event_is_not_cancelled(self):
        assert cancelled(DiscoveryOptions(cancel=asyncio.Event())) is False

    def test_a_set_event_is(self):
        e = asyncio.Event()
        e.set()
        assert cancelled(DiscoveryOptions(cancel=e)) is True
        assert cancelled(ScanOptions(cancel=e)) is True

    def test_the_job_and_the_adapter_share_ONE_event(self):
        """Not a copy of the flag -- the same object. A snapshot taken when
        the options were built would be permanently False, which is exactly
        the bug that made Stop look ignored."""
        e = asyncio.Event()
        opts = DiscoveryOptions(cancel=e)
        assert cancelled(opts) is False
        e.set()                       # the job is cancelled AFTER the build
        assert cancelled(opts) is True

    def test_a_plain_bool_or_callable_works_too(self):
        """So a test (or a caller with no Event to hand) can signal a stop
        without building one."""
        assert cancelled(DiscoveryOptions(cancel=True)) is True
        assert cancelled(DiscoveryOptions(cancel=lambda: True)) is True

    def test_an_object_with_no_cancel_attribute_at_all_is_safe(self):
        class BareOptions:
            max_results = 10

        assert cancelled(BareOptions()) is False


# ------------------------------------------- the deadline behind the flag


class _Job:
    """The minimum `JobStore.cancel` touches."""

    def __init__(self, coro) -> None:
        self.id = "job1"
        self.created_at = time.time()
        self.status = "running"
        self.cancel = asyncio.Event()
        self.task = asyncio.get_event_loop().create_task(coro)


def _store(monkeypatch, grace: float = 0.05) -> JS.JobStore:
    monkeypatch.setattr(JS, "HARD_CANCEL_GRACE_S", grace)
    return JS.JobStore(max_jobs=10, ttl_seconds=600, terminal_statuses={"done", "cancelled", "failed"})


class TestStopIsImmediateEvenMidStep:
    @pytest.mark.asyncio
    async def test_the_flag_is_set_straight_away(self, monkeypatch):
        """The cooperative half. A well-behaved runner sees this at its next
        checkpoint and stops cleanly, keeping what it has read."""
        store = _store(monkeypatch, grace=30.0)   # long: the flag must not depend on it
        job = _Job(asyncio.sleep(30))
        await store.put(job)
        assert await store.cancel(job.id) is True
        assert job.cancel.is_set()
        job.task.cancel()

    @pytest.mark.asyncio
    async def test_a_job_that_ignores_the_flag_is_cancelled_anyway(self, monkeypatch):
        """THE DEFECT THIS PINS. A step parked on one long await -- a page
        load, a resolve gather -- reaches no checkpoint, so the flag alone
        never stops it. The grace period expires and the task is cancelled
        where it stands."""
        store = _store(monkeypatch)
        job = _Job(asyncio.sleep(30))
        await store.put(job)
        await store.cancel(job.id)
        with pytest.raises(asyncio.CancelledError):
            await job.task
        assert job.task.cancelled()

    @pytest.mark.asyncio
    async def test_a_job_that_stops_cleanly_is_never_hard_cancelled(self, monkeypatch):
        """The grace period exists so the cooperative path WINS whenever it
        can -- that is the path that saves results and releases sessions."""
        store = _store(monkeypatch, grace=5.0)
        stopped = asyncio.Event()

        async def polite(job_ref: list) -> str:
            while not job_ref[0].cancel.is_set():
                await asyncio.sleep(0.01)
            job_ref[0].status = "cancelled"
            stopped.set()
            return "clean"

        ref: list = []
        job = _Job(polite(ref))
        ref.append(job)
        await store.put(job)
        await store.cancel(job.id)
        assert await asyncio.wait_for(job.task, timeout=2) == "clean"
        assert stopped.is_set() and not job.task.cancelled()

    @pytest.mark.asyncio
    async def test_cancelling_an_already_finished_job_is_a_no_op(self, monkeypatch):
        store = _store(monkeypatch)
        job = _Job(asyncio.sleep(0))
        job.status = "done"
        await store.put(job)
        assert await store.cancel(job.id) is False

    @pytest.mark.asyncio
    async def test_cancelling_twice_is_safe(self, monkeypatch):
        store = _store(monkeypatch)
        job = _Job(asyncio.sleep(30))
        await store.put(job)
        assert await store.cancel(job.id) is True
        assert await store.cancel(job.id) is True
        with pytest.raises(asyncio.CancelledError):
            await job.task

    @pytest.mark.asyncio
    async def test_the_watchdog_is_held_so_it_cannot_be_collected(self, monkeypatch):
        """asyncio keeps only a WEAK reference to a task. A fire-and-forget
        watchdog can be garbage collected before its deadline, which would
        remove the backstop silently -- under exactly the load that needs
        it."""
        store = _store(monkeypatch, grace=5.0)
        job = _Job(asyncio.sleep(30))
        await store.put(job)
        await store.cancel(job.id)
        assert len(store._watchdogs) == 1
        job.task.cancel()


# ---------------------------------------- both phases, one account pool


class _Pool:
    """The claim half of sessions/manager, with the real exclusion rule: an
    account handed out is not handed out again until it is released."""

    def __init__(self, ids: list[str]) -> None:
        self.ids = ids
        self.claimed: set[str] = set()

    async def claim(self, wait_s: float = 0.0, poll: float = 0.005):
        deadline = time.monotonic() + wait_s
        while True:
            free = [i for i in self.ids if i not in self.claimed]
            if free:
                self.claimed.add(free[0])
                return free[0]
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(poll)

    def release(self, sid: str) -> None:
        self.claimed.discard(sid)


class TestDiscoveryAndAnalysisTogether:
    """A single-account platform must SEQUENCE the two phases, not refuse
    the second one.

    Modelled on the pool's own claim rule rather than driving both runners,
    because the decision under test is entirely that rule: whether a claim
    that cannot be met right now fails or waits.
    """

    @pytest.mark.asyncio
    async def test_the_second_phase_used_to_fail_outright(self):
        """No wait: the old behaviour, kept as the thing being fixed."""
        pool = _Pool(["fb1"])
        assert await pool.claim() == "fb1"
        assert await pool.claim() is None     # -> ConflictError, in the real path

    @pytest.mark.asyncio
    async def test_with_a_wait_it_queues_behind_the_first(self):
        pool = _Pool(["fb1"])
        first = await pool.claim()
        assert first == "fb1"

        async def finish_first():
            await asyncio.sleep(0.02)
            pool.release(first)

        asyncio.create_task(finish_first())
        assert await pool.claim(wait_s=2.0) == "fb1"

    @pytest.mark.asyncio
    async def test_two_accounts_run_both_phases_at_once_with_no_wait(self):
        """Waiting is the single-account fallback, not the normal path: a
        pool with room hands both phases an account immediately, and never
        the SAME one."""
        pool = _Pool(["fb1", "fb2"])
        a = await pool.claim(wait_s=2.0)
        b = await pool.claim(wait_s=2.0)
        assert a and b and a != b

    @pytest.mark.asyncio
    async def test_waiting_is_bounded_not_forever(self):
        """A pool that never frees up must still give an answer. The real
        path only waits when the blocker is BUSY -- a dead or empty pool
        fails at once with the reason that applies, rather than hanging for
        five minutes and then reporting expired cookies."""
        pool = _Pool(["fb1"])
        await pool.claim()
        started = time.monotonic()
        assert await pool.claim(wait_s=0.05) is None
        assert time.monotonic() - started < 1.0
