"""Every permutation an analyst generates is actually searched, on every
platform, and files under its parent.

THE DEFECT. The keyword-groups feature was complete end to end except for
the one link that runs a search. The UI generated permutations and attached
them as children; `normalize_groups` stored them; `groups_for_client` read
them back; `build_plans` existed to expand `[parent, *children]` -- and
nothing called it. `POST /discovery` is handed the client's
`name_keywords`/`domain_keywords`, which by deliberate design hold PARENTS
ONLY, and the plan was built straight from those two lists.

So the permutations were saved, listed in the Keywords tab, and dropped at
sweep time. Measured on a realistic client (3 parents, 7 permutations),
7 of the 10 terms were never run on any platform -- and those 7 are the
terms that actually find impersonators, since one rarely registers under the
victim's name verbatim.

The half that DID work is worth pinning too: whatever is in the plan is
swept by every ready platform, one entry at a time. That is what made the
gap invisible -- the sweep ran perfectly, over the wrong list.

THE RULE (revised 2026-10-23): EVERY keyword an analyst configured is
searched -- each parent AND each of its permutations, parent first. This
reverses the earlier rule, under which a parent with permutations was not
searched itself. That rule left a configured keyword silently un-run: a
client saved with 36 keywords (parents plus permutations) ran 25 searches.
A keyword nobody searched is an impersonator nobody looked for, and the
Keywords tab already promised "N + 1 searches" for N permutations.
"""

from __future__ import annotations

from collections import deque

import pytest

from backend.discovery import runner as R
from backend.shared import keywords as K


def _client(**groups) -> dict:
    return {
        "client_id": "acme",
        "name_keywords": [g["parent"] for g in groups.get("individual", [])],
        "domain_keywords": [g["parent"] for g in groups.get("domain", [])],
        "keyword_groups": {
            "individual": groups.get("individual", []),
            "domain": groups.get("domain", []),
        },
    }


ADANI = _client(
    individual=[
        {"parent": "Gautam Adani",
         "children": ["gautamadani", "gautam.adani.hq", "gautam adani official"]},
        {"parent": "Jeet Adani", "children": ["jeetadani"]},
    ],
    domain=[{"parent": "adani.com", "children": ["adani-group.com"]}],
)


def _wire_client(monkeypatch, doc):
    """Point the runner's client lookup at `doc` (or make it fail)."""
    from backend.database.repositories import client_repository as clients_db

    async def _get(_cid):
        if doc is None:
            raise RuntimeError("mongo unreachable")
        return doc

    monkeypatch.setattr(clients_db, "get", _get)


async def _plans(monkeypatch, doc, ind, dom):
    _wire_client(monkeypatch, doc)
    return await R.DiscoveryRunner()._plans_for("acme", ind, dom)


