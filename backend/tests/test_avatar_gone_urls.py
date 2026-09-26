"""A picture URL the CDN has refused is not asked for again every hour.

THE BUG, MEASURED FROM THE RUN LOG. The same 72 lines appeared every hour
for four days: 32 x "403 from yt3.ggpht.com", 8 x "404 from yt3.ggpht.com",
20 x "404 from pbs.twimg.com", 12 x "403 from fbcdn". That was 18 profiles,
each fetched FOUR times an hour -- two tries inside `cache_one`, and the
whole thing again from `cache_for_profiles`' own retry -- by the hourly
picture retry.

YouTube and Twitter do not sign their URLs, so `url_is_live` can never rule
one out; when an account changes its picture, the old URL answers 404 for
ever and nothing stopped the retry. A 4xx is a verdict about the URL, not a
blip, so:

  * it is not retried inside the same pass;
  * it is counted on the profile, keyed to that exact URL;
  * after GONE_AFTER_STRIKES separate refusals the hourly retry stops, and
    says so -- a new URL from a re-sweep starts the count again.

PURE LOGIC ONLY, like the rest of this suite: Mongo and the network are
replaced by stand-ins.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.services import avatar_backfill as bf
from backend.services import avatar_cache as cache
from backend.shared import imagefetch
from backend.shared.imagefetch import FetchedImage, ImageFetchError

YT = "https://yt3.ggpht.com/old-picture=s176"


class TestWhichFailuresAreAVerdict:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
    def test_a_4xx_means_the_url_is_gone(self, status):
        assert ImageFetchError("x", upstream=status).gone is True

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
    def test_a_timeout_a_rate_limit_or_a_5xx_is_about_the_moment(self, status):
        assert ImageFetchError("x", upstream=status).gone is False

    def test_no_upstream_answer_is_not_a_verdict(self):
        """A DNS or TLS failure never reached the CDN, so it cannot have
        said anything about the URL."""
        assert ImageFetchError("x").gone is False

    @pytest.mark.asyncio
    async def test_fetch_image_keeps_the_cdns_status(self):
        dead = imagefetch._Hop(404, "", "text/html", b"")
        with patch.object(imagefetch.fast_http, "available", return_value=False), \
             patch.object(imagefetch, "_hop_aiohttp", AsyncMock(return_value=dead)):
            with pytest.raises(ImageFetchError) as err:
                await imagefetch.fetch_image(YT)
        assert err.value.upstream == 404
        assert err.value.gone is True
        # The API-facing status is unchanged: the media proxy still answers 502.
        assert err.value.status == 502


class TestCacheOneDoesNotAskTwice:
    @pytest.mark.asyncio
    async def test_a_404_is_fetched_once_and_reported(self):
        fetch = AsyncMock(side_effect=ImageFetchError("x", upstream=404))
        gone: set[str] = set()
        with patch.object(cache, "fetch_image", fetch):
            sha, *_ = await cache.cache_one(YT, retries=1, gone=gone)
        assert sha is None
        assert fetch.await_count == 1
        assert gone == {YT}

    @pytest.mark.asyncio
    async def test_a_502_is_still_retried_and_not_reported(self):
        calls = 0

        async def flaky(url):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ImageFetchError("x", upstream=502)
            return FetchedImage(data=b"img", content_type="image/jpeg")

        gone: set[str] = set()
        with patch.object(cache, "fetch_image", side_effect=flaky), \
             patch.object(cache.avatars_db, "store", AsyncMock(return_value="sha1")):
            sha, *_ = await cache.cache_one(YT, retries=1, gone=gone)
        assert sha == "sha1"
        assert calls == 2
        assert gone == set()


class TestTheBatchRecordsIt:
    @pytest.mark.asyncio
    async def test_a_gone_picture_costs_one_request_and_one_strike(self):
        """Four requests an hour became one, and the profile learns why."""
        fetch = AsyncMock(side_effect=ImageFetchError("x", upstream=404))
        note = AsyncMock(return_value=True)
        items = [{"url": "https://youtube.com/@a", "entity_id": "UC1",
                  "profile_image_url": YT}]
        with patch.object(cache, "fetch_image", fetch), \
             patch.object(cache.profiles_db, "existing_avatar_urls", AsyncMock(return_value={})), \
             patch.object(cache.profiles_db, "note_avatar_gone", note), \
             patch.object(cache.logos_db, "has_any", AsyncMock(return_value=False)):
            landed = await cache.cache_for_profiles("c1", "youtube", items)
        assert landed == 0
        assert fetch.await_count == 1
        note.assert_awaited_once_with(
            "c1", "youtube", YT, url="https://youtube.com/@a", entity_id="UC1")

    @pytest.mark.asyncio
    async def test_a_transient_failure_is_not_marked(self):
        fetch = AsyncMock(side_effect=ImageFetchError("x", upstream=503))
        note = AsyncMock()
        items = [{"url": "https://youtube.com/@a", "profile_image_url": YT}]
        with patch.object(cache, "fetch_image", fetch), \
             patch.object(cache.asyncio, "sleep", AsyncMock()), \
             patch.object(cache.profiles_db, "existing_avatar_urls", AsyncMock(return_value={})), \
             patch.object(cache.profiles_db, "note_avatar_gone", note), \
             patch.object(cache.logos_db, "has_any", AsyncMock(return_value=False)):
            await cache.cache_for_profiles("c1", "youtube", items)
        note.assert_not_awaited()
        # Both retry layers still apply to a blip: 2 tries x 2 passes.
        assert fetch.await_count == 4


class TestTheHourlyRetryStops:
    def test_three_refusals_of_the_same_url_stop_it(self):
        doc = {"avatar_gone_url": YT, "avatar_gone_strikes": bf.GONE_AFTER_STRIKES}
        assert bf._is_gone(doc, YT) is True

    def test_fewer_strikes_keep_trying(self):
        """One refusal could be a brief block -- a good picture must not be
        written off on a single answer."""
        doc = {"avatar_gone_url": YT, "avatar_gone_strikes": bf.GONE_AFTER_STRIKES - 1}
        assert bf._is_gone(doc, YT) is False

    def test_a_new_url_from_a_re_sweep_is_tried(self):
        doc = {"avatar_gone_url": YT, "avatar_gone_strikes": 99}
        assert bf._is_gone(doc, "https://yt3.ggpht.com/new-picture=s176") is False

    def test_an_unmarked_profile_is_tried(self):
        assert bf._is_gone({}, YT) is False


class TestTheStrikeWrite:
    @pytest.mark.asyncio
    async def test_the_count_is_keyed_to_the_url_and_atomic(self, monkeypatch):
        from backend.database.repositories import profile_repository as pdb

        captured = {}

        class _Coll:
            async def update_one(self, q, u):
                captured["q"], captured["u"] = q, u

                class R:
                    matched_count = 1
                return R()

        monkeypatch.setattr(pdb, "db", lambda: {pdb.PROFILES: _Coll()})
        assert await pdb.note_avatar_gone("c1", "youtube", YT,
                                          url="https://youtube.com/@a") is True
        # A pipeline update, so reading the old URL and bumping the count
        # cannot interleave with another writer.
        assert isinstance(captured["u"], list)
        stage = captured["u"][0]["$set"]
        cond = stage["avatar_gone_strikes"]["$cond"]
        assert cond[0] == {"$eq": ["$avatar_gone_url", {"$literal": YT}]}
        assert cond[2] == 1          # a different URL starts over
        assert stage["avatar_gone_url"] == {"$literal": YT}

    @pytest.mark.asyncio
    async def test_nothing_to_key_on_writes_nothing(self):
        from backend.database.repositories import profile_repository as pdb

        assert await pdb.note_avatar_gone("c1", "youtube", "", url="u") is False
        assert await pdb.note_avatar_gone("c1", "youtube", YT, url="") is False
