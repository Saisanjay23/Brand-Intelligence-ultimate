"""AssetName (Platform Format) names the PARENT keyword a profile was
found/validated under, while Original Name (Old Format) carries the platform
name (e.g. "Facebook"), with Facebook on row 1 and all Facebook URLs grouped
together beside it.

Both export layouts share one row-builder (`AnalysisRunner._build_rows`), so
proving this here covers the XLSX export and the Copy TSV button at once --
both just serialize `it.incident_row`/`it.legacy_row` as built here.
"""

from __future__ import annotations

import asyncio

from backend.analysis.runner import AnalysisItem, AnalysisJob, AnalysisRunner
from backend.shared.models.row import Row


def _populate(*, platform: str = "facebook", job_kwargs: dict | None = None,
              known: dict | None = None, row_kwargs: dict | None = None) -> AnalysisItem:
    runner = AnalysisRunner()
    item = AnalysisItem(id="i", raw_url="u", url=f"https://{platform}.com/x",
                        platform=platform, entity_id="x")
    row = Row(url=f"https://{platform}.com/x", target="Gautam Adani", **(row_kwargs or {}))
    asyncio.run(runner._populate(AnalysisJob(id="j", **(job_kwargs or {})), item, row, known))
    return item


class TestAssetNameUsesParentKeywordAndOriginalNameUsesPlatformName:
    def test_validated_profile_uses_its_seeded_parent_keyword_for_asset_name(self):
        """AssetName gets the parent keyword, while Original Name gets the
        platform display name."""
        item = _populate(known={"main_keyword": "Gautam Adani"})
        assert item.incident_row["AssetName"] == "Gautam Adani"
        assert item.legacy_row["Original Name"] == "Facebook"

    def test_manually_typed_target_name_still_works_with_no_known_keyword(self):
        """A pasted-URL job with no discovery behind it, where a caller (the
        API directly, not today's UI) did supply target_name by hand."""
        item = _populate(job_kwargs={"target_name": "Adani Group"})
        assert item.incident_row["AssetName"] == "Adani Group"
        assert item.legacy_row["Original Name"] == "Facebook"

    def test_seeded_parent_keyword_wins_over_a_typed_target_name(self):
        """Both are present: the parent keyword is the more specific, more
        current fact about THIS profile, so it wins over a job-wide typed
        name for AssetName."""
        item = _populate(
            job_kwargs={"target_name": "Adani Group"},
            known={"main_keyword": "Gautam Adani"},
        )
        assert item.incident_row["AssetName"] == "Gautam Adani"
        assert item.legacy_row["Original Name"] == "Facebook"

    def test_pasted_url_with_nothing_known_is_blank_asset_name_and_platform_for_original_name(self):
        """No client, no keyword, no typed name: AssetName stays blank,
        while Original Name has the platform name."""
        item = _populate()
        assert item.incident_row["AssetName"] == ""
        assert item.legacy_row["Original Name"] == "Facebook"

    def test_different_platforms_output_their_platform_name_under_original_name(self):
        """Every platform gets its human-readable platform name under Original Name."""
        fb = _populate(platform="facebook")
        assert fb.legacy_row["Original Name"] == "Facebook"

        ig = _populate(platform="instagram")
        assert ig.legacy_row["Original Name"] == "Instagram"

        tw = _populate(platform="twitter")
        assert tw.legacy_row["Original Name"] == "Twitter"

        yt = _populate(platform="youtube")
        assert yt.legacy_row["Original Name"] == "YouTube"

    def test_original_feed_is_untouched_by_this(self):
        """No keyword equivalent for a feed URL -- it stays exactly what the
        caller typed, blank included."""
        item = _populate(known={"main_keyword": "Gautam Adani"})
        assert item.legacy_row["Original feed"] == ""
        item = _populate(job_kwargs={"official_feed": "https://facebook.com/real"})
        assert item.legacy_row["Original feed"] == "https://facebook.com/real"

    def test_start_orders_items_with_facebook_on_row_1(self):
        """Urls entered in arbitrary/mixed platform order are sorted with
        Facebook on row 1, followed by Instagram, Twitter, etc., grouping all
        URLs for each platform together."""
        runner = AnalysisRunner()
        urls = [
            "https://twitter.com/user1",
            "https://www.instagram.com/user2",
            "https://www.facebook.com/user3",
            "https://www.facebook.com/user4",
            "https://www.youtube.com/@channel5",
        ]
        job, skipped = asyncio.run(runner.start(urls))
        platforms = [it.platform for it in job.items]
        # Facebook items first (row 1 and 2), followed by Instagram, Twitter, YouTube
        assert platforms == ["facebook", "facebook", "instagram", "twitter", "youtube"]

