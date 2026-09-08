"""Filtering discovered profiles by an explicit first-seen date range.

PURE LOGIC ONLY -- these exercise the query clause and the API's date
parsing, not Mongo (see this suite's scope note). The clause is checked
against the live database separately; what is worth pinning here is the
three rules that are easy to get subtly wrong and hard to notice:

THE RANGE IS HALF-OPEN, [from, to). The API turns "to 6 Sep" into "before
7 Sep 00:00", so the whole of the 6th is inside. Get this wrong in one
direction and the last day of every range silently vanishes; get it wrong
in the other (a closed `$lte` on the next midnight) and a profile found at
exactly midnight lands in two adjacent ranges at once. Adjacent ranges have
to tile the timeline with no gap and no overlap, which is what
TestAdjacentRangesTile asserts directly.

A PROFILE WITH NO `first_seen` IS EXCLUDED. That is the opposite of
`_age_clause`, where a missing timestamp counts as "old", and the
difference is deliberate: New/Old are two tabs that must between them
contain every pending profile, so an unknown age needs a home. A date range
is a question about a specific window, and a row whose discovery date is
unknown is not an answer to it.

A DATE IS NOT AN INSTANT. `first_seen` is stored in UTC; an analyst picks a
calendar day in their own timezone. At IST that is a 5.5 hour offset, so a
sweep run after 23:30 local is stored under the FOLLOWING UTC date -- the
day the analyst would filter to and the day the row is stored under are
different ones. The browser resolves the picked day against its own offset
and sends instants, so `_parse_when` has to take a full ISO-8601 timestamp
as readily as a bare date, and must not quietly re-interpret one as the
other.
"""

from datetime import datetime, timezone

import pytest

from backend.api.discovery import _parse_when
from backend.database.repositories.profile_repository import (
    _age_clause,
    _first_seen_range_clause,
)
from backend.shared.errors import ValidationError

UTC = timezone.utc


def _matches(clause: dict, doc: dict) -> bool:
    """Evaluate the small subset of Mongo this clause uses, so the rule can
    be tested without a database. Mirrors test_validated_age.py's helper."""
    for field, cond in clause.items():
        value = doc.get(field, None)
        for op, operand in cond.items():
            if op == "$gte":
                # Mongo: a comparison against a missing/None field is false.
                if value is None or not value >= operand:
                    return False
            elif op == "$lt":
                if value is None or not value < operand:
                    return False
            else:
                raise AssertionError(f"unhandled operator {op}")
    return True


