"""The Scheduler's clock: does it fire when it said it would?

This is the one part of the scheduled-sweep feature that cannot be checked
by looking at it. A schedule that is an hour off, or that skips a day twice
a year, or that quietly never fires at all, behaves identically to a
correct one right up until the night it matters -- and the symptom is a
sweep that did not happen, which looks exactly like a sweep nobody asked
for. So every rule the module claims is asserted here against real IANA
zones and real daylight-saving transitions.

The awkward cases, and why each has a test:

  * A daily schedule must hold its WALL CLOCK across a DST change, not its
    UTC instant. Storing the instant is the obvious implementation and it
    drifts the sweep an hour twice a year.
  * Spring forward deletes an hour. A 01:30 daily sweep has no 01:30 to run
    at on that day, and "no instant" must not become "no run".
  * Fall back repeats an hour. 01:30 happens twice and the sweep must
    happen ONCE.
  * A zone ahead of UTC rolls over to the next day before UTC does, so
    "which day do we start looking from" has to be asked in the schedule's
    own zone.
  * A one-time schedule whose moment has passed must answer None -- "never
    again" -- not the same instant for ever.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.services.schedule_math import (
    MODE_DAILY,
    MODE_ONCE,
    MODE_WEEKLY,
    Schedule,
    ScheduleError,
    catch_up_decision,
    humanize,
    localize,
    next_run_after,
    next_runs,
    parse_date,
    parse_hhmm,
    resolve_zone,
)

LONDON = ZoneInfo("Europe/London")
KOLKATA = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


def local(instant: datetime, tz: ZoneInfo) -> str:
    return instant.astimezone(tz).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------- parsing

class TestParsing:
    def test_hhmm_round_trips(self):
        assert parse_hhmm("02:30").hour == 2
        assert parse_hhmm("02:30").minute == 30
        assert parse_hhmm("23:59").hour == 23

    @pytest.mark.parametrize("bad", ["", "2:61", "25:00", "0230", "half two", None])
    def test_bad_time_names_what_failed(self, bad):
        with pytest.raises(ScheduleError) as e:
            parse_hhmm(bad)
        # The analyst has to be able to see WHICH value was rejected.
        assert repr(bad) in str(e.value) or str(bad) in str(e.value)

    @pytest.mark.parametrize("bad", ["", "22-09-2026", "2026/09/22", "tomorrow"])
    def test_bad_date_names_what_failed(self, bad):
        with pytest.raises(ScheduleError):
            parse_date(bad)

    def test_unknown_zone_is_fatal_when_strict(self):
        with pytest.raises(ScheduleError):
            resolve_zone("Mars/Olympus_Mons", strict=True)

    def test_unknown_zone_falls_back_to_utc_when_not_strict(self):
        # A scheduler that refuses to start because of one bad zone is
        # worse than one that fires on UTC and complains.
        assert resolve_zone("Mars/Olympus_Mons").key == "UTC"


# ------------------------------------------------------------ validation

class TestValidation:
    def test_weekly_with_no_days_is_refused(self):
        # It could never fire. Saving it would be a promise that is a lie.
        with pytest.raises(ScheduleError, match="weekday"):
            Schedule(enabled=True, mode=MODE_WEEKLY, at="09:00",
                     weekdays=(), tz="UTC").validate()

    def test_once_with_no_date_is_refused(self):
        with pytest.raises(ScheduleError, match="date"):
            Schedule(enabled=True, mode=MODE_ONCE, at="09:00", tz="UTC").validate()

    def test_unknown_mode_is_refused(self):
        with pytest.raises(ScheduleError, match="mode"):
            Schedule(enabled=True, mode="hourly", at="09:00", tz="UTC").validate()

    def test_valid_schedules_pass(self):
        Schedule(enabled=True, mode=MODE_DAILY, at="02:00", tz="Asia/Kolkata").validate()
        Schedule(enabled=True, mode=MODE_WEEKLY, at="09:00",
                 weekdays=(0, 4), tz="Europe/London").validate()
        Schedule(enabled=True, mode=MODE_ONCE, at="09:00",
                 on_date="2030-01-01", tz="UTC").validate()

    def test_from_dict_never_raises_on_rubbish(self):
        """A stored schedule is read on every tick. One bad field must
        degrade to a schedule that does not fire -- never to an exception
        that takes the whole Scheduler down with it."""
        s = Schedule.from_dict({"weekdays": ["x", None, 99], "mode": None, "at": None})
        assert s.weekdays == ()
        assert s.at == "02:00"
        s2 = Schedule.from_dict(None)
        assert s2.enabled is False


# -------------------------------------------------------- daylight saving

class TestDaylightSaving:
    """Europe/London 2026: forward 2026-03-29 01:00->02:00,
    back 2026-10-25 02:00->01:00."""

    def test_daily_holds_its_wall_clock_across_spring_forward(self):
        # The whole reason the WALL CLOCK is stored and the instant
        # recomputed. Storing the instant drifts this to 00:30 or 02:30.
        s = Schedule(enabled=True, mode=MODE_DAILY, at="01:30", tz="Europe/London")
        cursor = datetime(2026, 3, 27, 12, 0, tzinfo=UTC)
        seen = []
        for _ in range(4):
            nxt, _shift = next_run_after(s, cursor)
            seen.append(local(nxt, LONDON))
            cursor = nxt
        assert seen[0] == "2026-03-28 01:30"
        # 01:30 does not exist on the 29th -- it runs at the instant the
        # clocks jump to, and does NOT get skipped.
        assert seen[1] == "2026-03-29 02:30"
        assert seen[2] == "2026-03-30 01:30"
        assert seen[3] == "2026-03-31 01:30"

    def test_the_skipped_hour_is_reported_not_swallowed(self):
        s = Schedule(enabled=True, mode=MODE_DAILY, at="01:30", tz="Europe/London")
        nxt, shift = next_run_after(s, datetime(2026, 3, 28, 12, 0, tzinfo=UTC))
        assert local(nxt, LONDON) == "2026-03-29 02:30"
        # A sweep that quietly ran an hour late is the kind of thing nobody
        # notices for six months.
        assert shift is not None and shift.kind == "gap"
        assert "01:30" in shift.describe() and "02:30" in shift.describe()

    def test_fall_back_runs_once_at_the_first_of_the_two(self):
        instant, shift = localize(LONDON, date(2026, 10, 25), parse_hhmm("01:30"))
        # 01:30 BST (UTC+1), i.e. 00:30 UTC -- the earlier of the two.
        assert instant == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
        assert shift is not None and shift.kind == "overlap"

    def test_the_repeated_hour_produces_exactly_one_run(self):
        """The failure this guards against is a sweep running twice, an
        hour apart, on one night a year -- two sweeps of every client
        through the same accounts back to back."""
        s = Schedule(enabled=True, mode=MODE_DAILY, at="01:30", tz="Europe/London")
        runs = next_runs(s, datetime(2026, 10, 24, 12, 0, tzinfo=UTC), 3)
        on_the_25th = [r for r in runs if r.astimezone(LONDON).date() == date(2026, 10, 25)]
        assert len(on_the_25th) == 1

    def test_a_zone_without_dst_is_untouched(self):
        s = Schedule(enabled=True, mode=MODE_DAILY, at="02:00", tz="Asia/Kolkata")
        for r in next_runs(s, datetime(2026, 3, 27, 12, 0, tzinfo=UTC), 6):
            assert r.astimezone(KOLKATA).strftime("%H:%M") == "02:00"


# -------------------------------------------------------------- the modes

class TestDaily:
    def test_fires_today_when_the_time_is_still_ahead(self):
        s = Schedule(enabled=True, mode=MODE_DAILY, at="23:00", tz="UTC")
        nxt, _ = next_run_after(s, datetime(2026, 9, 22, 10, 0, tzinfo=UTC))
        assert nxt == datetime(2026, 9, 22, 23, 0, tzinfo=UTC)

    def test_rolls_to_tomorrow_when_the_time_has_passed(self):
        s = Schedule(enabled=True, mode=MODE_DAILY, at="02:00", tz="UTC")
        nxt, _ = next_run_after(s, datetime(2026, 9, 22, 10, 0, tzinfo=UTC))
        assert nxt == datetime(2026, 9, 23, 2, 0, tzinfo=UTC)

    def test_is_strictly_after_never_equal(self):
        """Firing at exactly `after` would re-fire the run that just
        happened, for ever."""
        s = Schedule(enabled=True, mode=MODE_DAILY, at="02:00", tz="UTC")
        at_the_moment = datetime(2026, 9, 22, 2, 0, tzinfo=UTC)
        nxt, _ = next_run_after(s, at_the_moment)
        assert nxt > at_the_moment
        assert nxt == datetime(2026, 9, 23, 2, 0, tzinfo=UTC)

    def test_the_day_is_chosen_in_the_schedules_own_zone(self):
        """At 21:00 UTC it is already tomorrow in Kolkata. Starting the
        search from the UTC date looks at a day that is already gone."""
        s = Schedule(enabled=True, mode=MODE_DAILY, at="02:00", tz="Asia/Kolkata")
        # 2026-09-22 21:00Z == 2026-09-23 02:30 IST, so today's 02:00 IST
        # has already been and gone.
        nxt, _ = next_run_after(s, datetime(2026, 9, 22, 21, 0, tzinfo=UTC))
        assert local(nxt, KOLKATA) == "2026-09-24 02:00"


class TestWeekly:
    def test_only_the_selected_weekdays(self):
        # Monday=0, Friday=4.
        s = Schedule(enabled=True, mode=MODE_WEEKLY, at="09:00",
                     weekdays=(0, 4), tz="UTC")
        got = [r.strftime("%a %Y-%m-%d %H:%M")
               for r in next_runs(s, datetime(2026, 9, 22, 0, 0, tzinfo=UTC), 4)]
        assert got == ["Fri 2026-09-25 09:00", "Mon 2026-09-28 09:00",
                       "Fri 2026-10-02 09:00", "Mon 2026-10-05 09:00"]

    def test_wraps_across_the_week_boundary(self):
        s = Schedule(enabled=True, mode=MODE_WEEKLY, at="09:00",
                     weekdays=(0,), tz="UTC")           # Mondays only
        # From a Saturday.
        nxt, _ = next_run_after(s, datetime(2026, 9, 26, 12, 0, tzinfo=UTC))
        assert nxt.weekday() == 0
        assert nxt == datetime(2026, 9, 28, 9, 0, tzinfo=UTC)

    def test_no_weekdays_never_fires(self):
        s = Schedule(enabled=True, mode=MODE_WEEKLY, at="09:00",
                     weekdays=(), tz="UTC")
        assert next_run_after(s, datetime.now(UTC))[0] is None


class TestOnce:
    def test_fires_at_its_moment(self):
        s = Schedule(enabled=True, mode=MODE_ONCE, at="14:30",
                     on_date="2026-12-25", tz="UTC")
        nxt, _ = next_run_after(s, datetime(2026, 9, 22, 0, 0, tzinfo=UTC))
        assert nxt == datetime(2026, 12, 25, 14, 30, tzinfo=UTC)

    def test_a_past_moment_answers_never_not_soon(self):
        """None means 'it never fires again'. The caller shows that as
        'not scheduled' -- the reading that must NOT happen is 'not yet',
        which leaves an analyst waiting all night for a run that was
        never coming."""
        s = Schedule(enabled=True, mode=MODE_ONCE, at="09:00",
                     on_date="2020-01-01", tz="UTC")
        assert next_run_after(s, datetime.now(UTC))[0] is None

    def test_does_not_repeat(self):
        s = Schedule(enabled=True, mode=MODE_ONCE, at="14:30",
                     on_date="2026-12-25", tz="UTC")
        assert len(next_runs(s, datetime(2026, 9, 22, 0, 0, tzinfo=UTC), 5)) == 1


class TestDisabled:
    def test_off_never_fires(self):
        s = Schedule(enabled=False, mode=MODE_DAILY, at="02:00", tz="UTC")
        assert next_run_after(s, datetime.now(UTC))[0] is None

    def test_a_naive_instant_is_refused(self):
        """Comparing a naive instant against a UTC one silently treats it
        as local time -- on a machine at UTC+5:30 that is a schedule five
        and a half hours off, with nothing to show for it."""
        s = Schedule(enabled=True, mode=MODE_DAILY, at="02:00", tz="UTC")
        with pytest.raises(ScheduleError):
            next_run_after(s, datetime(2026, 9, 22, 10, 0))


# ---------------------------------------------------------------- catch-up

class TestCatchUp:
    GRACE = timedelta(hours=6)

    def test_still_ahead_is_not_due(self):
        now = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
        due = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
        assert catch_up_decision(due, now, self.GRACE)[0] == "not_due"

    def test_nothing_scheduled_is_not_due(self):
        assert catch_up_decision(None, datetime.now(UTC), self.GRACE)[0] == "not_due"

    def test_exactly_due_runs(self):
        now = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
        assert catch_up_decision(now, now, self.GRACE)[0] == "run"

    def test_late_inside_the_window_runs(self):
        """A backend restarted during a deploy, or a laptop opened at nine
        after a 02:00 sweep. An hour late is late, not useless."""
        now = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
        due = datetime(2026, 9, 22, 2, 30, tzinfo=UTC)       # 5h30m late
        decision, lateness = catch_up_decision(due, now, self.GRACE)
        assert decision == "run"
        assert humanize(lateness) == "5h 30m"

    def test_the_boundary_itself_runs(self):
        now = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
        due = now - self.GRACE
        assert catch_up_decision(due, now, self.GRACE)[0] == "run"

    def test_later_than_the_window_is_missed_not_run(self):
        """A machine off over a long weekend must not suddenly start
        driving logged-in social accounts at an hour nobody expects."""
        now = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
        due = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
        decision, lateness = catch_up_decision(due, now, self.GRACE)
        assert decision == "missed"
        assert lateness > self.GRACE

    def test_missed_is_a_verdict_not_a_silence(self):
        """The three decisions are exhaustive and each one names itself.
        There is no fourth answer meaning 'nothing happened'."""
        now = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
        verdicts = {
            catch_up_decision(d, now, self.GRACE)[0]
            for d in (None,
                      now + timedelta(hours=1),
                      now,
                      now - timedelta(hours=1),
                      now - timedelta(days=3))
        }
        assert verdicts == {"not_due", "run", "missed"}


class TestHumanize:
    @pytest.mark.parametrize("delta,expected", [
        (timedelta(seconds=30), "30s"),
        (timedelta(minutes=5), "5m"),
        (timedelta(hours=3, minutes=20), "3h 20m"),
        (timedelta(days=2, hours=5), "2d 5h"),
    ])
    def test_reads_as_english(self, delta, expected):
        assert humanize(delta) == expected
