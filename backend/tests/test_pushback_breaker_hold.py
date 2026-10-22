"""A held worker must wait out an open breaker -- and never silently drop work.

The risk this guards: a breaker that opens and is then ignored (the same IP
keeps being hit), or one that waits forever (a stuck run), or one that drops
keywords on the floor instead of leaving them owed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.discovery import runner
from backend.shared import pushback as pb
from backend.shared.pushback import CHALLENGED, THROTTLED, Signal


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _job():
    return SimpleNamespace(cancel=asyncio.Event())


def _run():
    return SimpleNamespace(hard_stop=False, prog=SimpleNamespace(note=""))


@pytest.fixture(autouse=True)
def _fresh():
    pb.reset_all()
    yield
    pb.reset_all()


def _open_breaker(platform="instagram", clock=None):
    clock = clock or FakeClock()
    gov = pb.governor_for(platform)
    gov._clock = clock
    for _ in range(3):
        gov.record(Signal(THROTTLED, "429"))
    assert gov.is_open()
    return gov, clock


@pytest.mark.asyncio
async def test_closed_breaker_returns_immediately():
    run = _run()
    await asyncio.wait_for(runner._hold_for_breaker("facebook", _job(), run, "acct"), 0.5)
    assert run.hard_stop is False


@pytest.mark.asyncio
async def test_open_breaker_holds_until_it_closes(monkeypatch):
    gov, clock = _open_breaker()
    run = _run()
    task = asyncio.create_task(
        runner._hold_for_breaker("instagram", _job(), run, "acct", tick=0.01))
    await asyncio.sleep(0.05)
    assert not task.done()                      # genuinely held
    clock.t += gov.cfg.open_s + 1               # time passes -> breaker closes
    await asyncio.wait_for(task, 1.0)
    assert run.hard_stop is False               # waited it out, platform carries on


@pytest.mark.asyncio
async def test_stop_is_honoured_while_held():
    _open_breaker()
    job, run = _job(), _run()
    task = asyncio.create_task(
        runner._hold_for_breaker("instagram", job, run, "acct", tick=0.01))
    await asyncio.sleep(0.03)
    job.cancel.set()
    await asyncio.wait_for(task, 1.0)
    assert run.hard_stop is False               # a Stop is not a platform failure


@pytest.mark.asyncio
async def test_waiting_too_long_stops_the_platform_and_says_why(monkeypatch):
    _open_breaker()
    monkeypatch.setattr(runner.settings, "pushback_max_wait_minutes", 0.0005)  # 0.03 s
    run = _run()
    await asyncio.wait_for(
        runner._hold_for_breaker("instagram", _job(), run, "acct", tick=0.01), 2.0)
    assert run.hard_stop is True
    assert "kept pushing back" in run.prog.note
    assert "owed" in run.prog.note              # the keywords are not dropped


@pytest.mark.asyncio
async def test_breaker_can_be_switched_off(monkeypatch):
    _open_breaker()
    monkeypatch.setattr(runner.settings, "pushback_breaker_enabled", False)
    run = _run()
    await asyncio.wait_for(runner._hold_for_breaker("instagram", _job(), run, "acct"), 0.5)
    assert run.hard_stop is False


@pytest.mark.asyncio
async def test_other_platforms_are_unaffected():
    _open_breaker("instagram")
    run = _run()
    await asyncio.wait_for(runner._hold_for_breaker("facebook", _job(), run, "acct"), 0.5)
    assert run.hard_stop is False
