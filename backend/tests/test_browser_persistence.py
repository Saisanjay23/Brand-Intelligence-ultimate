"""Persistent browser profiles, and the rules that keep them from breaking a run.

WHAT THESE TESTS ARE DEFENDING. Persistence is a STEALTH improvement layered
under discovery and analysis, and neither of them knows it exists: both are
handed a BrowserContext and cannot tell which kind they got. That property
is the entire safety argument, so almost every assertion below is about the
degraded path being correct rather than the happy one:

    two callers cannot open one profile directory  -- Chromium locks it, and
                                                      the loser must run
                                                      ephemeral, not fail
    a session with no id never gets a profile      -- it would have to share
                                                      one with every other
                                                      anonymous caller
    a launch failure falls back                    -- a stale lock from a
                                                      killed process must
                                                      not fail a sweep
    the lease always comes back                    -- a leaked one silently
                                                      disables persistence
                                                      for the whole process
    the warm-up is skippable and non-fatal         -- it is a courtesy page
                                                      load, not a step

No browser is launched here, in line with this suite's rule (see pytest.ini):
every test drives the decisions, not Chromium.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.config.settings import settings
from backend.stealth import browser as B


@pytest.fixture(autouse=True)
def _clean_leases():
    B._profile_leases.clear()
    B._last_warm.clear()
    yield
    B._profile_leases.clear()
    B._last_warm.clear()


@pytest.fixture
def profiles_here(tmp_path, monkeypatch):
    """Point profiles_root at a throwaway directory for one test."""
    monkeypatch.setattr(settings, "session_blob_path", tmp_path, raising=False)
    return tmp_path / "browser_profiles"


def _opts(**kw):
    base = dict(headful=False, timeout=30, delay=0, warmup=True, cancel=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ------------------------------------------------------------ where it lives


def test_a_profile_lands_under_session_blob_path(profiles_here):
    path = B.profile_dir_for("facebook", "abc123")
    assert path.parent == profiles_here
    assert path.name == "facebook_abc123"


def test_an_operator_typed_identifier_cannot_escape_the_profiles_directory():
    """Both halves of the name reach the filesystem, and one of them is
    typed by a person. A session id carrying separators must not be able to
    write outside the profiles root."""
    path = B.profile_dir_for("facebook", "../../etc/passwd")
    assert ".." not in path.name
    assert path.parent.name == "browser_profiles"


def test_each_account_gets_its_own_directory():
    a = B.profile_dir_for("facebook", "one")
    b = B.profile_dir_for("facebook", "two")
    c = B.profile_dir_for("instagram", "one")
    assert len({a, b, c}) == 3


# ------------------------------------------------------- who gets a profile


def test_a_session_with_an_id_wants_a_persistent_profile():
    s = B.Session(_opts(), [], session_id="s1", platform="facebook")
    assert s._wanted_profile() == B.profile_dir_for("facebook", "s1")


def test_a_session_without_an_id_runs_ephemeral():
    """It would otherwise have to share one directory with every other
    anonymous caller on the platform -- the collision the lease exists to
    prevent, except permanent."""
    assert B.Session(_opts(), [], platform="facebook")._wanted_profile() is None


def test_a_session_with_no_platform_runs_ephemeral():
    assert B.Session(_opts(), [], session_id="s1")._wanted_profile() is None


def test_the_kill_switch_turns_persistence_off_everywhere(monkeypatch):
    monkeypatch.setattr(settings, "browser_persistent_profiles", False)
    s = B.Session(_opts(), [], session_id="s1", platform="facebook")
    assert s._wanted_profile() is None


def test_the_platform_is_read_off_the_engine_module_without_touching_it():
    """Every platform's Session subclass keeps its existing constructor.
    The platform id is derived from where the class lives, which is what
    lets discovery and analysis stay untouched by this feature."""
    from backend.platforms.facebook.discovery_engine import FacebookSession
    from backend.platforms.instagram.discovery_engine import InstagramSession
    from backend.platforms.tiktok.discovery_engine import TikTokSession
    from backend.platforms.twitter.discovery_engine import TwitterSession

    for cls, expected in (
        (FacebookSession, "facebook"), (TwitterSession, "twitter"),
        (InstagramSession, "instagram"), (TikTokSession, "tiktok"),
    ):
        assert cls(_opts(), [], session_id="x").platform == expected


def test_an_explicit_platform_wins_over_the_module():
    assert B.Session(_opts(), [], platform="twitter").platform == "twitter"
    assert B.Session(_opts(), []).platform == ""


# ------------------------------------------------------------- the lease


def test_one_directory_can_only_be_leased_once():
    path = B.profile_dir_for("facebook", "s1")
    assert B._lease_profile(path) is True
    assert B._lease_profile(path) is False
    B._release_profile(path)
    assert B._lease_profile(path) is True


def test_the_lease_is_case_insensitive_like_the_filesystem_it_guards():
    """On Windows two spellings of one path are one directory, and Chromium
    locks the directory, not the string."""
    import pathlib

    B._lease_profile(pathlib.Path("C:/Profiles/FaceBook_S1"))
    assert B._lease_profile(pathlib.Path("c:/profiles/facebook_s1")) is False


@pytest.mark.asyncio
async def test_stop_always_hands_the_lease_back_even_when_closing_fails():
    """A leaked lease disables persistence for that account for the life of
    the process, silently -- which is exactly the sort of failure nobody
    notices until someone asks why the profile is never reused."""
    path = B.profile_dir_for("facebook", "s1")
    s = B.Session(_opts(), [], session_id="s1", platform="facebook")
    B._lease_profile(path)
    s._profile = path
    s._persistent = True
    s.ctx = MagicMock()
    s.ctx.close = AsyncMock(side_effect=RuntimeError("browser already gone"))
    s.ctx.cookies = AsyncMock(return_value=[])
    s.browser = s.ctx
    s._pw = MagicMock()
    s._pw.stop = AsyncMock()

    await s.stop()

    assert str(path).lower() not in B._profile_leases
    assert s._profile is None


@pytest.mark.asyncio
async def test_a_persistent_context_is_never_closed_twice():
    """`self.browser IS self.ctx` on the persistent path, so walking the
    pair blindly would close one object twice. Harmless only because the
    second call is swallowed -- which is a bad thing to depend on."""
    s = B.Session(_opts(), [], session_id="s1", platform="facebook")
    ctx = MagicMock()
    ctx.close = AsyncMock()
    ctx.cookies = AsyncMock(return_value=[])
    s.ctx = s.browser = ctx
    s._pw = MagicMock()
    s._pw.stop = AsyncMock()

    await s.stop()

    assert ctx.close.await_count == 1


@pytest.mark.asyncio
async def test_an_ephemeral_session_still_closes_both_context_and_browser():
    s = B.Session(_opts(), [], platform="facebook")
    ctx, browser = MagicMock(), MagicMock()
    ctx.close = AsyncMock()
    ctx.cookies = AsyncMock(return_value=[])
    browser.close = AsyncMock()
    s.ctx, s.browser = ctx, browser
    s._pw = MagicMock()
    s._pw.stop = AsyncMock()

    await s.stop()

    assert ctx.close.await_count == 1
    assert browser.close.await_count == 1


# ---------------------------------------------------------------- warm-up


def _warm_session(**opt_kw):
    s = B.Session(_opts(**opt_kw), [], session_id="w1", platform="facebook")
    page = MagicMock()
    page.goto = AsyncMock()
    page.close = AsyncMock()
    page.mouse = MagicMock()
    page.mouse.wheel = AsyncMock()
    s.ctx = MagicMock()
    s.ctx.new_page = AsyncMock(return_value=page)
    return s, page


@pytest.mark.asyncio
async def test_a_real_sweep_warms_the_feed_before_working(monkeypatch):
    monkeypatch.setattr(B, "PLATFORM_HOME", {"facebook": "https://example.com/"})
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    s, page = _warm_session()
    await s._warmup()
    page.goto.assert_awaited_once()
    page.mouse.wheel.assert_awaited_once()
    page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_health_check_never_warms(monkeypatch):
    """Two page loads to answer one question, on an account already
    suspected of being unwell."""
    monkeypatch.setattr(B, "PLATFORM_HOME", {"facebook": "https://example.com/"})
    s, page = _warm_session(warmup=False)
    await s._warmup()
    page.goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_warming_happens_at_most_once_per_gap(monkeypatch):
    monkeypatch.setattr(B, "PLATFORM_HOME", {"facebook": "https://example.com/"})
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(settings, "browser_warmup_min_gap_minutes", 15.0)
    s, page = _warm_session()
    await s._warmup()
    await s._warmup()
    assert page.goto.await_count == 1


@pytest.mark.asyncio
async def test_warming_resumes_once_the_gap_has_passed(monkeypatch):
    monkeypatch.setattr(B, "PLATFORM_HOME", {"facebook": "https://example.com/"})
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(settings, "browser_warmup_min_gap_minutes", 15.0)
    s, page = _warm_session()
    await s._warmup()
    B._last_warm["facebook:w1"] = time.time() - (16 * 60)
    await s._warmup()
    assert page.goto.await_count == 2


@pytest.mark.asyncio
async def test_a_warm_up_that_fails_is_swallowed(monkeypatch):
    """It is a courtesy page load. Letting it raise would mean a sweep dying
    because the home feed was slow."""
    monkeypatch.setattr(B, "PLATFORM_HOME", {"facebook": "https://example.com/"})
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    s, page = _warm_session()
    page.goto = AsyncMock(side_effect=RuntimeError("net::ERR_TIMED_OUT"))
    await s._warmup()                       # must not raise
    page.close.assert_awaited_once()        # and must not leak the tab


@pytest.mark.asyncio
async def test_a_cancelled_job_does_not_warm(monkeypatch):
    monkeypatch.setattr(B, "PLATFORM_HOME", {"facebook": "https://example.com/"})
    s, page = _warm_session(cancel=True)
    await s._warmup()
    page.goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_platforms_with_no_browser_are_never_warmed(monkeypatch):
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    for platform in ("youtube", "telegram"):
        s = B.Session(_opts(), [], session_id="w1", platform=platform)
        page = MagicMock()
        page.goto = AsyncMock()
        s.ctx = MagicMock()
        s.ctx.new_page = AsyncMock(return_value=page)
        await s._warmup()
        page.goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_warm_up_kill_switch(monkeypatch):
    monkeypatch.setattr(B, "PLATFORM_HOME", {"facebook": "https://example.com/"})
    monkeypatch.setattr(settings, "browser_warmup_enabled", False)
    s, page = _warm_session()
    await s._warmup()
    page.goto.assert_not_awaited()


# ------------------------------------------------------ housekeeping on disk


def test_resetting_a_profile_removes_it(profiles_here):
    path = B.profile_dir_for("facebook", "s1")
    path.mkdir(parents=True)
    (path / "Default").mkdir()
    (path / "Default" / "Local Storage").write_text("x", encoding="utf-8")

    assert B.reset_profile("facebook", "s1") is True
    assert not path.exists()


def test_a_profile_in_use_is_never_deleted_from_under_a_running_browser(profiles_here):
    path = B.profile_dir_for("facebook", "s1")
    path.mkdir(parents=True)
    B._lease_profile(path)

    assert B.reset_profile("facebook", "s1") is False
    assert path.exists()


def test_resetting_a_profile_that_does_not_exist_is_not_an_error(profiles_here):
    assert B.reset_profile("facebook", "never-existed") is False


def test_pruning_removes_only_what_nothing_has_opened_recently(profiles_here):
    profiles_here.mkdir(parents=True)
    old = profiles_here / "facebook_old"
    new = profiles_here / "facebook_new"
    for d in (old, new):
        d.mkdir()
    import os
    stale = time.time() - (40 * 86400)
    os.utime(old, (stale, stale))

    assert B.prune_stale_profiles(30) == 1
    assert not old.exists()
    assert new.exists()


def test_pruning_skips_a_profile_that_is_open_right_now(profiles_here):
    profiles_here.mkdir(parents=True)
    old = profiles_here / "facebook_old"
    old.mkdir()
    import os
    stale = time.time() - (40 * 86400)
    os.utime(old, (stale, stale))
    B._lease_profile(old)

    assert B.prune_stale_profiles(30) == 0
    assert old.exists()


def test_pruning_is_disabled_by_zero(profiles_here):
    profiles_here.mkdir(parents=True)
    old = profiles_here / "facebook_old"
    old.mkdir()
    import os
    stale = time.time() - (400 * 86400)
    os.utime(old, (stale, stale))

    assert B.prune_stale_profiles(0) == 0
    assert old.exists()


def test_pruning_a_missing_root_is_not_an_error(profiles_here):
    assert B.prune_stale_profiles(30) == 0
