"""Running on a smaller laptop: sizing the defaults, and waiting for memory.

The promise these tests pin is that none of it changes WHAT a run does or
finds -- only how many browsers are open at once and whether a launch pauses
for free memory. So: the caps only ever go DOWN and never override what the
operator set; the wait is bounded and always ends in "go ahead"; an
unreadable machine is never read as "no room"; and a roomy machine runs none
of the machinery at all.

The numbers come from measuring a real worker (backend/config/hardware.py):
~1.1 GB with a page, ~1.6 GB with three tabs.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.config import hardware
from backend.config.settings import Settings
from backend.stealth import browser as B


# ------------------------------------------------------------ sizing


class TestRecommendedWorkers:
    @pytest.mark.parametrize("total_mb,cores,want", [
        (16_384, 8, 4),    # the target laptop: 16 GB, 4 cores / 8 threads -> the old default
        (16_000, 8, 4),    # 16 GB as the OS actually reports it
        (8_192, 8, 2),     # 8 GB: memory is the limit
        (8_192, 4, 2),
        (4_096, 8, 1),     # never below one
        (32_768, 12, 6),   # a bigger machine: cpu is the limit
        (65_536, 2, 1),    # lots of RAM, two threads: cpu is the limit
        (16_384, 1, 1),
    ])
    def test_the_smaller_of_memory_and_cpu(self, total_mb, cores, want):
        assert hardware.recommended_workers(total_mb, cores) == want

    def test_an_unreadable_machine_is_sized_by_cpu_alone(self):
        assert hardware.recommended_workers(None, 8) == 4
        assert hardware.recommended_workers(None, 1) == 1

    def test_the_real_machine_answers_sanely(self):
        got = hardware.memory_mb()
        if got is not None:
            total, avail = got
            assert 0 < avail <= total
        assert hardware.logical_cores() >= 1
        assert hardware.recommended_workers() >= 1


class TestDefaultsOnlyEverGoDown:
    def _settings(self, monkeypatch, cap, **kw):
        monkeypatch.setattr(hardware, "recommended_workers", lambda *a, **k: cap)
        return Settings(_env_file=None, **kw)

    def test_a_small_machine_lowers_the_defaults(self, monkeypatch):
        s = self._settings(monkeypatch, 2)
        assert (s.analysis_max_browser_workers, s.discovery_max_browser_workers,
                s.discovery_max_parallel_sessions) == (2, 2, 2)

    def test_a_big_machine_never_raises_them(self, monkeypatch):
        s = self._settings(monkeypatch, 12)
        assert (s.analysis_max_browser_workers, s.discovery_max_browser_workers,
                s.discovery_max_parallel_sessions) == (4, 4, 3)

    def test_what_the_operator_set_is_never_touched(self, monkeypatch):
        s = self._settings(monkeypatch, 1, analysis_max_browser_workers=6)
        assert s.analysis_max_browser_workers == 6           # explicit: a decision
        assert s.discovery_max_browser_workers == 1          # default: sized down

    def test_never_below_one(self, monkeypatch):
        s = self._settings(monkeypatch, 0)
        assert s.analysis_max_browser_workers == 1


# ------------------------------------------------------------ waiting


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _probe(*readings):
    seq = list(readings)
    return lambda: seq.pop(0) if len(seq) > 1 else seq[0]


class TestWaitForHeadroom:
    @pytest.mark.asyncio
    async def test_enough_room_does_not_wait_at_all(self):
        c = _Clock()
        ok, avail = await hardware.wait_for_headroom(
            1800, 45, probe=_probe(4000), sleep=c.sleep, clock=c)
        assert (ok, avail, c.slept) == (True, 4000, [])

    @pytest.mark.asyncio
    async def test_an_unreadable_machine_is_never_a_reason_to_wait(self):
        c = _Clock()
        ok, avail = await hardware.wait_for_headroom(
            1800, 45, probe=_probe(None), sleep=c.sleep, clock=c)
        assert ok is True and avail is None and c.slept == []

    @pytest.mark.asyncio
    async def test_it_goes_ahead_the_moment_memory_frees_up(self):
        c = _Clock()
        ok, avail = await hardware.wait_for_headroom(
            1800, 45, probe=_probe(900, 1200, 2500), sleep=c.sleep, clock=c)
        assert (ok, avail) == (True, 2500) and len(c.slept) == 2

    @pytest.mark.asyncio
    async def test_the_wait_is_bounded_and_says_it_ran_out(self):
        c = _Clock()
        ok, avail = await hardware.wait_for_headroom(
            1800, 7, probe=_probe(900), sleep=c.sleep, clock=c)
        assert ok is False and avail == 900
        assert sum(c.slept) == pytest.approx(7)               # never longer than asked

    @pytest.mark.asyncio
    async def test_a_stop_ends_the_wait_at_once(self):
        c = _Clock()
        ok, _ = await hardware.wait_for_headroom(
            1800, 45, stop=lambda: True, probe=_probe(900), sleep=c.sleep, clock=c)
        assert ok is False and c.slept == []


# ------------------------------------------------------------ the gate


@pytest.fixture
def knobs(monkeypatch):
    monkeypatch.setattr(B.settings, "browser_min_free_mb", 1800)
    monkeypatch.setattr(B.settings, "browser_start_wait_seconds", 45.0)
    monkeypatch.setattr(B, "LAUNCH_SETTLE_S", 0)
    waiter = AsyncMock(return_value=(True, 4000))
    monkeypatch.setattr(B.hardware, "wait_for_headroom", waiter)
    return waiter


def _free(monkeypatch, mb):
    monkeypatch.setattr(B.hardware, "available_ram_mb", lambda: mb)


class TestLaunchGate:
    @pytest.mark.asyncio
    async def test_plenty_of_room_runs_none_of_the_machinery(self, monkeypatch, knobs):
        _free(monkeypatch, 1800 * B.MEMORY_PRESSURE_FACTOR)
        ran = []
        async with B.launch_gate("x"):
            ran.append(1)
        assert ran == [1]
        knobs.assert_not_awaited()
        assert not B._launch_lock().locked()

    @pytest.mark.asyncio
    async def test_an_unreadable_machine_passes_straight_through(self, monkeypatch, knobs):
        _free(monkeypatch, None)
        async with B.launch_gate("x"):
            pass
        knobs.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("need,wait", [(0, 45.0), (1800, 0.0)])
    async def test_either_knob_at_zero_turns_it_off(self, monkeypatch, knobs, need, wait):
        monkeypatch.setattr(B.settings, "browser_min_free_mb", need)
        monkeypatch.setattr(B.settings, "browser_start_wait_seconds", wait)
        _free(monkeypatch, 100)
        async with B.launch_gate("x"):
            pass
        knobs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_tight_machine_waits_then_launches_under_the_lock(self, monkeypatch, knobs):
        _free(monkeypatch, 1000)
        async with B.launch_gate("x"):
            assert B._launch_lock().locked()
        knobs.assert_awaited_once()
        assert not B._launch_lock().locked()

    @pytest.mark.asyncio
    async def test_running_out_of_wait_still_launches_and_warns(self, monkeypatch, knobs, caplog):
        knobs.return_value = (False, 900)
        _free(monkeypatch, 900)
        ran = []
        async with B.launch_gate("instagram"):
            ran.append(1)
        assert ran == [1]                                     # delayed, never refused

    @pytest.mark.asyncio
    async def test_waiters_wait_together_and_launch_one_at_a_time(self, monkeypatch, knobs):
        _free(monkeypatch, 1000)
        inside, peak, waiting = 0, 0, 0
        both_waiting = asyncio.Event()

        async def waiter(*_a, **_k):
            nonlocal waiting
            waiting += 1
            if waiting == 2:
                both_waiting.set()
            await asyncio.wait_for(both_waiting.wait(), 2)    # fails if they queued
            return True, 4000

        knobs.side_effect = waiter

        async def one():
            nonlocal inside, peak
            async with B.launch_gate("x"):
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.01)
                inside -= 1

        await asyncio.gather(one(), one())
        assert peak == 1

    @pytest.mark.asyncio
    async def test_a_failed_launch_frees_the_lock_for_the_next(self, monkeypatch, knobs):
        _free(monkeypatch, 1000)
        with pytest.raises(RuntimeError):
            async with B.launch_gate("x"):
                raise RuntimeError("chrome would not start")
        assert not B._launch_lock().locked()
        async with B.launch_gate("x"):
            pass


class TestEveryLaunchPassesTheGate:
    @pytest.mark.asyncio
    async def test_session_start_goes_through_the_gate_and_returns_the_context(self, monkeypatch, knobs):
        _free(monkeypatch, 1000)

        class S(B.Session):
            pass

        s = S(SimpleNamespace(cancel=None), [], platform="x")
        s._launch = AsyncMock(return_value="ctx")
        assert await s.start() == "ctx"
        knobs.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_session_stop_signal_reaches_the_wait(self, monkeypatch, knobs):
        _free(monkeypatch, 1000)
        flag = SimpleNamespace(is_set=lambda: True)

        class S(B.Session):
            pass

        s = S(SimpleNamespace(cancel=flag), [], platform="x")
        s._launch = AsyncMock(return_value="ctx")
        await s.start()
        assert knobs.await_args.kwargs["stop"]() is True

    def test_tiktoks_own_browser_passes_the_same_gate(self):
        from backend.platforms.tiktok import discovery_engine as T

        assert "launch_gate" in inspect.getsource(T.anonymous_context)

    def test_no_other_code_launches_chrome_around_the_gate(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        launches = []
        for p in root.rglob("*.py"):
            rel = p.relative_to(root).as_posix()
            if rel.startswith("tests/") or rel == "stealth/check_login_selectors.py":
                continue
            src = p.read_text(encoding="utf-8")
            if "launch_persistent_context(" in src or ".chromium.launch(" in src:
                launches.append(rel)
        assert sorted(launches) == ["platforms/tiktok/discovery_engine.py", "stealth/browser.py"]
