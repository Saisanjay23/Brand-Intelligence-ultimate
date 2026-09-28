"""A browser close that the runner cuts off must still clean up after itself.

THE BUG, FOUND IN A LIVE CHECK ON 2026-09-26. The runners give a teardown
10s and then cancel it. The cancel landed inside Session.stop(), mid-close,
where `except Exception` does not catch it -- so the rest of stop() never
ran. One six-platform sweep left four Playwright node drivers running for
the life of the process, and three accounts' browser profiles leased for
good, so every later sweep on them ran on a throwaway profile.

Reproduced against a real Chrome before the fix (driver and lease both
still held after the grace period) and after it (both released).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.stealth import browser as SB


def _session(ctx_close, tmp_path: Path):
    s = SB.Session(SimpleNamespace(), [], platform="x")
    s.sync_cookies = AsyncMock()
    s.ctx = MagicMock(close=ctx_close)
    s.browser = s.ctx
    s._pw = MagicMock(stop=AsyncMock())
    s._profile = tmp_path / "x_profile"
    assert SB._lease_profile(s._profile)
    return s


def _leased(p: Path) -> bool:
    return str(p).lower() in SB._profile_leases


@pytest.mark.asyncio
async def test_a_normal_close_stops_the_driver_and_releases_now(tmp_path):
    s = _session(AsyncMock(), tmp_path)
    pw, profile = s._pw, s._profile
    await s.stop()
    pw.stop.assert_awaited_once()
    assert not _leased(profile)


@pytest.mark.asyncio
async def test_a_failing_close_still_stops_the_driver(tmp_path):
    s = _session(AsyncMock(side_effect=RuntimeError("target closed")), tmp_path)
    pw, profile = s._pw, s._profile
    await s.stop()
    pw.stop.assert_awaited_once()
    assert not _leased(profile)


@pytest.mark.asyncio
async def test_a_cut_off_close_finishes_in_the_background(tmp_path, monkeypatch):
    monkeypatch.setattr(SB, "_ABANDONED_CLOSE_GRACE_S", 0.05)

    async def slow_close():
        await asyncio.sleep(10)                 # Chrome still flushing its profile
    s = _session(slow_close, tmp_path)
    pw, profile = s._pw, s._profile

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(s.stop(), timeout=0.05)   # what _close_quietly does
    # Not yet: stopping the driver now would kill Chrome mid-write.
    pw.stop.assert_not_awaited()
    assert _leased(profile)

    await asyncio.sleep(0.3)
    pw.stop.assert_awaited_once()
    assert not _leased(profile)


@pytest.mark.asyncio
async def test_stopping_twice_is_harmless(tmp_path):
    s = _session(AsyncMock(), tmp_path)
    pw = s._pw
    await s.stop()
    await s.stop()
    pw.stop.assert_awaited_once()
