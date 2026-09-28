"""New holds the latest run's finds until the next run replaces them.

It used to be "first seen in the last 24 hours", which on a weekly schedule
emptied New a day after every run. The rules pinned here:

  * New begins at the START of the client's latest run that found anything;
  * a run moves that line only once it saves a NEW profile -- a run that
    fails at once or finds nothing never empties New;
  * the line only ever moves forward ($max);
  * both passes of one scheduled run share the run's start;
  * clients whose runs predate this get the line from their latest batch
    of finds;
  * with no history at all, the 24h rule still applies;
  * the Validated tab keeps its own 24h clock.

PURE LOGIC ONLY: Mongo is replaced by stand-ins.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.database.repositories import new_window_repository as nw
from backend.database.repositories import profile_repository as pdb

NOW = datetime.now(timezone.utc)
RUN_START = NOW - timedelta(days=5)          # last week's run


def _matches(clause: dict, doc: dict) -> bool:
    """Just enough of Mongo's matcher for the age clauses."""
    for k, v in clause.items():
        if k == "$or":
            if not any(_matches(c, doc) for c in v):
                return False
        elif k == "$and":
            if not all(_matches(c, doc) for c in v):
                return False
        else:
            val = doc.get(k)
            if v is None:
                if val is not None:
                    return False
            elif isinstance(v, dict):
                if "$exists" in v and (k in doc) != v["$exists"]:
                    return False
                if "$gte" in v and not (val is not None and val >= v["$gte"]):
                    return False
                if "$lt" in v and not (val is not None and val < v["$lt"]):
                    return False
    return True


class TestTheLineIsTheLatestRun:
    def test_a_profile_from_the_latest_run_is_new_days_later(self):
        doc = {"first_seen": RUN_START + timedelta(minutes=3)}
        assert _matches(pdb._age_clause("new", RUN_START), doc)
        assert not _matches(pdb._age_clause("old", RUN_START), doc)

    def test_a_profile_from_before_the_run_is_old(self):
        doc = {"first_seen": RUN_START - timedelta(days=1)}
        assert _matches(pdb._age_clause("old", RUN_START), doc)
        assert not _matches(pdb._age_clause("new", RUN_START), doc)

    def test_a_picture_change_during_the_run_brings_an_old_profile_back(self):
        doc = {"first_seen": RUN_START - timedelta(days=60),
               "avatar_changed_at": RUN_START + timedelta(minutes=10)}
        assert _matches(pdb._age_clause("new", RUN_START), doc)

    def test_without_a_line_the_24h_rule_still_applies(self):
        assert _matches(pdb._age_clause("new"), {"first_seen": NOW - timedelta(hours=2)})
        assert _matches(pdb._age_clause("old"), {"first_seen": NOW - timedelta(hours=30)})


class TestTheRunMovesTheLine:
    def _job(self, started):
        from backend.discovery.runner import DiscoveryJob

        job = DiscoveryJob(id="j", group_id="acme", keyword_plan=[])
        job.started_at_ts = started.timestamp()
        return job

    @pytest.mark.asyncio
    async def test_only_once_the_run_finds_something_new(self, monkeypatch):
        from backend.discovery import runner as R

        advance = AsyncMock()
        monkeypatch.setattr(R.new_window_db, "advance", advance)
        job = self._job(NOW)
        await R._mark_new_window(job, 0)          # re-found profiles only
        advance.assert_not_awaited()
        await R._mark_new_window(job, 3)
        await R._mark_new_window(job, 7)          # once per run
        advance.assert_awaited_once()
        group, since = advance.await_args.args
        assert group == "acme" and abs((since - NOW).total_seconds()) < 1

    @pytest.mark.asyncio
    async def test_a_scheduled_pass_uses_the_scheduled_runs_start(self, monkeypatch):
        """The gap-closing pass starts an hour after the first; both must
        mark the SAME line, or the second would push the first's finds out."""
        from backend.discovery import runner as R

        advance = AsyncMock()
        monkeypatch.setattr(R.new_window_db, "advance", advance)
        second_pass = self._job(RUN_START + timedelta(hours=1))
        second_pass.new_window_ts = RUN_START.timestamp()
        await R._mark_new_window(second_pass, 2)
        assert abs((advance.await_args.args[1] - RUN_START).total_seconds()) < 1

    @pytest.mark.asyncio
    async def test_a_failure_to_record_it_never_breaks_the_sweep(self, monkeypatch):
        from backend.discovery import runner as R

        monkeypatch.setattr(R.new_window_db, "advance", AsyncMock(side_effect=RuntimeError("db down")))
        await R._mark_new_window(self._job(NOW), 1)   # must not raise


class TestTheStore:
    @pytest.mark.asyncio
    async def test_the_line_only_moves_forward(self, monkeypatch):
        coll = MagicMock(update_one=AsyncMock())
        monkeypatch.setattr(nw, "db", lambda: {nw.COLLECTION: coll})
        await nw.advance("acme", RUN_START)
        q, update = coll.update_one.await_args.args
        assert q == {"_id": "acme"}
        assert update["$max"] == {"since": RUN_START}
        assert coll.update_one.await_args.kwargs["upsert"] is True

    @pytest.mark.asyncio
    async def test_a_stored_line_is_used(self, monkeypatch):
        coll = MagicMock(find_one=AsyncMock(return_value={"since": RUN_START}))
        monkeypatch.setattr(nw, "db", lambda: {nw.COLLECTION: coll})
        assert await nw.since("acme") == RUN_START

    @pytest.mark.asyncio
    async def test_an_existing_client_gets_its_line_from_its_latest_batch(self, monkeypatch):
        """Walk back from the newest find until a gap longer than 12h: the
        two passes of last week's run (an hour apart) are one batch, the
        run before that is not."""
        store = MagicMock(find_one=AsyncMock(return_value=None), update_one=AsyncMock())
        first_pass = RUN_START
        finds = [first_pass + timedelta(minutes=70),       # gap-closing pass
                 first_pass + timedelta(minutes=5),
                 first_pass,
                 first_pass - timedelta(days=7)]           # the run before

        class _Cursor:
            def __init__(self, rows):
                self.rows = rows

            def sort(self, *a):
                return self

            def __aiter__(self):
                self._it = iter(self.rows)
                return self

            async def __anext__(self):
                try:
                    return {"first_seen": next(self._it)}
                except StopIteration:
                    raise StopAsyncIteration

        profiles = MagicMock(find=lambda *a, **k: _Cursor(finds))
        monkeypatch.setattr(nw, "db", lambda: {nw.COLLECTION: store, nw.PROFILES: profiles})
        assert await nw.since("acme") == first_pass
        store.update_one.assert_awaited_once()      # remembered from now on

    @pytest.mark.asyncio
    async def test_no_history_means_the_24h_rule(self, monkeypatch):
        store = MagicMock(find_one=AsyncMock(return_value=None))

        class _Empty:
            def sort(self, *a):
                return self

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        profiles = MagicMock(find=lambda *a, **k: _Empty())
        monkeypatch.setattr(nw, "db", lambda: {nw.COLLECTION: store, nw.PROFILES: profiles})
        assert await nw.since("brand-new-client") is None
        cut = await pdb.discovery_new_cutoff("brand-new-client")
        expected = datetime.now(timezone.utc) - timedelta(hours=24)
        assert abs((expected - cut).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_a_lookup_failure_falls_back_instead_of_breaking_the_grid(self, monkeypatch):
        monkeypatch.setattr(nw, "db", lambda: (_ for _ in ()).throw(RuntimeError("down")))
        assert await nw.since("acme") is None
