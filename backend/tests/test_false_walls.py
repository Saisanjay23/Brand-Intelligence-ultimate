"""Ordinary content must never be read as a login/checkpoint wall.

A false wall is read as a dead SESSION: the account is quarantined and the
keyword replayed on the next one, which sees the same page. These pin the
cases found in the review -- and that a REAL wall is still caught.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.platforms.facebook.discovery_engine import (
    RE_CHECKPOINT, _session_blocked)
from backend.platforms.twitter.discovery_engine import (
    _X_CONTENT_SELECTORS, _x_session_blocked)
from backend.shared import pushback
from backend.shared.walls import chrome_text, first_segment, is_wall_url
from backend.stealth.auto_login import _login_is_fresh


def _page(url: str, body: str, evaluate=None):
    p = MagicMock()
    p.url = url
    p.inner_text = AsyncMock(return_value=body)
    if evaluate is not None:
        p.evaluate = AsyncMock(return_value=evaluate)
    return p


class TestAWallIsDecidedByPathNotSubstring:
    def test_real_walls(self):
        assert is_wall_url("https://www.facebook.com/login/?next=x", ("login",))
        assert is_wall_url("https://www.facebook.com/login.php", ("login.php",))
        assert is_wall_url("https://www.facebook.com/checkpoint/1/", ("checkpoint",))
        assert is_wall_url("https://www.instagram.com/challenge/abc/", ("challenge",))

    def test_a_profile_that_merely_contains_the_word_is_not(self):
        assert not is_wall_url("https://www.facebook.com/loginradius", ("login",))
        assert not is_wall_url("https://www.instagram.com/fitnesschallenge/", ("challenge",))
        assert not is_wall_url("https://www.facebook.com/checkpointcharlie", ("checkpoint",))

    def test_a_keyword_in_the_query_string_is_not(self):
        url = "https://www.facebook.com/search/people/?q=hdfcbank.com/login"
        assert first_segment(url) == "search"
        assert not is_wall_url(url, ("login",))

    def test_unparseable_is_not_a_wall(self):
        assert not is_wall_url("", ("login",))
        assert not is_wall_url(None, ("login",))  # type: ignore[arg-type]


class TestFacebookSearchPage:
    @pytest.mark.asyncio
    async def test_a_keyword_with_login_in_it_is_not_a_login_wall(self):
        page = _page("https://www.facebook.com/search/people/?q=bank.com/login", "People")
        assert await _session_blocked(page) == ""

    @pytest.mark.asyncio
    async def test_results_that_mention_suspicious_activity_are_not_a_checkpoint(self):
        body = "Pages\nHDFC Bank\nReport suspicious activity to us.\nBorder Checkpoint"
        page = _page("https://www.facebook.com/search/pages/?q=bank", body)
        assert await _session_blocked(page) == ""
        assert await _session_blocked(page, have_results=True) == ""

    @pytest.mark.asyncio
    async def test_text_is_not_trusted_once_results_exist(self):
        body = "We've temporarily closed our branch. Log in to Facebook to follow."
        page = _page("https://www.facebook.com/search/pages/?q=x", body)
        assert await _session_blocked(page, have_results=True) == ""

    @pytest.mark.asyncio
    async def test_a_real_wall_with_no_results_is_still_caught(self):
        page = _page("https://www.facebook.com/search/people/?q=x",
                     "Confirm Your Identity to continue")
        assert await _session_blocked(page) == "checkpoint"

    @pytest.mark.asyncio
    async def test_a_real_wall_url_is_caught_even_with_results(self):
        page = _page("https://www.facebook.com/checkpoint/123/", "")
        assert await _session_blocked(page, have_results=True) == "checkpoint"

    def test_the_regex_no_longer_matches_ordinary_words(self):
        assert not RE_CHECKPOINT.search("report suspicious activity at the checkpoint")


class TestXPages:
    @pytest.mark.asyncio
    async def test_a_bio_is_not_a_checkpoint(self):
        # chrome_text strips the user cells, so only X's own interface is left.
        page = _page("https://x.com/search?q=acme&f=user",
                     "Confirm your booking with us", evaluate="Search People")
        assert await _x_session_blocked(page, have_results=True) == ""

    @pytest.mark.asyncio
    async def test_x_own_checkpoint_panel_is_still_caught(self):
        page = _page("https://x.com/search?q=acme&f=user", "",
                     evaluate="Verify your identity to continue")
        assert await _x_session_blocked(page, have_results=True) == "checkpoint"

    @pytest.mark.asyncio
    async def test_chrome_text_cuts_the_content_selectors(self):
        page = _page("https://x.com/", "full body", evaluate="chrome only")
        assert await chrome_text(page, _X_CONTENT_SELECTORS) == "chrome only"
        sels = page.evaluate.await_args.args[1]
        assert '[data-testid="UserCell"]' in sels and "article" in sels

    @pytest.mark.asyncio
    async def test_chrome_text_falls_back_to_the_body(self):
        page = _page("https://x.com/", "full body")      # evaluate is a plain mock
        assert await chrome_text(page, _X_CONTENT_SELECTORS) == "full body"


class TestXPushbackIsRecordedWhereItIsRead:
    """The engine recorded X's throttles under "x" while the pacing governor
    and the breaker hold are keyed by the platform id, "twitter" -- so an X
    429 never slowed anything down."""

    def test_every_note_response_names_the_twitter_platform(self):
        import re
        from backend.platforms.twitter import discovery_engine as x
        import inspect
        src = inspect.getsource(x)
        keys = re.findall(r"note_response\(\s*\"([a-z]+)\"", src)
        assert keys and set(keys) == {"twitter"}

    def test_the_platform_id_is_what_the_governor_is_keyed_by(self):
        from backend.platforms import registry
        assert "twitter" in registry.PLATFORMS and "x" not in registry.PLATFORMS


class TestAutoLoginNeedsANewSession:
    def test_facebook_xs_unchanged_means_not_logged_in(self):
        before = {"c_user": "100", "xs": "old"}
        assert not _login_is_fresh("facebook", before, {"c_user": "100", "xs": "old"})

    def test_facebook_xs_rotated_means_logged_in(self):
        before = {"c_user": "100", "xs": "old"}
        assert _login_is_fresh("facebook", before, {"c_user": "100", "xs": "new"})

    def test_a_clean_profile_has_nothing_stale(self):
        assert _login_is_fresh("instagram", {}, {"sessionid": "new"})

    def test_twitter_auth_token_must_change(self):
        assert not _login_is_fresh("twitter", {"auth_token": "a"}, {"auth_token": "a"})
        assert _login_is_fresh("twitter", {"auth_token": "a"}, {"auth_token": "b"})
