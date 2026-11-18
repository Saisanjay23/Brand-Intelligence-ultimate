"""Three things added 2026-11-17 to keep cookie-session Instagram accounts alive:

  1. THE NETWORK-CHANGE GUARD (shared/egress.py + sessions/manager.py): an
     account is held when this machine is in a different COUNTRY than where
     the account last worked -- the "VPN flipped" hijack signal.
  2. LIMITS THAT LEARN (shared/account_budget.py): a throttle/challenge
     shrinks that account's limits, which then recover over days.
  3. CAPACITY: the limits still allow 15-20 keywords and 15-20 profiles an
     hour, including on day one of a new account.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backend.config.settings import settings
from backend.sessions import manager as M
from backend.shared import account_budget as AB
from backend.shared import egress as E

DAY = 86400.0


# ------------------------------------------------------------------ egress

class TestParse:
    def test_ipinfo_shape(self):
        assert E.parse(b'{"ip":"1.2.3.4","country":"in","org":"AS1 Some ISP"}') == {
            "country": "IN", "org": "AS1 Some ISP"}

    def test_ipapi_shape(self):
        got = E.parse('{"country":"SG","country_code":"SG","org":"Acme"}')
        assert got == {"country": "SG", "org": "Acme"}

    def test_a_long_country_name_or_garbage_is_no_answer_not_a_comparison(self):
        assert E.parse('{"country":"India"}') is None
        assert E.parse("not json") is None
        assert E.parse("[]") is None
        assert E.parse('{"org":"x"}') is None

    def test_the_address_itself_is_never_kept(self):
        got = E.parse('{"ip":"203.0.113.9","country":"IN","org":"x"}')
        assert "203.0.113.9" not in str(got)


class TestJudge:
    def test_first_use_is_remembered_and_allowed(self):
        assert E.judge({}, {"country": "IN", "org": "a"})[0] == E.FIRST
        assert E.judge(None, {"country": "IN", "org": "a"})[0] == E.FIRST

    def test_same_country_is_fine_even_on_another_network(self):
        v, _ = E.judge({"country": "IN", "org": "home"}, {"country": "IN", "org": "office"})
        assert v == E.OK

    def test_a_different_country_is_a_hold_that_says_how_to_answer_it(self):
        v, why = E.judge({"country": "IN"}, {"country": "SG", "org": "x"})
        assert v == E.CHANGED
        assert "IN" in why and "SG" in why
        assert "paste fresh cookies" in why and "Switch back" in why

    def test_no_lookup_is_unknown_never_a_silent_pass_in_the_verdict(self):
        assert E.judge({"country": "IN"}, None)[0] == E.UNKNOWN

    def test_remember_only_on_change_or_when_stale(self):
        now = {"country": "IN", "org": "a"}
        assert E.should_remember({}, now, 1000.0)
        assert E.should_remember({"country": "IN", "org": "b", "at": 999.0}, now, 1000.0)
        assert not E.should_remember({"country": "IN", "org": "a", "at": 900.0}, now, 1000.0)
        assert E.should_remember({"country": "IN", "org": "a", "at": 0.0}, now, 10_000.0)


class TestLookup:
    @pytest.mark.asyncio
    async def test_falls_back_to_the_second_service_and_caches(self):
        calls = []

        async def fetch(url, timeout):
            calls.append(url)
            if "ipinfo" in url:
                raise RuntimeError("down")
            return SimpleNamespace(status=200, body=b'{"country":"IN","org":"x"}')

        E.reset()
        assert (await E.current(fetch=fetch, now=100.0))["country"] == "IN"
        assert len(calls) == 2
        assert (await E.current(fetch=fetch, now=200.0))["country"] == "IN"
        assert len(calls) == 2                     # cached, no new request
        assert (await E.current(fetch=fetch, now=100.0 + E.CACHE_S + 1))["country"] == "IN"
        assert len(calls) == 4

    @pytest.mark.asyncio
    async def test_both_down_is_none(self):
        async def fetch(url, timeout):
            raise RuntimeError("down")
        E.reset()
        assert await E.current(fetch=fetch) is None


# ------------------------------------------------- the guard at the claim

def _cookies(uid="111"):
    return [{"name": "sessionid", "value": "x"}, {"name": "ds_user_id", "value": uid}]


class _Pool:
    def __init__(self):
        self.items = {"ig1": {
            "id": "ig1", "identifier": "ig1", "status": "ready", "rate_limited_until": 0.0,
            "last_used": 0.0, "use_count": 0, "consecutive_failures": 0,
            "cookies": _cookies(), "last_ok": 0.0, "egress": {}}}

    async def list_pool(self, platform):
        return [dict(v) for v in self.items.values()] if platform == "instagram" else []

    async def get_item(self, platform, sid):
        v = self.items.get(sid)
        return dict(v) if v and platform == "instagram" else None

    async def update_item(self, platform, sid, **fields):
        if sid in self.items:
            self.items[sid].update(fields)
            return True
        return False

    async def increment_use_count(self, platform, sid):
        self.items[sid]["use_count"] += 1
        return self.items[sid]["use_count"]


@pytest.fixture
def pool(monkeypatch):
    p = _Pool()
    monkeypatch.setattr(M, "sessions_db", p)
    monkeypatch.setattr(M, "_claims", {})
    monkeypatch.setattr(M, "_claim_accounts", {})
    monkeypatch.setattr(settings, "meta_egress_guard", True)
    monkeypatch.setattr(settings, "meta_egress_hold_minutes", 20.0)
    return p


def _where(monkeypatch, country):
    async def current(**kw):
        return {"country": country, "org": "net"} if country else None
    monkeypatch.setattr(E, "current", current)


class TestTheGuardAtTheClaim:
    @pytest.mark.asyncio
    async def test_first_claim_remembers_where_the_account_works_from(self, pool, monkeypatch):
        _where(monkeypatch, "IN")
        got = await M._claim_one("instagram")
        assert got is not None
        assert pool.items["ig1"]["egress"]["country"] == "IN"

    @pytest.mark.asyncio
    async def test_same_country_again_goes_through(self, pool, monkeypatch):
        _where(monkeypatch, "IN")
        await M._claim_one("instagram")
        M.release_claim("instagram", "ig1")
        assert await M._claim_one("instagram") is not None

    @pytest.mark.asyncio
    async def test_a_different_country_holds_the_account_and_says_why(self, pool, monkeypatch):
        _where(monkeypatch, "IN")
        await M._claim_one("instagram")
        M.release_claim("instagram", "ig1")
        _where(monkeypatch, "SG")
        assert await M._claim_one("instagram") is None
        it = pool.items["ig1"]
        assert it["rate_limited_until"] > time.time()              # resting
        assert it["consecutive_failures"] == 0                     # not a failure
        assert "network changed" in it["last_error"]               # visible reason
        assert ("instagram", "ig1") not in M._claims               # claim given back

    @pytest.mark.asyncio
    async def test_it_resumes_by_itself_when_the_network_goes_back(self, pool, monkeypatch):
        _where(monkeypatch, "IN")
        await M._claim_one("instagram")
        M.release_claim("instagram", "ig1")
        _where(monkeypatch, "SG")
        assert await M._claim_one("instagram") is None
        pool.items["ig1"]["rate_limited_until"] = 0.0              # the hold lapsed
        _where(monkeypatch, "IN")
        assert await M._claim_one("instagram") is not None

    @pytest.mark.asyncio
    async def test_a_failed_lookup_lets_the_job_through(self, pool, monkeypatch):
        _where(monkeypatch, "IN")
        await M._claim_one("instagram")
        M.release_claim("instagram", "ig1")
        _where(monkeypatch, None)
        assert await M._claim_one("instagram") is not None

    @pytest.mark.asyncio
    async def test_switched_off_means_no_lookup_at_all(self, pool, monkeypatch):
        monkeypatch.setattr(settings, "meta_egress_guard", False)

        async def boom(**kw):
            raise AssertionError("must not look up")
        monkeypatch.setattr(E, "current", boom)
        assert await M._claim_one("instagram") is not None

    @pytest.mark.asyncio
    async def test_a_crash_in_the_guard_never_blocks_a_job(self, pool, monkeypatch):
        async def boom(**kw):
            raise RuntimeError("lookup blew up")
        monkeypatch.setattr(E, "current", boom)
        assert await M._claim_one("instagram") is not None


def test_pasting_fresh_cookies_forgets_the_remembered_place():
    import inspect
    from backend.database.repositories import session_repository as R
    assert '"egress": {}' in inspect.getsource(R.update_session_credentials)


# ----------------------------------------------------- limits that learn

class TestPenalty:
    def test_no_warning_means_no_penalty(self):
        assert AB.penalty_factor({}) == 1.0
        assert AB.penalty_factor(None) == 1.0

    def test_a_throttle_halves_then_recovers_in_a_straight_line(self):
        now = 10_000_000.0
        fields = AB.next_penalty({}, "rate_limited", now)
        assert fields["budget_penalty_factor"] == 0.5
        it = dict(fields)
        assert AB.penalty_factor(it, now) == pytest.approx(0.5)
        assert AB.penalty_factor(it, now + 1.5 * DAY) == pytest.approx(0.75)
        assert AB.penalty_factor(it, now + 3 * DAY) == 1.0
        assert AB.penalty_factor(it, now + 30 * DAY) == 1.0

    def test_a_challenge_takes_longer_to_recover(self):
        now = 10_000_000.0
        it = AB.next_penalty({}, "checkpointed", now)
        assert AB.penalty_factor(it, now + 3 * DAY) < 1.0
        assert AB.penalty_factor(it, now + 7 * DAY) == 1.0

    def test_two_warnings_close_together_compound_but_never_below_the_floor(self):
        now = 10_000_000.0
        first = AB.next_penalty({}, "rate_limited", now)
        second = AB.next_penalty(first, "rate_limited", now + 60)
        assert second["budget_penalty_factor"] == pytest.approx(0.25, abs=0.01)
        it = second
        for i in range(6):
            it = AB.next_penalty(it, "checkpointed", now + 120 + i)
        assert it["budget_penalty_factor"] == AB.PENALTY_FLOOR

    def test_a_late_second_warning_starts_from_the_recovered_level(self):
        now = 10_000_000.0
        first = AB.next_penalty({}, "rate_limited", now)
        second = AB.next_penalty(first, "rate_limited", now + 10 * DAY)
        assert second["budget_penalty_factor"] == 0.5

    @pytest.mark.asyncio
    async def test_the_penalty_shrinks_both_the_daily_and_the_hourly_limit(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_searches_per_hour", 20)
        monkeypatch.setattr(settings, "instagram_searches_per_day", 100)
        monkeypatch.setattr(settings, "instagram_warmup_days", 0)
        now = time.time()
        item = {"id": "x", "cookies": _cookies("5"), **AB.next_penalty({}, "rate_limited", now)}
        assert AB.daily_limit_for("instagram", AB.SEARCH, item, now) == 50
        # hourly: 20 * 0.5 = 10 -> the 11th search in an hour has to wait
        for _ in range(10):
            assert await AB.hold("instagram", item, AB.SEARCH, lambda: False)
        assert AB.budget().wait_s("meta:5", AB.SEARCH, 10) > 0

    @pytest.mark.asyncio
    async def test_a_session_warning_writes_the_penalty_for_instagram_only(self, monkeypatch):
        pool = _Pool()
        monkeypatch.setattr(M, "sessions_db", pool)
        await M.mark_session_failed("instagram", "ig1", "rate_limited")
        it = pool.items["ig1"]
        assert it["budget_penalty_factor"] == 0.5
        assert it["budget_penalty_until"] > time.time() + 2 * DAY

        # an expired cookie is the cookie's problem, not a verdict on the pace
        pool2 = _Pool()
        monkeypatch.setattr(M, "sessions_db", pool2)
        await M.mark_session_failed("instagram", "ig1", "expired")
        assert "budget_penalty_factor" not in pool2.items["ig1"]


# ---------------------------------------------------------------- capacity

class TestTheTargetThroughputFits:
    """The ask: 15-20 keywords an hour in discovery and 15-20 profiles an
    hour in analysis, without suspensions. These pin that the limits and the
    pacing leave room for it, so a later 'make it safer' change cannot
    quietly take it away."""

    def test_the_hourly_limits_allow_twenty_of_each(self):
        # 0 = no ceiling (searches are uncapped by default since 2026-11-18).
        assert settings.instagram_searches_per_hour == 0 or settings.instagram_searches_per_hour >= 20
        assert settings.instagram_profile_visits_per_hour >= 20

    def test_searches_have_no_ceiling_by_default(self):
        item = {"id": "s", "cookies": _cookies("9"), "added_at": time.time()}
        assert AB.limit_for("instagram", AB.SEARCH, item) == 0
        assert AB.daily_limit_for("instagram", AB.SEARCH, item) == 0

    def test_twenty_keywords_fit_in_well_under_an_hour_at_high_stealth(self, monkeypatch):
        from backend.stealth import levels as L
        monkeypatch.setattr(L.settings, "instagram_stealth", "high")
        keyword_gap = L.value("instagram", "keyword_gap", 16.0)
        page_gap = L.value("instagram", "page_gap", 7.0)
        pages = 6                                   # the page budget, worst case
        per_keyword = keyword_gap + (pages - 1) * page_gap + 10   # +10s of requests
        assert 20 * per_keyword < 40 * 60, per_keyword

    def test_twenty_profiles_fit_in_well_under_an_hour_at_high_stealth(self, monkeypatch):
        from backend.stealth import levels as L
        monkeypatch.setattr(L.settings, "instagram_stealth", "high")
        batch_gap = L.value("instagram", "batch_gap", 9.0)
        per_profile = batch_gap + 30                # a profile visit is ~30s of loads
        assert 20 * per_profile < 40 * 60, per_profile

    def test_a_brand_new_account_can_still_do_a_twenty_keyword_hour_on_day_one(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_warmup_days", 14)
        now = time.time()
        fresh = {"id": "n", "added_at": now}
        searches = AB.daily_limit_for("instagram", AB.SEARCH, fresh, now)
        assert searches == 0 or searches >= 20          # 0 = no ceiling
        assert AB.daily_limit_for("instagram", AB.PROFILE, fresh, now) >= 20

    def test_a_warned_account_is_slower_by_design_not_by_accident(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_warmup_days", 0)
        monkeypatch.setattr(settings, "instagram_searches_per_day", 100)
        now = time.time()
        warned = {"id": "w", **AB.next_penalty({}, "rate_limited", now)}
        assert AB.daily_limit_for("instagram", AB.SEARCH, warned, now) < \
            AB.daily_limit_for("instagram", AB.SEARCH, {"id": "w"}, now)
