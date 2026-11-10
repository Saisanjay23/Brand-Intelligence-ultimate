"""Fixes to the tool's core promise: every saved keyword is searched, a
search that was refused is never filed as finished, and the browser does not
announce itself.

Each class pins one defect found in the end-to-end review.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.database.repositories import coverage_repository as cov
from backend.platforms.instagram.discovery_engine import (
    Discovery as IGDiscovery, iter_mobile_search_users)
from backend.sessions import cookies as cookie_mod
from backend.sessions import manager
from backend.shared import keywords as K
from backend.shared.resilience import classify_failure, sweep_outcome


# ------------------------------------------------ a term under both types

class TestATermConfiguredAsPersonAndBrandIsSearchedAsBoth:
    CLIENT = {"keyword_groups": {
        "individual": [{"parent": "Acme", "children": ["acme official"]}],
        "domain": [{"parent": "Acme", "children": ["acme.com"]}],
    }}

    def test_both_types_get_their_own_search(self):
        plans = K.build_plans(self.CLIENT)
        assert {(p.kw_type, p.search) for p in plans} == {
            ("individual", "Acme"), ("individual", "acme official"),
            ("domain", "Acme"), ("domain", "acme.com"),
        }

    def test_the_domain_parents_permutations_are_not_lost(self):
        assert "acme.com" in {p.search for p in K.build_plans(self.CLIENT)}

    def test_a_shared_child_under_two_parents_of_one_type_is_still_one_search(self):
        client = {"keyword_groups": {"individual": [
            {"parent": "Gautam Adani", "children": ["adani"]},
            {"parent": "Jeet Adani", "children": ["adani"]},
        ]}}
        plans = [p for p in K.build_plans(client) if p.search == "adani"]
        assert len(plans) == 1 and len(plans[0].targets) == 2


# ------------------------------------------- keywords the ledger never saw

class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


class _Coll:
    def __init__(self, docs):
        self._docs = docs

    def find(self, query, projection=None):
        return _Cursor(self._docs)


class TestTheCoverageReportNamesKeywordsNeverPlanned:
    @pytest.mark.asyncio
    async def test_a_keyword_added_after_the_last_sweep_is_owed(self, monkeypatch):
        docs = [{"platform": "facebook", "kw_type": "individual", "search": "Gautam Adani"}]
        monkeypatch.setattr(cov, "db", lambda: {cov.COVERAGE: _Coll(docs)})
        plans = K.build_plans({"keyword_groups": {"individual": [
            {"parent": "Gautam Adani", "children": ["gautamadani"]}]}})

        out = await cov.unplanned("c1", ["facebook"], plans)

        assert [(c["platform"], c["search"]) for c in out] == [("facebook", "gautamadani")]
        assert out[0]["outcome"] == "" and "never searched" in out[0]["reason"]

    @pytest.mark.asyncio
    async def test_casing_does_not_make_a_covered_keyword_look_missing(self, monkeypatch):
        docs = [{"platform": "x", "kw_type": "individual", "search": "GAUTAM ADANI"}]
        monkeypatch.setattr(cov, "db", lambda: {cov.COVERAGE: _Coll(docs)})
        plans = K.build_plans({"keyword_groups": {"individual": [
            {"parent": "gautam adani", "children": []}]}})
        assert await cov.unplanned("c1", ["x"], plans) == []

    @pytest.mark.asyncio
    async def test_an_unreadable_ledger_is_an_error_not_nothing(self, monkeypatch):
        class Boom:
            def find(self, *a, **k):
                raise RuntimeError("mongo down")

        monkeypatch.setattr(cov, "db", lambda: {cov.COVERAGE: Boom()})
        plans = K.build_plans({"name_keywords": ["a"]})
        with pytest.raises(RuntimeError):
            await cov.unplanned("c1", ["x"], plans)


# ------------------------------------------------------ Instagram search

def _ig(pages: list[dict], max_pages: int = 6):
    responses = []
    for data in pages:
        r = MagicMock()
        r.status = 200
        r.text = AsyncMock(return_value=json.dumps(data))
        responses.append(r)
    ctx = MagicMock()
    ctx.request.get = AsyncMock(side_effect=responses)
    opts = SimpleNamespace(max_pages=max_pages, max_results=0, timeout=5)
    return IGDiscovery(opts, ctx), ctx


def _users(*names):
    return [{"username": n, "full_name": n.title(), "pk": str(i),
             "has_anonymous_profile_picture": False,
             "profile_pic_url": f"https://cdn/{n}.jpg"} for i, n in enumerate(names)]


class TestInstagramPaging:
    @pytest.mark.asyncio
    async def test_has_more_without_a_page_token_does_not_resend_the_same_request(
        self, monkeypatch,
    ):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        disc, ctx = _ig([
            {"status": "ok", "users": _users("a", "b"), "has_more": True, "rank_token": "r"},
            {"status": "ok", "users": _users("a", "b"), "has_more": True, "rank_token": "r"},
        ])
        sweep = await disc.sweep("x")
        assert ctx.request.get.await_count == 1
        assert sweep.stopped == "cap:pages" and not sweep.complete
        assert [h.username for h in sweep.hits] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_the_same_cursor_twice_ends_the_sweep(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        page = {"status": "ok", "users": _users("a"), "has_more": True,
                "page_token": "p2", "rank_token": "r"}
        disc, ctx = _ig([page, page, page, page])
        sweep = await disc.sweep("x")
        assert ctx.request.get.await_count == 2
        assert sweep.stopped == "cap:pages"

    @pytest.mark.asyncio
    async def test_the_page_token_is_url_encoded(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        disc, ctx = _ig([
            {"status": "ok", "users": _users("a"), "has_more": True, "page_token": "a+b/c="},
            {"status": "ok", "users": _users("b"), "has_more": False},
        ])
        await disc.sweep("x")
        second_url = ctx.request.get.await_args_list[1].args[0]
        assert "page_token=a%2Bb%2Fc%3D" in second_url


class TestAnExpiredSessionIsNotLaunderedByTheWebFallback:
    async def _run(self, monkeypatch, message: str):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        disc, _ = _ig([{"status": "fail", "message": message}])

        async def web_ok(*a, **kw):
            return list(iter_mobile_search_users({"users": _users("w1", "w2")}))

        monkeypatch.setattr(
            "backend.platforms.instagram.discovery_engine.web_search_users", web_ok)
        return await disc.sweep("x")

    @pytest.mark.asyncio
    async def test_a_login_required_answer_still_reaches_the_runner(self, monkeypatch):
        sweep = await self._run(monkeypatch, "login_required")
        assert [h.username for h in sweep.hits] == ["w1", "w2"]      # rows still saved
        assert classify_failure(sweep.error or sweep.stopped) == "expired"

    @pytest.mark.asyncio
    async def test_a_challenge_answer_still_reaches_the_runner(self, monkeypatch):
        sweep = await self._run(monkeypatch, "challenge_required")
        assert classify_failure(sweep.error or sweep.stopped) == "checkpointed"

    @pytest.mark.asyncio
    async def test_a_throttle_stops_the_account_instead_of_asking_again(self, monkeypatch):
        """A throttle used to be answered with a web search on the same
        account and then cleared, so the account kept working straight
        through Instagram's warning. Now it ends the sweep, sends nothing
        more, and reaches the runner as a (non-dead) rate limit."""
        web_calls = []

        async def web_spy(*a, **kw):
            web_calls.append(a)
            return []

        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        disc, ctx = _ig([{"status": "fail", "message": "Please wait a few minutes before you try again."}])
        monkeypatch.setattr(
            "backend.platforms.instagram.discovery_engine.web_search_users", web_spy)
        sweep = await disc.sweep("x")
        assert web_calls == [] and ctx.request.get.await_count == 1
        assert sweep.stopped == "rate_limited" and not sweep.complete
        assert classify_failure(sweep.error or sweep.stopped) == "rate_limited"
        assert sweep_outcome(sweep.stopped, sweep.complete) == "broken"   # still owed


class TestInstagramStopsWhenItIsWarned:
    """The answers that mean "this account looks automated" or "slow down"
    end the sweep with no further request, keep what was already found,
    and carry a verdict the runner acts on."""

    async def _run(self, monkeypatch, pages):
        web_calls = []

        async def web_spy(*a, **kw):
            web_calls.append(a)
            return []

        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        disc, ctx = _ig(pages)
        monkeypatch.setattr(
            "backend.platforms.instagram.discovery_engine.web_search_users", web_spy)
        return await disc.sweep("x"), ctx, web_calls

    @pytest.mark.asyncio
    async def test_feedback_required_is_a_rate_limit(self, monkeypatch):
        sweep, ctx, web = await self._run(
            monkeypatch, [{"status": "fail", "message": "feedback_required"}])
        assert web == [] and ctx.request.get.await_count == 1
        assert classify_failure(sweep.error or sweep.stopped) == "rate_limited"

    @pytest.mark.asyncio
    async def test_suspected_automation_takes_the_account_out_of_rotation(self, monkeypatch):
        sweep, _, web = await self._run(monkeypatch, [{
            "status": "fail",
            "message": "We suspect automated behavior on your account"}])
        assert web == []
        assert sweep.stopped == "checkpoint"
        assert classify_failure(sweep.error or sweep.stopped) == "checkpointed"

    @pytest.mark.asyncio
    async def test_results_from_earlier_pages_are_kept(self, monkeypatch):
        sweep, ctx, web = await self._run(monkeypatch, [
            {"status": "ok", "users": _users("a", "b"), "has_more": True, "page_token": "p2"},
            {"status": "fail", "message": "feedback_required"},
        ])
        assert web == [] and ctx.request.get.await_count == 2
        assert [h.username for h in sweep.hits] == ["a", "b"]
        assert sweep.stopped == "rate_limited" and not sweep.complete

    @pytest.mark.asyncio
    async def test_an_http_429_stops_too(self, monkeypatch):
        r = MagicMock()
        r.status = 429
        r.text = AsyncMock(return_value="")
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        disc, ctx = _ig([])
        ctx.request.get = AsyncMock(return_value=r)
        web_calls = []

        async def web_spy(*a, **kw):
            web_calls.append(a)
            return []

        monkeypatch.setattr(
            "backend.platforms.instagram.discovery_engine.web_search_users", web_spy)
        sweep = await disc.sweep("x")
        assert web_calls == [] and sweep.stopped == "rate_limited"


# ------------------------------------------------------- sessions & cookies

class TestSessionHandling:
    def test_an_incomplete_account_is_not_handed_to_a_job(self):
        item = {"status": "incomplete", "rate_limited_until": 0.0}
        assert not manager._is_available(item, 1.0)
        assert manager._is_available({"status": "ready", "rate_limited_until": 0.0}, 1.0)

    def test_a_pasted_cookie_for_x_lands_on_x_dot_com(self):
        out = cookie_mod.normalize_cookies(
            [{"name": "auth_token", "value": "t"}, {"name": "ct0", "value": "c"}],
            "twitter x.com")
        assert {c["domain"] for c in out} == {".x.com"}

    def test_a_bare_stem_still_gets_dot_com(self):
        out = cookie_mod.normalize_cookies([{"name": "c_user", "value": "1"}], "facebook")
        assert out[0]["domain"] == ".facebook.com"


class TestTheBrowserDoesNotAnnounceItself:
    def test_the_user_agent_is_the_reduced_form_real_chrome_sends(self):
        from backend.stealth import fingerprint
        assert re.search(r"Chrome/\d+\.0\.0\.0 Safari", fingerprint.UA)


# ------------------------------------------------- X: how a search really ends

class TestHowAnXSearchEnds:
    """Measured live (2026-11-03): at the true end of a search X sends ONE small
    empty SearchTimeline response and then sends nothing more -- further
    scrolls fire no request at all. Silence after that empty answer is the
    end; silence with no empty answer since the last new users is X being
    slow, which must not be filed as a finished (satisfied) search."""

    @staticmethod
    def _run(first_page_users, deliver_empty: str, monkeypatch):
        """deliver_empty: "during-goto" (before the loop's first scroll),
        "on-scroll" (the answer to the first scroll), or "never"."""
        from backend.platforms.scan_options import DiscoveryOptions
        from backend.platforms.twitter import discovery_engine as X

        callbacks: list = []
        page = MagicMock()
        page.on = MagicMock(side_effect=lambda ev, cb: callbacks.append(cb))
        page.close = AsyncMock()
        page.wait_for_function = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        ctx = MagicMock()
        ctx.new_page = AsyncMock(return_value=page)

        def _resp(cursor: str, users: list):
            r = MagicMock()
            r.url = "https://x.com/i/api/graphql/abc/SearchTimeline"
            r.status = 200
            r.text = AsyncMock(return_value=json.dumps({"cursor": cursor, "users": users}))
            return r

        async def deliver(cursor: str, users: list):
            for cb in callbacks:
                cb(_resp(cursor, users))
            await asyncio.sleep(0.02)                 # let the handler task run

        def fake_state(blob, probe=None):
            us = [SimpleNamespace(entity_id=u, handle=u) for u in blob.get("users", [])]
            return SimpleNamespace(users=us, bottom_cursor=blob.get("cursor", ""))

        scrolls = {"n": 0}

        async def goto(*a, **k):
            await deliver("c1", first_page_users)
            if deliver_empty == "during-goto":
                await deliver("c2", [])

        async def scroll(*a, **k):
            scrolls["n"] += 1
            if deliver_empty == "on-scroll" and scrolls["n"] == 1:
                await deliver("c2", [])

        page.goto = AsyncMock(side_effect=goto)
        opts = DiscoveryOptions(timeout=5, settle=0.1, page_wait=0.1, max_results=0, patience=4)
        disc = X.Discovery(opts, ctx)

        async def go():
            with patch.object(X, "search_state", fake_state), \
                 patch.object(X, "natural_scroll_down", AsyncMock(side_effect=scroll)), \
                 patch.object(X, "humanize_interaction", AsyncMock()), \
                 patch.object(X, "hover_element_safely", AsyncMock()), \
                 patch.object(X.random, "uniform", lambda a, b: 0.0), \
                 patch.object(X, "run_strategies", AsyncMock(
                     return_value=MagicMock(value=[], degraded=False))):
                return await disc.sweep("brand")

        return go()

    @pytest.mark.asyncio
    async def test_an_empty_answer_then_silence_is_the_end(self, monkeypatch):
        sweep = await self._run(["u1", "u2"], "on-scroll", monkeypatch)
        assert sweep.stopped == "exhausted" and sweep.complete

    @pytest.mark.asyncio
    async def test_an_empty_answer_that_landed_before_the_loop_is_still_the_end(
        self, monkeypatch,
    ):
        """The race seen live: X's empty answer arrived while the page was
        still being checked, before the first scroll."""
        sweep = await self._run(["u1", "u2"], "during-goto", monkeypatch)
        assert sweep.stopped == "exhausted" and sweep.complete

    @pytest.mark.asyncio
    async def test_slow_responses_are_not_the_end(self, monkeypatch):
        """X never answered after the first page: that is throttling or a
        slow connection, not 'no more results', and it must not read as a
        satisfied search."""
        sweep = await self._run(["u1", "u2"], "never", monkeypatch)
        assert sweep.stopped == "stalled" and not sweep.complete
        assert sweep_outcome(sweep.stopped, sweep.complete) == "broken"
