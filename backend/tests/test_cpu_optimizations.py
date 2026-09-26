"""CPU optimisations that must not change behaviour.

THE BROWSER REQUEST FILTER. `ctx.route("**/*")` paused every request for
a round trip through Python and, as a side effect of Playwright's routing,
disabled the HTTP cache. The native filter pauses only what it acts on and
leaves the cache on. Measured locally (same Chrome, same LAUNCH_ARGS, six
pages, CPU over Python + driver + every Chrome process): 53.3s -> 31.7s with
images loaded, 44.7s -> 36.4s with images stubbed. Equivalence of what gets
blocked -- including inside cross-origin iframes -- was verified against a
real Chrome; these tests pin the decision logic and the wiring around it.

THE UI's STATIC FILES. Hashed assets are immutable; index.html must be
revalidated, or a browser keeps running the previous UI after an update.

PURE LOGIC ONLY, like the rest of this suite.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.stealth import browser as SB


def _session(load_images: bool) -> SB.Session:
    class S(SB.Session):
        ALWAYS_LOAD_IMAGES = False
    return S(SimpleNamespace(), [], load_images=load_images, platform="x")


class TestOneDecision:
    """The policy both wirings apply -- identical to the old `_filter`."""

    @pytest.mark.parametrize("url", [
        "https://connect.facebook.net/en_US/sdk.js",
        "https://www.facebook.com/tr/?id=1",
        "https://WWW.GOOGLE-ANALYTICS.COM/analytics.js",
        "https://www.tiktok.com/api/v1/web/report/?x=1",
    ])
    def test_trackers_get_an_empty_script(self, url):
        assert _session(True)._verdict(url, "script") == (b"", "application/javascript")

    def test_media_is_stubbed_in_either_spelling(self):
        s = _session(True)
        assert s._verdict("https://v.cdn/x.mp4", "media") == (b"", "video/mp4")
        assert s._verdict("https://v.cdn/x.mp4", "Media") == (b"", "video/mp4")

    def test_images_are_stubbed_only_when_not_loading_them(self):
        assert _session(False)._verdict("https://c.cdn/a.jpg", "Image") == \
            (SB.TRANSPARENT_GIF, "image/gif")
        assert _session(True)._verdict("https://c.cdn/a.jpg", "Image") is None

    def test_everything_else_goes_through(self):
        s = _session(False)
        for rtype in ("document", "Script", "XHR", "Fetch", "Stylesheet", "Font"):
            assert s._verdict("https://www.facebook.com/api/graphql/", rtype) is None


class TestWhatChromePauses:
    def test_patterns_cover_every_verdict_and_nothing_more(self):
        loading = _session(True)._fetch_patterns()
        stubbing = _session(False)._fetch_patterns()
        assert len(loading) == len(SB.BLOCKED_TRACKERS) + 1
        assert {"urlPattern": "*", "resourceType": "Image", "requestStage": "Request"} in stubbing
        assert not any(p.get("resourceType") == "Image" for p in loading)
        # No catch-all: pausing everything is exactly the cost being removed.
        assert not any(p == {"urlPattern": "*", "requestStage": "Request"} for p in stubbing)


class TestWiring:
    def _ctx(self, cdp):
        browser = MagicMock()
        browser.new_browser_cdp_session = AsyncMock(return_value=cdp)
        return SimpleNamespace(browser=browser, route=AsyncMock())

    @pytest.mark.asyncio
    async def test_native_filter_is_used_and_no_route_is_installed(self, monkeypatch):
        monkeypatch.setattr("backend.config.settings.settings.browser_native_request_filter", True)
        cdp = MagicMock(send=AsyncMock())
        s = _session(False)
        s.ctx = self._ctx(cdp)
        await s._install_filter()
        cdp.send.assert_awaited_once()
        assert cdp.send.await_args.args[0] == "Fetch.enable"
        s.ctx.route.assert_not_awaited()          # a route would switch the cache off
        assert s._cdp is cdp

    @pytest.mark.asyncio
    async def test_the_switch_restores_the_old_route(self, monkeypatch):
        monkeypatch.setattr("backend.config.settings.settings.browser_native_request_filter", False)
        s = _session(False)
        s.ctx = self._ctx(MagicMock(send=AsyncMock()))
        await s._install_filter()
        s.ctx.route.assert_awaited_once_with("**/*", s._filter)

    @pytest.mark.asyncio
    async def test_a_failed_native_setup_falls_back_rather_than_running_unfiltered(self, monkeypatch):
        monkeypatch.setattr("backend.config.settings.settings.browser_native_request_filter", True)
        s = _session(False)
        s.ctx = self._ctx(MagicMock(send=AsyncMock(side_effect=RuntimeError("no Fetch"))))
        await s._install_filter()
        s.ctx.route.assert_awaited_once_with("**/*", s._filter)


class TestEveryPausedRequestIsAnswered:
    @pytest.mark.asyncio
    async def test_a_stub_is_fulfilled(self):
        cdp = MagicMock(send=AsyncMock())
        await _session(False)._on_paused(cdp, {
            "requestId": "r1", "resourceType": "Image",
            "request": {"url": "https://c.cdn/a.jpg"}})
        method, params = cdp.send.await_args.args
        assert method == "Fetch.fulfillRequest" and params["responseCode"] == 200

    @pytest.mark.asyncio
    async def test_a_failed_fulfil_still_lets_the_request_go(self):
        """A paused request nobody answers hangs its page for good."""
        calls = []

        async def send(method, params):
            calls.append(method)
            if method == "Fetch.fulfillRequest":
                raise RuntimeError("target closed")
        await _session(False)._on_paused(MagicMock(send=send), {
            "requestId": "r1", "resourceType": "Image",
            "request": {"url": "https://c.cdn/a.jpg"}})
        assert calls == ["Fetch.fulfillRequest", "Fetch.continueRequest"]


class TestUIFileCaching:
    def test_assets_are_immutable_and_the_page_is_revalidated(self, tmp_path):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from backend.main import _UIFiles

        (tmp_path / "assets").mkdir()
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / "assets" / "index-abc123.js").write_text("x=1")
        app = Starlette()
        app.mount("/", _UIFiles(directory=str(tmp_path), html=True))
        c = TestClient(app)
        assert c.get("/").headers["cache-control"] == "no-cache"
        assert c.get("/index.html").headers["cache-control"] == "no-cache"
        assert "immutable" in c.get("/assets/index-abc123.js").headers["cache-control"]