def _at(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def _range(frm, to) -> dict:
    """The clause for a pair of API query values, parsed exactly as the route
    parses them -- so these tests cover both halves TOGETHER and cannot pass
    on a clause the API would never actually build."""
    return _first_seen_range_clause(
        _parse_when(frm, field="first_seen_from") if frm else None,
        _parse_when(to, field="first_seen_to", end_of_day=True) if to else None,
    )


class TestASingleDayIsThatWholeDay:
    """from == to is one calendar day, start to finish."""

    def test_midnight_is_inside(self):
        assert _matches(_range("2026-09-06", "2026-09-06"),
                        {"first_seen": _at(2026, 9, 6, 0, 0)})

    def test_last_minute_of_the_day_is_inside(self):
        assert _matches(_range("2026-09-06", "2026-09-06"),
                        {"first_seen": _at(2026, 9, 6, 23, 59)})

    def test_the_day_before_is_out(self):
        assert not _matches(_range("2026-09-06", "2026-09-06"),
                            {"first_seen": _at(2026, 9, 5, 23, 59)})

    def test_the_next_midnight_is_out(self):
        """The exclusive end. A closed range here would put this row in both
        this day and the next."""
        assert not _matches(_range("2026-09-06", "2026-09-06"),
                            {"first_seen": _at(2026, 9, 7, 0, 0)})


class TestAdjacentRangesTile:
    """No gap, no overlap -- the property that makes the filter trustworthy
    for reporting, where two ranges are expected to add up to their union."""

    def test_every_instant_lands_in_exactly_one_of_two_adjacent_days(self):
        first = _range("2026-09-06", "2026-09-06")
        second = _range("2026-09-07", "2026-09-07")
        instants = [
            _at(2026, 9, 6, 0, 0), _at(2026, 9, 6, 12, 0), _at(2026, 9, 6, 23, 59),
            _at(2026, 9, 7, 0, 0), _at(2026, 9, 7, 12, 0), _at(2026, 9, 7, 23, 59),
        ]
        for t in instants:
            doc = {"first_seen": t}
            assert _matches(first, doc) != _matches(second, doc), t

    def test_a_month_boundary_needs_no_special_case(self):
        assert _matches(_range("2026-08-31", "2026-08-31"),
                        {"first_seen": _at(2026, 8, 31, 22, 0)})
        assert not _matches(_range("2026-08-31", "2026-08-31"),
                            {"first_seen": _at(2026, 9, 1, 0, 1)})

    def test_a_year_boundary_needs_no_special_case(self):
        assert _matches(_range("2026-12-31", "2026-12-31"),
                        {"first_seen": _at(2026, 12, 31, 23, 30)})
        assert not _matches(_range("2026-12-31", "2026-12-31"),
                            {"first_seen": _at(2027, 1, 1, 0, 0)})


class TestOpenEnds:
    """Either bound may stand alone: "since" and "up to"."""

    def test_from_only_has_no_upper_bound(self):
        clause = _range("2026-09-06", None)
        assert _matches(clause, {"first_seen": _at(2030, 1, 1)})
        assert not _matches(clause, {"first_seen": _at(2026, 9, 5)})

    def test_to_only_has_no_lower_bound(self):
        clause = _range(None, "2026-09-06")
        assert _matches(clause, {"first_seen": _at(2001, 1, 1)})
        assert not _matches(clause, {"first_seen": _at(2026, 9, 7)})

    def test_the_two_open_halves_partition_everything(self):
        older = _range(None, "2026-09-06")
        newer = _range("2026-09-07", None)
        for t in [_at(2020, 1, 1), _at(2026, 9, 6, 23, 59),
                  _at(2026, 9, 7, 0, 0), _at(2030, 6, 1)]:
            doc = {"first_seen": t}
            assert _matches(older, doc) != _matches(newer, doc), t


class TestAnUnknownDiscoveryDate:
    """Excluded by a range -- and deliberately NOT the same rule as
    `_age_clause`, which gives a missing timestamp a home in Old."""

    @pytest.mark.parametrize("doc", [{}, {"first_seen": None}])
    def test_it_is_in_no_range(self, doc):
        assert not _matches(_range("2026-09-01", "2026-09-30"), doc)
        assert not _matches(_range("2026-09-01", None), doc)
        assert not _matches(_range(None, "2026-09-30"), doc)

    def test_but_it_is_still_old_for_the_new_old_tabs(self):
        """The contrast, asserted rather than left implied: the two rules read
        the same field and answer differently on purpose."""
        clause = _age_clause("old")
        # _age_clause("old") is now $and of two $or groups; the first_seen
        # group still claims rows with no first_seen.
        fs_group = next(g for g in clause["$and"]
                        if any("first_seen" in b for b in g["$or"]))
        assert {"first_seen": None} in fs_group["$or"]
        assert {"first_seen": {"$exists": False}} in fs_group["$or"]


class TestParsingADateVersusAnInstant:
    def test_a_bare_date_is_utc_midnight(self):
        assert _parse_when("2026-09-06", field="f") == _at(2026, 9, 6)

    def test_a_bare_end_date_advances_to_the_next_midnight(self):
        assert _parse_when("2026-09-06", field="t", end_of_day=True) == _at(2026, 9, 7)

    def test_an_instant_is_taken_as_given_not_re_read_as_a_date(self):
        """What the browser sends. If `end_of_day` also applied here, the UI's
        already-exclusive bound would be pushed a further day out and every
        range would silently include tomorrow."""
        sent = "2026-09-07T18:30:00.000Z"
        assert _parse_when(sent, field="t", end_of_day=True) == _at(2026, 9, 7, 18, 30)

    def test_an_offset_is_honoured(self):
        """A date picked at IST resolves to the previous UTC evening -- the
        whole reason the UI sends instants rather than days."""
        assert (_parse_when("2026-09-07T00:00:00+05:30", field="f")
                == _at(2026, 9, 6, 18, 30))

    def test_a_naive_timestamp_is_read_as_utc(self):
        assert _parse_when("2026-09-06T10:00:00", field="f") == _at(2026, 9, 6, 10, 0)

    def test_blank_is_no_bound_rather_than_an_error(self):
        assert _parse_when("", field="f") is None
        assert _parse_when("   ", field="f") is None
        assert _parse_when(None, field="f") is None

    @pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "nonsense",
                                     "2026/09/06", "06-09-2026"])
    def test_garbage_is_rejected_rather_than_ignored(self, bad):
        """A range the server could not parse and silently dropped would show
        an UNFILTERED list wearing a filtered label -- the one failure mode an
        analyst cannot see."""
        with pytest.raises(ValidationError):
            _parse_when(bad, field="first_seen_from")
