"""The curl_cffi accelerator, and the one property that matters about it.

WHAT THESE TESTS ARE ACTUALLY DEFENDING. `shared/fast_http.py` is allowed to
make three paths faster and is not allowed to make any of them wrong, so
almost every assertion below is about the module DECLINING to answer:

    a platform whose logged-out state is not visible over HTTP is not probed
    a 403 is not a dead session, because it is also a bot wall
    a 429 is not a dead profile, because it is also rate limiting
    a transport failure is not a verdict about anything

The pattern to notice is that the third state is never folded into one of
the other two. A two-state answer here would force "I could not tell" into
either "quarantine this account" or "report this profile gone", and both of
those are findings an analyst would act on about something nobody looked at.

Nothing here touches the network: every test drives a faked `fetch`, which
is the module's single exit point to the outside world.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from backend.shared import fast_http
from backend.shared.fast_http import (
    FastHttpTransportError,
    FastResponse,
    PreflightResult,
    SessionVerdict,
    cookie_header,
)


def _resp(status: int, headers: dict | None = None, body: bytes = b"", url: str = "https://x/") -> FastResponse:
    return FastResponse(
        status=status,
        headers={k.lower(): v for k, v in (headers or {}).items()},
        body=body,
        url=url,
    )


# ----------------------------------------------------------- impersonation


def test_impersonate_target_is_one_the_installed_curl_cffi_actually_has():
    """A hardcoded target is a version dependency in disguise: the failure
    mode is a raise at request time, on the avatar path, in production."""
    known = fast_http._supported_targets()
    if not known:                       # curl_cffi too old to publish the list
        pytest.skip("installed curl_cffi does not publish its target list")
    assert fast_http.IMPERSONATE in known
    assert fast_http.IMPERSONATE.startswith("chrome")


def test_impersonate_falls_back_when_the_target_list_is_unreadable():
    with patch.object(fast_http, "_supported_targets", return_value=frozenset()):
        assert fast_http._pick_impersonate() == "chrome"


def test_impersonate_prefers_the_newest_build_offered():
    with patch.object(fast_http, "_supported_targets",
                      return_value=frozenset({"chrome110", "chrome131", "chrome99"})):
        assert fast_http._pick_impersonate() == "chrome131"


# --------------------------------------------------------------- kill switch


def test_settings_kill_switch_disables_every_accelerated_path():
    from backend.config.settings import settings

    original = settings.fast_http_enabled
    try:
        settings.fast_http_enabled = False
        assert fast_http.available() is False
        assert "disabled by settings" in fast_http.why_unavailable()
    finally:
        settings.fast_http_enabled = original
    assert fast_http.why_unavailable() == ""


# ----------------------------------------------------------- cookie plumbing


def test_cookie_header_serialises_stored_mongo_cookies():
    cookies = [
        {"name": "c_user", "value": "100", "domain": ".facebook.com", "path": "/"},
        {"name": "xs", "value": "abc", "domain": ".facebook.com", "path": "/"},
    ]
    assert cookie_header(cookies) == "c_user=100; xs=abc"


def test_cookie_header_drops_already_expired_cookies():
    """Sending a dead cookie is how a session that is merely missing one
    gets reported logged out."""
    cookies = [
        {"name": "alive", "value": "1", "expires": 4_000_000_000},
        {"name": "dead", "value": "2", "expires": 1_000},
        {"name": "session_cookie", "value": "3"},          # no expiry at all
    ]
    out = cookie_header(cookies, now=2_000_000_000)
    assert "alive=1" in out
    assert "session_cookie=3" in out
    assert "dead" not in out


def test_cookie_header_keeps_one_platforms_cookies_out_of_anothers():
    cookies = [
        {"name": "sessionid", "value": "ig", "domain": ".instagram.com"},
        {"name": "auth_token", "value": "x", "domain": ".x.com"},
    ]
    out = cookie_header(cookies, host="www.instagram.com")
    assert out == "sessionid=ig"


def test_cookie_header_survives_malformed_entries():
    cookies = [
        "not a dict",
        {"no_name": 1},
        {"name": "", "value": "blank"},
        {"name": "ok", "value": "1"},
        {"name": "ok", "value": "2"},                       # duplicate
        {"name": "weird_expiry", "value": "3", "expires": "soon"},
    ]
    out = cookie_header(cookies)          # type: ignore[arg-type]
    assert "ok=1" in out
    assert "ok=2" not in out
    assert "weird_expiry=3" in out        # unparseable expiry is not an expiry


# ------------------------------------------------ the session canary verdict


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["twitter", "tiktok", "youtube", "telegram"])
async def test_platforms_without_a_server_side_signal_are_never_probed(platform):
    """X bounces a dead session client-side and TikTok answers a bot wall,
    so an HTTP probe on either would be a confident wrong answer. Not
    probed at all -- and no request is made to find that out."""
    fetch = AsyncMock()
    with patch.object(fast_http, "fetch", fetch):
        verdict = await fast_http.check_session_alive(platform, [{"name": "a", "value": "b"}])
    assert verdict.alive is None
    assert verdict.conclusive is False
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_facebook_me_redirecting_to_a_profile_confirms_a_live_session():
    fetch = AsyncMock(return_value=_resp(302, {"Location": "https://www.facebook.com/zuck"}))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        verdict = await fast_http.check_session_alive(
            "facebook", [{"name": "c_user", "value": "1", "domain": ".facebook.com"}])
    assert verdict.alive is True
    assert verdict.conclusive is True


@pytest.mark.asyncio
@pytest.mark.parametrize("location", [
    "https://www.facebook.com/login.php?next=x",
    "https://www.facebook.com/checkpoint/123",
    "https://www.facebook.com/",
    "https://www.facebook.com/index.php",
])
async def test_facebook_me_redirecting_to_a_logged_out_door_is_a_dead_session(location):
    fetch = AsyncMock(return_value=_resp(302, {"Location": location}))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        verdict = await fast_http.check_session_alive(
            "facebook", [{"name": "c_user", "value": "1", "domain": ".facebook.com"}])
    assert verdict.alive is False


@pytest.mark.asyncio
async def test_instagram_still_on_the_authenticated_page_confirms_a_live_session():
    fetch = AsyncMock(return_value=_resp(200, {"Content-Type": "text/html"}, b"<html>edit profile</html>"))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        verdict = await fast_http.check_session_alive(
            "instagram", [{"name": "sessionid", "value": "1", "domain": ".instagram.com"}])
    assert verdict.alive is True


@pytest.mark.asyncio
async def test_instagram_bounced_to_login_is_a_dead_session():
    fetch = AsyncMock(return_value=_resp(
        302, {"Location": "https://www.instagram.com/accounts/login/?next=/accounts/edit/"}))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        verdict = await fast_http.check_session_alive(
            "instagram", [{"name": "sessionid", "value": "1", "domain": ".instagram.com"}])
    assert verdict.alive is False


@pytest.mark.asyncio
async def test_a_200_that_is_really_a_login_wall_downgrades_to_unknown_not_to_dead():
    """The body gets one cheap look so a wall served AT the authenticated
    URL cannot pass as a live session. It withholds the pass -- it does not
    condemn the account, because these markers are heuristics over raw HTML
    and a wrong 'dead' quarantines a working account."""
    fetch = AsyncMock(return_value=_resp(
        200, {"Content-Type": "text/html"},
        b'<html><div id="loginForm">Log in</div></html>'))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        verdict = await fast_http.check_session_alive(
            "instagram", [{"name": "sessionid", "value": "1", "domain": ".instagram.com"}])
    assert verdict.alive is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_a_rejection_that_could_be_a_bot_wall_is_not_a_session_verdict(status):
    fetch = AsyncMock(return_value=_resp(status))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        verdict = await fast_http.check_session_alive(
            "facebook", [{"name": "c_user", "value": "1", "domain": ".facebook.com"}])
    assert verdict.alive is None


@pytest.mark.asyncio
async def test_a_transport_failure_is_not_a_session_verdict():
    fetch = AsyncMock(side_effect=FastHttpTransportError("DNSError: could not resolve"))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        verdict = await fast_http.check_session_alive(
            "facebook", [{"name": "c_user", "value": "1", "domain": ".facebook.com"}])
    assert verdict.alive is None
    assert "probe could not run" in verdict.reason


@pytest.mark.asyncio
async def test_no_usable_cookies_is_reported_as_unknown_not_as_dead():
    """"Saved without cookies" and "the platform rejected these cookies" are
    different findings and must not collapse into one."""
    fetch = AsyncMock()
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        verdict = await fast_http.check_session_alive(
            "facebook", [{"name": "c_user", "value": "1", "expires": 1_000}])
    assert verdict.alive is None
    fetch.assert_not_awaited()


# ------------------------------------------------------ the dead-URL preflight


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["tiktok", "facebook", "youtube", "telegram"])
async def test_platforms_without_a_provable_death_signal_are_never_preflighted(platform):
    fetch = AsyncMock()
    with patch.object(fast_http, "fetch", fetch):
        res = await fast_http.preflight_url_status("https://example.com/x", platform)
    assert res.is_dead is False
    assert res.checked is False
    fetch.assert_not_awaited()


# What X actually serves on a removed profile, and what Instagram actually
# serves on a missing one. Both measured 2026-09-22; see _DEATH_SIGNALS.
_X_GONE_PAGE = b"<title>User Profile Not Found - X | 404 Error</title>"
_IG_GONE_PAGE = b'<div id="PolarisErrorRoot">page not found</div>'
_IG_LIVE_PAGE = b'<meta property="og:description" content="104M Followers...">'


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
async def test_x_settles_only_when_the_status_and_the_page_agree(status):
    fetch = AsyncMock(return_value=_resp(status, body=_X_GONE_PAGE))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        res = await fast_http.preflight_url_status("https://x.com/nobody_77412", "twitter")
    assert res.is_dead is True
    assert res.status_code == status
    assert str(status) in res.reason        # the row must name the evidence


@pytest.mark.asyncio
async def test_a_404_without_the_platforms_own_wording_settles_nothing():
    """THE BLANKET-404 GUARD AT THE LEVEL OF ONE URL. A client the platform
    has decided to block can 404 everything, but its block page will not be
    carrying X's own removal copy. Requiring both is what tells the two
    apart -- and if X ever rewords that page, this abstains loudly rather
    than going quietly wrong in the other direction."""
    fetch = AsyncMock(return_value=_resp(404, body=b"<title>Rate limited</title>"))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        res = await fast_http.preflight_url_status("https://x.com/someone", "twitter")
    assert res.is_dead is False
    assert res.checked is True
    assert "without the platform" in res.reason


@pytest.mark.asyncio
async def test_instagram_is_settled_by_its_error_page_because_the_status_says_nothing():
    """Instagram answers 200 for a missing handle and 200 for a live one, so
    the status is no help at all. Its own error-page root is."""
    fetch = AsyncMock(return_value=_resp(200, body=_IG_GONE_PAGE))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        res = await fast_http.preflight_url_status(
            "https://www.instagram.com/nobody_77412/", "instagram")
    assert res.is_dead is True
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_real_profile_content_vetoes_a_death_marker_outright():
    """The veto is ordered first on purpose. If a page ever carried both --
    a partial render, a marker that turns up somewhere unexpected -- the
    safe reading is the one that does not condemn a live account."""
    fetch = AsyncMock(return_value=_resp(200, body=_IG_LIVE_PAGE + _IG_GONE_PAGE))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        res = await fast_http.preflight_url_status(
            "https://www.instagram.com/nasa/", "instagram")
    assert res.is_dead is False
    assert "real profile content" in res.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 301, 403, 429, 500, 503])
async def test_anything_short_of_gone_leaves_the_url_for_the_browser(status):
    fetch = AsyncMock(return_value=_resp(status, body=b"<html>an ordinary page</html>"))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        res = await fast_http.preflight_url_status("https://x.com/someone", "twitter")
    assert res.is_dead is False


@pytest.mark.asyncio
async def test_an_instagram_wall_or_rate_limit_is_not_a_missing_profile():
    """Neither marker present means neither question was answered. The
    browser gets it, exactly as before."""
    fetch = AsyncMock(return_value=_resp(200, body=b"<html>Log in to Instagram</html>"))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        res = await fast_http.preflight_url_status(
            "https://www.instagram.com/someone/", "instagram")
    assert res.is_dead is False


@pytest.mark.asyncio
async def test_a_preflight_that_could_not_run_never_reports_a_profile_gone():
    fetch = AsyncMock(side_effect=FastHttpTransportError("ReadTimeout"))
    with patch.object(fast_http, "fetch", fetch), \
         patch.object(fast_http, "available", return_value=True):
        res = await fast_http.preflight_url_status("https://x.com/someone", "twitter")
    assert res.is_dead is False
    assert res.checked is False


@pytest.mark.asyncio
async def test_preflight_many_never_raises_and_keeps_going_past_one_bad_url():
    async def flaky(url, **kw):
        if "boom" in url:
            raise RuntimeError("something nobody predicted")
        return _resp(404, body=_X_GONE_PAGE)

    with patch.object(fast_http, "fetch", AsyncMock(side_effect=flaky)), \
         patch.object(fast_http, "available", return_value=True):
        out = await fast_http.preflight_many([
            ("https://x.com/gone", "twitter"),
            ("https://x.com/boom", "twitter"),
        ])
    assert out["https://x.com/gone"].is_dead is True
    assert out["https://x.com/boom"].is_dead is False


@pytest.mark.asyncio
async def test_preflight_many_respects_its_concurrency_bound():
    live = 0
    peak = 0

    async def slow(url, **kw):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.01)
            return _resp(200)
        finally:
            live -= 1

    pairs = [(f"https://x.com/u{i}", "twitter") for i in range(20)]
    with patch.object(fast_http, "fetch", AsyncMock(side_effect=slow)), \
         patch.object(fast_http, "available", return_value=True):
        await fast_http.preflight_many(pairs, concurrency=3)
    assert peak <= 3


@pytest.mark.asyncio
async def test_preflight_many_gives_up_at_its_budget_and_keeps_what_finished():
    """An optimisation that can add a minute to the front of a job is not an
    optimisation. Whatever did not finish simply has no entry, which every
    caller already reads as 'not proven dead'."""
    async def one_fast_one_hung(url, **kw):
        if "hung" in url:
            await asyncio.sleep(30)
        return _resp(404, body=_X_GONE_PAGE)

    with patch.object(fast_http, "fetch", AsyncMock(side_effect=one_fast_one_hung)), \
         patch.object(fast_http, "available", return_value=True):
        out = await fast_http.preflight_many(
            [("https://x.com/gone", "twitter"),
             ("https://x.com/hung", "twitter")],
            budget=0.2,
        )
    assert out["https://x.com/gone"].is_dead is True
    assert "https://x.com/hung" not in out


# ------------------------------------------------------------- byte bounds


@pytest.mark.asyncio
async def test_fetch_refuses_a_body_that_runs_past_its_cap():
    """The cap is the only thing between this and an unbounded read into
    memory, and it has to survive a body that arrives in many small chunks."""
    class _FakeResp:
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}
        url = "https://scontent.xx.fbcdn.net/p.jpg"

        async def aiter_content(self):
            for _ in range(100):
                yield b"x" * 1024

    class _FakeSession:
        def stream(self, *a, **kw):
            class _CM:
                async def __aenter__(self):
                    return _FakeResp()

                async def __aexit__(self, *exc):
                    return False
            return _CM()

    with patch.object(fast_http, "_session", return_value=_FakeSession()), \
         patch.object(fast_http, "available", return_value=True):
        with pytest.raises(fast_http.FastHttpTooLarge):
            await fast_http.fetch("https://scontent.xx.fbcdn.net/p.jpg", max_bytes=4096)


@pytest.mark.asyncio
async def test_fetch_refuses_the_wrong_content_type_before_reading_a_byte():
    read = False

    class _FakeResp:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        url = "https://pbs.twimg.com/p.jpg"

        async def aiter_content(self):
            nonlocal read
            read = True
            yield b"<html>"

    class _FakeSession:
        def stream(self, *a, **kw):
            class _CM:
                async def __aenter__(self):
                    return _FakeResp()

                async def __aexit__(self, *exc):
                    return False
            return _CM()

    with patch.object(fast_http, "_session", return_value=_FakeSession()), \
         patch.object(fast_http, "available", return_value=True):
        with pytest.raises(fast_http.FastHttpWrongContentType):
            await fast_http.fetch("https://pbs.twimg.com/p.jpg", expect_content_type="image/")
    assert read is False


@pytest.mark.asyncio
async def test_an_http_status_is_an_answer_not_an_exception():
    """403 and 404 come back as responses on purpose: imagefetch decides
    whether to fall back on 'no answer', and a status is an answer."""
    class _FakeResp:
        status_code = 403
        headers = {}
        url = "https://scontent.xx.fbcdn.net/p.jpg"

        async def aiter_content(self):
            return
            yield                                   # pragma: no cover

    class _FakeSession:
        def stream(self, *a, **kw):
            class _CM:
                async def __aenter__(self):
                    return _FakeResp()

                async def __aexit__(self, *exc):
                    return False
            return _CM()

    with patch.object(fast_http, "_session", return_value=_FakeSession()), \
         patch.object(fast_http, "available", return_value=True):
        resp = await fast_http.fetch("https://scontent.xx.fbcdn.net/p.jpg")
    assert resp.status == 403


# --------------------------------------------------------------- data shapes


def test_session_verdict_conclusive_only_when_it_actually_decided():
    assert SessionVerdict(True).conclusive is True
    assert SessionVerdict(False).conclusive is True
    assert SessionVerdict(None).conclusive is False


def test_preflight_result_defaults_to_not_dead_and_not_checked():
    res = PreflightResult("https://example.com/")
    assert res.is_dead is False
    assert res.checked is False
    assert res.to_dict()["is_dead"] is False


# ------------------------------------------------------- marker early-stop


@pytest.mark.asyncio
async def test_reading_stops_as_soon_as_a_marker_appears():
    """A live Instagram profile is 600-800 KB and the marker that clears it
    lands about 9 KB in. Reading the rest is pure cost on every URL."""
    served = 0

    class _FakeResp:
        status_code = 200
        headers = {"Content-Type": "text/html"}
        url = "https://www.instagram.com/nasa/"

        async def aiter_content(self):
            nonlocal served
            for chunk in [b"x" * 100, b"...og:description...", b"y" * 100, b"z" * 100]:
                served += 1
                yield chunk

    class _FakeSession:
        def stream(self, *a, **kw):
            class _CM:
                async def __aenter__(self):
                    return _FakeResp()

                async def __aexit__(self, *exc):
                    return False
            return _CM()

    with patch.object(fast_http, "_session", return_value=_FakeSession()), \
         patch.object(fast_http, "available", return_value=True):
        resp = await fast_http.fetch(
            "https://www.instagram.com/nasa/", stop_markers=(b"og:description",))

    assert served == 2                      # stopped on the marker's chunk
    assert b"og:description" in resp.body


@pytest.mark.asyncio
async def test_a_marker_split_across_two_chunks_is_still_found():
    """Chunk boundaries are wherever the socket put them, so a marker that
    straddles one must not be missed -- a missed alive-marker is a live
    profile reported gone."""
    class _FakeResp:
        status_code = 200
        headers = {}
        url = "https://www.instagram.com/nasa/"

        async def aiter_content(self):
            yield b"aaaa og:desc"
            yield b"ription bbbb"
            yield b"cccc"

    class _FakeSession:
        def stream(self, *a, **kw):
            class _CM:
                async def __aenter__(self):
                    return _FakeResp()

                async def __aexit__(self, *exc):
                    return False
            return _CM()

    with patch.object(fast_http, "_session", return_value=_FakeSession()), \
         patch.object(fast_http, "available", return_value=True):
        resp = await fast_http.fetch(
            "https://www.instagram.com/nasa/", stop_markers=(b"og:description",))

    assert b"og:description" in resp.body
    assert b"cccc" not in resp.body         # stopped at the second chunk


@pytest.mark.asyncio
async def test_running_out_of_budget_while_scanning_is_an_answer_not_an_error():
    """`truncate` is the difference between "read this much and stop" and
    "a body this big is a failure". A scanner wants the first; an avatar
    fetch wants the second, because half an image is not an image."""
    class _FakeResp:
        status_code = 200
        headers = {}
        url = "https://www.instagram.com/someone/"

        async def aiter_content(self):
            for _ in range(50):
                yield b"q" * 1024

    class _FakeSession:
        def stream(self, *a, **kw):
            class _CM:
                async def __aenter__(self):
                    return _FakeResp()

                async def __aexit__(self, *exc):
                    return False
            return _CM()

    with patch.object(fast_http, "_session", return_value=_FakeSession()), \
         patch.object(fast_http, "available", return_value=True):
        resp = await fast_http.fetch(
            "https://www.instagram.com/someone/", max_bytes=4096,
            stop_markers=(b"never-appears",), truncate=True)
        assert len(resp.body) == 4096

        with pytest.raises(fast_http.FastHttpTooLarge):
            await fast_http.fetch(
                "https://www.instagram.com/someone/", max_bytes=4096, truncate=False)
