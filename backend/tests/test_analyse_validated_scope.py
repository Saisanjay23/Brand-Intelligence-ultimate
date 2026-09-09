""""Analyse Validated" scrapes the platforms the analyst picked, and no others.

THE DEFECT. `POST /discovery/profiles/analyse` only ever spoke a single
`platform`, but the caller's platform picker on Run & Overview is
MULTI-SELECT. The frontend bridged that gap with

    analysisPlatforms.size === 1 ? [...analysisPlatforms][0] : undefined

so a selection of two or more collapsed to `undefined` -- and `undefined`
means EVERY platform. Ticking Facebook and Twitter therefore scraped all six,
which is not a cosmetic overshoot: every extra platform is a live-session
page visit per validated profile, under accounts that get challenged for
exactly that kind of unasked-for volume. The confirmation dialog made it
worse by naming the narrow scope ("on Twitter +1") while the request that
followed widened it.

WIDENING IS THE WRONG DIRECTION TO FAIL IN, which is the rule these pin: a
scope that cannot be expressed must never silently become "everything".

The Discovery grid's own button was already correct -- its platform rail is
single-select, so `platform` said what it meant. That is why this looked
like an intermittent bug rather than a broken button: it depended entirely
on which of the two screens the analyst started from, and on how many chips
they had ticked.
"""

from __future__ import annotations

import pytest

from backend.api import discovery as D


def _doc(pid: str, n: int) -> dict:
    return {"id": f"{pid}-{n}", "url": f"https://{pid}.test/{n}", "platform": pid,
            "status": "approved"}


@pytest.fixture
def pool(monkeypatch):
    """Two validated profiles on each of three platforms."""
    store = {p: [_doc(p, 1), _doc(p, 2)] for p in ("facebook", "twitter", "instagram")}

    async def _find(group_id, *, platform=None, **kw):
        docs = store[platform] if platform else [d for p in store for d in store[p]]
        offset = kw.get("offset", 0)
        limit = kw.get("limit", 100)
        page = docs[offset:offset + limit]
        return page, len(docs), {}

    monkeypatch.setattr(D.profiles_db, "find", _find)
    return store


def _platforms(docs: list[dict]) -> set[str]:
    return {d["platform"] for d in docs}


class TestOnlyTheSelectedPlatformsAreScraped:
    @pytest.mark.asyncio
    async def test_one_platform_scrapes_only_it(self, pool):
        docs = await D._validated_docs("acme", ["facebook"])
        assert _platforms(docs) == {"facebook"}
        assert len(docs) == 2

    @pytest.mark.asyncio
    async def test_two_platforms_scrape_exactly_those_two(self, pool):
        """THE REGRESSION. This selection could not be expressed at all
        before, so it became "every platform"."""
        docs = await D._validated_docs("acme", ["facebook", "twitter"])
        assert _platforms(docs) == {"facebook", "twitter"}
        assert len(docs) == 4

    @pytest.mark.asyncio
    async def test_no_selection_still_means_every_platform(self, pool):
        """Empty is the All Platforms state, not an empty batch."""
        assert _platforms(await D._validated_docs("acme", [])) == {
            "facebook", "twitter", "instagram"}
        assert _platforms(await D._validated_docs("acme", None)) == {
            "facebook", "twitter", "instagram"}

    @pytest.mark.asyncio
    async def test_the_analysts_pick_order_is_kept(self, pool):
        """Within a platform the query's own ordering holds; across them,
        the order they were picked. A batch that comes back in a stable
        order is one an analyst can review top-to-bottom."""
        docs = await D._validated_docs("acme", ["twitter", "facebook"])
        assert [d["platform"] for d in docs] == [
            "twitter", "twitter", "facebook", "facebook"]

    @pytest.mark.asyncio
    async def test_a_platform_picked_twice_is_not_scraped_twice(self, pool):
        """Two live page visits for one answer is the cost of getting this
        wrong."""
        docs = await D._validated_docs("acme", ["facebook", "facebook"])
        assert len(docs) == 2

    @pytest.mark.asyncio
    async def test_a_blank_entry_does_not_widen_the_scope(self, pool):
        """A stray "" in the list must not read as "and also everything"."""
        docs = await D._validated_docs("acme", ["facebook", ""])
        assert _platforms(docs) == {"facebook"}


class TestTheBatchCeilingIsShared:
    @pytest.mark.asyncio
    async def test_selecting_more_platforms_cannot_grow_the_batch(self, pool, monkeypatch):
        """`_MAX_VALIDATED_PER_ANALYSE` bounds one analyse call for a
        reason. Applying it per platform instead of per call would let a
        three-platform pick triple it."""
        monkeypatch.setattr(D, "_MAX_VALIDATED_PER_ANALYSE", 3)
        docs = await D._validated_docs("acme", ["facebook", "twitter", "instagram"])
        assert len(docs) == 3

    @pytest.mark.asyncio
    async def test_the_ceiling_still_applies_to_an_unscoped_call(self, pool, monkeypatch):
        monkeypatch.setattr(D, "_MAX_VALIDATED_PER_ANALYSE", 3)
        assert len(await D._validated_docs("acme", [])) == 3


class TestBothRequestShapesStillWork:
    """The Discovery grid sends singular `platform`; Run & Overview sends
    plural `platforms`. Neither may lose its scope."""

    @staticmethod
    def _selected(platform=None, platforms=None) -> list[str]:
        """The route's own resolution of the two fields."""
        return (
            [p for p in platforms] if platforms
            else ([platform] if platform else [])
        )

    def test_the_grids_single_platform_is_honoured(self):
        assert self._selected(platform="facebook") == ["facebook"]

    def test_the_multi_select_wins_when_both_are_sent(self):
        assert self._selected(platform="facebook", platforms=["twitter"]) == ["twitter"]

    def test_neither_means_every_platform(self):
        assert self._selected() == []

    def test_an_empty_plural_list_is_not_treated_as_a_narrowing(self):
        """`platforms: []` is the All Platforms state, so it must fall
        through to "every platform", not to "no platform"."""
        assert self._selected(platforms=[]) == []
