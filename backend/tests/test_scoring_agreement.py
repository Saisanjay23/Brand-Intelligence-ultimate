"""The scoring rubric, and the two implementations of it agreeing.

WHY THIS FILE EXISTS
    There are two paths to a risk score and priority for the same profile:

        a fresh scrape      shared/models/row.py::Row.risk / Row.priority
        a stored document   profile_repository.py::compute_risk_score /
                            compute_priority

    They must produce identical verdicts from identical facts, or the same
    profile changes its rating depending on which code path last touched it
    -- silently, with nothing raising. That exact bug shipped:
    `compute_priority` used to take `has_name_match` and return "High" on
    any name match, skipping the activity/score gate `Row.priority` applies,
    so a long-dormant profile was High in the database and Low on rescrape.

    Every test here is a regression test for a defect that was actually
    found in this codebase, not a hypothetical.
"""

from __future__ import annotations

import pytest

from backend.database.repositories.profile_repository import (
    compute_priority,
    compute_risk_score,
)
from backend.shared.models.row import Row
from backend.shared.models.scoring import compute_score, resolve_match


def _row(**kw) -> Row:
    """A Row carrying only the fields the scoring properties read."""
    row = Row(url=kw.pop("url", "https://example.com/x"), target=kw.pop("target", "acme"))
    for k, v in kw.items():
        setattr(row, k, v)
    return row


# --------------------------------------------------------------- resolve_match

class TestResolveMatch:
    """`validated` means an analyst approved the profile, which implies the
    match unless they explicitly said otherwise. Getting this backwards is
    what made an early version of the priority test report false mismatches."""

    def test_raw_signal_used_when_no_analyst_input(self):
        assert resolve_match(True, None, False) is True
        assert resolve_match(False, None, False) is False

    def test_explicit_analyst_call_overrides_the_scrape(self):
        assert resolve_match(True, False, False) is False
        assert resolve_match(False, True, False) is True

    def test_validated_implies_a_match_when_analyst_gave_no_override(self):
        # the subtlety: approving a profile without touching the individual
        # flags means the analyst agreed with it being a match
        assert resolve_match(False, None, True) is True

    def test_explicit_no_beats_validated(self):
        # an analyst who validated the profile but explicitly cleared THIS
        # signal meant to clear it
        assert resolve_match(True, False, True) is False


# ------------------------------------------------------------- the rubric

class TestComputeScore:
    def test_score_is_bounded_and_monotonic_in_signals(self):
        nothing = compute_score(False, False, False, "")
        everything = compute_score(True, True, True, "2099-01-01")
        assert 0 <= nothing <= everything
        # adding a signal must never LOWER the score
        assert compute_score(True, False, False, "") >= nothing
        assert compute_score(False, True, False, "") >= nothing

    def test_dormancy_lowers_the_score_relative_to_activity(self):
        active = compute_score(True, True, True, "2099-01-01")
        dormant = compute_score(True, True, True, "2001-01-01")
        assert dormant < active, "an ancient last-post must not score like a live one"

    def test_blank_last_post_does_not_crash_or_count_as_active(self):
        blank = compute_score(True, True, True, "")
        active = compute_score(True, True, True, "2099-01-01")
        assert blank <= active


# --------------------------------- the two implementations must not diverge

class TestStoredAndFreshAgree:
    """THE regression test for the compute_priority defect.

    Each case is scored both ways -- through a live `Row` and through the
    repository's document-side functions -- and the two must return the same
    risk AND the same priority.
    """

    CASES = [
        # (label, has_logo, has_name_match, location, last_post)
        ("strong match, active",   True,  True,  "Mumbai", "2099-01-01"),
        ("strong match, dormant",  True,  True,  "Mumbai", "2001-01-01"),
        ("name only, active",      False, True,  "",       "2099-01-01"),
        ("name only, dormant",     False, True,  "",       "2001-01-01"),
        ("logo only",              True,  False, "",       ""),
        ("nothing",                False, False, "",       ""),
        ("location only",          False, False, "Delhi",  ""),
    ]

    @pytest.mark.parametrize("label,logo,name,loc,post", CASES)
    def test_risk_score_matches(self, label, logo, name, loc, post):
        row = _row(
            profile_name="Acme Official" if name else "zzz unrelated",
            target="Acme Official" if name else "acme",
            has_custom_pic=logo, location=loc, last_post_iso=post,
        )
        stored = compute_risk_score(
            has_logo=(row.logo_yes == "Yes"),
            has_name_match=(row.name_yes == "Yes"),
            location=loc, last_post_date=post,
        )
        assert row.risk == stored, f"{label}: fresh={row.risk} stored={stored}"

    @pytest.mark.parametrize("label,logo,name,loc,post", CASES)
    def test_priority_matches(self, label, logo, name, loc, post):
        row = _row(
            profile_name="Acme Official" if name else "zzz unrelated",
            target="Acme Official" if name else "acme",
            has_custom_pic=logo, location=loc, last_post_iso=post,
        )
        stored = compute_priority(
            has_logo=(row.logo_yes == "Yes"),
            risk_score=row.risk,
        )
        assert row.priority == stored, f"{label}: fresh={row.priority} stored={stored}"

    def test_logo_forces_high_regardless_of_score(self):
        """Both implementations define priority as photo-driven: a resolved
        logo match is High even when the score sits at its floor."""
        row = _row(profile_name="zzz", target="acme", has_custom_pic=True)
        assert row.priority == "High"
        assert compute_priority(has_logo=True, risk_score=row.risk) == "High"

    def test_analyst_undoing_a_logo_match_can_drop_priority(self):
        """The reason compute_priority takes logo_match at all: an analyst
        clearing the logo has to be able to lower the rating, or the stored
        verdict outlives the correction."""
        high = compute_priority(has_logo=True, risk_score=2)
        undone = compute_priority(has_logo=True, risk_score=2, logo_match=False)
        assert high == "High"
        assert undone == "Low", "clearing the logo match must be able to drop priority"
