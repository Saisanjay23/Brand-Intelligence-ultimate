"""Splitting the Validated tab into recently-validated and older.

PURE LOGIC ONLY -- these exercise the query clause the split is built on,
not Mongo (see this suite's scope note).

WHAT THIS SPLIT IS, AND WHAT IT IS NOT. The grid already had a New/Old
split, on `first_seen`: how long ago the SWEEP FOUND the profile. This one
is on `validated_at`: how long ago the ANALYST DECIDED. They are genuinely
different questions -- a profile discovered in March and validated this
morning is old by discovery and new by decision -- and the Validated tab is
about the decision.

THE DEFECT THIS GUARDS. Every profile validated before `validated_at`
existed has no such field. If a missing timestamp counted as "recent", the
entire historical backlog would land in New Validated on the day this
ships, which is exactly the tab that is supposed to mean "decisions I just
made". Absent means old, and the badge count has to agree with the list --
they are computed by two different mechanisms (a query clause and an
aggregation `$cond`), so they can disagree, and a badge that disagrees with
its own tab is worse than no badge.
"""

from datetime import datetime, timedelta, timezone

from backend.database.repositories.profile_repository import (
    NEW_WINDOW_HOURS,
    _age_clause,
    _validated_age_clause,
)


def _matches(clause: dict, doc: dict) -> bool:
    """Evaluate the small subset of Mongo this clause uses, so the rule can
    be tested without a database. Supports `$gte`/`$lt`/`$exists`/None on one
    field, a top-level `$or` of those, and a top-level `$and` of those."""
    if "$or" in clause:
        return any(_matches(c, doc) for c in clause["$or"])
    # `_age_clause("old")` is an $and of two $or legs since first_seen stopped
    # being the only thing that makes a profile new (an avatar swap does too).
    # Without this the helper walked into the list and raised AttributeError,
    # which reads as a broken rule rather than a helper that had not kept up.
    if "$and" in clause:
        return all(_matches(c, doc) for c in clause["$and"])
    if "$and" in clause:
        return all(_matches(c, doc) for c in clause["$and"])
    for field, cond in clause.items():
        value = doc.get(field, None)
        if cond is None:
            if value is not None:
                return False
            continue
        for op, operand in cond.items():
            if op == "$exists":
                if (field in doc) != operand:
                    return False
            elif op == "$gte":
                # Mongo: a comparison against a missing/None field is false.
                if value is None or not value >= operand:
                    return False
            elif op == "$lt":
                if value is None or not value < operand:
                    return False
            else:
                raise AssertionError(f"unhandled operator {op}")
    return True


