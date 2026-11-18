"""The DAILY ceiling per Instagram account, and the warm-up for new ones.

Added after the pool's only Instagram account was suspended (2026-11-16).
The hourly ceiling alone still allowed a full working day of nonstop
activity on one account; these pin down what the daily one guarantees:

  1. At the ceiling the work does NOT wait a day inside the sweep -- it
     raises, so the runner can rest that account and leave the rest owed.
  2. Instagram and Threads on one account share one daily count.
  3. A newly added account gets a fraction of the limit, rising to full
     over the warm-up; an account with no `added_at` is established.
  4. Platforms without an Instagram login, and anonymous browsers, have
     no daily ceiling at all.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.config.settings import settings
from backend.shared import account_budget as AB


def _cookies(uid: str) -> list[dict]:
    return [{"name": "ds_user_id", "value": uid}, {"name": "sessionid", "value": "s"}]


NEVER = lambda: False                                          # noqa: E731


@pytest.fixture
def small_day(monkeypatch):
    monkeypatch.setattr(settings, "instagram_searches_per_hour", 0)
    monkeypatch.setattr(settings, "instagram_profile_visits_per_hour", 0)
    monkeypatch.setattr(settings, "instagram_searches_per_day", 3)
    monkeypatch.setattr(settings, "instagram_profile_visits_per_day", 2)
    monkeypatch.setattr(settings, "instagram_warmup_days", 14)


class TestDailyCeiling:
    @pytest.mark.asyncio
    async def test_the_ceiling_raises_instead_of_waiting_a_day(self, small_day):
        acct = {"id": "x", "cookies": _cookies("7")}
        for _ in range(3):
            assert await AB.hold("instagram", acct, AB.SEARCH, NEVER)
        with pytest.raises(AB.DailyLimitReached) as exc:
            await asyncio.wait_for(AB.hold("instagram", acct, AB.SEARCH, NEVER), timeout=2)
        e = exc.value
        assert e.used == 3 and e.limit == 3
        assert e.resets_at > 0
        # The words an analyst reads must say it is a limit, not a failure,
        # and that the rest is owed -- never that it was searched.
        assert "daily limit reached" in str(e)
        assert "owed" in str(e)

    @pytest.mark.asyncio
    async def test_kinds_are_counted_separately(self, small_day):
        acct = {"id": "x", "cookies": _cookies("7")}
        for _ in range(3):
            await AB.hold("instagram", acct, AB.SEARCH, NEVER)
        # searches spent; profile visits still have their own allowance
        assert await AB.hold("instagram", acct, AB.PROFILE, NEVER)

    @pytest.mark.asyncio
    async def test_instagram_and_threads_on_one_account_share_the_day(self, small_day):
        ig = {"id": "a", "cookies": _cookies("42")}
        th = {"id": "b", "cookies": _cookies("42")}
        await AB.hold("instagram", ig, AB.SEARCH, NEVER)
        await AB.hold("threads", th, AB.SEARCH, NEVER)
        await AB.hold("instagram", ig, AB.SEARCH, NEVER)
        with pytest.raises(AB.DailyLimitReached):
            await AB.hold("threads", th, AB.SEARCH, NEVER)

    @pytest.mark.asyncio
    async def test_other_accounts_are_unaffected(self, small_day):
        a = {"id": "a", "cookies": _cookies("1")}
        b = {"id": "b", "cookies": _cookies("2")}
        for _ in range(3):
            await AB.hold("instagram", a, AB.SEARCH, NEVER)
        assert await AB.hold("instagram", b, AB.SEARCH, NEVER)

    @pytest.mark.asyncio
    async def test_a_stop_while_waiting_counts_nothing_against_the_day(self, monkeypatch, small_day):
        monkeypatch.setattr(settings, "instagram_searches_per_hour", 1)
        acct = {"id": "x", "cookies": _cookies("9")}
        assert await AB.hold("instagram", acct, AB.SEARCH, NEVER)
        assert await asyncio.wait_for(
            AB.hold("instagram", acct, AB.SEARCH, lambda: True), timeout=2) is False
        assert await AB.daily_used("instagram", acct, AB.SEARCH) == 1

    def test_no_daily_ceiling_off_instagram_or_for_an_anonymous_browser(self, small_day):
        assert AB.daily_limit_for("facebook", AB.SEARCH, {"id": "x"}) == 0
        assert AB.daily_limit_for("instagram", AB.SEARCH, {"anonymous": True}) == 0
        assert AB.daily_limit_for("instagram", AB.SEARCH, {"id": "x"}) == 3

    def test_zero_turns_the_daily_ceiling_off(self, monkeypatch, small_day):
        monkeypatch.setattr(settings, "instagram_searches_per_day", 0)
        assert AB.daily_limit_for("instagram", AB.SEARCH, {"id": "x"}) == 0

    def test_the_day_resets_at_utc_midnight(self):
        assert AB.next_reset(0) == 86400.0
        assert AB.next_reset(86399.0) == 86400.0
        assert AB.next_reset(86400.0) == 172800.0


class TestWarmUp:
    def test_an_established_account_gets_the_full_limit(self, small_day):
        assert AB.warmup_factor({"id": "x"}) == 1.0
        assert AB.warmup_factor({"id": "x", "added_at": 0}) == 1.0

    def test_a_brand_new_account_starts_at_a_quarter(self, monkeypatch, small_day):
        monkeypatch.setattr(settings, "instagram_searches_per_day", 100)
        now = 10_000_000.0
        item = {"id": "x", "added_at": now}
        assert AB.warmup_factor(item, now) == pytest.approx(0.25)
        assert AB.daily_limit_for("instagram", AB.SEARCH, item, now) == 25

    def test_it_rises_to_full_by_the_end_of_the_warm_up(self, monkeypatch, small_day):
        monkeypatch.setattr(settings, "instagram_searches_per_day", 100)
        added = 10_000_000.0
        item = {"id": "x", "added_at": added}
        half = AB.daily_limit_for("instagram", AB.SEARCH, item, added + 7 * 86400)
        full = AB.daily_limit_for("instagram", AB.SEARCH, item, added + 14 * 86400)
        later = AB.daily_limit_for("instagram", AB.SEARCH, item, added + 60 * 86400)
        assert 25 < half < 100
        assert full == 100 and later == 100

    def test_never_zero_while_a_limit_is_set(self, monkeypatch, small_day):
        monkeypatch.setattr(settings, "instagram_searches_per_day", 1)
        now = 10_000_000.0
        assert AB.daily_limit_for("instagram", AB.SEARCH, {"id": "x", "added_at": now}, now) == 1

    def test_warm_up_can_be_turned_off(self, monkeypatch, small_day):
        monkeypatch.setattr(settings, "instagram_warmup_days", 0)
        now = 10_000_000.0
        assert AB.warmup_factor({"id": "x", "added_at": now}, now) == 1.0


class TestInstagramLoginChecksAreRarer:
    """The Instagram login check is the account opening its own settings
    page; the pool's account was suspended at exactly that check."""

    def test_a_job_skips_the_check_for_six_hours_on_instagram_only(self, monkeypatch):
        from backend.sessions import manager as M
        monkeypatch.setattr(settings, "meta_proven_fresh_hours", 6.0)
        four_h_ago = M._now() - 4 * 3600
        assert M.proven_fresh({"platform": "instagram", "last_ok": four_h_ago})
        assert M.proven_fresh({"platform": "threads", "last_ok": four_h_ago})
        assert not M.proven_fresh({"platform": "facebook", "last_ok": four_h_ago})
        assert not M.proven_fresh({"platform": "instagram", "last_ok": M._now() - 7 * 3600})
        assert not M.proven_fresh({"platform": "instagram", "last_ok": 0})

    def test_an_idle_instagram_account_is_checked_at_most_daily(self, monkeypatch):
        from backend.sessions import manager as M
        monkeypatch.setattr(settings, "session_idle_recheck_hours", 8.0)
        monkeypatch.setattr(settings, "meta_idle_recheck_hours", 24.0)
        assert M._idle_recheck_s("instagram") == 24 * 3600
        assert M._idle_recheck_s("facebook") == 8 * 3600


