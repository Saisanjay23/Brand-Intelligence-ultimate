"""Regression tests for the TikTok / YouTube / analysis-runner fixes.

Governing rule behind every test here: a blocked, refused, half-loaded or
cancelled read must never look like a clean result, and an unknown must never
be written as a definite value.

No network and no browser: TikTok is driven through a fake page/context (the
same technique as test_tiktok_end_to_end.py), YouTube through a fake API
object, and the runner through fake scrapers and a fake session pool.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest

from backend.analysis import runner as R
from backend.platforms.scan_options import ScanOptions
from backend.platforms.tiktok import analysis_engine as A
from backend.platforms.youtube import analysis_engine as YA
from backend.platforms.youtube import discovery_engine as YD
from backend.shared.models.row import Row
from backend.shared.resilience import classify_failure

# ======================================================================
# TikTok
# ======================================================================


def _hydration(handle="adanigroup", bio="Official account", followers=120400):
    return json.dumps({
        "__DEFAULT_SCOPE__": {
            "webapp.user-detail": {
                "userInfo": {
                    "user": {
                        "id": "6812", "uniqueId": handle, "nickname": "Adani Group",
                        "signature": bio, "verified": False, "privateAccount": False,
                        "avatarLarger": "https://cdn.tiktok/a_large.jpg",
                    },
                    "stats": {"followerCount": followers, "followingCount": 12,
                              "heartCount": 9, "videoCount": 87},
                },
                "itemList": [],
            },
        }
    })


class _NavResp:
    """A main-frame document response, as Playwright's `response` event
    delivers it."""

    def __init__(self, page, status):
        self.status = status
        self.request = self
        self.frame = page.main_frame

    def is_navigation_request(self):
        return True


class _Page:
    def __init__(self, *, body="", chrome=None, hydration="", header=None,
                 goto_status=None, goto_raises=None, url=None):
        self.url = url or ""
        self.main_frame = object()
        self._body = body
        self._chrome = chrome          # what the page shows minus the profile's own text
        self._hydration = hydration
        self._header = header or {}
        self._goto_status = goto_status
        self._goto_raises = goto_raises
        self._handler = None
        self.goto_urls: list[str] = []
        self.closed = False

    def on(self, _event, handler):
        self._handler = handler

    async def goto(self, url, **_kw):
        self.goto_urls.append(url)
        if not self.url:
            self.url = url
        if self._goto_status is not None and self._handler:
            self._handler(_NavResp(self, self._goto_status))
        if self._goto_raises:
            raise self._goto_raises
        return None

    async def inner_text(self, _sel):
        return self._body

    async def wait_for_timeout(self, _ms):
        await asyncio.sleep(0)

    async def evaluate(self, js, *args):
        if "REHYDRATION" in js:
            return self._hydration
        if "split(x).join" in js:                      # A._chrome_text
            return self._chrome if self._chrome is not None else self._body
        if "following" in js or "followers" in js:      # JS_HEADER
            return self._header
        return []

    async def close(self):
        self.closed = True


class _Ctx:
    def __init__(self, page):
        self._page = page
        self.pages_opened = 0
        self.request_touched = False

    @property
    def request(self):
        # The old code made a non-browser request here on every navigation
        # failure. Touching it at all is the regression.
        self.request_touched = True
        raise AssertionError("ctx.request must not be used")

    async def new_page(self):
        self.pages_opened += 1
        return self._page


def _scraper(monkeypatch, page, *, anonymous=False):
    sc = A.Scraper(ScanOptions(timeout=5, settle=0.01), [], session_id="", anonymous=anonymous)
    sc.session = None
    ctx = _Ctx(page)
    monkeypatch.setattr(A.Scraper, "ctx", property(lambda self: ctx))

    async def _no_search(*_a, **_kw):
        return ""

    async def _no_users(*_a, **_kw):
        return {}

    monkeypatch.setattr(A, "newest_post_via_search", _no_search)
    monkeypatch.setattr(A, "search_users_in", _no_users)
    return sc, ctx


class TestTikTokGeoblock:
    @pytest.mark.asyncio
    async def test_a_bio_that_mentions_a_government_is_not_a_block(self, monkeypatch):
        bio = "Government of India press office. Not available in your region? Write to us."
        page = _Page(body=f"Adani Group\n120.4K Followers\n{bio}",
                     hydration=_hydration(bio=bio))
        sc, _ = _scraper(monkeypatch, page)
        row = await sc.process("https://www.tiktok.com/@adanigroup", "Adani", "")
        assert row.status == "OK"
        assert "blocked" not in row.notes.lower()
        assert row.followers == 120400

    @pytest.mark.asyncio
    async def test_the_redirect_is_still_a_block(self, monkeypatch):
        page = _Page(body="", url="https://www.tiktok.com/in/about")
        sc, _ = _scraper(monkeypatch, page)
        row = await sc.process("https://www.tiktok.com/@adanigroup", "Adani", "")
        assert row.status == "ERROR" and "blocked" in row.notes.lower()

    @pytest.mark.asyncio
    async def test_notice_text_counts_only_outside_the_profiles_own_content(self, monkeypatch):
        notice = "On June 29 the Govt. of India decided to block 59 apps."
        # No payload, notice IS the chrome -> a block.
        page = _Page(body=notice, chrome=notice)
        sc, _ = _scraper(monkeypatch, page)
        row = await sc.process("https://www.tiktok.com/@x1", "Adani", "")
        assert row.status == "ERROR" and "blocked" in row.notes.lower()
        # No payload, but the text is the account's own bio (chrome is empty)
        # -> not a block; falls through to "payload not seen".
        page = _Page(body=notice, chrome="")
        sc, _ = _scraper(monkeypatch, page)
        row = await sc.process("https://www.tiktok.com/@x1", "Adani", "")
        assert row.status == "PARTIAL"
        assert "payload not seen" in row.notes


class TestTikTokRefusalIsNotSessionFatal:
    @pytest.mark.asyncio
    async def test_a_403_note_is_not_read_as_a_dead_session(self, monkeypatch):
        page = _Page(goto_status=403, goto_raises=RuntimeError("net::ERR_HTTP_RESPONSE_CODE_FAILURE"))
        sc, ctx = _scraper(monkeypatch, page)
        row = await sc.process("https://www.tiktok.com/@gone_handle", "Adani", "")
        assert row.status == "ERROR"
        assert "access denied" in row.notes
        assert classify_failure(row.notes) is None
        assert ctx.request_touched is False, "a non-browser request was made"

    @pytest.mark.asyncio
    async def test_a_dead_network_has_no_status_and_says_so(self, monkeypatch):
        page = _Page(goto_raises=RuntimeError("net::ERR_CONNECTION_RESET"))
        sc, ctx = _scraper(monkeypatch, page)
        row = await sc.process("https://www.tiktok.com/@gone_handle", "Adani", "")
        assert row.status == "ERROR" and "navigation failed" in row.notes
        assert ctx.request_touched is False

    @pytest.mark.asyncio
    async def test_anonymous_walls_are_honest_errors_not_cookie_verdicts(self, monkeypatch):
        page = _Page(body="Log in to TikTok", chrome="Log in to TikTok")
        sc, _ = _scraper(monkeypatch, page, anonymous=True)
        row = await sc.process("https://www.tiktok.com/@someone", "Adani", "")
        assert row.status == "ERROR"
        assert "cookies" not in row.notes.lower()
        assert classify_failure(row.notes) is None

        page = _Page(body="Drag the slider to verify", chrome="Drag the slider to verify")
        sc, _ = _scraper(monkeypatch, page, anonymous=True)
        row = await sc.process("https://www.tiktok.com/@someone", "Adani", "")
        assert row.status == "ERROR"
        assert "checkpoint" not in row.notes.lower()
        assert classify_failure(row.notes) is None

    @pytest.mark.asyncio
    async def test_logged_in_walls_keep_their_session_statuses(self, monkeypatch):
        page = _Page(body="Log in to TikTok", chrome="Log in to TikTok")
        sc, _ = _scraper(monkeypatch, page)
        assert (await sc.process("https://www.tiktok.com/@someone", "A", "")).status == "LOGIN_REQUIRED"
        page = _Page(body="Drag the slider", chrome="Drag the slider")
        sc, _ = _scraper(monkeypatch, page)
        assert (await sc.process("https://www.tiktok.com/@someone", "A", "")).status == "CHECKPOINT"


class TestTikTokUrls:
    @pytest.mark.parametrize("path", [
        "t/ZMabc123", "embed/v2/123", "legal/terms", "business/x", "about",
        "foryou", "explore", "video/123", "live", "",
    ])
    def test_route_names_are_not_profiles(self, path):
        assert A.username_of(f"https://www.tiktok.com/{path}") == ""

    def test_only_an_at_segment_is_a_profile(self):
        assert A.username_of("https://www.tiktok.com/@foo") == "foo"
        assert A.username_of("https://www.tiktok.com/@foo/video/123") == "foo"
        assert A.username_of("https://vm.tiktok.com/ZMabc123/") == ""
        assert A.username_of("https://www.tiktok.com/@") == ""

    @pytest.mark.asyncio
    async def test_a_short_link_is_refused_without_opening_a_page(self, monkeypatch):
        page = _Page()
        sc, ctx = _scraper(monkeypatch, page)
        row = await sc.process("https://vm.tiktok.com/ZMabc123/", "Adani", "")
        assert row.status == "ERROR" and "not a profile url" in row.notes.lower()
        assert ctx.pages_opened == 0

    @pytest.mark.asyncio
    async def test_a_video_link_analyses_the_profile_page_not_the_pasted_path(self, monkeypatch):
        page = _Page(body="Adani Group\n120.4K Followers\n", hydration=_hydration("foo"))
        sc, _ = _scraper(monkeypatch, page)
        row = await sc.process("https://www.tiktok.com/@foo/video/7240000000", "Adani", "")
        assert page.goto_urls == ["https://www.tiktok.com/@foo"]
        assert row.profile_id == "foo"
        assert row.canonical_url == "https://www.tiktok.com/@foo"


class TestTikTokDomFallback:
    @pytest.mark.asyncio
    async def test_a_heading_alone_is_not_a_profile(self, monkeypatch):
        page = _Page(body="Something went wrong", header={"nickname": "Something went wrong"})
        sc, _ = _scraper(monkeypatch, page)
        row = await sc.process("https://www.tiktok.com/@someone", "Adani", "")
        assert row.status == "PARTIAL"
        assert not row.profile_name
        assert "payload not seen" in row.notes

    @pytest.mark.asyncio
    async def test_a_followers_count_is_proof(self, monkeypatch):
        page = _Page(body="x", header={"nickname": "Real Name", "followers": "1.2M",
                                       "following": "3", "avatar": "https://cdn/a.jpg"})
        sc, _ = _scraper(monkeypatch, page)
        row = await sc.process("https://www.tiktok.com/@someone", "Adani", "")
        assert row.status == "OK"
        assert row.followers == 1_200_000 and row.followers_exact == "no"
        assert row.src["followers"] == "dom-header"

    @pytest.mark.asyncio
    async def test_the_title_line_naming_this_handle_is_proof(self, monkeypatch):
        page = _Page(body="x", header={"nickname": "Real Name", "title": "@Someone"})
        sc, _ = _scraper(monkeypatch, page)
        row = await sc.process("https://www.tiktok.com/@someone", "Adani", "")
        assert row.profile_name == "Real Name"

    @pytest.mark.asyncio
    async def test_a_title_for_another_account_is_not_proof(self, monkeypatch):
        page = _Page(body="x", header={"nickname": "Viewer", "title": "@viewer"})
        sc, _ = _scraper(monkeypatch, page)
        row = await sc.process("https://www.tiktok.com/@someone", "Adani", "")
        assert row.status == "PARTIAL" and not row.profile_name

    def test_the_header_script_does_not_use_the_sidebar_avatar_selector(self):
        assert "img[class*=\"avatar\"" not in A.Scraper.JS_HEADER
        assert "header img" not in A.Scraper.JS_HEADER
        assert "user-avatar" in A.Scraper.JS_HEADER


class TestTikTokLastPostAndOverlay:
    @pytest.mark.asyncio
    async def test_a_search_derived_date_is_marked_a_lower_bound(self, monkeypatch):
        page = _Page(body="Adani Group\n120.4K Followers\n", hydration=_hydration())
        sc, _ = _scraper(monkeypatch, page)

        async def _search(*_a, **_kw):
            return "2020-01-05"

        monkeypatch.setattr(A, "newest_post_via_search", _search)
        row = await sc.process("https://www.tiktok.com/@adanigroup", "Adani", "")
        assert row.last_post_iso == "2020-01-05"
        assert row.src["last_post"] == A.LAST_POST_LOWER_BOUND == "search-lowerbound"
        assert "lower bound" in row.notes

    @pytest.mark.asyncio
    async def test_a_challenge_overlay_over_a_loaded_profile_is_partial(self, monkeypatch):
        page = _Page(body="Adani Group\nDrag the slider to verify", chrome="Drag the slider to verify",
                     hydration=_hydration())
        sc, _ = _scraper(monkeypatch, page)
        called = []

        async def _search(*_a, **_kw):
            called.append(1)
            return "2020-01-05"

        monkeypatch.setattr(A, "newest_post_via_search", _search)
        row = await sc.process("https://www.tiktok.com/@adanigroup", "Adani", "")
        assert row.status == "PARTIAL"
        assert "verification challenge" in row.notes
        assert row.followers == 120400            # the payload fields are still kept
        assert not called, "an extra request was spent on a challenged session"


# ======================================================================
# YouTube
# ======================================================================


class _FakeYT:
    def __init__(self, channels=None, handle=None, handle_raises=None):
        self._channels = channels or {}
        self._handle = handle
        self._handle_raises = handle_raises
        self.handle_calls: list[str] = []
        self.latest_calls = 0
        self.on_latest = None

    async def channels(self, ids):
        return [self._channels[i] for i in ids if i in self._channels]

    async def channel_by_handle(self, ref):
        self.handle_calls.append(ref)
        if self._handle_raises:
            raise self._handle_raises
        return self._handle

    async def latest_upload(self, _pl):
        self.latest_calls += 1
        if self.on_latest:
            self.on_latest()
        return "2026-01-02"


def _yt_channel(cid="UC" + "a" * 22, subs="1230", **extra):
    return {
        "id": cid,
        "snippet": {"title": "Acme", "customUrl": "@acme", "publishedAt": "2020-01-01T00:00:00Z"},
        "statistics": {"subscriberCount": subs, "videoCount": "3", **extra},
        "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}},
    }


def _yt_scraper(monkeypatch, api, args=None):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    sc = YA.Scraper(args or ScanOptions(timeout=5, settle=0.01))
    sc.api = api
    return sc


class TestYouTubeFollowers:
    def test_only_a_count_under_a_thousand_is_exact(self):
        r = Row(url="u", target="t")
        YA.Scraper.fill(r, _yt_channel(subs="999"))
        assert (r.followers, r.followers_exact) == (999, "yes")
        r = Row(url="u", target="t")
        YA.Scraper.fill(r, _yt_channel(subs="1230"))
        assert (r.followers, r.followers_exact) == (1230, "no")

    def test_a_hidden_count_stays_unknown(self):
        r = Row(url="u", target="t")
        YA.Scraper.fill(r, _yt_channel(subs="0", hiddenSubscriberCount=True))
        assert r.followers is None and r.followers_exact == ""


class TestYouTubeDeletedChannelCostsNothingExtra:
    @pytest.mark.asyncio
    async def test_process_skips_the_handle_endpoints_for_an_id(self, monkeypatch):
        api = _FakeYT(channels={}, handle=_yt_channel())
        sc = _yt_scraper(monkeypatch, api)
        row = await sc.process(f"https://www.youtube.com/channel/UC{'z' * 22}", "Acme", "")
        assert row.status == "GONE"
        assert api.handle_calls == []

    @pytest.mark.asyncio
    async def test_run_skips_the_handle_endpoints_for_an_id(self, monkeypatch):
        api = _FakeYT(channels={}, handle=_yt_channel())
        sc = _yt_scraper(monkeypatch, api)
        rows = await sc.run([(f"https://www.youtube.com/channel/UC{'z' * 22}", "Acme", "")])
        assert [r.status for r in rows] == ["GONE"]
        assert api.handle_calls == []

    @pytest.mark.asyncio
    async def test_a_handle_still_uses_the_handle_lookup(self, monkeypatch):
        api = _FakeYT(handle=_yt_channel())
        sc = _yt_scraper(monkeypatch, api)
        rows = await sc.run([("https://www.youtube.com/@acme", "Acme", "")])
        assert rows[0].status == "OK" and api.handle_calls == ["@acme"]


class TestYouTubeHandleLookupErrors:
    def _api(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
        return YD.YouTubeAPI()

    @pytest.mark.asyncio
    async def test_a_transient_error_is_not_a_missing_channel(self, monkeypatch):
        api = self._api(monkeypatch)
        searched = []

        async def _get(endpoint, **params):
            raise RuntimeError("youtube channels 503: backend error")

        async def _search(*_a, **_kw):
            searched.append(1)
            return [], ""

        monkeypatch.setattr(api, "get", _get)
        monkeypatch.setattr(api, "search_channels", _search)
        with pytest.raises(RuntimeError):
            await api.channel_by_handle("@acme")
        assert not searched

    @pytest.mark.asyncio
    async def test_only_a_clean_empty_answer_falls_through(self, monkeypatch):
        api = self._api(monkeypatch)
        seen = []

        async def _get(endpoint, **params):
            seen.append(params)
            return {"items": []}

        async def _search(*_a, **_kw):
            return [], ""

        monkeypatch.setattr(api, "get", _get)
        monkeypatch.setattr(api, "search_channels", _search)
        assert await api.channel_by_handle("@acme") is None
        assert len(seen) == 3

    @pytest.mark.asyncio
    async def test_a_failed_lookup_surfaces_as_an_error_row_not_gone(self, monkeypatch):
        api = _FakeYT(handle_raises=RuntimeError("youtube channels 503: backend error"))
        sc = _yt_scraper(monkeypatch, api)
        rows = await sc.run([("https://www.youtube.com/@acme", "Acme", "")])
        assert rows[0].status == "ERROR"


class TestYouTubeBatchHonoursCancel:
    @pytest.mark.asyncio
    async def test_the_batch_stops_between_channels_and_returns_what_it_read(self, monkeypatch):
        cancel = asyncio.Event()
        chans = {f"UC{n}" + "a" * 20: _yt_channel(f"UC{n}" + "a" * 20) for n in range(3)}
        api = _FakeYT(channels=chans)
        api.on_latest = cancel.set                    # the stop lands after the first channel
        sc = _yt_scraper(monkeypatch, api, ScanOptions(timeout=5, settle=0.01, cancel=cancel))
        sink: list[Row] = []
        jobs = [(f"https://www.youtube.com/channel/{cid}", "Acme", "") for cid in chans]
        rows = await sc.run(jobs, rows_out=sink)
        assert len(rows) == 1 and rows == sink
        assert rows[0].status == "OK"
        assert api.latest_calls == 1


# ======================================================================
# Runner: URLs, dedupe, cancel, timeouts, anonymous sessions
# ======================================================================


class _Pool:
    def __init__(self):
        self.failed: list[tuple] = []

    async def mark_session_failed(self, platform_id, session_id, reason="expired", detail=""):
        self.failed.append((session_id, reason))


@pytest.fixture
def pool(monkeypatch):
    p = _Pool()
    monkeypatch.setattr(R.sessions_engine, "mark_session_failed", p.mark_session_failed)
    saved: list = []

    async def _save(doc, **kw):
        saved.append(doc)

    monkeypatch.setattr(R.results_db, "save", _save)
    p.saved = saved
    return p


def _job(platform, urls):
    items = [R.AnalysisItem(id=f"i{n}", raw_url=u, url=u, platform=platform, entity_id=f"e{n}")
             for n, u in enumerate(urls)]
    job = R.AnalysisJob(id="job1", items=items, total=len(items), target_name="Acme")
    job.platform_progress[platform] = {"status": "running", "total": len(items), "completed": 0,
                                       "display_name": platform}
    return job, items


class TestDedupeAndUrls:
    async def _start(self, monkeypatch, urls):
        async def _noop(self, job):
            return None

        monkeypatch.setattr(R.AnalysisRunner, "_run", _noop)
        job, skipped = await R.AnalysisRunner().start(urls)
        if job.task:
            await job.task
        return job, skipped

    @pytest.mark.asyncio
    async def test_spelling_differences_are_one_account(self, monkeypatch):
        job, skipped = await self._start(monkeypatch, [
            "https://twitter.com/Foo",
            "https://x.com/foo/?s=20",
            "HTTPS://X.COM/FOO/",
            "x.com/foo#frag",
            "https://x.com/bar",
        ])
        assert [i.raw_url for i in job.items] == ["https://twitter.com/Foo", "https://x.com/bar"]
        assert len(skipped) == 3
        for s in skipped:
            assert "duplicate of https://twitter.com/Foo" in s["reason"]

    @pytest.mark.asyncio
    async def test_every_input_comes_out_exactly_once(self, monkeypatch):
        urls = ["https://x.com/a", "https://X.com/A/", "https://example.com/z", "https://tiktok.com/@a"]
        job, skipped = await self._start(monkeypatch, urls)
        assert len(job.items) + len(skipped) == len(urls)

    @pytest.mark.asyncio
    async def test_different_accounts_and_platforms_are_not_collapsed(self, monkeypatch):
        job, skipped = await self._start(monkeypatch, [
            "https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa",
            "https://www.youtube.com/channel/UCAAAAAAAAAAAAAAAAAAAAAA",
            "https://www.youtube.com/@Acme", "https://www.youtube.com/@acme",
            "https://x.com/acme", "https://www.tiktok.com/@acme",
        ])
        assert len(job.items) == 5 and len(skipped) == 1

    def test_scheme_is_case_insensitive(self):
        p = R.parse_direct_url("HTTPS://X.com/Foo")
        assert p and p[0] == "twitter" and p[1] == "https://X.com/Foo"
        assert R.parse_direct_url("Http://t.me/foo")[0] == "telegram"

    def test_trivially_safe_extra_hosts(self):
        assert R.parse_direct_url("https://telegram.dog/foo")[0] == "telegram"
        assert R.parse_direct_url("https://mbasic.facebook.com/foo")[0] == "facebook"
        assert R.parse_direct_url("https://notfacebook.com/foo") is None

    def test_a_tiktok_video_link_names_the_author_not_the_video(self):
        assert R.parse_direct_url("https://www.tiktok.com/@foo/video/123")[2] == "foo"


class TestCancelledItemsAreNeverCountedOrLeftRunning:
    @pytest.mark.asyncio
    async def test_a_hard_cancel_is_not_counted_and_is_stated(self, pool):
        job, (it,) = _job("tiktok", ["https://www.tiktok.com/@a"])

        class Hang:
            async def one(self, *a, **kw):
                await asyncio.sleep(30)

        task = asyncio.create_task(
            R.AnalysisRunner()._scrape_one(job, it, Hang(), "tiktok", {"id": "s"}))
        await asyncio.sleep(0.05)
        assert it.status == "running"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert job.completed == 0
        assert job.platform_progress["tiktok"]["completed"] == 0
        assert it.status == "error" and it.error == R.NOT_ANALYSED_CANCELLED
        assert not pool.saved, "a cancelled item must not overwrite a saved reading"

    @pytest.mark.asyncio
    async def test_a_finished_item_is_counted_once(self, pool):
        job, (it,) = _job("tiktok", ["https://www.tiktok.com/@a"])

        class Ok:
            async def one(self, url, target, feed, known=None):
                return Row(url=url, target=target, status="OK", profile_name="A")

        await R.AnalysisRunner()._scrape_one(job, it, Ok(), "tiktok", {"id": "s"})
        assert it.status == "done" and job.completed == 1

    def test_unread_items_of_a_stopped_job_are_stated_and_done_ones_kept(self):
        job, items = _job("tiktok", [f"https://www.tiktok.com/@u{n}" for n in range(3)])
        items[0].status = "done"
        items[1].status = "running"
        R.AnalysisRunner._mark_unread_not_analysed(job)
        assert items[0].status == "done"
        for it in items[1:]:
            assert it.status == "error" and it.error == "cancelled -- not analysed"
            assert it.row_status == "CANCELLED"


class TestBoundedAwaits:
    @pytest.mark.asyncio
    async def test_a_hung_scraper_gives_an_error_row_and_never_raises(self, monkeypatch, pool):
        monkeypatch.setattr(R, "_MIN_STEP_TIMEOUT_S", 0.05)
        monkeypatch.setattr(R.settings, "request_timeout_sec", 0)
        job, (it,) = _job("tiktok", ["https://www.tiktok.com/@a"])

        class Hang:
            async def one(self, *a, **kw):
                await asyncio.sleep(30)

        fatal = await R.AnalysisRunner()._scrape_one(job, it, Hang(), "tiktok", {"id": "s"})
        assert fatal is False
        assert it.status == "error" and "timed out" in it.comments
        assert job.completed == 1
        assert not pool.failed, "a hung page is not a dead session"

    def test_the_bound_is_generous_and_scales_with_the_navigation_timeout(self, monkeypatch):
        monkeypatch.setattr(R.settings, "request_timeout_sec", 10)
        assert R._step_timeout_s() == 120.0
        monkeypatch.setattr(R.settings, "request_timeout_sec", 90)
        assert R._step_timeout_s() == 270.0

    @pytest.mark.asyncio
    async def test_a_hung_start_becomes_a_runtime_error_naming_it(self, monkeypatch):
        monkeypatch.setattr(R, "_MIN_STEP_TIMEOUT_S", 0.05)
        monkeypatch.setattr(R.settings, "request_timeout_sec", 0)
        with pytest.raises(RuntimeError, match="browser start timed out"):
            await R._bounded(asyncio.sleep(30), "tiktok browser start")


class TestAnonymousSessionsAreNeverFailed:
    @staticmethod
    def _scraper_returning(status, note):
        class S:
            async def one(self, url, target, feed, known=None):
                r = Row(url=url, target=target, status=status)
                r.note(note)
                return r
        return S()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status,note", [
        ("ERROR", "refused (HTTP 403) -- access denied"),
        ("LOGIN_REQUIRED", "cookies rejected/expired"),
        ("CHECKPOINT", "session checkpointed"),
    ])
    async def test_an_anonymous_row_note_never_fails_the_session_or_the_worker(
            self, pool, status, note):
        job, (it,) = _job("tiktok", ["https://www.tiktok.com/@a"])
        fatal = await R.AnalysisRunner()._scrape_one(
            job, it, self._scraper_returning(status, note), "tiktok",
            {"id": "", "anonymous": True})
        assert fatal is False
        assert pool.failed == []
        assert it.status == "error"          # still an honest error row

    @pytest.mark.asyncio
    async def test_a_pooled_session_is_still_failed_on_a_real_wall(self, pool):
        job, (it,) = _job("tiktok", ["https://www.tiktok.com/@a"])
        fatal = await R.AnalysisRunner()._scrape_one(
            job, it, self._scraper_returning("CHECKPOINT", "session checkpointed"),
            "tiktok", {"id": "acct"})
        assert fatal is True and pool.failed == [("acct", "checkpointed")]


class TestLowerBoundActivity:
    def _populate(self, iso):
        job, (it,) = _job("tiktok", ["https://www.tiktok.com/@a"])
        row = Row(url=it.url, target="Acme", status="OK", profile_name="A", last_post_iso=iso)
        row.mark("last_post", "search-lowerbound")
        asyncio.run(R.AnalysisRunner()._populate(job, it, row))
        return it

    def test_an_old_lower_bound_leaves_active_unknown(self):
        assert self._populate("2019-01-01").is_active is None

    def test_a_recent_lower_bound_still_proves_active(self):
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        assert self._populate(today).is_active is True

    def test_a_read_date_keeps_its_definite_answer(self):
        job, (it,) = _job("tiktok", ["https://www.tiktok.com/@a"])
        row = Row(url=it.url, target="Acme", status="OK", profile_name="A", last_post_iso="2019-01-01")
        row.mark("last_post", "hydration")
        asyncio.run(R.AnalysisRunner()._populate(job, it, row))
        assert it.is_active is False


class TestYouTubeBatchInTheRunner:
    @staticmethod
    def _rows(items, n=None):
        return [Row(url=i.url, target="Acme", status="OK", profile_name=f"N{k}",
                    entity_type="channel")
                for k, i in enumerate(items[:n])]

    @pytest.mark.asyncio
    async def test_one_bad_item_does_not_strand_its_siblings(self, monkeypatch, pool):
        job, items = _job("youtube", [f"https://www.youtube.com/@c{n}" for n in range(3)])
        runner = R.AnalysisRunner()
        rows = self._rows(items)

        class Sc:
            async def run(self, jobs, known_by_url=None, rows_out=None):
                rows_out.extend(rows)
                return rows

        real = R.AnalysisRunner._populate

        async def _populate(self, job, it, row, known=None):
            if it.id == "i1":
                raise ValueError("boom")
            return await real(self, job, it, row, known)

        monkeypatch.setattr(R.AnalysisRunner, "_populate", _populate)
        await runner._scrape_youtube_batch(
            job, "youtube", items, Sc(), {"id": "k"}, job.platform_progress["youtube"])
        assert [i.status for i in items] == ["done", "error", "done"]
        assert "boom" in items[1].error
        assert job.completed == 3
        assert len(pool.saved) == 3, "every item, including the failed one, is saved"

    @pytest.mark.asyncio
    async def test_a_hard_cancel_keeps_the_channels_already_read(self, pool):
        job, items = _job("youtube", [f"https://www.youtube.com/@c{n}" for n in range(3)])
        runner = R.AnalysisRunner()
        rows = self._rows(items, 2)

        class Sc:
            async def run(self, jobs, known_by_url=None, rows_out=None):
                rows_out.extend(rows)
                await asyncio.sleep(30)

        task = asyncio.create_task(runner._scrape_youtube_batch(
            job, "youtube", items, Sc(), {"id": "k"}, job.platform_progress["youtube"]))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [i.status for i in items[:2]] == ["done", "done"]
        assert job.completed == 2
        assert len(pool.saved) == 2
        runner._mark_unread_not_analysed(job)
        assert items[2].status == "error" and items[2].error == R.NOT_ANALYSED_CANCELLED

    @pytest.mark.asyncio
    async def test_a_cooperative_cancel_is_not_reported_as_a_quota_failure(self, pool):
        job, items = _job("youtube", [f"https://www.youtube.com/@c{n}" for n in range(3)])
        job.cancel.set()
        rows = self._rows(items, 1)

        class Sc:
            async def run(self, jobs, known_by_url=None, rows_out=None):
                rows_out.extend(rows)
                return rows

        await R.AnalysisRunner()._scrape_youtube_batch(
            job, "youtube", items, Sc(), {"id": "k"}, job.platform_progress["youtube"])
        assert items[0].status == "done"
        assert all("quota" not in (i.error or "") for i in items)
        R.AnalysisRunner._mark_unread_not_analysed(job)
        assert items[1].error == R.NOT_ANALYSED_CANCELLED
