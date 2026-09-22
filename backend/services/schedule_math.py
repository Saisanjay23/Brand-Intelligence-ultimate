"""When the Scheduler should next fire -- the clock arithmetic, on its own.

PURE ON PURPOSE. Nothing in this module touches Mongo, the network, the
discovery engine or the wall clock unless a caller hands it one. Every
function takes the instant to reason from as an argument and returns a new
instant. That is what makes the one question this feature lives or dies on
-- "will it actually fire at 02:30 on the right day?" -- answerable by a
test rather than by waiting until 02:30.

THE HARD PART IS NOT THE ARITHMETIC, IT IS THE TIMEZONE. An analyst types
"02:30" meaning 02:30 where they are sitting. Stored as a UTC instant and
repeated daily, that drifts an hour twice a year and starts firing at 01:30
or 03:30 local with nothing to explain it. So the WALL CLOCK is what is
stored -- "02:30", plus an IANA zone name -- and the instant is recomputed
from it every single time. Daylight saving then takes care of itself: the
sweep stays at 02:30 local all year, which is what was meant.

TWICE A YEAR A WALL CLOCK IS NOT A TIME AT ALL:

  * SPRING FORWARD leaves a gap. In Europe/London on the last Sunday in
    March, 01:30 does not happen -- the clocks go 00:59 -> 02:00. A daily
    01:30 sweep has no instant to run at that day.
  * FALL BACK repeats an hour. On the last Sunday in October, 01:30 happens
    twice, an hour apart.

Both are resolved here rather than left to chance, and both are REPORTED
(`WallClockShift`) instead of silently absorbed, because a sweep that
quietly ran an hour late is precisely the kind of thing nobody notices for
six months. Gap: run at the instant the clocks jump to. Overlap: run at the
FIRST of the two, so the sweep happens once, earlier rather than later.

WEEKDAYS ARE PYTHON'S: Monday is 0, Sunday is 6 (`date.weekday()`). The UI
sends the same convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.shared.logging import get_logger

log = get_logger("services.schedule_math")

# The three shapes an analyst can ask for. Deliberately not cron: a cron
# box is a typo away from scheduling the wrong thing with no way to see it,
# and these three cover nightly sweeps, weekday sweeps and a one-off
# out-of-hours run, which is the whole of what the Scheduler is asked for.
MODE_ONCE = "once"
MODE_DAILY = "daily"
MODE_WEEKLY = "weekly"
MODES = (MODE_ONCE, MODE_DAILY, MODE_WEEKLY)

# How far ahead to look for the next matching day before giving up. Only
# reachable by a weekly schedule with no weekdays selected, which is
# rejected earlier -- this is the backstop that stops a bad schedule
# spinning a loop for ever.
_SEARCH_DAYS = 400


class ScheduleError(ValueError):
    """A schedule that cannot be honoured as written."""


@dataclass(frozen=True)
class WallClockShift:
    """A local wall clock that did not exist, or existed twice.

    Carried out of `next_run_after` so the caller can say so in the UI and
    the log. `requested` is what the analyst typed; `actual` is the wall
    clock the run will really happen at.
    """

    kind: str            # "gap" | "overlap"
    requested: str       # "02:30"
    actual: str          # "03:30"
    on: str              # "2026-03-29"

    def describe(self) -> str:
        if self.kind == "gap":
            return (f"{self.on}: the clocks skip {self.requested} in this timezone "
                    f"(daylight saving), so this run happens at {self.actual}")
        return (f"{self.on}: {self.requested} happens twice in this timezone "
                f"(daylight saving), so this run happens at the first one")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "requested": self.requested,
                "actual": self.actual, "on": self.on, "note": self.describe()}


@dataclass(frozen=True)
class Schedule:
    """What an analyst set, exactly as they set it.

    `at` is a WALL CLOCK in `tz`, never a UTC instant -- see the module
    docstring for why that distinction is the whole point.
    """

    enabled: bool = False
    mode: str = MODE_DAILY
    at: str = "02:00"                       # "HH:MM", local to `tz`
    on_date: str = ""                       # "YYYY-MM-DD", MODE_ONCE only
    weekdays: tuple[int, ...] = ()          # Mon=0..Sun=6, MODE_WEEKLY only
    tz: str = "UTC"                         # IANA name, e.g. "Asia/Kolkata"

    # ---------------------------------------------------------- validation

    def validate(self) -> None:
        """Raise `ScheduleError` if this could never fire as written.

        Called before a schedule is SAVED, not when it is read back: a
        schedule already in the database has to load whatever it says, so
        that a bad one can be seen and corrected rather than taking the
        panel down with it.
        """
        if self.mode not in MODES:
            raise ScheduleError(
                f"mode must be one of {', '.join(MODES)} -- got {self.mode!r}")
        parse_hhmm(self.at)                 # raises with its own message
        resolve_zone(self.tz, strict=True)  # ditto
        if self.mode == MODE_ONCE:
            if not self.on_date:
                raise ScheduleError("a one-time schedule needs a date")
            parse_date(self.on_date)
        if self.mode == MODE_WEEKLY:
            if not self.weekdays:
                raise ScheduleError(
                    "a weekly schedule needs at least one weekday selected -- "
                    "with none it could never fire")
            for d in self.weekdays:
                if not 0 <= int(d) <= 6:
                    raise ScheduleError(
                        f"weekday must be 0 (Monday) to 6 (Sunday) -- got {d!r}")

    # -------------------------------------------------------------- output

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "at": self.at,
            "on_date": self.on_date,
            "weekdays": list(self.weekdays),
            "tz": self.tz,
        }

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "Schedule":
        """Rebuild from storage, TOLERANTLY.

        A stored schedule is read on every tick and on every panel load, so
        a single bad field must degrade to a schedule that does not fire --
        never to an exception that takes the whole Scheduler with it. The
        strict pass belongs on the way IN (`validate`), where an analyst is
        watching and can fix it.
        """
        raw = raw or {}
        try:
            weekdays = tuple(sorted({int(d) for d in (raw.get("weekdays") or [])
                                     if 0 <= int(d) <= 6}))
        except (TypeError, ValueError):
            weekdays = ()
        return cls(
            enabled=bool(raw.get("enabled", False)),
            mode=str(raw.get("mode") or MODE_DAILY),
            at=str(raw.get("at") or "02:00"),
            on_date=str(raw.get("on_date") or ""),
            weekdays=weekdays,
            tz=str(raw.get("tz") or "UTC"),
        )


# --------------------------------------------------------------- parsing

def parse_hhmm(value: str) -> time:
    """HH:MM -> a time. Anything else raises, naming the text that failed."""
    try:
        hh, mm = str(value).strip().split(":")
        parsed = time(int(hh), int(mm))
    except Exception:
        raise ScheduleError(
            f"time must look like HH:MM on a 24-hour clock -- got {value!r}") from None
    return parsed


def parse_date(value: str) -> date:
    """YYYY-MM-DD -> a date. Anything else raises, naming the text that failed."""
    try:
        return date.fromisoformat(str(value).strip())
    except Exception:
        raise ScheduleError(
            f"date must look like YYYY-MM-DD -- got {value!r}") from None


def resolve_zone(name: str, *, strict: bool = False) -> ZoneInfo:
    """An IANA zone name -> a tzinfo.

    `strict` is for the save path, where an analyst is there to correct it.
    Everywhere else an unknown zone falls back to UTC and SAYS SO at error
    level: a scheduler that refuses to start because a timezone database is
    missing is worse than one that fires at the wrong hour and complains
    loudly about it.

    Unknown zones are a real possibility on Windows, where `zoneinfo` has
    no system database of its own and reads the `tzdata` package instead.
    """
    try:
        return ZoneInfo(str(name).strip() or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        if strict:
            raise ScheduleError(
                f"unknown timezone {name!r} -- use an IANA name such as "
                "'Asia/Kolkata' or 'Europe/London'. On Windows this also "
                "means the `tzdata` package is missing.") from None
        log.error(
            f"schedule: unknown timezone {name!r} -- falling back to UTC, so runs "
            "will fire at the UTC reading of the configured time. Install `tzdata` "
            "or correct the timezone in the Scheduler.")
        return ZoneInfo("UTC")


# -------------------------------------------------- wall clock -> instant

def localize(tz: ZoneInfo, day: date, at: time) -> tuple[datetime, Optional[WallClockShift]]:
    """One local wall clock on one local day -> the UTC instant it means.

    Returns the instant plus, when the wall clock was not an ordinary one,
    a `WallClockShift` describing what happened to it. See the module
    docstring for the two cases.
    """
    naive = datetime.combine(day, at)
    # fold=0 is the default and is the right answer for BOTH edge cases:
    # inside a spring-forward gap it resolves to the instant the clocks
    # jump to, and inside a fall-back overlap it resolves to the first of
    # the two readings.
    aware = naive.replace(tzinfo=tz)
    instant = aware.astimezone(timezone.utc)

    # Did the wall clock survive the round trip? If not, the reading the
    # analyst typed does not exist on this day in this zone.
    back = instant.astimezone(tz).replace(tzinfo=None)
    if back != naive:
        return instant, WallClockShift(
            kind="gap", requested=at.strftime("%H:%M"),
            actual=back.strftime("%H:%M"), on=day.isoformat())

    # It exists -- but does it exist twice? fold=1 naming a different
    # instant is the definition of an ambiguous local time.
    if naive.replace(tzinfo=tz, fold=1).astimezone(timezone.utc) != instant:
        return instant, WallClockShift(
            kind="overlap", requested=at.strftime("%H:%M"),
            actual=at.strftime("%H:%M"), on=day.isoformat())

    return instant, None


# ------------------------------------------------------------- the answer

def next_run_after(
    schedule: Schedule, after: datetime,
) -> tuple[Optional[datetime], Optional[WallClockShift]]:
    """The first instant this schedule fires STRICTLY AFTER `after`.

    `None` means it never fires again: the schedule is off, or it is a
    one-time schedule whose moment has passed. A caller must treat that as
    "nothing is scheduled" and say so -- never as "something is scheduled,
    just not yet", which is the reading that leaves an analyst waiting all
    night for a run that was never coming.

    `after` must be timezone-aware. Days are walked one at a time and each
    is localized independently rather than adding 24h repeatedly, because
    across a daylight-saving boundary a local day is not 24 hours long and
    the repeated-addition version drifts off the wall clock it is meant to
    hold.
    """
    if after.tzinfo is None:
        raise ScheduleError("`after` must be timezone-aware")
    after = after.astimezone(timezone.utc)

    if not schedule.enabled:
        return None, None

    try:
        at = parse_hhmm(schedule.at)
        tz = resolve_zone(schedule.tz)
    except ScheduleError as e:
        # A stored schedule that cannot be read is a schedule that does not
        # fire -- loudly. Returning None here is what makes the panel say
        # "not scheduled" instead of counting down to nothing.
        log.error(f"schedule: cannot compute a next run -- {e}")
        return None, None

    if schedule.mode == MODE_ONCE:
        try:
            day = parse_date(schedule.on_date)
        except ScheduleError as e:
            log.error(f"schedule: cannot compute a next run -- {e}")
            return None, None
        instant, shift = localize(tz, day, at)
        return (instant, shift) if instant > after else (None, None)

    if schedule.mode == MODE_WEEKLY:
        wanted = set(schedule.weekdays)
        if not wanted:
            log.error("schedule: weekly with no weekdays selected -- it can never fire")
            return None, None
    else:
        wanted = None       # daily: every day qualifies

    # Start from the day `after` falls on IN THE SCHEDULE'S OWN ZONE, not
    # in UTC. At 23:00 UTC in Asia/Kolkata it is already tomorrow, and
    # starting from the UTC date would look at a day that is already gone.
    day = after.astimezone(tz).date()
    for _ in range(_SEARCH_DAYS):
        if wanted is None or day.weekday() in wanted:
            instant, shift = localize(tz, day, at)
            if instant > after:
                return instant, shift
        day += timedelta(days=1)

    log.error(
        f"schedule: no run found within {_SEARCH_DAYS} days for {schedule.to_dict()} "
        "-- treating as not scheduled")
    return None, None


def next_runs(schedule: Schedule, after: datetime, count: int) -> list[datetime]:
    """The next `count` firing instants, for showing an analyst what they
    just set up before they walk away from it. A one-time schedule returns
    at most one."""
    out: list[datetime] = []
    cursor = after
    for _ in range(max(0, count)):
        instant, _shift = next_run_after(schedule, cursor)
        if instant is None:
            break
        out.append(instant)
        cursor = instant
    return out


# ------------------------------------------------------------- catch-up

def catch_up_decision(
    due_at: Optional[datetime], now: datetime, grace: timedelta,
) -> tuple[str, timedelta]:
    """What to do about a firing time that has already gone past.

    Returns `(decision, lateness)` where decision is one of:

        "not_due"  -- `due_at` is still ahead, or there is nothing scheduled
        "run"      -- it is due, or late by less than `grace`: run it now
        "missed"   -- late by more than `grace`: do NOT run, record it

    THE POINT OF THE GRACE WINDOW. A backend restarted during a deploy, or
    a laptop opened at nine after a 02:00 sweep, should still get the sweep
    -- an hour or six late is late, not useless. A machine switched off
    over a long weekend should not suddenly start driving logged-in social
    accounts at an hour nobody expects, for a run whose moment is long past
    and whose next one is hours away anyway.

    "missed" IS A RESULT, NOT A SILENCE. The caller records it and the
    panel shows it. A run that did not happen and left nothing behind is
    exactly the failure this whole feature is supposed to be immune to.
    """
    if due_at is None:
        return "not_due", timedelta(0)
    if due_at.tzinfo is None or now.tzinfo is None:
        raise ScheduleError("catch-up needs timezone-aware instants")
    lateness = now.astimezone(timezone.utc) - due_at.astimezone(timezone.utc)
    if lateness < timedelta(0):
        return "not_due", timedelta(0)
    if lateness <= grace:
        return "run", lateness
    return "missed", lateness


def humanize(delta: timedelta) -> str:
    """"3h 20m" -- for saying how late a catch-up run is, in the panel and
    in the log, without either having to do the arithmetic."""
    secs = int(abs(delta).total_seconds())
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"