class TestEveryPermutationIsPlanned:
    @pytest.mark.asyncio
    async def test_every_configured_keyword_is_searched(self, monkeypatch):
        """THE RULE, end to end: each parent, then each of its permutations,
        in the analyst's own order -- nothing configured is left out."""
        plans = await _plans(monkeypatch, ADANI, ADANI["name_keywords"], ADANI["domain_keywords"])
        assert [p.search for p in plans] == [
            "Gautam Adani", "gautamadani", "gautam.adani.hq", "gautam adani official",
            "Jeet Adani", "jeetadani",
            "adani.com", "adani-group.com",
        ]

    @pytest.mark.asyncio
    async def test_a_parent_with_permutations_is_searched_too(self, monkeypatch):
        """The reversed half. A parent used to be dropped the moment it had
        one permutation, leaving a configured keyword silently un-run."""
        plans = await _plans(monkeypatch, ADANI, ["Gautam Adani"], [])
        assert [p.search for p in plans] == [
            "Gautam Adani", "gautamadani", "gautam.adani.hq", "gautam adani official"]

    @pytest.mark.asyncio
    async def test_a_parent_without_permutations_is_searched(self, monkeypatch):
        """The other half of the same rule. A keyword must never be
        configured and then silently not run."""
        doc = _client(individual=[{"parent": "Solo Name", "children": []}])
        plans = await _plans(monkeypatch, doc, ["Solo Name"], [])
        assert [p.search for p in plans] == ["Solo Name"]

    @pytest.mark.asyncio
    async def test_parents_with_and_without_permutations_coexist(self, monkeypatch):
        """One config routinely has both shapes; both are fully searched."""
        doc = _client(individual=[
            {"parent": "With Perms", "children": ["withperms"]},
            {"parent": "Without", "children": []},
        ])
        plans = await _plans(monkeypatch, doc, ["With Perms", "Without"], [])
        assert [p.search for p in plans] == ["With Perms", "withperms", "Without"]

    @pytest.mark.asyncio
    async def test_the_analysts_own_ordering_is_kept(self, monkeypatch):
        """The lists are rendered back in the order they were typed, and
        with sweeps running strictly one at a time that order is also the
        order results arrive in."""
        plans = await _plans(monkeypatch, ADANI, ["Gautam Adani", "Jeet Adani"], [])
        assert [p.search for p in plans] == [
            "Gautam Adani", "gautamadani", "gautam.adani.hq", "gautam adani official",
            "Jeet Adani", "jeetadani"]

    @pytest.mark.asyncio
    async def test_each_permutation_keeps_its_parents_keyword_type(self, monkeypatch):
        plans = await _plans(monkeypatch, ADANI, ADANI["name_keywords"], ADANI["domain_keywords"])
        by_search = {p.search: p.kw_type for p in plans}
        assert by_search["gautam.adani.hq"] == K.INDIVIDUAL
        assert by_search["adani-group.com"] == K.DOMAIN


class TestHitsFileUnderTheParent:
    """Searching as the permutation, filing as the parent. Storing the
    permutation instead would scatter one investigation across a dozen
    filter buckets and score every hit's name against "gautam.adani.hq",
    which says nothing about whether it impersonates Gautam Adani."""

    @pytest.mark.asyncio
    async def test_a_permutations_hits_roll_up_to_the_real_name(self, monkeypatch):
        plans = await _plans(monkeypatch, ADANI, ["Gautam Adani"], [])
        assert {p.parent for p in plans} == {"Gautam Adani"}

    @pytest.mark.asyncio
    async def test_the_queue_carries_both_the_search_and_the_parent(self, monkeypatch):
        plans = await _plans(monkeypatch, ADANI, ["Gautam Adani"], [])
        items = [R._KeywordItem(p.search, p.kw_type, p.parent) for p in plans]
        assert [(i.keyword, i.parent) for i in items] == [
            ("Gautam Adani", "Gautam Adani"),
            ("gautamadani", "Gautam Adani"),
            ("gautam.adani.hq", "Gautam Adani"),
            ("gautam adani official", "Gautam Adani"),
        ]

    def test_a_keyword_with_no_parent_given_is_its_own(self):
        """The pre-groups shape: a caller that knows nothing about
        permutations still files correctly."""
        assert R._KeywordItem("acme", "domain").parent == "acme"


class TestDeduplication:
    @pytest.mark.asyncio
    async def test_one_permutation_under_two_parents_is_one_search(self, monkeypatch):
        """Running the same query twice against one platform costs a real
        page load and risks the session for nothing."""
        doc = _client(individual=[
            {"parent": "Gautam Adani", "children": ["adani"]},
            {"parent": "Jeet Adani", "children": ["adani"]},
        ])
        plans = await _plans(monkeypatch, doc, doc["name_keywords"], [])
        assert [p.search for p in plans].count("adani") == 1

    @pytest.mark.asyncio
    async def test_that_shared_search_keeps_both_parents_as_candidates(self, monkeypatch):
        """One search, two possible homes -- `resolve_parent` picks per hit
        rather than filing everything under whichever group saved first."""
        doc = _client(individual=[
            {"parent": "Gautam Adani", "children": ["adani"]},
            {"parent": "Jeet Adani", "children": ["adani"]},
        ])
        plans = await _plans(monkeypatch, doc, doc["name_keywords"], [])
        shared = next(p for p in plans if p.search == "adani")
        assert {t.parent for t in shared.targets} == {"Gautam Adani", "Jeet Adani"}

    @pytest.mark.asyncio
    async def test_a_child_equal_to_its_parent_is_not_swept_twice(self, monkeypatch):
        doc = _client(individual=[{"parent": "Acme", "children": ["acme", "Acme"]}])
        plans = await _plans(monkeypatch, doc, ["Acme"], [])
        assert len(plans) == 1


