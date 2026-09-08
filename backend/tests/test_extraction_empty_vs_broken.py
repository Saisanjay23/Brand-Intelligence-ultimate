""""Nothing matched" and "the extractor is broken" are not the same event.

THE DEFECT THIS PINS, measured off this deployment's own logs. Every
strategy in a chain returning empty was reported identically to every
strategy blowing up: `ERROR ... every strategy failed`. Both end with no
value and every strategy sitting in `failures`, so nothing downstream --
and no human reading the log -- could tell them apart.

What that cost, concretely: 28 of those ERRORs across six days, against
real brand keywords ('Pranav Adani'/groups 8 times, 'Jeet Adani'/groups 5
times) and, decisively, against 'Zzyxwq Adanixyz Fakebrand' -- a nonsense
keyword typed to match nothing, which therefore SHOULD return nothing.
A search for a brand nobody is impersonating looked exactly like a
GraphQL doc-id rotation having silently blinded the sweep. The log cried
wolf often enough to be worth ignoring, which is the real damage: the day
the chain genuinely rots, that line is the warning.

THE RULE: a strategy that RAISED is a real failure and still reports as
one. A chain where every strategy ran cleanly and simply had nothing to
hand back is an empty result, which is an ordinary, healthy answer.
"""

from __future__ import annotations

import logging

import pytest

from backend.shared.extraction import run_strategies


def _boom():
    raise RuntimeError("doc id rotated")


class TestAllEmpty:
    @pytest.mark.asyncio
    async def test_every_strategy_returning_nothing_is_an_empty_result(self):
        res = await run_strategies(
            "fb/search['nobody']", [("network", lambda: []), ("dom", lambda: [])])
        assert res.all_empty is True
        assert res.ok is False

    @pytest.mark.asyncio
    async def test_a_strategy_that_raised_is_a_real_failure(self):
        """The case the loud error exists for -- it must stay loud."""
        res = await run_strategies(
            "fb/search['acme']", [("network", _boom), ("dom", lambda: [])])
        assert res.all_empty is False
        assert res.ok is False

    @pytest.mark.asyncio
    async def test_a_chain_that_produced_data_is_neither(self):
        res = await run_strategies(
            "fb/search['acme']", [("network", lambda: []), ("dom", lambda: ["hit"])])
        assert res.ok is True
        assert res.all_empty is False
        assert res.degraded is True, "recovered on the fallback -- still worth knowing"


class TestWhatTheReportSays:
    @pytest.mark.asyncio
    async def test_an_empty_result_does_not_claim_anything_failed(self):
        res = await run_strategies(
            "fb/search['nobody']", [("network", lambda: []), ("dom", lambda: [])])
        assert "No data to extract" in res.report()
        assert "Every extraction strategy failed" not in res.report()

    @pytest.mark.asyncio
    async def test_a_broken_chain_still_says_so(self):
        res = await run_strategies(
            "fb/search['acme']", [("network", _boom), ("dom", lambda: [])])
        assert "Every extraction strategy failed." in res.report()


class TestTheLogLevel:
    """The whole point of the fix: an ordinary empty search must not page
    anyone, and a genuinely broken chain must still shout."""

    @pytest.mark.asyncio
    async def test_an_empty_result_is_not_logged_as_an_error(self, caplog):
        with caplog.at_level(logging.INFO, logger="bi.shared.extraction"):
            await run_strategies(
                "fb/search['nobody']", [("network", lambda: []), ("dom", lambda: [])])
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert [r for r in caplog.records if r.levelno == logging.INFO]

    @pytest.mark.asyncio
    async def test_a_raised_strategy_is_still_logged_as_an_error(self, caplog):
        with caplog.at_level(logging.INFO, logger="bi.shared.extraction"):
            await run_strategies(
                "fb/search['acme']", [("network", _boom), ("dom", lambda: [])])
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "a rotting extraction chain must still be loud"
        assert "every strategy failed" in errors[0].getMessage()


class TestItDoesNotWeakenTheRealWarning:
    @pytest.mark.asyncio
    async def test_a_recovered_fallback_still_warns(self, caplog):
        """`degraded` is the early warning that the primary path is rotting
        while output still looks fine -- untouched by this change."""
        with caplog.at_level(logging.WARNING, logger="bi.shared.extraction"):
            res = await run_strategies(
                "fb/search['acme']", [("network", _boom), ("dom", lambda: ["hit"])])
        assert res.ok and res.degraded
        assert [r for r in caplog.records if r.levelno == logging.WARNING]

    @pytest.mark.asyncio
    async def test_an_empty_primary_with_a_working_fallback_is_not_empty(self):
        """`all_empty` is about the CHAIN, not about any one strategy."""
        res = await run_strategies(
            "fb/search['acme']", [("network", lambda: []), ("dom", lambda: ["hit"])])
        assert res.all_empty is False
