"""Instagram low-and-slow: a natural search request, a shallow page cap,
and a rest between searching and visiting. Each guards a change made after
the account was flagged for automated behaviour."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.analysis import runner as AR
from backend.config.settings import settings
from backend.platforms.instagram import discovery_engine as DE
from backend.platforms.instagram.discovery_engine import Discovery
from backend.shared import pushback as pb


@pytest.fixture(autouse=True)
def _fresh_governors():
    pb.reset_all()
    yield
    pb.reset_all()


def _users(*names):
    return [{"username": n, "pk": str(i), "profile_pic_url": f"https://cdn/{n}.jpg"}
            for i, n in enumerate(names)]


def _disc(pages, max_pages=0):
    responses = []
    for data in pages:
        r = MagicMock()
        r.status = 200
        r.text = AsyncMock(return_value=json.dumps(data))
        responses.append(r)
    ctx = MagicMock()
    ctx.request.get = AsyncMock(side_effect=responses)
    return Discovery(SimpleNamespace(max_pages=max_pages, max_results=0, timeout=5), ctx), ctx


class TestTheSearchRequest:
    @pytest.mark.asyncio
    async def test_asks_for_a_natural_page_size_from_the_search_screen(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        disc, ctx = _disc([{"status": "ok", "users": _users("a"), "has_more": False}])
        await disc.sweep("brand")
        url = ctx.request.get.await_args_list[0].args[0]
        assert "count=50" in url and "count=100" not in url
        assert "search_surface=user_search_page" in url

    @pytest.mark.asyncio
    async def test_stops_at_four_pages_and_says_it_was_cut(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        pages = [{"status": "ok", "users": _users(f"u{i}"), "has_more": True,
                  "page_token": f"p{i}"} for i in range(10)]
        disc, ctx = _disc(pages)
        sweep = await disc.sweep("brand")
        assert ctx.request.get.await_count == 4
        assert sweep.stopped == "cap:pages" and not sweep.complete

    @pytest.mark.asyncio
    async def test_a_configured_page_budget_still_wins(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        pages = [{"status": "ok", "users": _users(f"u{i}"), "has_more": True,
                  "page_token": f"p{i}"} for i in range(10)]
        disc, ctx = _disc(pages, max_pages=6)
        await disc.sweep("brand")
        assert ctx.request.get.await_count == 6


class TestRestBeforeAnalysis:
    def test_an_account_used_moments_ago_rests(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_rest_before_analysis_sec", 45.0)
        assert AR.rest_needed_s("instagram", {"last_ok": 1000.0}, 1010.0) == 35.0

    def test_an_account_idle_long_enough_does_not(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_rest_before_analysis_sec", 45.0)
        assert AR.rest_needed_s("instagram", {"last_ok": 1000.0}, 2000.0) == 0.0

    def test_never_used_anonymous_and_other_platforms_do_not(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_rest_before_analysis_sec", 45.0)
        assert AR.rest_needed_s("instagram", {"last_ok": 0.0}, 10.0) == 0.0
        assert AR.rest_needed_s("instagram", {"anonymous": True, "last_ok": 9.0}, 10.0) == 0.0
        assert AR.rest_needed_s("facebook", {"last_ok": 9.0}, 10.0) == 0.0

    def test_zero_switches_it_off(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_rest_before_analysis_sec", 0)
        assert AR.rest_needed_s("instagram", {"last_ok": 9.0}, 10.0) == 0.0

    @pytest.mark.asyncio
    async def test_a_stop_ends_the_rest(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_rest_before_analysis_sec", 600.0)
        job = SimpleNamespace(cancel=asyncio.Event())
        job.cancel.set()
        import time
        await asyncio.wait_for(AR._rest_after_recent_use(
            "instagram", {"last_ok": time.time()}, job, "acct"), timeout=2)
