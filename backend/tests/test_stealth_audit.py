"""Anti-ban posture, as measured on 2026-09-24.

Each test pins one finding of the stealth audit:

  * the init script is EMPTY -- real Chrome under patchright is already
    clean in the page's main world, and every override measured there only
    added a detectable difference (see stealth/navigator_spoofing.py);
  * Instagram's user search goes out through the account's own browser tab
    (Chrome's TLS/HTTP-2, cookies and fetch metadata), with only the
    User-Agent swapped for that one request;
  * accounts are not touched at night, idle ones are re-checked hours apart
    rather than every 90 minutes, and scheduled runs do not start on the
    same minute every night.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.platforms.instagram import discovery_engine as ig
from backend.stealth import detection_probe
from backend.stealth.navigator_spoofing import build_init_js


# --------------------------------------------------------------- init script

class TestNoInitScript:
    def test_nothing_is_injected(self):
        assert build_init_js() == ""

    def test_the_probe_fails_every_tell_the_old_script_left(self):
        """The raw facts the OLD script produced in the main world, as
        measured. The upgraded grader must fail each of them."""
        raw = {
            "webdriver": False, "has_window_chrome": True, "chrome_runtime": True,
            "plugins_length": 5, "languages": ["en-US", "en"], "outer_width": 1, "outer_height": 1,
            "native_identity": ["Object.keys: \"function () { [native code] }\" / "
                                "\"function get keys() { [native code] }\""],
            "document_own_props": ["location", "visibilityState", "hidden"],
            "navigator_own_props": [],
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/153.0.0.0",
            "webgl": {"available": True, "renderer": "ANGLE (Apple, Apple M1, OpenGL 4.1)"},
            "webgl_worker_renderer": "ANGLE (Intel, Intel(R) UHD Graphics)",
            "canvas_read_mutates": True,
        }
        fails = {sig for verdict, sig, _ in detection_probe.grade(raw) if verdict == "FAIL"}
        assert {"chrome.runtime", "native function identity", "document own properties",
                "WebGL main vs worker", "WebGL vs UA", "canvas read"} <= fails

    def test_a_clean_main_world_passes(self):
        raw = {
            "webdriver": False, "has_window_chrome": True, "chrome_runtime": False,
            "plugins_length": 5, "languages": ["en-US", "en"], "outer_width": 1, "outer_height": 1,
            "native_identity": [], "document_own_props": ["location"], "navigator_own_props": [],
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/153.0.0.0",
            "webgl": {"available": True, "renderer": "ANGLE (Intel)"},
            "webgl_worker_renderer": "ANGLE (Intel)", "canvas_read_mutates": False,
        }
        fails = [sig for verdict, sig, _ in detection_probe.grade(raw) if verdict == "FAIL"]
        assert fails == []


# ------------------------------------------------ Instagram search transport

def _tab(pages: list[dict]):
    tab = MagicMock()
    tab.is_closed = MagicMock(return_value=False)
    tab.route = AsyncMock()
    tab.goto = AsyncMock()
    tab.evaluate = AsyncMock(side_effect=[{"status": 200, "text": json.dumps(p)} for p in pages])
    return tab


def _disc(tab=None, new_page_error: Exception | None = None):
    ctx = MagicMock()
    if new_page_error:
        ctx.new_page = AsyncMock(side_effect=new_page_error)
    else:
        ctx.new_page = AsyncMock(return_value=tab)
    r = MagicMock()
    r.status = 200
    r.text = AsyncMock(return_value=json.dumps({"status": "ok", "users": [], "has_more": False}))
    ctx.request.get = AsyncMock(return_value=r)
    return ig.Discovery(SimpleNamespace(max_pages=3, max_results=0, timeout=5), ctx), ctx


class TestInstagramSearchUsesTheBrowser:
    @pytest.mark.asyncio
    async def test_the_search_is_an_in_page_same_origin_fetch(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        tab = _tab([{"status": "ok", "has_more": False,
                     "users": [{"username": "a", "pk": "1", "profile_pic_url": "https://x/a.jpg"}]}])
        disc, ctx = _disc(tab)
        sweep = await disc.sweep("globe capital")

        assert [h.username for h in sweep.hits] == ["a"]
        ctx.request.get.assert_not_called()                 # no second HTTP client
        url, headers = tab.evaluate.call_args.args[1]
        assert url.startswith("/api/v1/users/search/?q=globe%20capital")   # same origin
        assert "user-agent" not in {k.lower() for k in headers}   # swapped by the route
        pattern, handler = tab.route.call_args.args
        assert "users/search" in pattern and handler is ig._swap_to_app_ua

    @pytest.mark.asyncio
    async def test_the_tab_is_opened_once_and_reused(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        empty = {"status": "ok", "users": [], "has_more": False}
        tab = _tab([empty, empty])
        disc, ctx = _disc(tab)
        await disc.sweep("one")
        await disc.sweep("two")
        assert ctx.new_page.await_count == 1
        tab.goto.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_browser_failure_falls_back_rather_than_losing_the_keyword(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        disc, ctx = _disc(new_page_error=RuntimeError("browser crashed"))
        sweep = await disc.sweep("x")
        ctx.request.get.assert_awaited()
        assert sweep.stopped == "exhausted"

    @pytest.mark.asyncio
    async def test_only_that_request_gets_the_app_identity(self):
        route = MagicMock()
        route.request.headers = {"user-agent": "Chrome/153", "sec-ch-ua": '"Chrome"',
                                 "sec-ch-ua-platform": '"Windows"', "cookie": "sessionid=1",
                                 "sec-fetch-site": "same-origin"}
        route.continue_ = AsyncMock()
        await ig._swap_to_app_ua(route)
        sent = route.continue_.call_args.kwargs["headers"]
        assert sent["user-agent"] == ig.MOBILE_UA
        assert not any(k.startswith("sec-ch-ua") for k in sent)
        assert sent["cookie"] == "sessionid=1" and sent["sec-fetch-site"] == "same-origin"


# ------------------------------------------------------- when accounts move

class TestQuietHoursAndIdleChecks:
    def test_night_is_quiet_and_day_is_not(self, monkeypatch):
        from backend.sessions import manager
        monkeypatch.setattr(manager.settings, "session_quiet_hours_start", 0)
        monkeypatch.setattr(manager.settings, "session_quiet_hours_end", 7)
        assert manager.in_quiet_hours(datetime(2026, 9, 24, 3, 0)) is True
        assert manager.in_quiet_hours(datetime(2026, 9, 24, 7, 0)) is False
        assert manager.in_quiet_hours(datetime(2026, 9, 24, 14, 0)) is False

    def test_a_window_across_midnight(self, monkeypatch):
        from backend.sessions import manager
        monkeypatch.setattr(manager.settings, "session_quiet_hours_start", 23)
        monkeypatch.setattr(manager.settings, "session_quiet_hours_end", 6)
        assert manager.in_quiet_hours(datetime(2026, 9, 24, 23, 30)) is True
        assert manager.in_quiet_hours(datetime(2026, 9, 24, 5, 59)) is True
        assert manager.in_quiet_hours(datetime(2026, 9, 24, 12, 0)) is False

    def test_equal_bounds_disable_it(self, monkeypatch):
        from backend.sessions import manager
        monkeypatch.setattr(manager.settings, "session_quiet_hours_start", 0)
        monkeypatch.setattr(manager.settings, "session_quiet_hours_end", 0)
        assert manager.in_quiet_hours(datetime(2026, 9, 24, 3, 0)) is False

    @pytest.mark.asyncio
    async def test_the_monitor_touches_no_account_in_quiet_hours(self, monkeypatch):
        from backend.sessions import manager
        from backend.services import engine_health_service, session_canary_service
        monkeypatch.setattr(manager, "in_quiet_hours", lambda now=None: True)
        monkeypatch.setattr(manager.settings, "session_fast_check_enabled", True)
        check_all = AsyncMock()
        liveness = AsyncMock()
        monkeypatch.setattr(manager, "check_all_once", check_all)
        monkeypatch.setattr(manager, "purge_stale_dead_sessions", AsyncMock(return_value=0))
        monkeypatch.setattr(session_canary_service, "probe_pool_liveness", liveness)
        monkeypatch.setattr(session_canary_service, "check_token_expiries", AsyncMock())
        monkeypatch.setattr(engine_health_service, "check_once", AsyncMock())
        await manager._monitor_pass()
        check_all.assert_not_called()
        liveness.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_http_liveness_pass_is_off_by_default(self, monkeypatch):
        from backend.sessions import manager
        from backend.services import engine_health_service, session_canary_service
        monkeypatch.setattr(manager, "in_quiet_hours", lambda now=None: False)
        monkeypatch.setattr(manager.settings, "session_fast_check_enabled", False)
        liveness = AsyncMock()
        monkeypatch.setattr(manager, "check_all_once", AsyncMock())
        monkeypatch.setattr(manager, "purge_stale_dead_sessions", AsyncMock(return_value=0))
        monkeypatch.setattr(session_canary_service, "probe_pool_liveness", liveness)
        monkeypatch.setattr(session_canary_service, "check_token_expiries", AsyncMock())
        monkeypatch.setattr(engine_health_service, "check_once", AsyncMock())
        await manager._monitor_pass()
        liveness.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_idle_account_checked_recently_is_left_alone(self, monkeypatch):
        from backend.sessions import manager
        monkeypatch.setattr(manager.settings, "session_idle_recheck_hours", 8.0)
        pool = [
            {"id": "recent", "identifier": "r", "cookies": [], "status": "ready",
             "rate_limited_until": 0, "last_ok": 0.0},
            {"id": "stale", "identifier": "s", "cookies": [], "status": "ready",
             "rate_limited_until": 0, "last_ok": 0.0},
        ]
        checked = {"recent": datetime.now(timezone.utc) - timedelta(hours=2),
                   "stale": datetime.now(timezone.utc) - timedelta(hours=9)}
        monkeypatch.setattr(manager.sessions_db, "list_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(manager.sessions_db, "item_last_checked",
                            AsyncMock(side_effect=lambda p, sid: checked[sid]))
        monkeypatch.setattr(manager, "_session_in_use", lambda p, sid: False)
        picked = await manager._pick_batch("instagram", 5)
        assert [sid for sid, _, _ in picked] == ["stale"]


class TestScheduledRunsDoNotStartOnTheMinute:
    def test_a_scheduled_start_is_delayed_within_the_window(self, monkeypatch):
        from backend.config.settings import settings
        from backend.services import scheduler_service as S
        monkeypatch.setattr(settings, "scheduler_start_jitter_minutes", 15.0)
        draws = {S._start_jitter_s("scheduled") for _ in range(50)}
        assert all(0.0 <= d <= 900.0 for d in draws) and len(draws) > 40

    def test_a_manual_run_is_never_delayed(self, monkeypatch):
        from backend.config.settings import settings
        from backend.services import scheduler_service as S
        monkeypatch.setattr(settings, "scheduler_start_jitter_minutes", 15.0)
        assert S._start_jitter_s("manual") == 0.0
