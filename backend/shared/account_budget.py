"""How much one account may do in an hour.

WHY THIS EXISTS. Every other safeguard reacts: the governor slows down after
Instagram pushes back, the breaker stops after it keeps pushing back, a
warning takes the account out. Nothing set a ceiling BEFORE that, so a long
client queue could run one account through ~150 searches an hour at the
slowest stealth level, and the first anyone heard of it was Instagram's own
warning. This is the ceiling.

WHAT IT COUNTS. Actions per real account over a rolling hour -- `kind` is
"search" (one keyword swept) or "profile" (one profile visited). Instagram
and Threads on the same account count together (shared/meta_account.py
supplies the key), because Meta counts them together.

WHAT HAPPENS AT THE CEILING. The work WAITS for the oldest action to age out
of the hour, then carries on. Nothing is skipped and nothing is marked done:
a keyword or profile that has to wait is still the next one done. A Stop
ends the wait at once.

The numbers are guesses. No platform publishes its limits; the defaults are
deliberately below what this tool used to do and are settings, so they can
be moved once a real account shows where the line is. Counts live in memory:
a restart forgets the last hour, which errs towards doing more, so restart
is not a way to reset a tired account.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Callable, Deque, Dict, Tuple

WINDOW_S = 3600.0

# The two kinds counted, in the words a log line uses.
SEARCH = "search"
PROFILE = "profile"
_NOUNS = {SEARCH: "searches", PROFILE: "profile visits"}


class HourlyBudget:
    """Rolling one-hour counts per (account, kind). The clock is injectable
    so every behaviour is tested without sleeping."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._events: Dict[Tuple[str, str], Deque[float]] = {}

    def _trim(self, key: Tuple[str, str], now: float) -> Deque[float]:
        q = self._events.setdefault(key, deque())
        while q and q[0] <= now - WINDOW_S:
            q.popleft()
        return q

    def used(self, account: str, kind: str) -> int:
        return len(self._trim((account, kind), self._clock()))

    def wait_s(self, account: str, kind: str, limit: int) -> float:
        """Seconds until one more action fits. 0 when it fits now, or when
        `limit` is 0 or less (no ceiling)."""
        if limit <= 0:
            return 0.0
        now = self._clock()
        q = self._trim((account, kind), now)
        if len(q) < limit:
            return 0.0
        # The (len - limit + 1)-th oldest must age out before one more fits.
        return max(0.0, q[len(q) - limit] + WINDOW_S - now)

    def record(self, account: str, kind: str) -> None:
        now = self._clock()
        self._trim((account, kind), now).append(now)

    def reset(self) -> None:
        self._events.clear()


_budget = HourlyBudget()


def budget() -> HourlyBudget:
    return _budget


def limit_for(platform_id: str, kind: str) -> int:
    """The configured hourly ceiling for `kind` on this platform. Only the
    platforms that sign in with an Instagram account have one; 0 (no
    ceiling) for everything else."""
    from backend.config.settings import settings
    from backend.shared import meta_account

    if not meta_account.is_meta(platform_id):
        return 0
    name = ("instagram_searches_per_hour" if kind == SEARCH
            else "instagram_profile_visits_per_hour")
    return max(0, int(getattr(settings, name, 0) or 0))


async def hold(platform_id: str, session_item: dict, kind: str,
               should_stop: Callable[[], bool], log=None, label: str = "") -> bool:
    """The one call a runner makes before a search or a profile visit:
    wait for room under this account's hourly ceiling, then count the
    action. -> False only when stopped while waiting. Free for platforms
    with no ceiling."""
    from backend.shared import meta_account

    limit = limit_for(platform_id, kind)
    if limit <= 0:
        return not should_stop()
    account = meta_account.budget_key(
        platform_id, str(session_item.get("id") or ""), session_item.get("cookies"))
    return await take(account, kind, limit, should_stop, log=log,
                      label=f"[{platform_id}] {label}".strip())


async def take(account: str, kind: str, limit: int,
               should_stop: Callable[[], bool], log=None, label: str = "",
               tick: float = 1.0) -> bool:
    """Wait until one more `kind` action fits this account's hour, then
    count it. -> True when taken, False when stopped while waiting (and
    then nothing is counted)."""
    b = _budget
    wait = b.wait_s(account, kind, limit)
    if wait > 0 and log is not None:
        noun = _NOUNS.get(kind, kind)
        log.warning(
            f"{label or account}: hourly limit reached ({limit} {noun}/hour on this "
            f"account) -- waiting ~{max(1, round(wait / 60))}m before the next one")
    while True:
        if should_stop():
            return False
        wait = b.wait_s(account, kind, limit)
        if wait <= 0:
            b.record(account, kind)
            return True
        await asyncio.sleep(min(tick, wait))
