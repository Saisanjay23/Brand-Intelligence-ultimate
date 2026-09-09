"""One keyword at a time, every tab in order, each under its own cap.

WHAT BROKE, AND WHY IT LOOKED LIKE TWO SEPARATE BUGS. `_sweep_platform`
claims up to `discovery_max_parallel_sessions` sessions and hands each one
the shared keyword queue, so a client with two or three pooled Facebook
accounts had two or three keywords in flight at once. From the analyst's
side that reads as "my keywords are not being searched one by one":

  * the progress chip carries ONE `current_keyword`/`current_tab` pair, so
    with N workers writing to it, it names whichever coroutine touched it
    last -- a sweep working perfectly looks like it is jumping around.
  * the discovery grid sorts by ascending `_id` (insertion order) precisely
    so page 1 is what the platform's own search returned first. Interleaved
    writes from N workers destroy that for every keyword involved.

`discovery_sequential_keywords` (default on) is the single switch that
makes both properties true, and it has to outrank BOTH knobs that can
reintroduce overlap -- the session count and `discovery_tab_concurrency` --
which is what most of this module pins.

WHY THIS IS TESTABLE WITHOUT A BROWSER: same reasoning as
test_discovery_parallel.py. Everything here is SCHEDULING (which session,
which keyword, which tab, under which cap) decided against a discoverer
interface of one method, `sweep(keyword, tab)`. The fake below records the
cap it was handed AT SWEEP TIME rather than at construction, which is the
only way to catch a per-tab cap that is resolved correctly and then not
actually applied to the sweep that needed it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from backend.discovery import runner as R
from backend.platforms.facebook.discovery_engine import capped_hits, rank_hits
from backend.shared.models.hit import Hit
from backend.shared.models.row import Row

PLATFORM = "facebook"  # the only platform with more than one tab
TABS = ["people", "pages", "groups"]
FATAL = "checkpoint detected"


@dataclass
class FakeSweep:
    hits: list = field(default_factory=list)
    complete: bool = True
    stopped: str = ""
    error: str = ""
    resolved_visits: int = 0
    resolve_seconds: float = 0.0


class FakePool:
    """Exclusive-until-released, and marked-failed-stays-dead -- the two
    properties `_claim_sessions` actually depends on."""

    def __init__(self, sessions: list[dict], plat) -> None:
        self.sessions = sessions
        self.plat = plat
        self.claimed: set[str] = set()
        self.dead: set[str] = set()
        self.marked_failed: list[tuple[str, str]] = []

    async def session_for_job(self, platform_id: str):
        for s in self.sessions:
            if s["id"] not in self.claimed and s["id"] not in self.dead:
                self.claimed.add(s["id"])
                return self.plat, dict(s)
        raise RuntimeError(f"{platform_id}: no healthy sessions available")

    def release_claim(self, platform_id: str, session_id: str) -> None:
        self.claimed.discard(session_id)

    async def mark_session_failed(self, platform_id, session_id, reason="expired", detail=""):
        self.dead.add(session_id)
        self.marked_failed.append((session_id, reason))

    async def mark_session_ok(self, platform_id, session_id) -> None:
        pass

    def cookie_saver(self, platform_id, session_id):
        return None

    def proven_fresh(self, session_item) -> bool:
        return True


class Trace:
    """What actually happened, in the order it happened."""

    def __init__(self) -> None:
        # (keyword, tab, cap_at_sweep_time, session_id)
        self.sweeps: list[tuple[str, str, int, str]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    @property
    def order(self) -> list[tuple[str, str]]:
        return [(k, t) for k, t, _, _ in self.sweeps]

    def caps(self) -> dict[tuple[str, str], int]:
        return {(k, t): c for k, t, c, _ in self.sweeps}


def _discoverer_class(trace: Trace, fail_keywords: dict[str, set[str]]):
    class FakeDiscoverer:
        def __init__(self, options, ctx, anonymous: bool = False):
            self.options = options
            self.session_id = str(ctx or "").removeprefix("ctx-")

        async def sweep(self, keyword: str, tab: str) -> FakeSweep:
            # READ THE CAP HERE, NOT IN __init__. The sequential path
            # mutates one shared options object between tabs
            # (`options.max_results = cap`) and reuses one discoverer, so a
            # cap read at construction time would be the same number for
            # all three tabs and would pass no matter what the code did.
            cap = int(getattr(self.options, "max_results", 0) or 0)
            trace.in_flight += 1
            trace.max_in_flight = max(trace.max_in_flight, trace.in_flight)
            try:
                # Two real suspension points, so genuinely concurrent
                # sweeps WOULD overlap here and be caught by max_in_flight.
                # Without them the assertion would hold trivially.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                trace.sweeps.append((keyword, tab, cap, self.session_id))
                if keyword in fail_keywords.get(self.session_id, set()):
                    raise RuntimeError(FATAL)
                row = Row(url=f"https://facebook.com/{keyword}-{tab}", target=keyword,
                          original_feed="", status="OK", profile_name=keyword)
                return FakeSweep(hits=[row])
            finally:
                trace.in_flight -= 1

    return FakeDiscoverer


def _session_class():
    class FakeSession:
        def __init__(self, options, cookies, session_id: str = ""):
            self.session_id = session_id
            self.on_cookies = None
            self.ctx = f"ctx-{session_id}"

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

    return FakeSession


def _session(sid: str) -> dict:
    return {"id": sid, "identifier": f"acct-{sid}", "cookies": [{"name": "c"}], "last_ok": 1.0}


def _wire(monkeypatch, sessions: list[dict], fail_keywords: dict[str, set[str]] | None = None):
    trace = Trace()
    disc_cls = _discoverer_class(trace, fail_keywords or {})
    sess_cls = _session_class()

    class FakePlatform:
        session_path = "fake:Session"
        uses_api_key = False
        env_keys = ()

        def discoverer(self):
            return disc_cls

        def session_cls(self):
            return sess_cls

    pool = FakePool(sessions, FakePlatform())
    for name in ("session_for_job", "release_claim", "mark_session_failed",
                 "mark_session_ok", "cookie_saver", "proven_fresh"):
        monkeypatch.setattr(R.sessions_engine, name, getattr(pool, name))

    async def _save_many(client_id, plat_id, phase, rows):
        return len(rows), len(rows)

    monkeypatch.setattr(R.profiles_db, "save_many", _save_many)
    monkeypatch.setattr(R.avatar_cache, "spawn", lambda *a, **kw: None)
    monkeypatch.setattr(R, "WORKER_STAGGER_SEC", 0.0)
    monkeypatch.setattr(R, "TAB_STAGGER_SEC", 0.0)
    monkeypatch.setitem(R._PLATFORM_INTER_KEYWORD_DELAY, PLATFORM, 0.0)
    monkeypatch.setattr(R.settings, "discovery_sequential_keywords", True)
    return pool, trace


def _job(keywords: list[str]) -> R.DiscoveryJob:
    plan = [(kw, "individual") for kw in keywords]
    job = R.DiscoveryJob(id="job1", group_id="client1", keyword_plan=plan)
    job.platforms[PLATFORM] = R.PlatformSweep(
        platform=PLATFORM, display_name="Facebook",
        keywords_total=len(plan) * len(TABS),
    )
    return job


async def _sweep(job, *, max_results: int = 0, tab_limits: dict | None = None,
                 type_limits: dict | None = None):
    await R.DiscoveryRunner()._sweep_platform(
        job, PLATFORM, max_results=max_results, max_seconds=None,
        platform_limits_individual=type_limits or {},
        platform_limits_domain={},
        platform_tab_limits=tab_limits or {},
    )


# --------------------------------------------------- one keyword at a time


class TestKeywordsRunOneByOne:
    @pytest.mark.asyncio
    async def test_every_keyword_is_swept_on_every_tab_exactly_once(self, monkeypatch):
        _, trace = _wire(monkeypatch, [_session("s1"), _session("s2"), _session("s3")])
        job = _job(["alpha", "beta", "gamma"])
        await _sweep(job)
        assert sorted(trace.order) == sorted(
            (kw, tab) for kw in ("alpha", "beta", "gamma") for tab in TABS)
        assert job.platforms[PLATFORM].keywords_done == 9

    @pytest.mark.asyncio
    async def test_the_order_is_keyword_then_people_pages_groups(self, monkeypatch):
        _, trace = _wire(monkeypatch, [_session("s1"), _session("s2"), _session("s3")])
        await _sweep(_job(["alpha", "beta"]))
        assert trace.order == [
            ("alpha", "people"), ("alpha", "pages"), ("alpha", "groups"),
            ("beta", "people"), ("beta", "pages"), ("beta", "groups"),
        ]

    @pytest.mark.asyncio
    async def test_no_two_sweeps_are_ever_in_flight_at_once(self, monkeypatch):
        """The property the progress chip and the grid's insertion order
        both depend on. `max_in_flight` is sampled INSIDE the fake sweep,
        across two await points, so overlap is observed rather than
        inferred from the call order."""
        _, trace = _wire(monkeypatch, [_session("s1"), _session("s2"), _session("s3")])
        await _sweep(_job(["alpha", "beta", "gamma"]))
        assert trace.max_in_flight == 1

    @pytest.mark.asyncio
    async def test_only_one_session_is_claimed_however_many_are_healthy(self, monkeypatch):
        _, trace = _wire(monkeypatch, [_session("s1"), _session("s2"), _session("s3")])
        await _sweep(_job(["alpha", "beta", "gamma"]))
        assert len({sid for _, _, _, sid in trace.sweeps}) == 1

    @pytest.mark.asyncio
    async def test_raising_tab_concurrency_does_not_take_it_back(self, monkeypatch):
        """`discovery_tab_concurrency` is the other knob that can put three
        sweeps in flight for one keyword. The sequential switch has to
        outrank it, or tuning throughput silently breaks the ordering."""
        _, trace = _wire(monkeypatch, [_session("s1")])
        monkeypatch.setattr(R.settings, "discovery_tab_concurrency", 3)
        await _sweep(_job(["alpha", "beta"]))
        assert trace.max_in_flight == 1
        assert trace.order == [
            ("alpha", "people"), ("alpha", "pages"), ("alpha", "groups"),
            ("beta", "people"), ("beta", "pages"), ("beta", "groups"),
        ]

    def test_the_session_count_is_pinned_to_one_for_every_platform(self, monkeypatch):
        monkeypatch.setattr(R.settings, "discovery_sequential_keywords", True)
        monkeypatch.setattr(R.settings, "discovery_max_parallel_sessions", 3)
        for pid in ("facebook", "twitter", "instagram", "tiktok"):
            assert R._sessions_wanted(pid, 10) == 1

    def test_turning_it_off_restores_the_parallel_pool(self, monkeypatch):
        """The switch is a policy, not a deletion -- the multi-session path
        is still there for anyone who wants throughput over ordering."""
        monkeypatch.setattr(R.settings, "discovery_sequential_keywords", False)
        monkeypatch.setattr(R.settings, "discovery_max_parallel_sessions", 3)
        assert R._sessions_wanted("facebook", 10) == 3


# ------------------------------------------------------------------- caps


class TestEachTabSweepsUnderItsOwnCap:
    @pytest.mark.asyncio
    async def test_a_per_tab_cap_reaches_the_sweep_that_needs_it(self, monkeypatch):
        _, trace = _wire(monkeypatch, [_session("s1")])
        await _sweep(_job(["alpha"]), tab_limits={PLATFORM: {
            "people": {"individual": 25},
            "pages": {"individual": 10},
            "groups": {"individual": 5},
        }})
        assert trace.caps() == {
            ("alpha", "people"): 25,
            ("alpha", "pages"): 10,
            ("alpha", "groups"): 5,
        }

    @pytest.mark.asyncio
    async def test_the_cap_does_not_leak_from_one_tab_into_the_next(self, monkeypatch):
        """The sequential path reuses ONE discoverer over ONE options
        object, reassigning `max_results` before each tab. A tab whose cap
        is unset must fall back to the blanket `max_results` -- not inherit
        whatever the previous tab was capped at."""
        _, trace = _wire(monkeypatch, [_session("s1")])
        await _sweep(_job(["alpha"]), max_results=100,
                     tab_limits={PLATFORM: {"people": {"individual": 3}}})
        caps = trace.caps()
        assert caps[("alpha", "people")] == 3
        assert caps[("alpha", "pages")] == 100
        assert caps[("alpha", "groups")] == 100

    @pytest.mark.asyncio
    async def test_the_most_restrictive_configured_cap_wins(self, monkeypatch):
        _, trace = _wire(monkeypatch, [_session("s1")])
        await _sweep(_job(["alpha"]), max_results=50,
                     type_limits={PLATFORM: 20},
                     tab_limits={PLATFORM: {"groups": {"individual": 5}}})
        caps = trace.caps()
        assert caps[("alpha", "people")] == 20   # blanket 50 vs per-type 20
        assert caps[("alpha", "pages")] == 20
        assert caps[("alpha", "groups")] == 5    # per-tab beats both

    @pytest.mark.asyncio
    async def test_nothing_configured_means_uncapped_not_zero_results(self, monkeypatch):
        _, trace = _wire(monkeypatch, [_session("s1")])
        await _sweep(_job(["alpha"]))
        assert set(trace.caps().values()) == {0}

    @pytest.mark.asyncio
    async def test_every_keyword_gets_the_same_caps_not_just_the_first(self, monkeypatch):
        """A cap resolved once and reused would pass every single-keyword
        test above while still being wrong for keyword 2 onwards."""
        _, trace = _wire(monkeypatch, [_session("s1")])
        await _sweep(_job(["alpha", "beta"]), tab_limits={PLATFORM: {
            "people": {"individual": 7}, "pages": {"individual": 4},
            "groups": {"individual": 2},
        }})
        caps = trace.caps()
        for kw in ("alpha", "beta"):
            assert caps[(kw, "people")] == 7
            assert caps[(kw, "pages")] == 4
            assert caps[(kw, "groups")] == 2


# --------------------------------------------------------------- failover


class TestFailoverSurvivesTheSingleWorker:
    @pytest.mark.asyncio
    async def test_a_replacement_session_finishes_what_the_dead_one_held(self, monkeypatch):
        """One worker is the whole point, but it must not mean one chance.
        Recovery is `_sweep_platform`'s claim-round loop, not the worker
        count -- so a session dying on keyword 2 still leaves the job
        complete, on a different account."""
        pool, trace = _wire(monkeypatch, [_session("s1"), _session("s2")],
                            fail_keywords={"s1": {"beta"}})
        job = _job(["alpha", "beta", "gamma"])
        await _sweep(job)
        assert pool.marked_failed and pool.marked_failed[0][0] == "s1"
        swept = {(k, t) for k, t, _, _ in trace.sweeps}
        assert swept == {(kw, tab) for kw in ("alpha", "beta", "gamma") for tab in TABS}
        assert job.platforms[PLATFORM].status == "done"

    @pytest.mark.asyncio
    async def test_the_retried_keyword_is_not_counted_twice(self, monkeypatch):
        _wire(monkeypatch, [_session("s1"), _session("s2")],
              fail_keywords={"s1": {"beta"}})
        job = _job(["alpha", "beta", "gamma"])
        await _sweep(job)
        prog = job.platforms[PLATFORM]
        assert prog.keywords_done == prog.keywords_total == 9


# --------------------------------------- which results a cap actually keeps


def _hit(eid: str, source: str = "graphql") -> Hit:
    return Hit(entity_id=eid, name=f"n{eid}", url=f"https://facebook.com/{eid}",
               source=source)


class TestTheCapKeepsTheTopResults:
    """A cap of N has to mean "the N results Facebook ranked highest", not
    "some N of them". These pin `capped_hits`, the last of the engine's
    three cap checks and the only one that can still be crossed -- the
    backfill pass adds rows AFTER the scroll loop has already stopped at
    the limit."""

    def test_it_returns_exactly_the_cap(self):
        hits = [_hit(str(i)) for i in range(50)]
        assert len(capped_hits(hits, 12)) == 12

    def test_it_keeps_facebooks_own_order(self):
        hits = [_hit(str(i)) for i in range(10)]
        assert [h.entity_id for h in capped_hits(hits, 4)] == ["0", "1", "2", "3"]

    def test_a_cap_of_zero_means_uncapped_not_empty(self):
        hits = [_hit(str(i)) for i in range(7)]
        assert len(capped_hits(hits, 0)) == 7

    def test_fewer_results_than_the_cap_are_all_kept(self):
        hits = [_hit(str(i)) for i in range(3)]
        assert len(capped_hits(hits, 12)) == 3

    def test_a_backfill_is_what_the_cap_sheds_first(self):
        """A graphql edge is something Facebook ranked and showed; a
        backfill is a row we reconstructed for an id it rendered but never
        gave us an edge for. When only one can survive, keep the former."""
        hits = [_hit("b1", "id-backfill"), _hit("g1"), _hit("b2", "id-backfill"),
                _hit("g2")]
        assert [h.entity_id for h in capped_hits(hits, 2)] == ["g1", "g2"]

    def test_backfills_still_fill_the_room_a_cap_leaves(self):
        hits = [_hit("b1", "id-backfill"), _hit("g1"), _hit("g2")]
        assert [h.entity_id for h in capped_hits(hits, 3)] == ["g1", "g2", "b1"]

    def test_promoting_backfills_does_not_reshuffle_the_rest(self):
        """`sorted` is stable, so the ONLY reordering is confirmed-ahead-of-
        backfilled. Everything else keeps the insertion order that carries
        Facebook's top-to-bottom ranking."""
        hits = [_hit(str(i)) for i in range(6)]
        assert [h.entity_id for h in rank_hits(hits)] == ["0", "1", "2", "3", "4", "5"]

    def test_the_rank_field_is_not_used_because_it_restarts_per_page(self):
        """Page 2's first result also carries rank 0. Sorting by it would
        interleave pages -- the bug this ordering exists to avoid."""
        page1 = [_hit("a"), _hit("b")]
        page2 = [_hit("c"), _hit("d")]
        for i, h in enumerate(page1):
            h.rank = i
        for i, h in enumerate(page2):
            h.rank = i
        assert [h.entity_id for h in rank_hits(page1 + page2)] == ["a", "b", "c", "d"]