class _Pools:
    """Just enough of session_repository for two platforms."""

    def __init__(self, ig_uid="111", th_uid="111"):
        def item(i, uid):
            return {"id": i, "identifier": i, "status": "ready",
                    "rate_limited_until": 0.0, "last_used": 0.0, "use_count": 0,
                    "consecutive_failures": 0, "cookies": _cookies(uid), "last_ok": 0.0}
        self.pools = {"instagram": [item("ig1", ig_uid), item("ig2", "999")],
                      "threads": [item("th1", th_uid)]}

    async def list_pool(self, platform):
        return [dict(s) for s in self.pools.get(platform, [])]

    async def get_item(self, platform, session_id):
        return next((dict(s) for s in self.pools.get(platform, []) if s["id"] == session_id), None)

    async def update_item(self, platform, session_id, **fields):
        for s in self.pools.get(platform, []):
            if s["id"] == session_id:
                s.update(fields)
                return True
        return False


class TestRestingAnAccountThatHitItsDailyLimit:
    @pytest.mark.asyncio
    async def test_it_rests_without_counting_as_a_failure_and_the_next_account_is_free(
        self, monkeypatch,
    ):
        import time
        from backend.sessions import manager as M
        pools = _Pools()
        monkeypatch.setattr(M, "sessions_db", pools)
        until = time.time() + 3600
        await M.rest_session_until("instagram", "ig1", until, "daily limit reached")

        ig1 = (await pools.get_item("instagram", "ig1"))
        assert ig1["rate_limited_until"] == until
        # NOT a failure: nothing is wrong with the account, so no failure
        # is added (that would climb the quarantine ladder) and it is not dead.
        assert ig1["consecutive_failures"] == 0
        assert ig1["status"] not in M.DEAD_STATES
        # unavailable now, available again once the time passes
        assert not M._is_available(ig1, time.time())
        assert M._is_available(ig1, until + 1)
        # the OTHER Instagram account is untouched and free to take over
        assert M._is_available(await pools.get_item("instagram", "ig2"), time.time())

    @pytest.mark.asyncio
    async def test_the_same_account_on_threads_rests_with_it(self, monkeypatch):
        import time
        from backend.sessions import manager as M
        pools = _Pools(ig_uid="111", th_uid="111")
        monkeypatch.setattr(M, "sessions_db", pools)
        until = time.time() + 3600
        await M.rest_session_until("instagram", "ig1", until, "daily limit reached")
        th1 = await pools.get_item("threads", "th1")
        assert th1["rate_limited_until"] == until

    @pytest.mark.asyncio
    async def test_a_blank_session_id_is_ignored(self, monkeypatch):
        from backend.sessions import manager as M
        pools = _Pools()
        monkeypatch.setattr(M, "sessions_db", pools)
        await M.rest_session_until("instagram", "", 1e12, "x")
        assert all(s["rate_limited_until"] == 0.0 for s in pools.pools["instagram"])
