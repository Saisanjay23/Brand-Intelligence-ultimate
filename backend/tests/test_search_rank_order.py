"""Platform result order, recorded rather than inferred.

WHAT BROKE AND WHY THIS EXISTS. The discovery grid shows profiles in the
order the platform's own search returned them, and it used to get that for
free from ascending `_id`: an ObjectId starts with an insertion timestamp,
so while ONE account swept ONE keyword at a time, "the order we saved them"
and "the order the platform listed them" were the same sequence.

Sharding the keyword list across several pooled accounts ended that. Three
workers write concurrently, so insertion order became an interleaving of
three keywords and the grid stopped reflecting what any platform actually
returned -- silently, because the rows all still look plausible. The fix is
to record the position at save time, which is what `search_rank` is.

These tests pin the rule and the plumbing. The ordering itself was verified
against the live database (21,176 rows): filtered to one platform and
keyword the ranks come back 1, 2, 3, 5, 6, 7...; unfiltered, page 1 leads
with every keyword's #1.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.database.repositories.profile_repository import (
    PHASE_ANALYSIS,
    PHASE_DISCOVERY,
    build_sort_spec,
    save_many,
)


# ------------------------------------------------------------- the sort rule


def test_discovery_orders_by_the_platforms_own_position():
    spec = build_sort_spec(PHASE_DISCOVERY, "pending", "_id", 1)
    assert spec == [("logo_similarity", -1), ("search_rank", 1), ("_id", 1)]


def test_a_logo_match_still_outranks_position():
    """A profile wearing the client's own logo is stronger evidence than a
    search engine's opinion about it, and that was true before this change."""
    assert build_sort_spec(PHASE_DISCOVERY, "pending", "_id", 1)[0] == ("logo_similarity", -1)


def test_insertion_order_survives_as_the_final_tie_break():
    """Rows with equal rank -- or no rank at all, which is every row until
    the backfill runs -- must fall back to exactly the order they had
    before any of this existed."""
    assert build_sort_spec(PHASE_DISCOVERY, "approved", "_id", 1)[-1] == ("_id", 1)


@pytest.mark.parametrize("status", ["pending", "approved", None])
def test_every_non_rejected_discovery_view_gets_the_new_order(status):
    assert ("search_rank", 1) in build_sort_spec(PHASE_DISCOVERY, status, "_id", 1)


def test_the_rejected_view_is_left_exactly_as_it_was():
    """Its ordering exists so the profile an analyst JUST rejected sits at
    the top. Floating an old logo match or a low search position over that
    would defeat the one thing that view is for."""
    assert build_sort_spec(PHASE_DISCOVERY, "rejected", "rejected_at", -1) == [("rejected_at", -1)]


def test_analysis_views_are_unaffected():
    """An analysis pass re-reads a profile it was handed; it never found it
    in a result list, so it has no position to sort by."""
    spec = build_sort_spec(PHASE_ANALYSIS, None, "last_seen", -1)
    assert spec == [("logo_similarity", -1), ("last_seen", -1)]
    assert not [k for k, _ in spec if k == "search_rank"]


# --------------------------------------------------------------- the plumbing


@pytest.mark.asyncio
async def test_a_rank_reaches_save_as_an_argument():
    saved = AsyncMock(return_value=True)
    rows = [
        {"url": "https://x.com/a", "entity_id": "a", "keyword": "kw", "display_name": "A", "rank": 1},
        {"url": "https://x.com/b", "entity_id": "b", "keyword": "kw", "display_name": "B", "rank": 2},
    ]
    with patch("backend.database.repositories.profile_repository.save", saved):
        await save_many("c1", "twitter", PHASE_DISCOVERY, rows)

    assert [c.kwargs["rank"] for c in saved.await_args_list] == [1, 2]


@pytest.mark.asyncio
async def test_the_rank_never_lands_in_the_document_itself():
    """`rank` is a control key like `retry_pending` and `matched_keyword`:
    it tells `save` what to do, it is not a field of the profile. Leaving it
    in the fields dict would write an unowned key into the document."""
    saved = AsyncMock(return_value=True)
    with patch("backend.database.repositories.profile_repository.save", saved):
        await save_many("c1", "twitter", PHASE_DISCOVERY, [
            {"url": "https://x.com/a", "entity_id": "a", "display_name": "A", "rank": 7},
        ])

    fields = saved.await_args.args[3]
    assert "rank" not in fields
    assert "search_rank" not in fields


@pytest.mark.asyncio
async def test_a_row_with_no_rank_is_still_saved():
    """Every caller that predates this keeps working, and a platform that
    cannot report a position is not a reason to drop the profile."""
    saved = AsyncMock(return_value=True)
    with patch("backend.database.repositories.profile_repository.save", saved):
        await save_many("c1", "twitter", PHASE_DISCOVERY, [
            {"url": "https://x.com/a", "entity_id": "a", "display_name": "A"},
        ])

    assert saved.await_args.kwargs["rank"] == 0


@pytest.mark.asyncio
async def test_a_junk_rank_cannot_break_the_batch():
    saved = AsyncMock(return_value=True)
    with patch("backend.database.repositories.profile_repository.save", saved):
        await save_many("c1", "twitter", PHASE_DISCOVERY, [
            {"url": "https://x.com/a", "entity_id": "a", "display_name": "A", "rank": None},
        ])

    assert saved.await_args.kwargs["rank"] == 0
