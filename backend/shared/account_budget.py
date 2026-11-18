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

A DAILY CEILING TOO (added 2026-11-17, after the pool's only Instagram
account was suspended). An hourly ceiling alone still let one account do
40 searches and 60 profile visits every hour, all day -- no person does
that. The daily ceiling is different in kind from the hourly one:

  * It does NOT wait. Waiting out a day inside a sweep is not pacing, it is
    a hung job. `hold` raises `DailyLimitReached` instead; the runner rests
    that account until the limit resets (another account in the pool can
    carry on) and whatever is left is recorded OWED, by name, with this
    reason -- never as searched, never as "found nothing".
  * It SURVIVES A RESTART. The count is kept in Mongo once the app's
    storage is up (`use_persistent_store`), so restarting the backend is
    not a way round it. Until then, and in tests, it is kept in memory.
  * NEW ACCOUNTS WARM UP. An account added to the pool less than
    `instagram_warmup_days` ago gets a fraction of the daily limit, rising
    from 25% on its first day to 100% at the end of the warm-up. A brand
    new account doing a full day's work at once is the profile Meta's
    integrity systems are built to catch.
  * Days are UTC calendar days, so the limit resets at 00:00 UTC.

LIMITS THAT LEARN (added 2026-11-17). The numbers above are guesses, and an
account that is told "slow down" or "we suspect automated behaviour" has
just measured where its real line is. So a throttle or a challenge shrinks
that account's hourly AND daily limits (halved each time, never below 20%),
then lets them climb back in a straight line over 3 days (7 for a
challenge). Two warnings close together compound. The account is not
punished for a warning it never got, and one that behaves for a few days
earns its full limits back on its own. See `next_penalty`.

NO-LOGIN VISITS ARE NOT AN ACCOUNT'S (added 2026-11-17). Instagram analysis
now reads public profile pages with no account first (analysis/runner.py).
Those visits are counted under one machine-wide key, against their own
hourly ceiling (`instagram_no_login_profiles_per_hour`), with no daily
ceiling and no warm-up or penalty -- all of those exist to protect an
account, and there is none. They never add to any account's count either.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, Optional, Tuple

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
        # The daily counts too: tests reset between cases through this, and
        # a day's count leaking between them would make a late test raise.
        _memory_daily.reset()


_budget = HourlyBudget()


def budget() -> HourlyBudget:
    return _budget


def limit_for(platform_id: str, kind: str, session_item: Optional[dict] = None) -> int:
    """The configured hourly ceiling for `kind` on this platform. Only the
    platforms that sign in with an Instagram account have one; 0 (no
    ceiling) for everything else.

    A NO-LOGIN PROFILE VISIT HAS ITS OWN CEILING. `session_item` carrying
    `anonymous` is analysis reading a public Instagram page with no account
    at all (analysis/runner.py's no-login phase). Holding it to an ACCOUNT's
    ceiling made no sense -- there is no account to protect -- and the
    thing that does need a ceiling is this machine's IP address, which is
    what Instagram counts for a logged-out visitor. So it gets
    `instagram_no_login_profiles_per_hour` instead, under its own key (see
    `NO_LOGIN_KEY`), and never a share of any account's count. A logged-out
    SEARCH does not exist (Instagram answers it with a login page); should
    one ever be asked for, it keeps the account ceiling rather than none."""
    from backend.config.settings import settings
    from backend.shared import meta_account

    if not meta_account.is_meta(platform_id):
        return 0
    if (session_item or {}).get("anonymous") and kind == PROFILE:
        return max(0, int(getattr(settings, "instagram_no_login_profiles_per_hour", 0) or 0))
    name = ("instagram_searches_per_hour" if kind == SEARCH
            else "instagram_profile_visits_per_hour")
    return max(0, int(getattr(settings, name, 0) or 0))


# The one hourly count every no-login visit shares, whichever job makes it.
# Spelt out rather than left to `budget_key`'s blank-session fallback, so it
# can never be mistaken for -- or collide with -- a real account's key
# ("meta:<id>" or "<platform>:<session id>").
NO_LOGIN_KEY = "{platform}:no-login-ip"


def _budget_account(platform_id: str, session_item: dict) -> str:
    """The key `hold` counts this item under."""
    from backend.shared import meta_account

    if (session_item or {}).get("anonymous"):
        return NO_LOGIN_KEY.format(platform=platform_id)
    return meta_account.budget_key(
        platform_id, str(session_item.get("id") or ""), session_item.get("cookies"))


