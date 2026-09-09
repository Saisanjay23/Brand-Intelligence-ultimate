"""Two complaints that turn out to be the same shape, and both are waiting.

  "results should show      Discovery writes its profiles per completed
   the instant they land"   sweep, so a hit is in MongoDB seconds after it
                            is found. The UI then took up to two more
                            seconds to notice, because a fixed 2s timer was
                            the only thing that told it anything had
                            happened. The row was saved, readable and
                            invisible for longer than it took to save.

  "stop should be smooth"   `cancel` is checked by exactly ONE of the twelve
                            platform engines, so for the other eleven the
                            cooperative path never landed and Stop always
                            fell through to the runner's five-second
                            hard-cancel backstop -- which unwinds a task
                            wherever it stands, mid-navigation. Worse, the
                            longest stretch of any run is `maybe_rest`'s
                            20-60 SECOND pacing nap, and nothing could
                            interrupt it.

Both were a wait nobody could end early. So: the status endpoints hold a
request open until the job actually moves (live_poll), and the pacing layer
every engine shares asks whether the run has been stopped while it waits
(stealth/human).

Timings here are asserted as generous bounds, never as exact durations --
these run on CI as well as a workstation, and the property under test is
"this ends when the thing happens" rather than "this takes exactly N ms".
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.shared import live_poll
from backend.stealth.human import Human


class TestTheWaitEndsWhenTheJobMoves:

    @pytest.mark.asyncio
    async def test_a_first_request_is_never_held(self):
        """A client with no `rev` has nothing to wait for, and holding it
        would put the WHOLE wait window in front of the first paint."""
        started = time.monotonic()
        rev = await live_poll.wait_for_change(lambda: ("a",), rev="", wait_s=30)
        assert rev and time.monotonic() - started < 0.5

    @pytest.mark.asyncio
    async def test_a_stale_rev_is_answered_at_once(self):
        """The change already happened. Holding here would make a client
        that fell behind wait for the NEXT one to catch up on this one."""
        state = ["a"]
        rev = await live_poll.wait_for_change(lambda: tuple(state), rev="", wait_s=0)
        state[0] = "b"
        started = time.monotonic()
        assert await live_poll.wait_for_change(
            lambda: tuple(state), rev=rev, wait_s=30) != rev
        assert time.monotonic() - started < 0.5

    @pytest.mark.asyncio
    async def test_it_returns_as_soon_as_the_state_changes(self):
        """THE POINT OF ALL OF THIS. The answer arrives on the change, not
        on a tick -- so the bound here is the change itself plus a slice,
        nowhere near the wait window that was asked for."""
        state = ["a"]
        rev = live_poll.revision(tuple(state))

        async def change_it():
            await asyncio.sleep(0.3)
            state[0] = "b"

        asyncio.create_task(change_it())
        started = time.monotonic()
        got = await live_poll.wait_for_change(lambda: tuple(state), rev=rev, wait_s=30)
        elapsed = time.monotonic() - started
        assert got != rev
        assert 0.25 < elapsed < 1.5, elapsed

    @pytest.mark.asyncio
    async def test_an_idle_job_holds_the_window_and_then_answers(self):
        """The other half: no change means no early return, which is what
        makes an idle job cost one request per window instead of one every
        two seconds."""
        rev = live_poll.revision(("a",))
        started = time.monotonic()
        assert await live_poll.wait_for_change(
            lambda: ("a",), rev=rev, wait_s=0.5) == rev
        assert time.monotonic() - started >= 0.45

    @pytest.mark.asyncio
    async def test_a_finished_job_is_never_held(self):
        """A terminal job cannot change again. Without this, every poll a
        client makes between the job ending and it noticing would burn the
        full window for an answer that was already final."""
        rev = live_poll.revision(("a",))
        started = time.monotonic()
        assert await live_poll.wait_for_change(
            lambda: ("a",), rev=rev, wait_s=30, is_final=lambda: True) == rev
        assert time.monotonic() - started < 0.5

    @pytest.mark.asyncio
    async def test_a_client_cannot_ask_to_be_held_for_ever(self):
        """The cap is the server's, not the caller's. A request held past
        an intermediary's idle timeout is killed and reaches the client as
        a network error -- worse than the polling this replaces."""
        rev = live_poll.revision(("a",))
        # Not run to completion (that would take MAX_WAIT_S); the guarantee
        # being pinned is that the ceiling exists and is under the 30s these
        # timeouts start at.
        assert 0 < live_poll.MAX_WAIT_S < 30

        task = asyncio.create_task(
            live_poll.wait_for_change(lambda: ("a",), rev=rev, wait_s=9999))
        await asyncio.sleep(0.3)
        assert not task.done()          # genuinely waiting, not spinning
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    def test_the_revision_tracks_the_state_and_nothing_else(self):
        assert live_poll.revision(("a", 1)) == live_poll.revision(("a", 1))
        assert live_poll.revision(("a", 1)) != live_poll.revision(("a", 2))


class TestPacingCanBeInterrupted:
    """`maybe_rest` sleeps for 20-60 seconds and sits on the path of every
    sweep and every analysis visit on every platform. It was a flat
    `asyncio.sleep`, which made it the longest stretch of a run during which
    a stop could not land."""

    @pytest.mark.asyncio
    async def test_a_long_nap_ends_when_the_run_is_stopped(self):
        stop = {"now": False}
        human = Human(stop=lambda: stop["now"])

        async def press_stop():
            await asyncio.sleep(0.3)
            stop["now"] = True

        asyncio.create_task(press_stop())
        started = time.monotonic()
        cut_short = await human.sleep(30.0)
        elapsed = time.monotonic() - started
        assert cut_short is True
        assert elapsed < 2.0, elapsed

    @pytest.mark.asyncio
    async def test_an_already_stopped_run_does_not_wait_at_all(self):
        human = Human(stop=lambda: True)
        started = time.monotonic()
        assert await human.sleep(30.0) is True
        assert time.monotonic() - started < 0.2

    @pytest.mark.asyncio
    async def test_an_uninterrupted_gap_is_still_the_gap_it_asked_for(self):
        """The pacing is the point of the module and is NOT being quietly
        shortened -- a run nobody stopped waits exactly as long as before."""
        human = Human(stop=lambda: False)
        started = time.monotonic()
        assert await human.sleep(0.5) is False
        assert time.monotonic() - started >= 0.45

    @pytest.mark.asyncio
    async def test_with_no_stop_wired_up_it_behaves_exactly_as_before(self):
        """`stop` is optional, and a `Human` built without one must be the
        Human this module always had."""
        human = Human()
        assert human.stopping() is False
        started = time.monotonic()
        assert await human.sleep(0.3) is False
        assert time.monotonic() - started >= 0.25

    @pytest.mark.asyncio
    async def test_a_broken_stop_predicate_cannot_fail_a_sweep(self):
        """Pacing must never be able to take a run down. "No" is the answer
        that preserves the behaviour this module had before `stop` existed.
        """
        def boom() -> bool:
            raise RuntimeError("something upstream is wrong")

        human = Human(stop=boom)
        assert human.stopping() is False
        assert await human.sleep(0.1) is False

    @pytest.mark.asyncio
    async def test_a_rest_cut_short_is_not_reported_as_a_rest_taken(self):
        """The caller logs this number, and an analyst reading the log to
        work out why a stop took as long as it did is entitled to have it
        be true."""
        human = Human(stop=lambda: True)
        human.actions = 0
        assert await human.maybe_rest() == 0.0


# ---------------------------------- what actually counts as "it changed"


class TestTheFingerprintWakesOnProgressAndNothingElse:
    """The fingerprint decides when a held request returns, so it has two
    failure modes and they are opposites.

    Too WIDE and the feature evaporates: a field derived from the clock
    differs on every read, every wait returns instantly, and this is a
    2-millisecond poll loop wearing a long poll's clothes. Too NARROW and
    real progress goes unnoticed until the window expires, which is slower
    than the polling it replaced.
    """

    def _job(self):
        from backend.discovery.runner import RUNNING, DiscoveryJob, PlatformSweep

        job = DiscoveryJob(id="j1", group_id="acme", keyword_plan=[])
        job.status = RUNNING
        job.started_at_ts = time.time() - 5
        job.platforms["facebook"] = PlatformSweep(
            platform="facebook", display_name="Facebook",
            status="running", keywords_total=3,
        )
        return job

    def test_a_job_that_did_nothing_reads_as_unchanged(self):
        """THE ONE THAT MATTERS MOST. `elapsed_seconds` is recomputed from
        `time.time()` on every snapshot, so a fingerprint taken over the
        rendered payload would differ on every single read and no request
        would ever be held. Real time is allowed to pass here on purpose --
        that is the whole point of the assertion."""
        from backend.api.discovery import _sweep_fingerprint

        job = self._job()
        before = _sweep_fingerprint(job)
        elapsed_before = job.to_dict()["elapsed_seconds"]

        time.sleep(0.25)

        # The clock really did move underneath it -- so this is proving the
        # exclusion, not just a job that happened to sit still.
        assert job.to_dict()["elapsed_seconds"] > elapsed_before
        assert _sweep_fingerprint(job) == before

    @pytest.mark.parametrize("change", [
        pytest.param(lambda j: setattr(j, "found", j.found + 1), id="a hit was saved"),
        pytest.param(lambda j: setattr(j, "new", j.new + 1), id="the hit was new"),
        pytest.param(lambda j: setattr(j, "status", "done"), id="the sweep ended"),
        pytest.param(lambda j: setattr(j, "message", "boom"), id="the message changed"),
        pytest.param(
            lambda j: j.platforms["facebook"].__setattr__("keywords_done", 1),
            id="a keyword completed"),
        pytest.param(
            lambda j: j.platforms["facebook"].__setattr__("current_keyword", "acme ltd"),
            id="a new keyword started"),
        pytest.param(
            lambda j: j.platforms["facebook"].__setattr__("current_step", "Searching..."),
            id="the live step changed"),
        pytest.param(
            lambda j: j.platforms["facebook"].__setattr__("workers", 2),
            id="a second session joined"),
        pytest.param(
            lambda j: j.platforms["facebook"].__setattr__("status", "failed"),
            id="the platform failed"),
    ])
    def test_every_visible_move_wakes_a_waiting_client(self, change):
        from backend.api.discovery import _sweep_fingerprint

        job = self._job()
        before = _sweep_fingerprint(job)
        change(job)
        assert _sweep_fingerprint(job) != before

    def test_a_completed_sweep_landing_in_the_history_wakes_it(self):
        """History is compared by LENGTH -- entries are appended, never
        edited -- so this pins that the cheap comparison is still enough."""
        from backend.api.discovery import _sweep_fingerprint
        from backend.discovery.runner import CompletedSweep

        job = self._job()
        before = _sweep_fingerprint(job)
        job.history.append(CompletedSweep(
            platform="facebook", display_name="Facebook", keyword="acme",
            tab="people", duration_seconds=1.0, hits_found=2, hits_new=1,
            timestamp="12:00:00"))
        assert _sweep_fingerprint(job) != before
