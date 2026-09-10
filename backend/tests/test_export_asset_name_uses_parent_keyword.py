"""AssetName (Platform Format) and Original Name (Old Format) both name the
PARENT keyword a profile was found/validated under, not whatever the caller
typed as `target_name` -- because no current UI flow actually collects one.
"Analyse Validated Profiles" never sends it (see discovery.py's
`analyse_validated`) and the paste-URLs analysis form always sends an empty
string (see analysisApi.ts/quickAnalysisApi.ts's callers), so a column
sourced from `target_name` alone was permanently blank in practice.
`main_keyword` -- seeded from the validated profile's own `keywords[0]`
(see discovery.py's `_seed_from_doc`) -- is the thing that is actually known.

Both export layouts share one row-builder (`AnalysisRunner._build_rows`), so
proving this here covers the XLSX export and the Copy TSV button at once --
both just serialize `it.incident_row`/`it.legacy_row` as built here.
"""

from __future__ import annotations

import asyncio

from backend.analysis.runner import AnalysisItem, AnalysisJob, AnalysisRunner
from backend.shared.models.row import Row


def _populate(*, job_kwargs: dict | None = None, known: dict | None = None,
              row_kwargs: dict | None = None) -> AnalysisItem:
    runner = AnalysisRunner()
    item = AnalysisItem(id="i", raw_url="u", url="https://facebook.com/x",
                        platform="facebook", entity_id="x")
    row = Row(url="https://facebook.com/x", target="Gautam Adani", **(row_kwargs or {}))
    asyncio.run(runner._populate(AnalysisJob(id="j", **(job_kwargs or {})), item, row, known))
    return item


class TestAssetNameAndOriginalNamePreferTheParentKeyword:
    def test_validated_profile_uses_its_seeded_parent_keyword(self):
        """The ordinary "Analyse Validated Profiles" path: no target_name is
        sent, but `known["main_keyword"]` is -- discovery already resolved
        the parent this profile was found under."""
        item = _populate(known={"main_keyword": "Gautam Adani"})
        assert item.incident_row["AssetName"] == "Gautam Adani"
        assert item.legacy_row["Original Name"] == "Gautam Adani"

    def test_manually_typed_target_name_still_works_with_no_known_keyword(self):
        """A pasted-URL job with no discovery behind it, where a caller (the
        API directly, not today's UI) did supply target_name by hand."""
        item = _populate(job_kwargs={"target_name": "Adani Group"})
        assert item.incident_row["AssetName"] == "Adani Group"
        assert item.legacy_row["Original Name"] == "Adani Group"

    def test_seeded_parent_keyword_wins_over_a_typed_target_name(self):
        """Both are present: the parent keyword is the more specific, more
        current fact about THIS profile, so it wins over a job-wide typed
        name that may cover profiles found under several different parents."""
        item = _populate(
            job_kwargs={"target_name": "Adani Group"},
            known={"main_keyword": "Gautam Adani"},
        )
        assert item.incident_row["AssetName"] == "Gautam Adani"
        assert item.legacy_row["Original Name"] == "Gautam Adani"

    def test_pasted_url_with_nothing_known_falls_back_same_as_before(self):
        """No client, no keyword, no typed name: AssetName still falls back
        to the handle rather than a blank cell; Original Name -- which has
        no handle-like fallback to reach for -- stays blank, unchanged from
        before this fix."""
        item = _populate()
        assert item.incident_row["AssetName"] == "x"  # entity_id
        assert item.legacy_row["Original Name"] == ""

    def test_original_feed_is_untouched_by_this(self):
        """No keyword equivalent for a feed URL -- it stays exactly what the
        caller typed, blank included."""
        item = _populate(known={"main_keyword": "Gautam Adani"})
        assert item.legacy_row["Original feed"] == ""
        item = _populate(job_kwargs={"official_feed": "https://facebook.com/real"})
        assert item.legacy_row["Original feed"] == "https://facebook.com/real"
