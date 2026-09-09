"""Pressing Stop must not cost you the account.

THE COMPLAINT. "Run discovery, stop it half-way, run it again, and the
platform reports `no healthy sessions available` even though the cookies
are right there." It was reproducible, it survived every retry, and the
only cure was restarting the backend.

THE CAUSE. A session claimed for a job was recorded in one module-level
set and removed from it by exactly one thing: `release_claim`, called from
a caller's `finally`. `JobStore.cancel` hard-cancels the job's task five
seconds after Stop, and a task cancelled at an `await` that sits BEFORE
its own try/finally never runs that `finally`. Both runners have such an
await -- the process-wide worker slot, discovery's per-worker start
stagger, and the up-to-five-minute busy wait inside `_claim_sessions`.
The entry then stayed in the set for the life of the PROCESS: the row was
still `ready` in Mongo, the Sessions panel still showed a green light, and
every later job was refused against a pool that was entirely healthy.

WHAT IS PINNED HERE. A claim is a lease with a holder, so the account
comes back when that holder ends, whatever ended it -- and, just as
importantly, does NOT come back while the holder is still running. Both
halves matter: handing one account to two jobs at once opens two browser
contexts on one IP, which sessions/manager.py's own health notes call the
most reliable way to earn a checkpoint. Reclaiming late is cheap;
reclaiming early is the failure being avoided.

No Mongo and no browser -- the decision under test is entirely the claim
rule, so a dict-backed pool exercises the real code path.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.analysis import runner as AR
from backend.discovery import runner as DR
from backend.sessions import manager as M

PLATFORM = "facebook"


class _FakePool:
    """Just enough of session_repository for the claim path."""

    def __init__(self, ids):
        self.items = [
            {"id": i, "identifier": i + "@example.com", "status": "ready",
             "rate_limited_until": 0.0, "last_used": 0.0, "use_count": 0,
             "cookies": [{"name": "c_user", "value": "1"}], "last_ok": 0.0}
            for i in ids
        ]

    async def list_pool(self, platform):
        return [dict(s) for s in self.items]

    async def update_item(self, platform, session_id, **fields):
        for s in self.items:
            if s["id"] == session_id:
                s.update(fields)
                return True
        return False

    async def increment_use_count(self, platform, session_id):
        for s in self.items:
            if s["id"] == session_id:
                s["use_count"] += 1
                return s["use_count"]
        return 0


@pytest.fixture
def two_accounts(monkeypatch):
    """A healthy two-account facebook pool, and a claim table that starts
    empty and cannot leak into the next test."""
    monkeypatch.setattr(M, "sessions_db", _FakePool(["fb1", "fb2"]))
    monkeypatch.setattr(M, "_claims", {})


@pytest.fixture
def one_account(monkeypatch):
    monkeypatch.setattr(M, "sessions_db", _FakePool(["fb1"]))
    monkeypatch.setattr(M, "_claims", {})


async def _hold_forever(claimed):
    """A job that takes an account and keeps running -- the case where
    reclaiming its lease would be WRONG."""
    assert await M.get_healthy_session(PLATFORM) is not None
    claimed.set()
    await asyncio.sleep(3600)


async def _stopped_holder():
    """One account taken by a job that is then stopped the way JobStore's
    hard cancel stops it: no `finally`, no release."""
    claimed = asyncio.Event()
    holder = asyncio.create_task(_hold_forever(claimed))
    await claimed.wait()
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder


class TestAStoppedJobGivesItsAccountBack:

    @pytest.mark.asyncio
    async def test_a_cancelled_holder_frees_its_claim(self, two_accounts):
        """THE REGRESSION. This used to hand back one account and then
        None forever: two healthy accounts, one of them claimed by a task
        that no longer exists, and no way back short of a restart."""
        await _stopped_holder()

        assert await M.get_healthy_session(PLATFORM) is not None
        assert await M.get_healthy_session(PLATFORM) is not None

    @pytest.mark.asyncio
    async def test_the_reason_shown_agrees_with_the_pool(self, two_accounts):
        """The message an analyst acts on has to match what the pool can
        actually do. A stopped job's account is not "in use by another
        running job", and telling someone to add cookies for it means
        re-pasting credentials that were never the problem."""
        await _stopped_holder()

        assert await M.get_healthy_session(PLATFORM) is not None
        assert await M.get_healthy_session(PLATFORM) is not None
        why = await M.unavailable_reason(PLATFORM)
        assert "2 of 2" in why and "already in use" in why

    @pytest.mark.asyncio
    async def test_waiting_does_not_hang_on_a_dead_holder(self, one_account):
        """`_busy_only` decides whether waiting can possibly succeed. A
        lease whose holder is gone is not a busy pool -- the account is
        free now, so the wait ends on the first look rather than five
        minutes later."""
        await _stopped_holder()

        got = await asyncio.wait_for(
            M.get_healthy_session(PLATFORM, wait_s=30.0), timeout=2.0)
        assert got is not None and got["id"] == "fb1"


class TestALiveHolderKeepsItsAccount:
    """The other half. A lease must not expire under a job that is still
    running, or one account ends up driving two browser contexts at once."""

    @pytest.mark.asyncio
    async def test_a_running_holder_is_never_reclaimed(self, two_accounts):
        claimed = asyncio.Event()
        holder = asyncio.create_task(_hold_forever(claimed))
        await claimed.wait()
        try:
            second = await M.get_healthy_session(PLATFORM)
            assert second is not None
            # Pool exhausted while the first holder is alive and well.
            assert await M.get_healthy_session(PLATFORM) is None
        finally:
            holder.cancel()

    @pytest.mark.asyncio
    async def test_two_claims_are_never_the_same_account(self, two_accounts):
        a = await M.get_healthy_session(PLATFORM)
        b = await M.get_healthy_session(PLATFORM)
        assert a and b and a["id"] != b["id"]

    @pytest.mark.asyncio
    async def test_releasing_hands_it_straight_back(self, two_accounts):
        a = await M.get_healthy_session(PLATFORM)
        b = await M.get_healthy_session(PLATFORM)
        assert await M.get_healthy_session(PLATFORM) is None
        M.release_claim(PLATFORM, a["id"])
        again = await M.get_healthy_session(PLATFORM)
        assert again["id"] == a["id"]
        M.release_claim(PLATFORM, b["id"])
        M.release_claim(PLATFORM, b["id"])          # idempotent
        M.release_claim(PLATFORM, "")               # anonymous, no-op


class TestStoppingMidClaimStrandsNothing:
    """`_claim_sessions` takes accounts one at a time and waits up to
    SESSION_WAIT_S for the first. A cancel landing inside that loop used to
    unwind past everything already claimed, and those never reached a
    worker -- so no worker `finally` was ever going to release them.

    Called unbound with a `None` self on purpose: neither runner's copy
    touches instance state, and driving a whole job would be testing
    scheduling rather than the claim bookkeeping this is about.
    """

    @pytest.mark.parametrize("runner", [DR.DiscoveryRunner, AR.AnalysisRunner])
    @pytest.mark.asyncio
    async def test_a_cancel_between_claims_releases_the_first(
        self, two_accounts, runner,
    ):
        started = asyncio.Event()
        real = M.session_for_job
        calls = {"n": 0}

        async def park_on_the_second(platform_id, *, wait_s=0.0):
            calls["n"] += 1
            if calls["n"] > 1:
                started.set()
                await asyncio.sleep(3600)   # where the stop lands
            return await real(platform_id, wait_s=wait_s)

        M.session_for_job = park_on_the_second
        try:
            task = asyncio.create_task(
                runner._claim_sessions(None, PLATFORM, 2))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            M.session_for_job = real

        # The account the cancelled loop had already taken is free RIGHT
        # NOW -- not merely reclaimable once its holder is collected.
        assert M._claims == {}
        assert await M.get_healthy_session(PLATFORM) is not None
        assert await M.get_healthy_session(PLATFORM) is not None


# --------------------------------------------- stopping is not a verdict


class _Boom:
    """A scraper whose page visit reports a genuinely session-shaped
    failure -- the platform itself saying the account is challenged, which
    is the strongest verdict `classify_failure` recognises."""

    async def one(self, *a, **kw):
        raise RuntimeError("checkpoint required -- verify your identity")


class _TimedOutOnALoginRedirect:
    """The other shape, and the one that used to burn healthy accounts: a
    Playwright timeout still quoting the URL it was navigating to. On
    Facebook an interrupted request very often ends up at `/login/`, and
    the token match read that URL as proof the session was expired."""

    async def one(self, *a, **kw):
        raise TimeoutError(
            'Timeout 30000ms exceeded. navigating to '
            '"https://www.facebook.com/login/?next=%2Fsomeprofile"')


class TestStoppingDoesNotQuarantineTheAccount:
    """The OTHER way a stop used to cost you a platform.

    A cancel tears the browser down under whatever page is in flight, and
    what comes back is platform-shaped error text rather than a clean
    CancelledError. Classified, that reads as a dead session, and
    `mark_session_failed` then puts a healthy account into the graduated
    15m/1h/6h/24h cooldown. The analyst pressed Stop and got a pool that
    reports itself expired -- the same complaint, from the other side.
    """

    def _job_and_item(self):
        job = AR.AnalysisJob(id="j1", target_name="acme", total=1)
        it = AR.AnalysisItem(
            id="i1", raw_url="https://facebook.com/x",
            url="https://facebook.com/x", platform=PLATFORM, entity_id="x")
        job.items = [it]
        job.platform_progress[PLATFORM] = {
            "status": "running", "total": 1, "completed": 0}
        return job, it

    async def _run(self, monkeypatch, cancelled, scraper=None):
        runner = AR.AnalysisRunner()
        marked = []

        async def fake_mark(platform_id, session_id, reason="expired", **kw):
            marked.append((session_id, reason))

        async def fake_fail(job, it, error):
            it.status, it.error = "error", error

        monkeypatch.setattr(AR.sessions_engine, "mark_session_failed", fake_mark)
        monkeypatch.setattr(runner, "_fail_item", fake_fail)

        job, it = self._job_and_item()
        if cancelled:
            job.cancel.set()
        fatal = await runner._scrape_one(
            job, it, scraper or _Boom(), PLATFORM, {"id": "fb1"})
        return marked, fatal, it

    @pytest.mark.asyncio
    async def test_a_stop_leaves_the_session_alone(self, monkeypatch):
        marked, fatal, it = await self._run(monkeypatch, cancelled=True)
        assert marked == []          # not quarantined
        assert fatal is False        # and not counted as a session death
        assert it.status == "error"  # the URL still records what happened

    @pytest.mark.asyncio
    async def test_the_same_failure_uncancelled_still_quarantines(self, monkeypatch):
        """The guard must be about the STOP, not about the error: an
        identical failure in a job nobody stopped is a real dead session and
        has to be marked, or a genuinely expired account keeps being handed
        out."""
        marked, fatal, _ = await self._run(monkeypatch, cancelled=False)
        assert marked == [("fb1", "checkpointed")]
        assert fatal is True

    @pytest.mark.asyncio
    async def test_a_timeout_quoting_a_login_url_is_not_a_verdict(self, monkeypatch):
        """THE OTHER SOURCE OF A DEAD-LOOKING POOL. Nobody stopped this
        job -- the page simply did not load in time, and the error text
        quoted the URL it was navigating to. Reading `login` out of that
        URL put a healthy account into a 15m-to-24h cooldown for a network
        blip, and enough of those is a platform reporting every account
        expired. The URL is dropped before the tokens are matched, so this
        is now recorded against the item and nothing else."""
        marked, fatal, it = await self._run(
            monkeypatch, cancelled=False, scraper=_TimedOutOnALoginRedirect())
        assert marked == []
        assert fatal is False
        assert it.status == "error"


# ------------------------------------- "is anything using this account?"


class TestInUseIsAnsweredFromTheClaim:
    """`_session_in_use` gates the two things that open a SECOND browser on
    an account: the background health monitor's probe, and the analyst's
    per-session Check button.

    It used to ask only the runners' `hold_session` bookkeeping, which a
    worker records after it has waited out the global slot semaphore and
    its start stagger. The account is reserved well before that -- a
    platform claims every account it wants up front, then starts workers
    one at a time -- so there was a window, minutes wide under load, where
    a reserved account read as idle and the 30-minute monitor could open a
    second Playwright context on it. A challenge, a checkpointed session,
    and a pool that reports itself dead: the same complaint again, from a
    third direction.
    """

    @pytest.mark.asyncio
    async def test_a_claimed_account_reads_as_in_use(self, two_accounts):
        claimed = asyncio.Event()
        holder = asyncio.create_task(_hold_forever(claimed))
        await claimed.wait()
        try:
            # Claimed but NOT yet held -- no worker has called hold_session,
            # which is exactly the window that used to read as idle.
            assert M._session_in_use(PLATFORM, "fb1") is True
            assert M._session_in_use(PLATFORM, "fb2") is False
        finally:
            holder.cancel()

    @pytest.mark.asyncio
    async def test_a_released_account_reads_as_idle_again(self, two_accounts):
        got = await M.get_healthy_session(PLATFORM)
        assert M._session_in_use(PLATFORM, got["id"]) is True
        M.release_claim(PLATFORM, got["id"])
        assert M._session_in_use(PLATFORM, got["id"]) is False

    @pytest.mark.asyncio
    async def test_a_stopped_jobs_account_does_not_stay_locked_out(self, two_accounts):
        """The monitor must get its account BACK. Answering "in use" from a
        claim would be a bad trade if a leaked claim could hide a session
        from health checks for ever -- so this is the lease doing its other
        job."""
        await _stopped_holder()
        assert M._session_in_use(PLATFORM, "fb1") is False
