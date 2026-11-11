"""Instagram and Threads as ONE account, and an hourly ceiling per account.

Threads signs in with an Instagram account. The tool kept them in separate
pools with separate pacing, so Meta saw one account and the tool saw two:
a warning on Instagram left Threads working at full speed on it, and both
could run at once -- one account on two devices. Pinned here:

  * one pacing governor for both platforms, whoever pushed back;
  * a session is held back while the SAME account (equal `ds_user_id`)
    is claimed on the other platform -- and only then;
  * a throttle or challenge on one rests the same account on the other;
  * an hourly ceiling per account that makes work wait, never skips it.

No Mongo and no browser: a dict-backed pool exercises the real claim code.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.config.settings import settings
from backend.sessions import manager as M
from backend.shared import account_budget as AB
from backend.shared import meta_account as MA
from backend.shared import pushback as pb


def _cookies(uid):
    return [{"name": "sessionid", "value": "x"}] + (
        [{"name": "ds_user_id", "value": uid}] if uid else [])


class _Pools:
    """Just enough of session_repository, for two platforms."""

    def __init__(self, ig_uid="111", th_uid="111"):
        def item(i, uid):
            return {"id": i, "identifier": i, "status": "ready",
                    "rate_limited_until": 0.0, "last_used": 0.0, "use_count": 0,
                    "consecutive_failures": 0, "cookies": _cookies(uid), "last_ok": 0.0}
        self.pools = {"instagram": [item("ig1", ig_uid)], "threads": [item("th1", th_uid)]}
        self.updates: list[tuple[str, str, dict]] = []

    async def list_pool(self, platform):
        return [dict(s) for s in self.pools.get(platform, [])]

    async def get_item(self, platform, session_id):
        return next((dict(s) for s in self.pools.get(platform, []) if s["id"] == session_id), None)

    async def update_item(self, platform, session_id, **fields):
        self.updates.append((platform, session_id, fields))
        for s in self.pools.get(platform, []):
            if s["id"] == session_id:
                s.update(fields)
                return True
        return False

    async def increment_use_count(self, platform, session_id):
        return 1


@pytest.fixture
def pools(monkeypatch):
    p = _Pools()
    monkeypatch.setattr(M, "sessions_db", p)
    monkeypatch.setattr(M, "_claims", {})
    return p


# ---------------------------------------------------------------- identity

class TestAccountKey:
    def test_same_numeric_id_is_the_same_account(self):
        assert MA.account_key("instagram", _cookies("42")) == MA.account_key("threads", _cookies("42"))
        assert MA.account_key("instagram", _cookies("42")) == "meta:42"

    def test_no_id_or_another_platform_links_to_nothing(self):
        assert MA.account_key("instagram", _cookies("")) == ""
        assert MA.account_key("instagram", [{"name": "ds_user_id", "value": "abc"}]) == ""
        assert MA.account_key("facebook", _cookies("42")) == ""

    def test_budget_key_falls_back_to_the_session(self):
        assert MA.budget_key("instagram", "s1", _cookies("")) == "instagram:s1"
        assert MA.budget_key("threads", "s1", _cookies("42")) == "meta:42"


# --------------------------------------------------------------- governor

class TestOneGovernor:
    def test_threads_and_instagram_share_it(self):
        assert pb.governor_for("threads") is pb.governor_for("instagram")

    def test_a_throttle_on_threads_slows_instagram(self):
        pb.note_response("threads", 429)
        assert pb.governor_for("instagram").gap_multiplier() > 1.0

    def test_other_platforms_keep_their_own(self):
        assert pb.governor_for("facebook") is not pb.governor_for("instagram")


# ------------------------------------------------------------------ claims

class TestNeverBothAtOnce:
    @pytest.mark.asyncio
    async def test_the_same_account_is_held_back_on_the_other_platform(self, pools):
        assert (await M._claim_one("instagram"))["id"] == "ig1"
        assert await M._claim_one("threads") is None
        assert await M._linked_only("threads")
        assert "same Instagram account" in await M.unavailable_reason("threads")

    @pytest.mark.asyncio
    async def test_it_comes_free_when_the_other_job_lets_go(self, pools):
        await M._claim_one("instagram")
        M.release_claim("instagram", "ig1")
        assert (await M._claim_one("threads"))["id"] == "th1"

    @pytest.mark.asyncio
    async def test_different_accounts_run_side_by_side(self, monkeypatch):
        monkeypatch.setattr(M, "sessions_db", _Pools(ig_uid="111", th_uid="222"))
        monkeypatch.setattr(M, "_claims", {})
        assert await M._claim_one("instagram") is not None
        assert await M._claim_one("threads") is not None

    @pytest.mark.asyncio
    async def test_a_session_with_no_account_id_is_never_held_back(self, monkeypatch):
        monkeypatch.setattr(M, "sessions_db", _Pools(ig_uid="111", th_uid=""))
        monkeypatch.setattr(M, "_claims", {})
        await M._claim_one("instagram")
        assert await M._claim_one("threads") is not None

    @pytest.mark.asyncio
    async def test_a_job_waits_for_the_linked_one_instead_of_failing(self, pools, monkeypatch):
        """The ordinary wait gives up after a few minutes; a linked-account
        wait lasts as long as the other job may, and ends the moment it lets
        go."""
        monkeypatch.setattr(M, "_SESSION_POLL_S", 0.01)
        monkeypatch.setattr(settings, "meta_linked_wait_minutes", 10.0)
        await M._claim_one("instagram")

        async def let_go():
            await asyncio.sleep(0.05)
            M.release_claim("instagram", "ig1")
        asyncio.get_running_loop().create_task(let_go())
        got = await asyncio.wait_for(M.get_healthy_session("threads", wait_s=0.01), timeout=2)
        assert got is not None and got["id"] == "th1"


# -------------------------------------------------------------------- rest

class TestRestTheLinkedAccount:
    @pytest.fixture(autouse=True)
    def _quiet_alerts(self, monkeypatch):
        """A checkpoint raises an incident and an email; neither is the
        subject here."""
        from backend.database.repositories import incident_repository
        from backend.services import email_service

        async def nothing(*a, **k):
            return None
        monkeypatch.setattr(incident_repository, "record", nothing)
        monkeypatch.setattr(email_service, "send_session_failure_alert", nothing)

    @pytest.mark.asyncio
    async def test_a_throttle_rests_the_other_site_as_long(self, pools):
        await M.mark_session_failed("instagram", "ig1", "rate_limited")
        ig = pools.pools["instagram"][0]
        th = pools.pools["threads"][0]
        assert th["status"] == "rate_limited"
        assert th["rate_limited_until"] == ig["rate_limited_until"]

    @pytest.mark.asyncio
    async def test_a_challenge_rests_the_other_site_for_a_day(self, pools, monkeypatch):
        monkeypatch.setattr(settings, "meta_linked_checkpoint_rest_hours", 24.0)
        await M.mark_session_failed("threads", "th1", "checkpointed")
        ig = pools.pools["instagram"][0]
        assert ig["status"] == "rate_limited"           # resting, not dead
        assert ig["rate_limited_until"] >= M._now() + 23.9 * 3600

    @pytest.mark.asyncio
    async def test_expired_cookies_are_not_an_account_verdict(self, pools):
        await M.mark_session_failed("instagram", "ig1", "expired")
        assert pools.pools["threads"][0]["status"] == "ready"

    @pytest.mark.asyncio
    async def test_a_different_account_is_left_alone(self, monkeypatch):
        p = _Pools(ig_uid="111", th_uid="222")
        monkeypatch.setattr(M, "sessions_db", p)
        await M.mark_session_failed("instagram", "ig1", "rate_limited")
        assert p.pools["threads"][0]["status"] == "ready"


# ----------------------------------------------------------- hourly ceiling

class TestHourlyBudget:
    def _budget(self):
        now = [0.0]
        return AB.HourlyBudget(clock=lambda: now[0]), now

    def test_fits_until_the_ceiling_then_waits_for_the_oldest(self):
        b, now = self._budget()
        for t in (0, 100, 200):
            now[0] = t
            assert b.wait_s("a", AB.SEARCH, 3) == 0
            b.record("a", AB.SEARCH)
        now[0] = 300
        assert b.wait_s("a", AB.SEARCH, 3) == pytest.approx(3300)   # 0 + 3600 - 300
        now[0] = 3600
        assert b.wait_s("a", AB.SEARCH, 3) == 0

    def test_kinds_and_accounts_are_counted_apart(self):
        b, _ = self._budget()
        b.record("a", AB.SEARCH)
        assert b.wait_s("a", AB.PROFILE, 1) == 0
        assert b.wait_s("b", AB.SEARCH, 1) == 0
        assert b.wait_s("a", AB.SEARCH, 1) > 0

    def test_zero_means_no_ceiling(self):
        b, _ = self._budget()
        for _ in range(5):
            b.record("a", AB.SEARCH)
        assert b.wait_s("a", AB.SEARCH, 0) == 0

    def test_only_instagram_and_threads_have_one(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_searches_per_hour", 40)
        monkeypatch.setattr(settings, "instagram_profile_visits_per_hour", 60)
        assert AB.limit_for("instagram", AB.SEARCH) == 40
        assert AB.limit_for("threads", AB.PROFILE) == 60
        assert AB.limit_for("facebook", AB.SEARCH) == 0

    @pytest.mark.asyncio
    async def test_instagram_and_threads_on_one_account_share_the_count(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_searches_per_hour", 2)
        same = {"id": "x", "cookies": _cookies("42")}
        stop = lambda: False                                   # noqa: E731
        assert await AB.hold("instagram", same, AB.SEARCH, stop)
        assert await AB.hold("threads", {**same, "id": "y"}, AB.SEARCH, stop)
        assert AB.budget().wait_s("meta:42", AB.SEARCH, 2) > 0

    @pytest.mark.asyncio
    async def test_a_stop_ends_the_wait_and_counts_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_searches_per_hour", 1)
        acct = {"id": "x", "cookies": _cookies("42")}
        assert await AB.hold("instagram", acct, AB.SEARCH, lambda: False)
        stopped = await asyncio.wait_for(
            AB.hold("instagram", acct, AB.SEARCH, lambda: True), timeout=2)
        assert stopped is False
        assert AB.budget().used("meta:42", AB.SEARCH) == 1
