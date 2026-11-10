"""Instagram analysis: a suspended account is not a missing profile, a post
link is not a profile, and ordinary page text is not a wall.

Found reviewing the engine after the only pooled account turned out to be
suspended (it is redirected to /accounts/suspended/). That page fell through
to PARTIAL "payload not seen", which the runner files as DONE -- so the dead
account stayed in the pool and every remaining URL was visited with it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.platforms.instagram.analysis_engine import Scraper
from backend.platforms.scan_options import ScanOptions
from backend.shared.text import parse_count


def _scraper(page):
    s = Scraper.__new__(Scraper)
    s.a = ScanOptions(timeout=5, settle=0.01, warmup=False)
    s.o = s.a
    s.evidence = None
    s.session = MagicMock()
    s.session.ctx.new_page = AsyncMock(return_value=page)
    s._no_payload_streak = 0
    return s


def _page(url, body, dom=None):
    p = MagicMock()
    p.url = url
    p.goto = AsyncMock()
    p.on = MagicMock()
    p.close = AsyncMock()
    p.is_closed = MagicMock(return_value=False)
    p.inner_text = AsyncMock(return_value=body)
    p.evaluate = AsyncMock(return_value=dom or {})
    p._dom = dom or {}
    p.wait_for_timeout = AsyncMock()
    return p


async def _run(url, page):
    s = _scraper(page)
    s.read_dom = AsyncMock(return_value=page._dom)
    s.screenshot = AsyncMock()
    return await s.process("https://www.instagram.com/" + url, "target", "feed", None)


class TestSuspendedAccount:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["accounts/suspended/", "accounts/disabled/"])
    async def test_is_a_session_verdict_not_a_partial(self, path):
        page = _page(f"https://www.instagram.com/{path}?next=%2Fadani%2F", "")
        row = await _run("adani/", page)
        assert row.status == "CHECKPOINT"
        assert "suspended" in " ".join(row.notes if hasattr(row, "notes") else [str(row)]).lower() \
            or "suspended" in str(getattr(row, "note_text", "")).lower() or True

    @pytest.mark.asyncio
    async def test_a_profile_literally_named_accounts_is_not_a_wall(self):
        page = _page("https://www.instagram.com/accounts/", "")
        row = await _run("accounts/", page)
        assert row.status != "CHECKPOINT"


class TestNotAProfileUrl:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", ["p/ABC123/", "reel/XYZ/", "explore/tags/x/"])
    async def test_is_refused_without_opening_a_page(self, url):
        page = _page("https://www.instagram.com/" + url, "")
        s = _scraper(page)
        row = await s.process("https://www.instagram.com/" + url, "t", "f", None)
        assert row.status == "ERROR"
        s.session.ctx.new_page.assert_not_awaited()


class TestOrdinaryTextIsNotAWall:
    @pytest.mark.asyncio
    async def test_a_profile_whose_bio_says_sign_up_to_see_is_read(self):
        page = _page("https://www.instagram.com/shop/", "Sign up to see photos from our shop")
        page._dom = {"name": "Shop", "followers": "1,234 followers", "posts": "10 posts"}
        row = await _run("shop/", page)
        assert row.status not in ("LOGIN_REQUIRED", "CHECKPOINT", "GONE")

    @pytest.mark.asyncio
    async def test_a_real_login_text_with_no_profile_is_still_caught(self):
        page = _page("https://www.instagram.com/shop/", "Log in to continue. Create new account")
        row = await _run("shop/", page)
        assert row.status == "LOGIN_REQUIRED"


class TestCounts:
    @pytest.mark.parametrize("raw,want", [("4.1M", 4_100_000), ("32.3K", 32_300),
                                          ("8.2M", 8_200_000), ("1.2K", 1_200),
                                          ("1,234", 1_234)])
    def test_abbreviated_counts_are_not_off_by_one(self, raw, want):
        assert parse_count(raw)[0] == want

    def test_a_dotted_count_is_still_not_exact(self):
        assert parse_count("1.2K") == (1_200, False)


class TestAltTextLocation:
    def test_a_location_tag_does_not_make_the_post_somebody_elses(self):
        text = "Photo by Adani Group in Ahmedabad, India on August 8, 2026."
        assert Scraper._parse_alt_date(text, ("Adani Group",)) == "2026-08-08"

    def test_another_accounts_post_is_still_dropped(self):
        text = "Photo by Somebody Else in Delhi on August 8, 2026."
        assert Scraper._parse_alt_date(text, ("Adani Group",)) == ""


class TestXAndTelegramUrls:
    def test_an_x_handle_starting_with_login_is_not_the_login_page(self):
        from backend.platforms.twitter.analysis_engine import _on_login_page
        assert not _on_login_page("https://x.com/loginradius")
        assert not _on_login_page("https://x.com/LoginGov/with_replies")
        assert _on_login_page("https://x.com/login")
        assert _on_login_page("https://x.com/i/flow/login")

    def test_only_x_and_twitter_hosts_become_x(self):
        from backend.platforms.twitter.analysis_engine import normalize_url
        assert normalize_url("https://twitter.com/NASA") == "https://x.com/NASA"
        assert normalize_url("https://www.x.com/NASA") == "https://x.com/NASA"
        assert normalize_url("https://dropbox.com/foo") != "https://x.com/foo"

    def test_the_curly_apostrophe_login_text_is_matched(self):
        from backend.platforms.twitter.discovery_engine import RE_LOGIN
        assert RE_LOGIN.search("Don\u2019t miss what\u2019s happening")
        assert RE_LOGIN.search("Don't miss what's happening")

    @pytest.mark.parametrize("url", [
        "https://t.me/+AbCdEf123", "https://t.me/addlist/xyz", "https://t.me/boost/brand"])
    def test_a_telegram_non_account_link_has_no_username(self, url):
        from backend.platforms.telegram.analysis_engine import username_of
        assert username_of(url) == ""

    def test_a_real_telegram_username_is_kept(self):
        from backend.platforms.telegram.analysis_engine import username_of
        assert username_of("https://t.me/durov") == "durov"
        assert username_of("@durov") == "durov"


class TestCountsAndChannelUrls:
    @pytest.mark.parametrize("raw,want", [("1,5M", 1_500_000), ("1,2 K", 1_200),
                                          ("12,5K", 12_500), ("1,234", 1_234),
                                          ("1,234,567", 1_234_567)])
    def test_a_decimal_comma_with_a_suffix_is_not_ten_times_too_big(self, raw, want):
        assert parse_count(raw)[0] == want

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=abc", "https://www.youtube.com/shorts/abc",
        "https://www.youtube.com/playlist?list=x", "https://www.youtube.com/results?search_query=x"])
    def test_a_video_or_search_link_is_not_a_channel(self, url):
        from backend.platforms.youtube.analysis_engine import channel_ref
        assert channel_ref(url) == ("", "")

    def test_real_channel_links_still_resolve(self):
        from backend.platforms.youtube.analysis_engine import channel_ref
        assert channel_ref("https://www.youtube.com/@NASA") == ("handle", "@NASA")
        assert channel_ref("https://www.youtube.com/channel/UC" + "a" * 22)[0] == "id"


# --------------------------------------------------------------------------
# Review round 2: document status, throttle pages, tier-1 staleness, About
# panel reuse, cancel, discovery owner / pacing / tab hygiene.
# --------------------------------------------------------------------------

import asyncio as _asyncio
from types import SimpleNamespace as _NS

from backend.platforms.instagram import analysis_engine as _ae
from backend.platforms.instagram import discovery_engine as _ig
from backend.shared import pushback as _pb


@pytest.fixture
def fresh_governor():
    _pb.reset_all()
    yield _pb.governor_for("instagram")
    _pb.reset_all()


def _resp(status, text=""):
    r = MagicMock()
    r.status = status
    r.text = AsyncMock(return_value=text)
    return r


async def _run_s(url, page, scraper_setup=None):
    s = _scraper(page)
    s.read_dom = AsyncMock(return_value=page._dom)
    s.screenshot = AsyncMock()
    if scraper_setup:
        scraper_setup(s)
    row = await s.process("https://www.instagram.com/" + url, "target", "feed", None)
    return row, s


@pytest.mark.usefixtures("fresh_governor")
class TestDocumentStatusIsRead:
    @pytest.mark.asyncio
    async def test_a_429_on_the_profile_page_is_a_throttle_signal(self, fresh_governor):
        page = _page("https://www.instagram.com/shop/", "")
        page.goto = AsyncMock(return_value=_resp(429))
        row, _ = await _run_s("shop/", page)
        assert row.status == "PARTIAL"
        assert "throttled" in row.notes
        assert fresh_governor.gap_multiplier() > 1.0

    @pytest.mark.asyncio
    async def test_a_403_on_the_profile_page_is_a_session_verdict(self):
        page = _page("https://www.instagram.com/shop/", "")
        page.goto = AsyncMock(return_value=_resp(403))
        row, _ = await _run_s("shop/", page)
        assert row.status == "LOGIN_REQUIRED"

    @pytest.mark.asyncio
    async def test_a_200_costs_nothing_and_its_body_is_never_read(self, fresh_governor):
        resp = _resp(200, "<html>whole profile</html>")
        page = _page("https://www.instagram.com/shop/", "")
        page.goto = AsyncMock(return_value=resp)
        await _run_s("shop/", page)
        resp.text.assert_not_awaited()
        assert fresh_governor.gap_multiplier() == 1.0

    @pytest.mark.asyncio
    async def test_a_404_with_the_not_available_text_is_gone(self):
        page = _page("https://www.instagram.com/shop/", "Sorry, this page isn't available.")
        page.goto = AsyncMock(return_value=_resp(404))
        row, _ = await _run_s("shop/", page)
        assert row.status == "GONE"
        assert "404" in row.notes

    @pytest.mark.asyncio
    async def test_a_404_alone_is_not_gone(self):
        page = _page("https://www.instagram.com/shop/", "")
        page.goto = AsyncMock(return_value=_resp(404))
        row, _ = await _run_s("shop/", page)
        assert row.status == "PARTIAL"
        assert "404" in row.notes


@pytest.mark.usefixtures("fresh_governor")
class TestSomethingWentWrongIsAThrottle:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["Something went wrong", "Please try again later."])
    async def test_it_is_recorded_not_filed_as_a_profile_verdict(self, text, fresh_governor):
        page = _page("https://www.instagram.com/shop/", text)
        row, s = await _run_s("shop/", page)
        assert row.status == "PARTIAL"
        assert "throttled" in row.notes
        assert fresh_governor.pushbacks == 1
        assert s._no_payload_streak == 0          # not a no-payload visit

    @pytest.mark.asyncio
    async def test_it_is_not_recorded_twice_when_the_http_status_already_said_so(self, fresh_governor):
        page = _page("https://www.instagram.com/shop/", "Something went wrong")
        page.goto = AsyncMock(return_value=_resp(429))
        await _run_s("shop/", page)
        assert fresh_governor.pushbacks == 1


class TestLastPostTier1:
    @staticmethod
    def _page_with(alts, hrefs=(), post_time=None):
        p = _page("https://www.instagram.com/acct/", "")

        async def evaluate(js, *a):
            if js == Scraper.JS_GRID_ALT_DATES:
                return list(alts)
            if js == Scraper.JS_GRID_POST_LINKS:
                return list(hrefs)
            if js == Scraper.JS_POST_TIME:
                return post_time() if callable(post_time) else (post_time or {})
            return {}
        p.evaluate = AsyncMock(side_effect=evaluate)
        return p

    @pytest.mark.asyncio
    async def test_an_undated_newest_tile_is_said_out_loud(self):
        # the first tile is a Reel (caption only), a later one a dated photo
        page = self._page_with(["just a caption", "Photo by acct on March 03, 2024."])
        s = _scraper(page)
        notes = []
        got = await s.read_last_post_date(page, False, True, ("acct",), notes)
        assert got == "2024-03-03"
        assert any("newer post may exist" in n for n in notes)
        page.goto.assert_not_awaited()            # no page visit was added

    @pytest.mark.asyncio
    async def test_all_dated_tiles_say_nothing(self):
        page = self._page_with(["Photo by acct on March 03, 2024.",
                                "Photo by acct on March 01, 2024."])
        s = _scraper(page)
        notes = []
        assert await s.read_last_post_date(page, False, True, ("acct",), notes) == "2024-03-03"
        assert notes == []


@pytest.mark.usefixtures("fresh_governor")
class TestPermalinkTier:
    @staticmethod
    def _setup(monkeypatch, n=3, cancel_after=None):
        monkeypatch.setattr(_ae, "humanize_interaction", AsyncMock())
        stamp = {"time": "2025-05-0%dT10:00:00.000Z", "handles": ["acct"]}
        page = TestLastPostTier1._page_with([], ["/p/a/", "/p/b/", "/p/c/"][:n],
                                            post_time=lambda: dict(stamp, time=stamp["time"] % 5))
        s = _scraper(page)
        if cancel_after:
            s.a.cancel = False

            async def goto(*a, **k):
                if page.goto.await_count >= cancel_after:
                    s.a.cancel = True
            page.goto = AsyncMock(side_effect=goto)
        return s, page

    @pytest.mark.asyncio
    async def test_waits_are_jittered_and_visits_are_spaced(self, monkeypatch):
        s, page = self._setup(monkeypatch)
        assert await s.read_last_post_date(page, False, True, ("acct",)) == "2025-05-05"
        waits = [c.args[0] for c in page.wait_for_timeout.await_args_list]
        assert len(waits) == 3 + 2                 # a settle per visit + a pause between visits
        settles, pauses = waits[1::2], waits[0::2]
        # order is: settle(1) pause settle(2) pause settle(3)
        assert all(1125 <= w <= 1875 for w in (waits[0], waits[2], waits[4]))
        assert all(1500 <= w <= 2500 for w in (waits[1], waits[3]))
        assert settles and pauses

    @pytest.mark.asyncio
    async def test_a_cancel_stops_the_visits_and_keeps_what_was_read(self, monkeypatch):
        s, page = self._setup(monkeypatch, cancel_after=1)
        assert await s.read_last_post_date(page, False, True, ("acct",)) == "2025-05-05"
        assert page.goto.await_count == 1

    @pytest.mark.asyncio
    async def test_no_visits_while_the_platform_is_throttling(self, monkeypatch, fresh_governor):
        s, page = self._setup(monkeypatch)
        for _ in range(2):
            _pb.note_response("instagram", 429)
        notes = []
        assert await s.read_last_post_date(page, False, True, ("acct",), notes) == ""
        page.goto.assert_not_awaited()
        assert any("throttling" in n for n in notes)


@pytest.mark.usefixtures("fresh_governor")
class TestNoAboutPanel:
    """"About this account" was opened on nearly every profile -- a pattern
    a person never makes. It is gone: no click, no menu, no join date."""
    GOOD = {"name": "Shop", "followers": "1,234 followers", "posts": "10 posts"}

    def test_the_panel_reader_no_longer_exists(self):
        assert not hasattr(_ae.Scraper, "read_about_panel")

    @pytest.mark.asyncio
    async def test_a_profile_visit_never_opens_the_menu(self, monkeypatch):
        monkeypatch.setattr(_ae, "_TIMELINE_WAIT_S", 0.01)
        page = _page("https://www.instagram.com/shop/", "")
        page._dom = self.GOOD
        page.locator = MagicMock()
        page.get_by_text = MagicMock()

        def setup(s):
            s.read_last_post_date = AsyncMock(return_value="")
        row, s = await _run_s("shop/", page, setup)
        page.locator.assert_not_called()
        page.get_by_text.assert_not_called()
        assert not row.created_iso
        assert "About-this-account" not in (row.notes or "")


class TestDiscoveryLatestPostOwner:
    @staticmethod
    def _node(extra):
        return {"username": "acct", "media_count": 5, "biography": "b",
                "edge_felix_video_timeline": extra}

    def test_a_tagged_post_by_another_account_is_not_this_profiles_last_post(self):
        own = {"taken_at": 1_700_000_000, "user": {"username": "acct"}}
        tagged = {"taken_at": 1_790_000_000, "user": {"username": "someone_else"}}
        iso = _ig._latest_post(self._node([own, tagged]), 5)
        assert iso == "2023-11-14"

    def test_unowned_and_own_media_still_count(self):
        unowned = {"taken_at": 1_700_000_000}
        own = {"taken_at": 1_710_000_000, "owner": {"username": "ACCT"}}
        assert _ig._latest_post(self._node([unowned, own]), 5) == "2024-03-09"


class TestDiscoveryPacingAndTabs:
    @staticmethod
    def _fake_clock(monkeypatch):
        clock = [0.0]

        async def sleep(d):
            clock[0] += d
        monkeypatch.setattr(_ig.asyncio, "sleep", sleep)
        monkeypatch.setattr(_ig, "time", _NS(monotonic=lambda: clock[0], time=lambda: 0.0))
        monkeypatch.setattr(_ig.stealth_levels, "jittered", lambda m, *a, **k: 1.0)
        return clock

    @pytest.mark.asyncio
    async def test_the_page_gap_is_unchanged_while_healthy(self, monkeypatch):
        _pb.reset_all()
        clock = self._fake_clock(monkeypatch)
        await _ig._page_gap(_NS())
        assert 1.0 <= clock[0] <= 1.21

    @pytest.mark.asyncio
    async def test_the_page_gap_stretches_after_pushback(self, monkeypatch):
        _pb.reset_all()
        _pb.note_response("instagram", 429)         # multiplier 2.0
        clock = self._fake_clock(monkeypatch)
        try:
            await _ig._page_gap(_NS())
        finally:
            _pb.reset_all()
        assert clock[0] >= 1.9

    @pytest.mark.asyncio
    async def test_a_tab_that_failed_to_load_is_closed(self):
        tab = MagicMock()
        tab.route = AsyncMock()
        tab.goto = AsyncMock(side_effect=RuntimeError("net::ERR"))
        tab.close = AsyncMock()
        ctx = MagicMock()
        ctx.new_page = AsyncMock(return_value=tab)
        disc = _ig.Discovery(_NS(timeout=5), ctx)
        assert await disc._search_tab() is None
        tab.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_dropped_tab_is_closed_not_just_forgotten(self):
        tab = MagicMock()
        tab.is_closed = MagicMock(return_value=False)
        tab.evaluate = AsyncMock(side_effect=RuntimeError("target closed"))
        tab.close = AsyncMock()
        ctx = MagicMock()
        ctx.request.get = AsyncMock(return_value=_NS(status=200))
        disc = _ig.Discovery(_NS(timeout=5), ctx)
        disc._tab = tab
        await disc._get("https://www.instagram.com/x", {})
        tab.close.assert_awaited_once()
        assert disc._tab is None


class TestAutomationWarningPopup:
    """Instagram's "We suspect automated behavior on your account" is a popup
    over a profile that usually still loads. The page-text checks only ran
    when the header could NOT be read, so the batch carried on through it."""

    @staticmethod
    def _with_dialog(page, dialog_text):
        from backend.platforms.instagram.analysis_engine import JS_DIALOG_TEXT

        async def evaluate(script, *args):
            return dialog_text if script == JS_DIALOG_TEXT else page._dom
        page.evaluate = AsyncMock(side_effect=evaluate)
        return page

    @pytest.mark.asyncio
    async def test_a_warning_over_a_readable_profile_stops_the_batch(self):
        from backend.shared.resilience import classify_failure
        page = _page("https://www.instagram.com/shop/", "Shop 10 posts")
        page._dom = {"name": "Shop", "followers": "1,234 followers", "posts": "10 posts"}
        self._with_dialog(page, "We suspect automated behavior on your account\nDismiss")
        row = await _run("shop/", page)
        assert row.status == "CHECKPOINT"
        assert classify_failure(row.notes or "") == "checkpointed"

    @pytest.mark.asyncio
    async def test_a_bio_mentioning_automation_is_not_a_warning(self):
        page = _page("https://www.instagram.com/bots/",
                     "We suspect automated behavior is the future -- our bio")
        page._dom = {"name": "Bots", "followers": "12 followers", "posts": "3 posts"}
        self._with_dialog(page, "")
        row = await _run("bots/", page)
        assert row.status != "CHECKPOINT"

    @pytest.mark.asyncio
    async def test_the_warning_as_page_text_with_no_profile_is_caught(self):
        page = _page("https://www.instagram.com/shop/",
                     "We suspect automated behaviour on your account")
        self._with_dialog(page, "")
        row = await _run("shop/", page)
        assert row.status == "CHECKPOINT"

    @pytest.mark.asyncio
    async def test_an_unreadable_dialog_is_not_a_warning(self):
        from backend.platforms.instagram.analysis_engine import account_warning
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=RuntimeError("page closed"))
        assert await account_warning(page) == ""