async def hold(platform_id: str, session_item: dict, kind: str,
               should_stop: Callable[[], bool], log=None, label: str = "") -> bool:
    """The one call a runner makes before a search or a profile visit:
    check the account's DAILY ceiling (raising `DailyLimitReached` when it
    is spent), wait for room under its hourly ceiling, then count the
    action against both. -> False only when stopped while waiting. Free for
    platforms with no ceiling.

    An anonymous (no-login) item has no daily ceiling (`daily_limit_for`
    says 0: there is no account to rest) and its hourly ceiling is the
    no-login one, counted under `NO_LOGIN_KEY` -- see `limit_for`."""
    account = _budget_account(platform_id, session_item)
    daily = daily_limit_for(platform_id, kind, session_item)
    if daily > 0:
        used = await _daily_store.count(account, kind, _day())
        if used >= daily:
            raise DailyLimitReached(platform_id, kind, used, daily)

    limit = limit_for(platform_id, kind, session_item)
    if limit > 0:
        # An account that was warned works at a fraction of the hourly
        # limit too (see "LIMITS THAT LEARN").
        limit = max(1, int(limit * penalty_factor(session_item)))
    if limit <= 0:
        taken = not should_stop()
    else:
        taken = await take(account, kind, limit, should_stop, log=log,
                           label=f"[{platform_id}] {label}".strip())
    if taken and daily > 0:
        await _daily_store.add(account, kind, _day())
    return taken


# ------------------------------------------------------------ daily ceiling

DAY_S = 86400.0


