"""Username (Name Yes/No) is unconditionally Yes in analysis, for every
platform, by explicit product decision -- it is never derived from the
fuzzy name-similarity score.

WHY. A profile only ever reaches analysis because a discovery sweep already
matched it to the client's own keywords -- that is what put it in front of
an analyst in the first place. `Row.name_yes` used to re-litigate that with
its own, stricter bar (`name_score >= NAME_THRESHOLD`, an order-insensitive
token-set ratio), and `compute_score`'s rubric puts a FLOOR under everything
else whenever that second opinion disagreed: a profile carrying the brand's
actual logo, matching location and a live post still scored the bare
minimum (2) if this one fuzzy check missed. `test_accuracy_regressions.py`
already has the fix for the case where the two visibly conflicted (a stored
row reading "Logo: Yes, Name: No, Risk: 2"); this is the deliberate next
step -- removing the second opinion entirely rather than tuning its bar.

Paired with `Row.logo_yes` (see test_facebook_generated_avatar.py and
test_logo_verdict_is_discoverys.py), the two Yes/No columns analysis
exports now follow one consistent policy:

    Logo (Yes/No)      Yes ONLY when a real, account-chosen picture was
                       confirmed -- the one column that must still say No,
                       because a stock avatar or a platform-drawn letter
                       tile is not evidence of impersonation.
    Name (Yes/No)      Yes, always -- the row would not be in analysis at
                       all if discovery had not already matched it.

NOT REMOVED: `name_score` and `name_exact_run` are still computed and
stored. They drive discovery's own High/Medium/Low match-level filter
(profile_repository.py's `_build_query`) and remain visible to an analyst;
only `name_yes`'s OWN gate on them is gone. An analyst who looks at one
specific profile and disagrees can still turn its Name column to "No" by
hand -- that goes through `username_match`/`resolve_match`, which is
untouched by this file and still wins outright over any default (see
scoring.py's `resolve_match` docstring: "an analyst's explicit call wins
outright, either way").
"""

from __future__ import annotations

import asyncio

import pytest

from backend.analysis.runner import AnalysisItem, AnalysisJob, AnalysisRunner
from backend.shared.models.row import Row
from backend.shared.models.scoring import NAME_THRESHOLD


def _populate(*, row_kwargs: dict, known: dict | None = None,
             platform: str = "facebook") -> AnalysisItem:
    runner = AnalysisRunner()
    item = AnalysisItem(id="i", raw_url="u", url=f"https://{platform}.com/x",
                        platform=platform, entity_id="x")
    row = Row(url=f"https://{platform}.com/x", target="acme", **row_kwargs)
    asyncio.run(runner._populate(AnalysisJob(id="j"), item, row, known))
    return item


class TestRowNameYesIsAlwaysYes:
    def test_a_strong_match_is_yes(self):
        assert Row(url="u", target="Acme", profile_name="Acme Official").name_yes == "Yes"

    def test_a_total_mismatch_is_still_yes(self):
        """The case that used to be 'No': a name with nothing in common with
        the target, scored 0 by the fuzzy matcher."""
        row = Row(url="u", target="Acme Corp", profile_name="zzz totally unrelated")
        assert row.name_score < NAME_THRESHOLD
        assert row.name_yes == "Yes"

    def test_no_profile_name_read_at_all_is_yes(self):
        assert Row(url="u", target="Acme").name_yes == "Yes"

    def test_a_name_score_of_zero_is_yes(self):
        row = Row(url="u", target="Acme", profile_name="Acme", name_score=0)
        assert row.name_yes == "Yes"

    @pytest.mark.parametrize("platform", [
        "facebook", "instagram", "twitter", "youtube", "telegram", "tiktok",
    ])
    def test_every_platform_agrees(self, platform):
        """`name_yes` reads no platform-specific field -- proven directly
        rather than assumed, the same way the per-platform logo rules in
        shared/avatars.py are each proven on their own evidence."""
        row = Row(url=f"https://{platform}.com/x", target="Acme",
                  profile_name="zzz unrelated garbage", name_score=0)
        assert row.name_yes == "Yes"


class TestItReachesTheAnalysisExport:
    def test_has_name_match_is_true_from_a_bare_visit(self):
        item = _populate(row_kwargs=dict(profile_name="zzz unrelated"))
        assert item.has_name_match is True

    def test_has_name_match_is_true_even_with_a_low_restored_score(self):
        """The `known` merge path (test_accuracy_regressions.py's
        TestRiskScoreUsesRestoredFields.test_username_is_always_yes_
        regardless_of_score covers the score/risk side of this same case)."""
        item = _populate(row_kwargs=dict(profile_name="Acme"),
                         known={"name_score": 5})
        assert item.has_name_match is True
        assert item.name_score == 5   # restored for display, not used as a gate


class TestTheRubricNoLongerFloorsOnName:
    """`compute_score` returns BASE (2) only when `has_name_match` is False.
    Since that can no longer happen from a fresh analysis visit, the
    practical floor for anything analysis actually produces is BASE+W_NAME
    (3), not BASE."""

    def test_logo_alone_no_longer_needs_a_name_match_to_score_above_floor(self):
        item = _populate(row_kwargs=dict(has_custom_pic=True, profile_name="zzz unrelated"))
        assert item.risk_score > 2

    def test_nothing_else_present_still_lands_on_the_new_floor_not_the_old_one(self):
        item = _populate(row_kwargs=dict(profile_name="zzz totally unrelated garbage"))
        assert item.risk_score == 3   # BASE + W_NAME, not BASE
