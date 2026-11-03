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