def _day(now: Optional[float] = None) -> str:
    t = time.time() if now is None else now
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def next_reset(now: Optional[float] = None) -> float:
    """Epoch seconds of the next 00:00 UTC -- when a spent daily limit
    comes back."""
    t = time.time() if now is None else now
    return (int(t // DAY_S) + 1) * DAY_S


class DailyLimitReached(Exception):
    """This account has done its day's share of `kind`. Not a failure of the
    account and not of the keyword: rest the account until `resets_at` and
    leave the remaining work owed."""

    def __init__(self, platform_id: str, kind: str, used: int, limit: int):
        self.platform_id = platform_id
        self.kind = kind
        self.used = used
        self.limit = limit
        self.resets_at = next_reset()
        noun = _NOUNS.get(kind, kind)
        when = datetime.fromtimestamp(self.resets_at, timezone.utc).strftime("%H:%M UTC")
        super().__init__(
            f"daily limit reached -- this {platform_id} account has done {used} {noun} "
            f"today (limit {limit}); resting it until {when}. Anything not reached is "
            f"left owed for the next run")


def warmup_factor(session_item: dict, now: Optional[float] = None) -> float:
    """1.0 for an established account; 0.25 -> 1.0 across the warm-up for
    one added to the pool recently. `added_at` 0 or missing means "added
    before this existed" and is treated as established -- never as new."""
    from backend.config.settings import settings

    days = float(getattr(settings, "instagram_warmup_days", 0) or 0)
    added = float((session_item or {}).get("added_at") or 0)
    if days <= 0 or added <= 0:
        return 1.0
    t = time.time() if now is None else now
    age_days = max(0.0, (t - added) / DAY_S)
    return min(1.0, 0.25 + 0.75 * (age_days / days))


PENALTY_DAYS = {"rate_limited": 3.0, "checkpointed": 7.0}
PENALTY_STEP = 0.5
PENALTY_FLOOR = 0.2


def penalty_factor(session_item: Optional[dict], now: Optional[float] = None) -> float:
    """1.0 for an account with no recent warning; otherwise the share of its
    limits it may use right now. Starts at the penalty and rises in a
    straight line to 1.0 at `budget_penalty_until`."""
    it = session_item or {}
    t = time.time() if now is None else now
    until = float(it.get("budget_penalty_until") or 0)
    at = float(it.get("budget_penalty_at") or 0)
    f = float(it.get("budget_penalty_factor") or 1.0)
    if f >= 1.0 or until <= t or until <= at:
        return 1.0
    frac = max(0.0, min(1.0, (t - at) / (until - at)))
    return min(1.0, f + (1.0 - f) * frac)


def next_penalty(session_item: Optional[dict], reason: str,
                 now: Optional[float] = None) -> dict:
    """The session-record fields to write when this account was warned
    (`reason` is the session verdict: rate_limited / checkpointed). Halves
    whatever it is allowed RIGHT NOW, so repeat warnings compound, and
    restarts the recovery clock."""
    t = time.time() if now is None else now
    cur = penalty_factor(session_item, t)
    days = PENALTY_DAYS.get(reason, 3.0)
    return {
        "budget_penalty_factor": max(PENALTY_FLOOR, cur * PENALTY_STEP),
        "budget_penalty_at": t,
        "budget_penalty_until": t + days * DAY_S,
    }


def daily_limit_for(platform_id: str, kind: str, session_item: Optional[dict] = None,
                    now: Optional[float] = None) -> int:
    """The daily ceiling for `kind` on this account, after warm-up. 0 = none
    (every platform that does not sign in with an Instagram account, and an
    anonymous browser, which has no account to protect)."""
    from backend.config.settings import settings
    from backend.shared import meta_account

    if not meta_account.is_meta(platform_id) or (session_item or {}).get("anonymous"):
        return 0
    name = ("instagram_searches_per_day" if kind == SEARCH
            else "instagram_profile_visits_per_day")
    base = max(0, int(getattr(settings, name, 0) or 0))
    if base <= 0:
        return 0
    return max(1, int(base * warmup_factor(session_item or {}, now)
                      * penalty_factor(session_item, now)))


class _MemoryDailyStore:
    def __init__(self):
        self._counts: Dict[Tuple[str, str, str], int] = {}

    async def count(self, account: str, kind: str, day: str) -> int:
        return self._counts.get((account, kind, day), 0)

    async def add(self, account: str, kind: str, day: str) -> None:
        key = (account, kind, day)
        self._counts[key] = self._counts.get(key, 0) + 1

    def reset(self) -> None:
        self._counts.clear()


class _MongoDailyStore:
    """Daily counts in Mongo, so a restart does not hand a tired account a
    fresh day. A failed read or write is logged and falls back to the
    in-memory count -- the hourly ceiling and the pushback governor still
    apply, so a database blip costs accuracy here, never every safeguard."""

    COLLECTION = "account_budget_daily"

    def __init__(self, fallback: _MemoryDailyStore):
        self._mem = fallback

    @staticmethod
    def _id(account: str, kind: str, day: str) -> str:
        return f"{account}|{kind}|{day}"

    async def count(self, account: str, kind: str, day: str) -> int:
        try:
            from backend.database.connection import db
            doc = await db()[self.COLLECTION].find_one({"_id": self._id(account, kind, day)})
            stored = int((doc or {}).get("count") or 0)
            return max(stored, await self._mem.count(account, kind, day))
        except Exception as e:                       # noqa: BLE001
            _log_store_error("read", e)
            return await self._mem.count(account, kind, day)

    async def add(self, account: str, kind: str, day: str) -> None:
        await self._mem.add(account, kind, day)
        try:
            from backend.database.connection import db
            await db()[self.COLLECTION].update_one(
                {"_id": self._id(account, kind, day)},
                {"$inc": {"count": 1},
                 "$set": {"account": account, "kind": kind, "day": day,
                          "updated_at": datetime.now(timezone.utc)}},
                upsert=True)
        except Exception as e:                       # noqa: BLE001
            _log_store_error("write", e)

    def reset(self) -> None:
        self._mem.reset()


def _log_store_error(what: str, e: Exception) -> None:
    try:
        from backend.shared.logging import get_logger
        get_logger("shared.account_budget").warning(
            f"daily account budget: could not {what} the stored count "
            f"({type(e).__name__}: {e}) -- using this process's own count")
    except Exception:                                # noqa: BLE001
        pass


_memory_daily = _MemoryDailyStore()
_daily_store: Any = _memory_daily


def use_persistent_store() -> None:
    """Called once the app's Mongo is up (main.py). Tests never call it, so
    they count in memory and never touch the real database."""
    global _daily_store
    if not isinstance(_daily_store, _MongoDailyStore):
        _daily_store = _MongoDailyStore(_memory_daily)


async def daily_used(platform_id: str, session_item: dict, kind: str) -> int:
    """How much of `kind` this account has done today (for the UI)."""
    from backend.shared import meta_account
    account = meta_account.budget_key(
        platform_id, str(session_item.get("id") or ""), session_item.get("cookies"))
    return await _daily_store.count(account, kind, _day())


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
        whose = ("from this machine without login" if account.endswith(":no-login-ip")
                 else "on this account")
        log.warning(
            f"{label or account}: hourly limit reached ({limit} {noun}/hour {whose}) "
            f"-- waiting ~{max(1, round(wait / 60))}m before the next one")
    while True:
        if should_stop():
            return False
        wait = b.wait_s(account, kind, limit)
        if wait <= 0:
            b.record(account, kind)
            return True
        await asyncio.sleep(min(tick, wait))
