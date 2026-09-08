"""Analysis inherits discovery's logo verdict instead of re-deriving it.

WHY THIS IS THE RIGHT SOURCE, and it is not merely a preference. `known` is
populated only for a profile that arrived through
POST /discovery/profiles/analyse -- that is, one an analyst looked at on a
discovery card and validated by hand. The picture on that card is the thing
they judged, and by the time it reaches analysis a human has already stood
behind it.

Analysis re-deriving the same verdict from a second visit is not a free
double-check, because the two visits do not see the same thing. Facebook
substitutes THE VIEWER'S OWN photo into a privacy-restricted profile's
picture fields (facebook/analysis_engine.py's PAGE_CONTEXT_PICTURE_KEYS
documents this), so the profile-page visit can report a picture that has
nothing to do with the account. When the two disagree, analysis is the one
more likely to be wrong.

WHAT MAKES THE INHERITED VALUE TRUSTWORTHY. Discovery decides from the URL,
which cannot see a platform-DRAWN avatar. That gap is closed where the bytes
already exist -- services/avatar_cache.py, which downloads and decodes every
discovered avatar behind the sweep, and writes a corrected `has_logo` back
through `set_has_logo` (see test_facebook_generated_avatar.py). So the
record this reads has already been corrected by the pixels; inheriting it is
inheriting the best answer in the system, not the earliest one.

THE ORDERING MATTERS TOO. `has_logo` is the heaviest single input to the
risk rubric, and `_populate` computes risk AFTER this merge -- so a verdict
restored here has to reach the score, not just the exported row.
"""

from __future__ import annotations

import asyncio

from backend.analysis.runner import AnalysisItem, AnalysisJob, AnalysisRunner
from backend.shared.models.row import Row


def _populate(*, row_kwargs: dict, known: dict | None) -> AnalysisItem:
    runner = AnalysisRunner()
    item = AnalysisItem(id="i", raw_url="u", url="https://facebook.com/x",
                        platform="facebook", entity_id="x")
    row = Row(url="https://facebook.com/x", target="", status="OK",
              profile_name="Acme", **row_kwargs)
    asyncio.run(runner._populate(AnalysisJob(id="j"), item, row, known))
    return item


class TestDiscoveryWins:
    def test_a_discovery_no_overrides_an_analysis_yes(self):
        """The case the whole change exists for: the sweep (corrected by the
        avatar bytes) said this is a drawn letter avatar; the profile visit
        found some picture URL and concluded Yes. Discovery's No stands."""
        item = _populate(row_kwargs=dict(has_custom_pic=True),
                         known={"has_logo": False})
        assert item.has_logo is False

    def test_a_discovery_yes_overrides_an_analysis_no(self):
        """It wins in BOTH directions -- this is inheritance, not a
        one-way veto. A visit that simply failed to find the picture must not
        erase a logo the analyst saw on the card."""
        item = _populate(row_kwargs=dict(has_custom_pic=False),
                         known={"has_logo": True})
        assert item.has_logo is True

    def test_it_also_fills_a_verdict_analysis_never_reached(self):
        """The original behaviour, still intact: an unknown is filled."""
        item = _populate(row_kwargs={}, known={"has_logo": True})
        assert item.has_logo is True


class TestWithoutDiscoveryAnalysisStillDecides:
    """A manually pasted URL has no seed record, so nothing is inherited and
    the visit's own reading is all there is."""

    def test_no_known_record_leaves_the_visit_in_charge(self):
        assert _populate(row_kwargs=dict(has_custom_pic=True), known=None).has_logo is True
        assert _populate(row_kwargs=dict(has_custom_pic=False), known=None).has_logo is False

    def test_a_known_record_without_a_verdict_changes_nothing(self):
        """`has_logo` absent, or explicitly unknown, must not be read as No --
        that is the tri-state rule this codebase relies on everywhere."""
        assert _populate(row_kwargs=dict(has_custom_pic=True), known={}).has_logo is True
        assert _populate(row_kwargs=dict(has_custom_pic=True),
                         known={"has_logo": None}).has_logo is True


class TestTheInheritedVerdictReachesTheScore:
    """`has_logo` alone forces High, so a verdict that lands in the exported
    row but not in the risk computation would be worse than not inheriting
    at all -- the row and its score would disagree."""

    def test_the_row_object_is_corrected_too_not_just_the_item(self):
        runner = AnalysisRunner()
        item = AnalysisItem(id="i", raw_url="u", url="https://facebook.com/x",
                            platform="facebook", entity_id="x")
        row = Row(url="https://facebook.com/x", target="", status="OK",
                  profile_name="Acme", has_custom_pic=True)
        asyncio.run(runner._populate(AnalysisJob(id="j"), item, row,
                                     {"has_logo": False}))
        assert row.has_custom_pic is False
        assert row.logo_yes == "No"
