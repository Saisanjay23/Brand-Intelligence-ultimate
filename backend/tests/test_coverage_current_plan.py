"""Owed work is counted against the client's CURRENT plan, not the whole ledger.

THE BUG THIS GUARDS. The coverage ledger never forgets a cell. A keyword,
permutation, platform or Facebook tab the analyst later REMOVED left its
owed cells behind, and the gap-closing lap -- correctly -- never sweeps
them (`DiscoveryRunner._queue_for` only keeps cells still in the job's
plan and tabs). So the Scheduler's `_owed_for`, which counted every owed
cell in the ledger, said "still owing searches" every night for work no
sweep would ever do, and the run alert went out about it.

No Mongo: the ledger's `owed()`/`summary()` are replaced with fakes; the
filter under test (`coverage_repository.in_scope`) and the runner's own
plan builder are the real ones.
"""

from __future__ import annotations

import pytest

from backend.database.repositories import coverage_repository as coverage_db
from backend.discovery.runner import discovery_runner
from backend.services import scheduler_service
from backend.services.scheduler_service import OWED_UNKNOWN, SchedulerEngine


def _cell(platform, tab, search, kw_type="individual"):
    return {"platform": platform, "tab": tab, "kw_type": kw_type, "search": search,
            "parent": search, "outcome": "missed", "reason": "", "attempts": 1,
            "attempted_at": None, "covered_at": None}


def _client(**over):
    doc = {
        "client_id": "c1", "name": "C1",
        "name_keywords": ["Jane Doe"], "domain_keywords": ["acme"],
        "scheduler_keyword_scope": "",
        "scheduler_platforms": [],
        "scheduler_facebook_tabs": [],
    }
    doc.update(over)
    return doc


# A ledger with one cell that is still in plan, and one for each way a cell
# can fall out of it.
_LEDGER = [
    _cell("facebook", "people", "jane doe"),            # in plan (casing differs)
    _cell("facebook", "groups", "Jane Doe"),            # in plan unless groups deselected
    _cell("twitter", "people", "acme", "domain"),       # in plan unless twitter deselected
    _cell("facebook", "people", "Old Exec"),            # keyword since removed
]


class TestInScope:
    def test_a_removed_keyword_is_out_of_plan(self):
        plans = discovery_runner._plans_from(_client(), ["Jane Doe"], ["acme"])
        check = coverage_db.in_scope(plans, None, lambda pid: ["people", "pages", "groups"])
        assert [check(c) for c in _LEDGER] == [True, True, True, False]

    def test_a_deselected_platform_and_tab_are_out_of_plan(self):
        plans = discovery_runner._plans_from(_client(), ["Jane Doe"], ["acme"])
        check = coverage_db.in_scope(plans, ["facebook"], lambda pid: ["people"])
        assert [check(c) for c in _LEDGER] == [True, False, False, False]


@pytest.fixture
def engine(monkeypatch):
    state = {"client": _client(), "ledger": list(_LEDGER), "raises": False}

    class FakeClients:
        async def try_get(self, client_id):
            return state["client"]

    async def fake_owed(group_id, platforms=None):
        if state["raises"]:
            raise RuntimeError("mongo is down")
        return list(state["ledger"])

    async def fake_summary(group_id):
        if state["raises"]:
            raise RuntimeError("mongo is down")
        return {"owed": len(state["ledger"])}

    monkeypatch.setattr(scheduler_service, "clients_db", FakeClients())
    monkeypatch.setattr(coverage_db, "owed", fake_owed)
    monkeypatch.setattr(coverage_db, "summary", fake_summary)
    eng = SchedulerEngine()
    eng.state = state
    return eng


@pytest.mark.asyncio
class TestSchedulerOwedFor:
    async def test_a_removed_keyword_is_no_longer_owed(self, engine):
        assert await engine._owed_for("c1") == 3

    async def test_a_deselected_facebook_tab_is_no_longer_owed(self, engine):
        engine.state["client"] = _client(scheduler_facebook_tabs=["people", "pages"])
        assert await engine._owed_for("c1") == 2

    async def test_a_deselected_platform_is_no_longer_owed(self, engine):
        engine.state["client"] = _client(scheduler_platforms=["facebook"])
        assert await engine._owed_for("c1") == 2

    async def test_a_keyword_type_outside_the_scope_is_no_longer_owed(self, engine):
        engine.state["client"] = _client(scheduler_keyword_scope="individual")
        assert await engine._owed_for("c1") == 2

    async def test_genuinely_owed_work_is_still_counted(self, engine):
        """The filter must never turn real owed work into a clean zero."""
        engine.state["ledger"] = [_cell("facebook", "people", "Jane Doe")]
        assert await engine._owed_for("c1") == 1

    async def test_an_unreadable_client_falls_back_to_the_whole_ledger(self, engine):
        engine.state["client"] = None
        assert await engine._owed_for("c1") == len(_LEDGER)

    async def test_an_unreadable_ledger_is_unknown_never_zero(self, engine):
        engine.state["raises"] = True
        assert await engine._owed_for("c1") == OWED_UNKNOWN


@pytest.mark.asyncio
async def test_the_coverage_route_moves_removed_keywords_to_retired(monkeypatch):
    from backend.api import discovery as api

    async def fake_summary(group_id):
        return {"group_id": group_id, "cells": 10, "owed": len(_LEDGER),
                "by_outcome": {"missed": len(_LEDGER)}, "by_platform": {}}

    async def fake_owed(group_id, platforms=None):
        return list(_LEDGER)

    async def fake_unplanned(group_id, platforms, plans):
        return []

    async def fake_get(group_id):
        return _client()

    monkeypatch.setattr(api.coverage_db, "summary", fake_summary)
    monkeypatch.setattr(api.coverage_db, "owed", fake_owed)
    monkeypatch.setattr(api.coverage_db, "unplanned", fake_unplanned)
    monkeypatch.setattr(api.clients_db, "get", fake_get)

    report = await api.coverage(group_id="c1", platform=None)
    assert report.owed == 3
    assert report.retired == 1, "the removed keyword's cell is shown as retired, not dropped silently"
    assert all(c.search != "Old Exec" for c in report.items)