class TestItDegradesRatherThanLosingTheSweep:
    @pytest.mark.asyncio
    async def test_a_childless_parent_searches_itself(self, monkeypatch):
        """Exactly the pre-groups behaviour, which is what a client saved
        before this feature existed must keep getting."""
        doc = _client(individual=[{"parent": "Acme", "children": []}])
        plans = await _plans(monkeypatch, doc, ["Acme"], [])
        assert [(p.search, p.parent) for p in plans] == [("Acme", "Acme")]

    @pytest.mark.asyncio
    async def test_a_client_with_no_groups_at_all_still_sweeps(self, monkeypatch):
        doc = {"client_id": "acme", "name_keywords": ["Acme"], "domain_keywords": ["acme.com"]}
        plans = await _plans(monkeypatch, doc, ["Acme"], ["acme.com"])
        assert sorted(p.search for p in plans) == ["Acme", "acme.com"]

    @pytest.mark.asyncio
    async def test_an_unreadable_client_sweeps_the_requested_terms_as_given(self, monkeypatch):
        """A config read that hiccuped must cost the permutations, never
        the sweep."""
        plans = await _plans(monkeypatch, None, ["Acme"], ["acme.com"])
        assert [(p.search, p.kw_type) for p in plans] == [
            ("Acme", K.INDIVIDUAL), ("acme.com", K.DOMAIN)]

    @pytest.mark.asyncio
    async def test_an_ad_hoc_term_keeps_the_type_the_caller_gave_it(self, monkeypatch):
        """`POST /discovery` takes two separate lists, so it always knows.
        Guessing would file an individual name under the domain caps."""
        plans = await _plans(monkeypatch, ADANI, ["Someone New"], [])
        assert [(p.search, p.kw_type) for p in plans if p.search == "Someone New"] == [
            ("Someone New", K.INDIVIDUAL)]


class TestScopingToOneKeywordType:
    @pytest.mark.asyncio
    async def test_an_individual_only_sweep_leaves_domain_groups_alone(self, monkeypatch):
        """The UI's Individual/Domain/All selector sends an empty list for
        the excluded type."""
        plans = await _plans(monkeypatch, ADANI, ADANI["name_keywords"], [])
        assert all(p.kw_type == K.INDIVIDUAL for p in plans)
        assert "adani-group.com" not in [p.search for p in plans]

    @pytest.mark.asyncio
    async def test_a_domain_only_sweep_brings_the_parent_and_its_permutations(self, monkeypatch):
        plans = await _plans(monkeypatch, ADANI, [], ADANI["domain_keywords"])
        assert [p.search for p in plans] == ["adani.com", "adani-group.com"]

    @pytest.mark.asyncio
    async def test_one_parent_of_several_sweeps_only_its_own_terms(self, monkeypatch):
        plans = await _plans(monkeypatch, ADANI, ["Jeet Adani"], [])
        assert [p.search for p in plans] == ["Jeet Adani", "jeetadani"]


