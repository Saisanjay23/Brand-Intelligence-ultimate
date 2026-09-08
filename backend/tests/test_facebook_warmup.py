"""The feed impression a Facebook context takes before its first search.

WHAT THIS DEFENDS, and it is behavioural rather than a fingerprint patch: a
real session never opens with a search. It opens on facebook.com, the feed
renders, the person reads for a moment, and only then do they search. A
context whose very first request is `/search/people/?q=...` has no such
history, and that shape is visible to Meta however clean the browser
fingerprint is. This pool has had an account disabled, which is what makes
one page load cheap by comparison.

WHY THE THREE RULES BELOW ARE THE WHOLE TEST. Everything that can go wrong
here goes wrong in one of three ways, and each one is worse than not having
warmed up at all:

  once per CONTEXT   warming before every keyword would add exactly the
                     load this exists to reduce -- a 15-keyword client
                     would open 15 extra feed loads on a live account.

  never fatal        a warm-up is added realism, not a prerequisite. If it
                     throws, the keyword must still be swept; the sweep is
                     the thing the analyst asked for.

  no retry storm     `_warmed` is set BEFORE the attempt, so a context
                     whose warm-up times out does not pay that timeout
                     again on every later keyword.
"""

from __future__ import annotations

import pytest

from backend.platforms.facebook.discovery_engine import Discovery


class FakePage:
    def __init__(self, owner: "FakeCtx", fail: bool = False) -> None:
        self.owner = owner
        self.fail = fail
        self.closed = False

    async def goto(self, url: str, **kw) -> None:
        self.owner.visited.append(url)
        if self.fail:
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded")

    async def close(self) -> None:
        self.closed = True


class FakeCtx:
    """Just the two things `_warm` touches on a BrowserContext."""

    def __init__(self, fail: bool = False) -> None:
        self.visited: list[str] = []
        self.pages: list[FakePage] = []
        self.fail = fail

    async def new_page(self) -> FakePage:
        page = FakePage(self, fail=self.fail)
        self.pages.append(page)
        return page


@pytest.fixture(autouse=True)
def _no_real_pointer_motion(monkeypatch):
    """`humanize_interaction` drives a real mouse over a real page. The warm
    -up's own contract is what is under test, not human.py's motion."""
    async def _noop(page, scroll: bool = True, moves: int = 3) -> None:
        return None

    monkeypatch.setattr(
        "backend.platforms.facebook.discovery_engine.humanize_interaction", _noop)


class TestItLandsOnTheFeedFirst:
    @pytest.mark.asyncio
    async def test_it_loads_the_feed_not_a_search_url(self):
        ctx = FakeCtx()
        await Discovery(None, ctx)._warm()
        assert ctx.visited == ["https://www.facebook.com/"]

    @pytest.mark.asyncio
    async def test_it_closes_the_page_it_opened(self):
        """A leaked tab per context is a real cost on a long sweep."""
        ctx = FakeCtx()
        await Discovery(None, ctx)._warm()
        assert all(p.closed for p in ctx.pages)


class TestItRunsOncePerContext:
    @pytest.mark.asyncio
    async def test_a_second_call_is_a_no_op(self):
        ctx = FakeCtx()
        disc = Discovery(None, ctx)
        await disc._warm()
        await disc._warm()
        await disc._warm()
        assert ctx.visited == ["https://www.facebook.com/"], (
            "warmed more than once on one context")

    @pytest.mark.asyncio
    async def test_a_fresh_context_warms_again(self):
        """The flag belongs to the context, not the process -- a new session
        genuinely has no history to inherit."""
        first, second = FakeCtx(), FakeCtx()
        await Discovery(None, first)._warm()
        await Discovery(None, second)._warm()
        assert first.visited == second.visited == ["https://www.facebook.com/"]


class TestItIsNeverFatal:
    @pytest.mark.asyncio
    async def test_a_failing_warm_up_does_not_raise(self):
        """Same contract as tiktok's: the sweep is what matters."""
        ctx = FakeCtx(fail=True)
        await Discovery(None, ctx)._warm()  # must not raise

    @pytest.mark.asyncio
    async def test_a_failing_warm_up_still_closes_its_page(self):
        ctx = FakeCtx(fail=True)
        await Discovery(None, ctx)._warm()
        assert all(p.closed for p in ctx.pages)

    @pytest.mark.asyncio
    async def test_a_failed_warm_up_is_not_retried_on_the_next_keyword(self):
        """The defect this pins: setting `_warmed` only on SUCCESS would make
        a context whose warm-up times out pay that timeout again before
        every single keyword -- turning added realism into a sweep that
        never finishes."""
        ctx = FakeCtx(fail=True)
        disc = Discovery(None, ctx)
        await disc._warm()
        await disc._warm()
        assert len(ctx.visited) == 1, "retried a warm-up that had already failed"
