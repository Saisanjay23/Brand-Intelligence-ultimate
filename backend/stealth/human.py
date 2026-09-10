"""Pacing that looks like a person reading, not a script iterating.

The single biggest detection signal is rhythm: identical gaps between
identical actions. Everything here exists to break that up.

Deliberately small. Mouse-jitter and typing simulation are omitted because
this tool never types or clicks, it navigates and reads payloads, so faking
input events would add signal, not remove it.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime

# rough seconds per action, before jitter and the multipliers below
BASE = {
    "page_load": 0.8,
    "between_profiles": 2.2,
    "between_pages": 0.5,
    "tab_switch": 1.0,
    "default": 1.0,
}


# A SLEEP NOBODY CAN INTERRUPT IS A STOP BUTTON THAT DOES NOT WORK.
#
# Every gap in this file was a flat `asyncio.sleep`, and `maybe_rest` sleeps
# for TWENTY TO SIXTY SECONDS. That pacing is the point of the module and it
# is not being removed -- but it also sits on the path of every sweep and
# every analysis visit on every platform, which made it the longest stretch
# of a run during which nothing could be told to stop.
#
# What that cost, concretely: a `cancel` signal is only useful where
# something looks at it, and of the twelve platform engines exactly one
# checks it. For the other eleven the cooperative path never landed at all,
# so Stop always fell through to the runner's five-second hard-cancel
# backstop -- which unwinds a task wherever it stands, mid-navigation, and
# tears the browser down under an in-flight page. Slow, and messy in a way
# that the rest of this system then had to defend against.
#
# Rather than teach eleven engines a checkpoint each, the wait itself
# becomes interruptible: every engine's pacing funnels through
# `StealthSession.pause`, which owns a `Human`, so one predicate here
# reaches all of them. `stop` is injected rather than imported so this
# module keeps knowing nothing about jobs, options or runners.
#
# Polled in short slices rather than awaiting an event, deliberately: the
# signal is documented as an Event OR a bool OR a callable (see
# scan_options.cancelled), and a module-level asyncio primitive binds to the
# first loop that awaits it -- the trap discovery/runner.py's own
# `_worker_semaphore` exists to document. A fifth of a second is invisible
# against gaps measured in seconds and costs 300 no-op wakeups across the
# longest nap this file can produce.
_STOP_TICK_S = 0.2


class Human:
    """Per-session pacing. Fatigue and time-of-day shape the delays.

    `stop` is an optional `() -> bool` the owning run supplies (see
    `stealth/browser.py`), asked during every wait so a stopped job stops
    waiting. Absent, every pause behaves exactly as it always did.
    """

    def __init__(self, session_start: datetime | None = None, stop=None):
        self.started = session_start or datetime.now()
        self.actions = 0
        self.stop = stop

    def stopping(self) -> bool:
        """Has the owning run been asked to stop? Never raises: a pacing
        layer must not be able to fail a sweep because the predicate it was
        handed misbehaved, and "no" is the answer that preserves the
        previous behaviour."""
        if self.stop is None:
            return False
        try:
            return bool(self.stop())
        except Exception:                        # noqa: BLE001 - never fatal
            return False

    async def sleep(self, seconds: float) -> bool:
        """Wait `seconds`, or until the run is stopped. -> True if it was
        cut short, so a caller can report the rest it did NOT take.

        Timed against a monotonic deadline rather than by counting slices,
        so the gap is the gap asked for and does not drift with the tick.
        """
        if seconds <= 0 or self.stopping():
            return self.stopping()
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_STOP_TICK_S, remaining))
            if self.stopping():
                return True

    def fatigue(self) -> float:
        """People slow down. After ~200 actions this is a 1.5x drag."""
        return min(1.0 + self.actions / 400, 1.5)

    def circadian(self) -> float:
        """Nobody browses at 4am the way they do at lunchtime."""
        hour = datetime.now().hour
        if 1 <= hour < 6:
            return 1.6
        if 6 <= hour < 9 or 22 <= hour <= 23:
            return 1.2
        return 1.0

    def delay_for(self, context: str = "default", scale: float = 1.0) -> float:
        base = BASE.get(context, BASE["default"]) * scale
        # lognormal: mostly short gaps, occasionally a long one, never zero
        jitter = random.lognormvariate(0, 0.35)
        return max(0.15, base * jitter * self.fatigue() * self.circadian())

    async def pause(self, context: str = "default", scale: float = 1.0) -> None:
        self.actions += 1
        await self.sleep(self.delay_for(context, scale))

    def should_rest(self) -> bool:
        """Occasional longer gap, the way a person gets distracted."""
        return self.actions > 0 and self.actions % random.randint(25, 45) == 0

    async def maybe_rest(self) -> float:
        """-> the rest actually taken. 0.0 when none was due, and 0.0 when
        one was cut short by a stop -- the caller logs this, and reporting a
        60s break that was abandoned after 200ms would be a lie in the log
        an analyst reads to work out why a stop took as long as it did."""
        if not self.should_rest():
            return 0.0
        nap = random.uniform(20, 60)
        return 0.0 if await self.sleep(nap) else nap