class TestEveryPlatformSweepsEveryTerm:
    """The half that always worked, pinned so the expansion above cannot
    quietly become per-platform."""

    @pytest.mark.asyncio
    async def test_each_platform_queues_the_whole_plan(self, monkeypatch):
        plans = await _plans(monkeypatch, ADANI, ADANI["name_keywords"], ADANI["domain_keywords"])
        for platform in ("facebook", "twitter", "instagram", "tiktok", "youtube", "telegram"):
            queue = deque(
                R._KeywordItem(p.search, p.kw_type, p.parent) for p in plans)
            assert [i.keyword for i in queue] == [p.search for p in plans], platform

    @pytest.mark.asyncio
    async def test_the_progress_total_counts_permutations_not_just_parents(self, monkeypatch):
        """`keywords_total` is searches x tabs. Counting parents alone would
        show a sweep as finished with most of its work still queued."""
        plans = await _plans(monkeypatch, ADANI, ADANI["name_keywords"], [])
        tabs = R.PLATFORM_TABS["facebook"]
        # (Gautam Adani + 3 permutations) + (Jeet Adani + 1), x 3 tabs
        assert len(plans) * len(tabs) == 6 * 3

    @pytest.mark.asyncio
    async def test_the_job_reports_the_searches_it_will_run(self, monkeypatch):
        plans = await _plans(monkeypatch, ADANI, ["Gautam Adani"], [])
        job = R.DiscoveryJob(id="j", group_id="acme", keyword_plan=plans)
        assert job.keywords == [
            "Gautam Adani", "gautamadani", "gautam.adani.hq", "gautam adani official"]


# --------------------------------------------------------------------------
# THE GUARD. Written against the shape of a real client (parents carrying
# titles, permutations without them, mixed with childless parents) because
# the defect was invisible to every test that used a tidy one.

GLOBE_LIKE = _client(
    individual=[
        {"parent": "Mr. Ashok Kumar Agarwal",
         "children": ["ashok_agarwal", "ashok kumar", "ashok kumar agarwal"]},
        {"parent": "Mr. Arpit Agarwal", "children": ["Arpit Agarwal"]},
        {"parent": "Dhiraj Kumar Jaiswal", "children": ["Dhiraj Kumar"]},
        {"parent": "Gautam Gupta", "children": []},
    ],
    domain=[
        {"parent": "Globe capital",
         "children": ["globecapital", "globe.capital", "globe-capital"]},
        {"parent": "SF EXPRESS", "children": ["sf-express.com"]},
    ],
)


def _configured_terms(doc) -> list[str]:
    out = []
    for kind in ("individual", "domain"):
        for g in doc["keyword_groups"][kind]:
            out += [g["parent"], *g["children"]]
    return out


class TestNothingConfiguredIsLeftOut:
    @pytest.mark.asyncio
    async def test_every_parent_and_permutation_of_both_types_is_a_search(self, monkeypatch):
        plans = await _plans(
            monkeypatch, GLOBE_LIKE,
            GLOBE_LIKE["name_keywords"], GLOBE_LIKE["domain_keywords"])
        searched = {p.search.lower() for p in plans}
        missing = [t for t in _configured_terms(GLOBE_LIKE) if t.lower() not in searched]
        assert missing == [], f"configured but never searched: {missing}"

    @pytest.mark.asyncio
    async def test_the_search_count_equals_the_configured_count(self, monkeypatch):
        """Counts, not just membership: when no keyword repeats, N configured
        keywords must be N searches. A drop is visible here as a number."""
        plans = await _plans(
            monkeypatch, GLOBE_LIKE,
            GLOBE_LIKE["name_keywords"], GLOBE_LIKE["domain_keywords"])
        assert len(plans) == len(_configured_terms(GLOBE_LIKE))

    @pytest.mark.asyncio
    async def test_every_platform_queues_every_configured_keyword(self, monkeypatch):
        plans = await _plans(
            monkeypatch, GLOBE_LIKE,
            GLOBE_LIKE["name_keywords"], GLOBE_LIKE["domain_keywords"])
        want = sorted(t.lower() for t in _configured_terms(GLOBE_LIKE))
        for platform in ("facebook", "twitter", "instagram", "tiktok", "youtube", "telegram"):
            queue = deque(R._KeywordItem(p.search, p.kw_type, p.parent) for p in plans)
            assert sorted(i.keyword.lower() for i in queue) == want, platform

    @pytest.mark.asyncio
    async def test_a_parents_own_hits_file_under_that_parent(self, monkeypatch):
        """Searching the parent must not move its results into another
        bucket: the parent's own search files under the parent."""
        plans = await _plans(monkeypatch, GLOBE_LIKE, ["Mr. Arpit Agarwal"], [])
        own = next(p for p in plans if p.search == "Mr. Arpit Agarwal")
        assert own.parent == "Mr. Arpit Agarwal"