def _ago(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


class TestTheBoundary:
    def test_validated_an_hour_ago_is_new(self):
        doc = {"validated_at": _ago(1)}
        assert _matches(_validated_age_clause("new"), doc)
        assert not _matches(_validated_age_clause("old"), doc)

    def test_validated_two_days_ago_is_old(self):
        doc = {"validated_at": _ago(48)}
        assert _matches(_validated_age_clause("old"), doc)
        assert not _matches(_validated_age_clause("new"), doc)

    def test_the_boundary_is_twenty_four_hours(self):
        assert NEW_WINDOW_HOURS == 24
        just_inside = {"validated_at": _ago(NEW_WINDOW_HOURS - 0.5)}
        just_outside = {"validated_at": _ago(NEW_WINDOW_HOURS + 0.5)}
        assert _matches(_validated_age_clause("new"), just_inside)
        assert _matches(_validated_age_clause("old"), just_outside)


class TestTheBacklog:
    """Everything validated before this field existed."""

    def test_a_missing_timestamp_is_old_not_new(self):
        doc = {"status": "approved"}
        assert _matches(_validated_age_clause("old"), doc)
        assert not _matches(_validated_age_clause("new"), doc)

    def test_an_explicitly_null_timestamp_is_old(self):
        doc = {"validated_at": None}
        assert _matches(_validated_age_clause("old"), doc)
        assert not _matches(_validated_age_clause("new"), doc)


class TestEveryRowLandsInExactlyOneTab:
    """A row in neither tab is invisible; a row in both is double-counted
    against a total that came from a separate query."""

    def test_partition_is_total_and_disjoint(self):
        docs = [
            {"validated_at": _ago(0)},
            {"validated_at": _ago(1)},
            {"validated_at": _ago(23.9)},
            {"validated_at": _ago(24.1)},
            {"validated_at": _ago(500)},
            {"validated_at": None},
            {},
        ]
        for doc in docs:
            in_new = _matches(_validated_age_clause("new"), doc)
            in_old = _matches(_validated_age_clause("old"), doc)
            assert in_new != in_old, doc


class TestItIsNotTheDiscoveryAgeSplit:
    def test_discovered_long_ago_but_validated_today_is_NEW_validated(self):
        """The case the whole feature exists for: an old profile the analyst
        has only just decided on."""
        doc = {"first_seen": _ago(24 * 90), "validated_at": _ago(2)}
        assert _matches(_validated_age_clause("new"), doc)
        assert _matches(_age_clause("old"), doc)      # old by discovery

    def test_discovered_today_but_validated_long_ago_is_impossible_but_safe(self):
        """Not reachable in practice, but the two clauses must stay
        independent rather than one quietly reading the other's field."""
        doc = {"first_seen": _ago(1), "validated_at": _ago(48)}
        assert _matches(_validated_age_clause("old"), doc)
        assert _matches(_age_clause("new"), doc)

    def test_a_row_with_no_validated_at_still_splits_on_first_seen(self):
        doc = {"first_seen": _ago(1)}
        assert _matches(_age_clause("new"), doc)
        assert _matches(_validated_age_clause("old"), doc)


class TestAvatarChangedAtPromotesToNew:
    """A profile discovered long ago that changes its picture within the 24h
    window must land in the New tab, not Old -- the whole point of the
    avatar_changed_at feature."""

    def test_old_discovery_recent_dp_change_is_new(self):
        doc = {"first_seen": _ago(24 * 30), "avatar_changed_at": _ago(2)}
        assert _matches(_age_clause("new"), doc)
        assert not _matches(_age_clause("old"), doc)

    def test_old_discovery_old_dp_change_is_old(self):
        doc = {"first_seen": _ago(24 * 30), "avatar_changed_at": _ago(48)}
        assert _matches(_age_clause("old"), doc)
        assert not _matches(_age_clause("new"), doc)

    def test_new_discovery_no_dp_change_is_still_new(self):
        doc = {"first_seen": _ago(1)}
        assert _matches(_age_clause("new"), doc)

    def test_no_first_seen_recent_dp_change_is_new(self):
        doc = {"avatar_changed_at": _ago(1)}
        assert _matches(_age_clause("new"), doc)
        assert not _matches(_age_clause("old"), doc)

    def test_no_first_seen_no_dp_change_is_old(self):
        doc = {}
        assert _matches(_age_clause("old"), doc)
        assert not _matches(_age_clause("new"), doc)

    def test_avatar_changed_boundary(self):
        just_inside = {"first_seen": _ago(24 * 90),
                       "avatar_changed_at": _ago(NEW_WINDOW_HOURS - 0.5)}
        just_outside = {"first_seen": _ago(24 * 90),
                        "avatar_changed_at": _ago(NEW_WINDOW_HOURS + 0.5)}
        assert _matches(_age_clause("new"), just_inside)
        assert _matches(_age_clause("old"), just_outside)

    def test_partition_with_avatar_changed_at(self):
        """Every document must land in exactly one of the two buckets."""
        docs = [
            {"first_seen": _ago(0)},
            {"first_seen": _ago(1), "avatar_changed_at": _ago(0.5)},
            {"first_seen": _ago(24 * 90), "avatar_changed_at": _ago(1)},
            {"first_seen": _ago(24 * 90), "avatar_changed_at": _ago(48)},
            {"first_seen": _ago(24 * 90)},
            {"avatar_changed_at": _ago(1)},
            {"avatar_changed_at": _ago(48)},
            {},
            {"first_seen": None},
            {"first_seen": None, "avatar_changed_at": None},
        ]
        for doc in docs:
            in_new = _matches(_age_clause("new"), doc)
            in_old = _matches(_age_clause("old"), doc)
            assert in_new != in_old, f"partition violated for {doc}"
