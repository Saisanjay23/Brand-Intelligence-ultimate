"""Results reach the screen while a sweep is still running, in page order.

WHAT WAS BROKEN. Rows were only ever written after a whole (keyword, tab)
sweep returned. A Facebook People sweep for a common name runs to its
fifteen-minute budget, so an analyst watching the grid saw nothing for
fifteen minutes and then everything at once -- and if the session died at
minute fourteen, everything it had already read went with it.

Both halves of the plumbing existed and neither was connected. Facebook's
engine has had an `on_progress` callback since it was written; X's had no
such parameter at all; and `discovery/runner.py` called
`disc.sweep(keyword, tab)` with no callback either way, so Facebook's was
dead code and X's did not exist.

THE THREE PROPERTIES THAT MAKE STREAMING SAFE, all pinned here:

  * counts stay exact -- a profile streamed mid-sweep and then present in
    the finished sweep's own hits must be counted once, not twice;
  * order is preserved -- what streams is the platform's own top-to-bottom
    ranking, because that ordering is the analyst's review order;
  * the cap still holds -- streaming must never save a row the finished,
    capped sweep would have trimmed away.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from backend.discovery import runner as R
from backend.shared.models.row import Row

PLATFORM = "twitter"


@dataclass
class FakeSweep:
    hits: list = field(default_factory=list)
    complete: bool = True
    stopped: str = "exhausted"
    error: str = ""
    source: str = "graphql"
    schema: dict = field(default_factory=dict)
    resolved_visits: int = 0
    resolve_seconds: float = 0.0


def _row(name: str) -> Row:
    return Row(url=f"https://x.com/{name}", target=name, original_feed="",
               status="OK", profile_name=name)


class FakePool:
    def __init__(self, plat) -> None:
        self.plat = plat

    async def session_for_job(self, platform_id: str, *, wait_s: float = 0.0):
        return self.plat, {"id": "a", "identifier": "acct-a",
                           "cookies": [{"name": "c"}], "last_ok": 1.0}

    def release_claim(self, platform_id, session_id) -> None:
        pass

    async def mark_session_failed(self, platform_id, session_id, reason="expired", detail=""):
        pass

    async def mark_session_ok(self, platform_id, session_id) -> None:
        pass

    def cookie_saver(self, platform_id, session_id):
        return None

    def proven_fresh(self, session_item) -> bool:
        return True


def _wire(monkeypatch, *, streams: bool, batches: list[list[str]], final: list[str],
          cap: int = 0):
    """`batches` are what the engine hands back DURING the sweep; `final` is
    what its finished Sweep reports. They overlap in practice -- the last
    page streamed is also in the final list -- which is exactly the
    double-count this has to get right."""
    saved: list[list[str]] = []

    class StreamingDiscoverer:
        def __init__(self, options, ctx, anonymous: bool = False):
            self.options = options

        async def sweep(self, keyword: str, tab: str, on_progress=None):
            for batch in batches:
                await asyncio.sleep(0)
                if on_progress is not None:
                    await on_progress(len(batch), 1, [_row(n) for n in batch])
            return FakeSweep(hits=[_row(n) for n in final])

    class PlainDiscoverer:
        def __init__(self, options, ctx, anonymous: bool = False):
            self.options = options

        async def sweep(self, keyword: str, tab: str):
            await asyncio.sleep(0)
            return FakeSweep(hits=[_row(n) for n in final])

    class FakeSession:
        def __init__(self, options, cookies, session_id: str = ""):
            self.ctx = "ctx-a"
            self.on_cookies = None

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def check_session(self) -> bool:
            return True

        async def sync_cookies(self) -> None:
            pass

        async def pause(self, mult: float = 1.0) -> None:
            pass

    class FakePlatform:
        session_path = "fake:Session"
        uses_api_key = False
        env_keys = ()

        def discoverer(self):
            return StreamingDiscoverer if streams else PlainDiscoverer

        def session_cls(self):
            return FakeSession

    pool = FakePool(FakePlatform())
    for name in ("session_for_job", "release_claim", "mark_session_failed",
                 "mark_session_ok", "cookie_saver", "proven_fresh"):
        monkeypatch.setattr(R.sessions_engine, name, getattr(pool, name))

    async def _save_many(client_id, plat_id, phase, rows):
        saved.append([r["url"].rsplit("/", 1)[-1] for r in rows])
        return len(rows), len(rows)

    monkeypatch.setattr(R.profiles_db, "save_many", _save_many)
    monkeypatch.setattr(R.avatar_cache, "spawn", lambda *a, **kw: None)
    monkeypatch.setattr(R, "WORKER_STAGGER_SEC", 0.0)
    monkeypatch.setattr(R, "TAB_STAGGER_SEC", 0.0)
    monkeypatch.setitem(R._PLATFORM_INTER_KEYWORD_DELAY, PLATFORM, 0.0)
    monkeypatch.setattr(R.settings, "discovery_sequential_keywords", True)

    class _Ledger:
        async def plan(self, *a, **kw):
            return 0

        async def record(self, *a, **kw):
            return None

        async def miss(self, *a, **kw):
            return 0

        async def owed(self, *a, **kw):
            return []

    for name in ("plan", "record", "miss", "owed"):
        monkeypatch.setattr(R.coverage_db, name, getattr(_Ledger(), name))
    return saved


def _job() -> R.DiscoveryJob:
    plan = [R.kw_groups.KeywordPlan(
        search="acme", kw_type="individual",
        targets=(R.kw_groups.MatchTarget(parent="acme", terms=("acme",)),))]
    job = R.DiscoveryJob(id="job1", group_id="c1", keyword_plan=plan,
                         tabs={PLATFORM: ["people"]})
    job.platforms[PLATFORM] = R.PlatformSweep(
        platform=PLATFORM, display_name="X", keywords_total=1)
    return job


async def _sweep(job, cap: int = 0):
    await R.DiscoveryRunner()._sweep_platform(
        job, PLATFORM, max_results=cap, max_seconds=None,
        platform_limits_individual={}, platform_limits_domain={},
        platform_tab_limits={},
    )
    return job.platforms[PLATFORM]


class TestResultsArriveWhileTheSweepRuns:
    @pytest.mark.asyncio
    async def test_each_page_is_saved_as_it_is_found(self, monkeypatch):
        saved = _wire(
            monkeypatch, streams=True,
            batches=[["a", "b"], ["c", "d"]],
            final=["a", "b", "c", "d"])

        await _sweep(_job())

        # Two saves during the sweep, not one at the end -- and the final
        # save adds nothing, because everything was already delivered.
        assert saved == [["a", "b"], ["c", "d"]]

    @pytest.mark.asyncio
    async def test_a_profile_seen_twice_is_counted_once(self, monkeypatch):
        """The double-count this could so easily have introduced. Every
        streamed row is also in the finished sweep's own hits, so counting
        both would report twice the profiles that exist."""
        saved = _wire(
            monkeypatch, streams=True,
            batches=[["a", "b"], ["c"]],
            final=["a", "b", "c"])

        prog = await _sweep(_job())

        assert [u for batch in saved for u in batch] == ["a", "b", "c"]
        assert prog.found == 3
        assert prog.new == 3

    @pytest.mark.asyncio
    async def test_anything_streaming_missed_is_still_saved_at_the_end(
        self, monkeypatch,
    ):
        """Streaming is an optimisation, never the only path. A row the
        engine only resolves after its loop ends -- Facebook backfills ids
        it saw rendered but never parsed -- still has to land."""
        saved = _wire(
            monkeypatch, streams=True,
            batches=[["a", "b"]],
            final=["a", "b", "backfilled"])

        prog = await _sweep(_job())

        assert saved == [["a", "b"], ["backfilled"]]
        assert prog.found == 3

    @pytest.mark.asyncio
    async def test_order_is_the_platforms_own_ranking(self, monkeypatch):
        """Top to bottom, as the platform ranked it. That ordering IS the
        analyst's review order -- the grid sorts by insertion -- so a
        streamed row landing out of sequence would reshuffle page one."""
        saved = _wire(
            monkeypatch, streams=True,
            batches=[["first", "second"], ["third"]],
            final=["first", "second", "third"])

        await _sweep(_job())

        assert [u for batch in saved for u in batch] == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_an_engine_that_cannot_stream_is_untouched(self, monkeypatch):
        """Four of the six engines return before streaming would matter.
        They must keep working exactly as before -- the runner asks the
        signature rather than keeping a list of platform names."""
        saved = _wire(
            monkeypatch, streams=False, batches=[], final=["a", "b", "c"])

        prog = await _sweep(_job())

        assert saved == [["a", "b", "c"]]
        assert prog.found == 3


class TestStreamingCanNeverLoseAResult:
    """THE BUG THE FIRST LIVE SWEEP FOUND, and the reason it was invisible
    in every test before it.

    YouTube's engine hands its progress callback raw `Hit` objects rather
    than the `Row`s the finished sweep returns. Converting one raised -- and
    every engine's progress callback swallows exceptions by contract, so
    that a broken caller can never abort a scrape. The rows had already been
    added to `delivered`, so the post-sweep save skipped them as duplicates.
    Result: five channels found, five reported, none written.

    The fix is ordering. Nothing is marked delivered until it is actually
    saved, so a failure in the streaming path costs a little freshness and
    never a row.
    """

    @pytest.mark.asyncio
    async def test_a_streaming_save_that_fails_still_lands_at_the_end(
        self, monkeypatch,
    ):
        saved = _wire(monkeypatch, streams=True,
                      batches=[["a", "b"]], final=["a", "b", "c"])

        calls = {"n": 0}
        real_save = R.profiles_db.save_many

        async def _flaky(client_id, plat_id, phase, rows):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("row conversion blew up")
            return await real_save(client_id, plat_id, phase, rows)

        monkeypatch.setattr(R.profiles_db, "save_many", _flaky)

        prog = await _sweep(_job())

        # The streamed batch failed, so nothing was marked delivered -- and
        # the post-sweep save wrote all three, exactly as it would for an
        # engine that never streamed.
        assert [u for batch in saved for u in batch] == ["a", "b", "c"]
        assert prog.found == 3

    @pytest.mark.asyncio
    async def test_counts_are_not_doubled_on_an_engine_that_cannot_stream(
        self, monkeypatch,
    ):
        """The other half of the same live sweep: Instagram reported ten
        hits for five profiles, because the per-sweep tally was seeded with
        what streaming banked, overwritten by the post-sweep save, and then
        had that save added to it a second time."""
        _wire(monkeypatch, streams=False, batches=[], final=["a", "b", "c", "d", "e"])
        job = _job()

        prog = await _sweep(job)

        assert prog.found == 5
        assert [h.hits_found for h in job.history] == [5]
