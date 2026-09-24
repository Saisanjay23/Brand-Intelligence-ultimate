"""Engine behaviours established against the live platforms on 2026-09-24.

Each test pins one shape a live capture showed (see backend/tools/
live_probe.py, which re-takes those captures on demand).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.platforms.facebook.discovery_engine import _session_blocked
from backend.platforms.instagram.discovery_engine import Discovery as IGDiscovery
from backend.platforms.twitter.discovery_engine import latest_post, latest_repost
from backend.shared.keywords import build_plans
from backend.shared.resilience import classify_failure, sweep_outcome


# ----------------------------------------------------------------- X reposts

ME, ME_ID = "Gautam_Adani_07", "1360882644"


def _tweet(author: str, author_id: str, created: str, *, repost_of: dict | None = None) -> dict:
    legacy = {"created_at": created, "full_text": "t", "user_id_str": author_id,
              "id_str": created[-4:] + author_id[:3]}
    if repost_of:
        legacy["retweeted_status_result"] = {"result": repost_of}
    return {"__typename": "Tweet", "rest_id": legacy["id_str"], "legacy": legacy,
            "core": {"user_results": {"result": {"core": {"screen_name": author}}}}}


class TestARepostOnlyAccountHasALastActivityDate:
    """Live: 13 posts in the header, empty Posts and Replies tabs, all 13
    are reposts. The wrapper carries the REPOST time; the original inside
    it is somebody else's, with its own older date."""

    def _payload(self):
        original = _tweet("gautam_adani", "2378585124", "Sun Apr 19 10:52:47 +0000 2026")
        wrapper = _tweet(ME, ME_ID, "Mon Apr 27 23:28:41 +0000 2026", repost_of=original)
        return {"data": {"timeline": {"entries": [wrapper]}}}

    def test_the_repost_time_is_read_not_the_originals(self):
        assert latest_repost(self._payload(), ME.lower(), ME_ID) == "2026-04-27"

    def test_latest_post_still_ignores_reposts(self):
        assert latest_post(self._payload(), ME.lower(), ME_ID) == ""

    def test_another_accounts_repost_is_not_this_ones(self):
        assert latest_repost(self._payload(), "someoneelse", "42") == ""


# ------------------------------------------------ Facebook login wall

def _page(url: str, body: str):
    p = MagicMock()
    p.url = url
    p.inner_text = AsyncMock(return_value=body)
    return p


class TestAFacebookSearchPageThatIsAWall:
    @pytest.mark.asyncio
    async def test_a_checkpoint_url(self):
        assert await _session_blocked(_page("https://www.facebook.com/checkpoint/123", "")) == "checkpoint"

    @pytest.mark.asyncio
    async def test_a_login_wall(self):
        body = "You must log in to continue.\nLog in to Facebook"
        assert await _session_blocked(_page("https://www.facebook.com/search/people/?q=x", body)) == "login"

    @pytest.mark.asyncio
    async def test_real_results_are_not_a_wall(self):
        body = "People\nPranav Adani\nAdd friend"
        assert await _session_blocked(_page("https://www.facebook.com/search/people/?q=x", body)) == ""

    def test_both_stops_requeue_the_keyword_on_another_session(self):
        assert classify_failure("facebook search page shows a login wall") == "expired"
        assert classify_failure("checkpoint") == "checkpointed"


# ------------------------------------------------- Instagram search sweep

def _ig_disc(pages: list[dict], max_results: int = 0):
    responses = []
    for data in pages:
        r = MagicMock()
        r.status = 200
        import json as _j
        r.text = AsyncMock(return_value=_j.dumps(data))
        responses.append(r)
    ctx = MagicMock()
    ctx.request.get = AsyncMock(side_effect=responses)
    opts = SimpleNamespace(max_pages=5, max_results=max_results, timeout=5)
    return IGDiscovery(opts, ctx)


def _users(*names):
    return [{"username": n, "full_name": n.title(), "pk": str(i),
             "has_anonymous_profile_picture": False,
             "profile_pic_url": f"https://cdn/{n}.jpg"} for i, n in enumerate(names)]


class TestInstagramStreamsAndDetectsSoftBlocks:
    @pytest.mark.asyncio
    async def test_cards_stream_per_page_in_order(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        disc = _ig_disc([
            {"status": "ok", "users": _users("a", "b"), "has_more": True, "page_token": "p2"},
            {"status": "ok", "users": _users("c"), "has_more": False},
        ])
        batches: list[list[str]] = []

        async def on_progress(found, page, rows):
            batches.append([r.username for r in rows])

        sweep = await disc.sweep("x", on_progress=on_progress)
        assert batches == [["a", "b"], ["c"]]
        assert [h.username for h in sweep.hits] == ["a", "b", "c"]
        assert sweep.complete

    @pytest.mark.asyncio
    async def test_streaming_never_passes_the_cap(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        disc = _ig_disc([{"status": "ok", "users": _users("a", "b", "c"), "has_more": False}],
                        max_results=2)
        seen: list[str] = []

        async def on_progress(found, page, rows):
            seen.extend(r.username for r in rows)

        sweep = await disc.sweep("x", on_progress=on_progress)
        assert seen == ["a", "b"] and len(sweep.hits) == 2

    @pytest.mark.asyncio
    async def test_a_200_that_says_fail_is_not_an_empty_search(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        disc = _ig_disc([{"status": "fail", "message": "Please wait a few minutes before you try again."}])

        async def no_web(*a, **kw):
            raise RuntimeError("web/search/topsearch returned HTTP 429")

        monkeypatch.setattr("backend.platforms.instagram.discovery_engine.web_search_users", no_web)
        sweep = await disc.sweep("x")
        assert not sweep.complete
        assert sweep_outcome(sweep.stopped, sweep.complete) == "broken"
        assert classify_failure(sweep.error or sweep.stopped) == "rate_limited"

    def test_the_search_payload_carries_instagrams_own_picture_flag(self):
        from backend.platforms.instagram.discovery_engine import (
            iter_mobile_search_users, user_to_row)
        from backend.shared import logo_verdict
        (u,) = iter_mobile_search_users({"users": [{
            "username": "anon", "pk": "1", "has_anonymous_profile_picture": True,
            "profile_pic_url": "https://cdn/rotated-new-asset-id.jpg"}]})
        row = user_to_row(u, "x")
        assert row.has_custom_pic is False
        assert logo_verdict.row_evidence(row).strength == logo_verdict.FLAG


# --------------------------------------------------------------- keywords

class TestKeywordWhitespace:
    def test_a_double_space_is_the_same_search(self):
        client = {"keyword_groups": {"individual": [{
            "parent": "Mr. Yash Pal Mendiratta",
            "children": ["Yash Pal Mendiratta", "Yash  Mendiratta", "yash mendiratta"]}]}}
        assert [p.search for p in build_plans(client)] == ["Yash Pal Mendiratta", "Yash Mendiratta"]
