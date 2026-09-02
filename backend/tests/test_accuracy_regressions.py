"""Regressions for six accuracy bugs, each of which was silent -- every one
of them produced a confident, plausible-looking wrong answer rather than an
error, which is why none had been noticed.

    1. Every discovered profile stored the platform's internal id in its
       `username` column, so the UI rendered "@50840430092" for a profile
       whose handle is "@defnce.app".
    2. Picking a keyword AND a keyword category in the discovery grid
       returned MORE rows than the keyword alone -- the second filter
       overwrote the first in the query dict.
    3. An analysed profile carrying the brand's logo, name, location and a
       recent post scored 2, the "no name match" FLOOR, instead of 9.
    4. A follower count rendered "1.234.567" (the format Facebook serves to
       a large share of locales) raised an uncaught ValueError.
    5. The New/Old split was computed in the browser over one capped fetch,
       so on a client with 1635 pending profiles 635 of them were unreachable
       in either tab while the badges presented the visible 1000 as the whole
       set.
    6. The server's "High Match" meant `name_score >= 80` while the grid's
       meant `name_exact_run` -- two different answers to the same question,
       disagreeing on 31 of 1653 live rows.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.analysis.runner import AnalysisItem, AnalysisJob, AnalysisRunner
from backend.database.repositories.profile_repository import _build_query, _without
from backend.discovery.runner import row_to_fields
from backend.shared.models.hit import Hit, hit_to_row
from backend.shared.models.row import Row
from backend.shared.models.scoring import MEDIUM_MATCH_THRESHOLD, compute_score
from backend.shared.text import handle_from_url, parse_count


class TestHandleFromUrl:
    """The handle a profile URL carries, or "" when it carries none."""

    @pytest.mark.parametrize("url,expected", [
        ("https://www.instagram.com/defnce.app/", "defnce.app"),
        ("https://x.com/CyfirmaDev", "CyfirmaDev"),
        ("https://t.me/mr_hari_hari", "mr_hari_hari"),
        ("https://www.facebook.com/llaudreyisabelcc", "llaudreyisabelcc"),
        ("https://www.tiktok.com/@someone", "someone"),
        ("https://www.youtube.com/@realchannel", "realchannel"),
        ("https://www.youtube.com/user/LegacyName", "LegacyName"),
    ])
    def test_extracts_the_handle(self, url, expected):
        assert handle_from_url(url) == expected

    @pytest.mark.parametrize("url", [
        "https://www.facebook.com/profile.php?id=61580047726142",  # id, not a handle
        "https://t.me/c/8925111777",                               # internal id
        "https://www.youtube.com/channel/UCtL-QQINXbfU885mGNsHb_g",
        "https://www.facebook.com/groups/123456",
        "",
    ])
    def test_returns_blank_when_the_url_has_no_handle(self, url):
        """Blank, so the caller falls back to the id rather than storing a
        path segment that is not a handle."""
        assert handle_from_url(url) == ""


class TestDiscoveryStoresTheHandle:
    """`username` must be the public handle, NOT `profile_id`."""

    def test_platform_supplied_handle_wins(self):
        row = Row(url="https://www.instagram.com/defnce.app/", target="defnce",
                  profile_id="50840430092", username="defnce.app", profile_name="Defnce")
        fields = row_to_fields(row, "defnce")
        assert fields["username"] == "defnce.app"
        assert fields["entity_id"] == "50840430092"

    def test_hit_based_platforms_recover_it_from_the_url(self):
        """Facebook and YouTube go through `Hit`, which carries no handle."""
        row = hit_to_row(Hit(entity_id="61579051876339", name="Audrey",
                             url="https://www.facebook.com/llaudreyisabelcc"))
        assert row_to_fields(row, "audrey")["username"] == "llaudreyisabelcc"

    def test_falls_back_to_the_id_when_there_is_no_handle(self):
        row = hit_to_row(Hit(entity_id="61580047726142", name="No Vanity",
                             url="https://www.facebook.com/profile.php?id=61580047726142"))
        assert row_to_fields(row, "kw")["username"] == "61580047726142"


class TestKeywordFilterCombines:
    """A second filter must NARROW the result set, never replace the first."""

    KEYWORDS = {"name_keywords": ["Adani", "Gautam Adani"], "domain_keywords": []}

    def test_keyword_survives_alongside_the_category_filter(self):
        q = _build_query("c1", keyword="Adani",
                         keyword_match_type="individual", client_keywords=self.KEYWORDS)
        # both clauses present, AND-ed -- neither overwrites the other
        clauses = q.get("$and", [])
        assert {"keywords": "Adani"} in clauses
        assert any("$in" in (c.get("keywords") or {}) for c in clauses if isinstance(c.get("keywords"), dict))

    def test_keyword_alone_still_filters(self):
        q = _build_query("c1", keyword="Adani")
        assert {"keywords": "Adani"} in q.get("$and", [])

    def test_an_empty_category_matches_nothing(self):
        """Not 'everything' -- a configured-but-empty list is a real
        constraint, and degrading it to no filter silently widens the view."""
        q = _build_query("c1", keyword_match_type="domain",
                         client_keywords={"name_keywords": [], "domain_keywords": []})
        assert {"keywords": {"$in": [None]}} in q.get("$and", [])


class TestRiskScoreUsesRestoredFields:
    """Risk is DERIVED, so it must be computed after the `known` merge fills
    whatever the visit itself came back without."""

    @staticmethod
    def _populate(row_kwargs, known):
        runner = AnalysisRunner()
        item = AnalysisItem(id="i", raw_url="u", url="https://x.com/f",
                            platform="twitter", entity_id="f")
        row = Row(url="https://x.com/f", target="", status="OK",
                  profile_name="CYFIRMA Official", **row_kwargs)
        asyncio.run(runner._populate(AnalysisJob(id="j"), item, row, known))
        return item

    def test_the_worst_offender_scores_the_maximum(self):
        """Brand logo + matching name + location + a recent post. This is the
        exact shape that scored 2 (the floor) before the fix."""
        item = self._populate(
            dict(has_custom_pic=True, location="Singapore", last_post_iso="2026-08-20"),
            {"name_score": 100},
        )
        assert item.name_score == 100
        assert item.has_name_match is True
        assert item.risk_score == 9
        assert item.priority == "High"

    @pytest.mark.parametrize("logo,location,last_post,expected", [
        (True,  "Singapore", "2026-08-20", 9),
        (True,  "",          "2026-08-20", 8),
        (True,  "Singapore", "2020-01-01", 7),
        (True,  "",          "",           6),
        (False, "",          "2026-08-20", 5),
        (False, "",          "2020-01-01", 4),
        (False, "",          "",           3),
    ])
    def test_every_tier_of_the_cascade(self, logo, location, last_post, expected):
        item = self._populate(
            dict(has_custom_pic=logo, location=location, last_post_iso=last_post),
            {"name_score": 100},
        )
        assert item.risk_score == expected
        assert item.risk_score == compute_score(
            has_logo=logo, has_name_match=True,
            has_location=bool(location), last_post_iso=last_post,
        )

    def test_a_real_non_match_still_sits_on_the_floor(self):
        """The fix must not inflate rows that genuinely do not match."""
        item = self._populate(
            dict(has_custom_pic=False, location="", last_post_iso=""),
            {"name_score": 10},
        )
        assert item.risk_score == 2


class TestParseCountNeverRaises:
    """Every caller feeds it raw scraped text, so it has to be total."""

    @pytest.mark.parametrize("raw,expected", [
        ("1.2K", (1200, False)),
        ("1,234", (1234, True)),
        ("154M", (154_000_000, False)),
        ("1,234,567", (1_234_567, True)),
        ("500", (500, True)),
    ])
    def test_the_formats_it_always_read(self, raw, expected):
        assert parse_count(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("1.234.567", 1_234_567),
        ("12.345.678", 12_345_678),
        ("1.234", 1234),
    ])
    def test_dotted_thousands_are_read_not_merely_survived(self, raw, expected):
        """"1.234.567" is what Facebook serves in German/Spanish/Portuguese/
        Indonesian. It used to raise; returning None would lose a real count."""
        assert parse_count(raw) == (expected, True)

    @pytest.mark.parametrize("raw", ["...", ".", "..2", "1.2.3", "1.2.3K", "", "abc"])
    def test_unparseable_input_returns_none_rather_than_raising(self, raw):
        assert parse_count(raw) == (None, False)


class TestAgeFilterPartitions:
    """New/Old is a server filter, so the split survives pagination."""

    def test_the_two_buckets_are_exclusive_and_exhaustive(self):
        """Every row lands in exactly one bucket -- no row can hide between
        them, which is the whole point of moving this off the client."""
        new_q = _build_query("c1", status="pending", age="new")
        old_q = _build_query("c1", status="pending", age="old")
        new_clause = next(c for c in new_q["$and"] if "first_seen" in c)
        old_clause = next(c for c in old_q["$and"] if "$or" in c)
        assert "$gte" in new_clause["first_seen"]
        # "old" must also claim rows with NO first_seen, matching the
        # frontend's isProfileNew, which reads a missing timestamp as not-new
        branches = old_clause["$or"]
        assert {"first_seen": None} in branches
        assert {"first_seen": {"$exists": False}} in branches

    def test_no_age_filter_leaves_first_seen_alone(self):
        q = _build_query("c1", status="pending")
        assert not any("first_seen" in c for c in q.get("$and", []))


class TestMatchLevelMatchesTheCardBadge:
    """The server's bands must be the grid's bands (matchLevelOf), which gate
    High on a contiguous letter-run and NOT on a score threshold."""

    @staticmethod
    def _clause(level):
        q = _build_query("c1", match_level=level)
        return next(c for c in q["$and"] if "name_exact_run" in c)

    def test_high_is_the_exact_run_not_a_score(self):
        assert self._clause("high") == {"name_exact_run": True}

    def test_medium_excludes_high_and_bands_on_the_score(self):
        c = self._clause("medium")
        assert c["name_exact_run"] == {"$ne": True}
        assert c["name_score"]["$gte"] == MEDIUM_MATCH_THRESHOLD

    def test_low_excludes_high_and_takes_missing_scores(self):
        """matchLevelOf returns "low" for a null score, so the query must
        too -- otherwise those rows belong to no band at all."""
        c = self._clause("low")
        assert c["name_exact_run"] == {"$ne": True}
        assert {"name_score": None} in c["$or"]
        assert {"name_score": {"$exists": False}} in c["$or"]


class TestFacetsDropOnlyTheirOwnFilter:
    """A facet count answers "how many for each value of THIS field, with the
    other filters still applied", so it must drop its own filter wherever it
    lives -- including out of `$and`, where the keyword filter now sits."""

    KEYWORDS = {"name_keywords": ["Adani"], "domain_keywords": []}

    def test_keyword_facet_drops_the_keyword_filter(self):
        q = _build_query("c1", keyword="Adani", platform="facebook", status="pending")
        stripped = _without(q, "keywords")
        assert not any("keywords" in c for c in stripped.get("$and", []))
        assert "keywords" not in stripped

    def test_but_keeps_every_other_filter(self):
        q = _build_query("c1", keyword="Adani", platform="facebook", status="pending")
        stripped = _without(q, "keywords")
        assert stripped["platform"] == "facebook"
        assert stripped["status"] == "pending"
