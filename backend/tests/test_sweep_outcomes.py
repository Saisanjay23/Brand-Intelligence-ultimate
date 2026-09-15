"""A sweep's ending, and what the analyst is told about it.

WHAT BROKE. Every discovery engine sets `Sweep.complete = True` on exactly
one thing -- it paged or scrolled until the platform ran out of results --
and `_sweep_platform` read that one boolean as the answer to a completely
different question: "is anything wrong here". So a sweep that stopped
because it had collected exactly the `max_results` the analyst configured
was pooled together with a dead parser, a geoblock and a blown time budget,
and all of them surfaced as the same sentence:

    "4 sweep(s) did not run to completion"

Two costs. The sentence named no reason (the per-sweep `stopped` code was
recorded and then dropped by the aggregation), so it was un-actionable; and
because the commonest cause of it by far was a cap doing precisely its job,
it fired on healthy runs constantly, which is how a warning stops being
read. The scheduler's "still owing" badge reads the same `partial` status,
so a capped client was also permanently owing work it had already done.

WHAT THIS PINS. `shared/resilience.py::sweep_outcome` splits an ending into
satisfied / truncated / broken, and `_PlatformSweepRun` counts and words
them separately. The properties worth protecting are: a met result cap is a
COMPLETE answer and says nothing at all; anything else always names its
reason; and an ending nobody has taught the classifier about is loud rather
than silent.

Testable without a browser for the same reason the other discovery suites
are (see test_discovery_parallel.py): this is all decided against a
discoverer interface of one method, `sweep(keyword, tab)`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from backend.discovery import runner as R
from backend.shared import resilience
from backend.shared.models.row import Row

PLATFORM = "twitter"  # single tab, so one keyword is exactly one sweep
TAB = "people"


# --------------------------------------------------------------------- fakes


@dataclass
class FakeSweep:
    hits: list = field(default_factory=list)
    complete: bool = True
    stopped: str = ""
    error: str = ""
    resolved_visits: int = 0
    resolve_seconds: float = 0.0


class FakePool:
    """Only the three calls an always-healthy single-session sweep makes.
    Nothing here ever fails: every test in this module is about how a
    SWEEP ended, never about how a session did -- that half is already
    covered in test_discovery_parallel.py."""

    def __init__(self, plat) -> None:
        self.plat = plat

    async def session_for_job(self, platform_id: str, *, wait_s: float = 0.0):
        return self.plat, {"id": "a", "identifier": "acct-a",
                           "cookies": [{"name": "c"}], "last_ok": 1.0}

    def release_claim(self, platform_id: str, session_id: str) -> None:
        pass

    async def mark_session_failed(self, platform_id, session_id, reason="expired", detail=""):
        pass

    async def mark_session_ok(self, platform_id, session_id) -> None:
        pass

    def cookie_saver(self, platform_id, session_id):
        return None

    def proven_fresh(self, session_item) -> bool:
        return True


def _wire(monkeypatch, endings: dict[str, FakeSweep | Exception]):
    """`endings` maps keyword -> the sweep it ends on (or the exception it
    raises). Scripting the ENDING rather than the hits is the whole point:
    every test here differs only in how a sweep finished."""

    class FakeDiscoverer:
        def __init__(self, options, ctx, anonymous: bool = False):
            pass

        async def sweep(self, keyword: str, tab: str):
            await asyncio.sleep(0)
            ending = endings[keyword]
            if isinstance(ending, Exception):
                raise ending
            # One hit on every sweep, so `found` can never be the thing
            # that makes a platform look healthy or unhealthy here.
            row = Row(url=f"https://x.com/{keyword}", target=keyword,
                      original_feed="", status="OK", profile_name=keyword)
            return FakeSweep(
                hits=[row], complete=ending.complete,
                stopped=ending.stopped, error=ending.error)

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
            return FakeDiscoverer

        def session_cls(self):
            return FakeSession

    pool = FakePool(FakePlatform())
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
    return pool


def _job(keywords: list[str]) -> R.DiscoveryJob:
    plan = [
        R.kw_groups.KeywordPlan(
            search=kw, kw_type="individual",
            targets=(R.kw_groups.MatchTarget(parent=kw, terms=(kw,)),),
        )
        for kw in keywords
    ]
    job = R.DiscoveryJob(id="job1", group_id="client1", keyword_plan=plan)
    job.platforms[PLATFORM] = R.PlatformSweep(
        platform=PLATFORM, display_name="X", keywords_total=len(plan))
    return job


async def _sweep(job) -> R.PlatformSweep:
    await R.DiscoveryRunner()._sweep_platform(
        job, PLATFORM, max_results=5, max_seconds=None,
        platform_limits_individual={}, platform_limits_domain={},
        platform_tab_limits={},
    )
    return job.platforms[PLATFORM]


# ------------------------------------------- a met cap is a complete answer


class TestAResultCapIsNotAFailure:
    @pytest.mark.asyncio
    async def test_a_platform_that_only_hit_its_result_cap_is_done_and_silent(
        self, monkeypatch,
    ):
        """THE HEADLINE REGRESSION. `cap:results` means the sweep stopped
        because it had collected exactly what it was asked for. That is
        the definition of a complete answer, and it used to be reported as
        "2 sweep(s) did not run to completion"."""
        _wire(monkeypatch, {
            "kw0": FakeSweep(complete=False, stopped="cap:results"),
            "kw1": FakeSweep(complete=False, stopped="cap:results"),
        })

        prog = await _sweep(_job(["kw0", "kw1"]))

        assert prog.status == "done"
        assert prog.note == ""

    @pytest.mark.asyncio
    async def test_exhausted_and_no_results_are_equally_clean(self, monkeypatch):
        """A keyword nobody matches is a real answer, not a broken sweep."""
        _wire(monkeypatch, {
            "kw0": FakeSweep(complete=True, stopped="exhausted"),
            "kw1": FakeSweep(complete=False, stopped="no-results"),
        })

        prog = await _sweep(_job(["kw0", "kw1"]))

        assert prog.status == "done"
        assert prog.note == ""


# --------------------------------------------- everything else names itself


class TestEveryOtherEndingSaysWhy:
    @pytest.mark.asyncio
    async def test_a_time_budget_is_partial_and_says_so_in_words(self, monkeypatch):
        _wire(monkeypatch, {
            "kw0": FakeSweep(complete=False, stopped="cap:seconds"),
            "kw1": FakeSweep(complete=False, stopped="cap:seconds"),
            "kw2": FakeSweep(complete=True, stopped="exhausted"),
        })

        prog = await _sweep(_job(["kw0", "kw1", "kw2"]))

        assert prog.status == "partial"
        assert prog.note == "2 sweep(s) ran out of time budget"
        # The bare count that said nothing is gone for good.
        assert "did not run to completion" not in prog.note

    @pytest.mark.asyncio
    async def test_mixed_reasons_lead_with_the_commonest(self, monkeypatch):
        """An analyst reading one line gets the dominant cause first."""
        _wire(monkeypatch, {
            "kw0": FakeSweep(complete=False, stopped="stalled"),
            "kw1": FakeSweep(complete=False, stopped="cap:seconds"),
            "kw2": FakeSweep(complete=False, stopped="cap:seconds"),
        })

        prog = await _sweep(_job(["kw0", "kw1", "kw2"]))

        assert prog.status == "partial"
        assert prog.note == (
            "3 sweep(s) stopped early -- 2 ran out of time budget, "
            "1 stalled with no new results")

    @pytest.mark.asyncio
    async def test_a_real_error_message_still_leads(self, monkeypatch):
        """A platform's own words beat any category this module invents,
        so they stay first and the breakdown follows in brackets."""
        _wire(monkeypatch, {
            "kw0": FakeSweep(
                complete=False, stopped="geoblocked",
                error="TikTok is blocked for this IP -- route via a proxy."),
        })

        prog = await _sweep(_job(["kw0"]))

        assert prog.status == "partial"
        assert prog.note == (
            "TikTok is blocked for this IP -- route via a proxy. "
            "(1 sweep(s) were geoblocked for this IP)")

    @pytest.mark.asyncio
    async def test_a_raised_exception_is_quoted_not_just_counted(self, monkeypatch):
        """A sweep that raised used to increment the counter and go
        nowhere near `sweep_errors` -- so the ONE failure that always had
        a real message attached was the one whose message was dropped.
        `RuntimeError` deliberately carries no session-shaped token, so
        `classify_failure` leaves the pool alone and this stays a sweep
        failure rather than becoming a session failure."""
        _wire(monkeypatch, {"kw0": RuntimeError("parser returned no edges")})

        prog = await _sweep(_job(["kw0"]))

        assert prog.status == "partial"
        assert "parser returned no edges" in prog.note


# ------------------------------------------------ the classifier's own edges


class TestTheClassifierFailsLoud:
    def test_an_unknown_stop_code_is_broken_not_silently_fine(self):
        """A code this module has never been taught is either a new
        failure mode or a new engine. Both are better surfaced once than
        swallowed forever, so the default is BROKEN, not satisfied."""
        assert resilience.sweep_outcome("some-new-engine-thing", False) == resilience.BROKEN
        assert resilience.describe_stop("some-new-engine-thing") == (
            "stopped on 'some-new-engine-thing'")

    def test_an_engine_that_sets_complete_without_a_code_still_reads_clean(self):
        assert resilience.sweep_outcome("", True) == resilience.SATISFIED

    def test_http_codes_are_described_rather_than_echoed(self):
        assert resilience.sweep_outcome("http-403", False) == resilience.BROKEN
        assert resilience.describe_stop("http-403") == "got HTTP 403"

    def test_a_cancel_is_a_budget_decision_not_a_breakage(self):
        """The analyst pressing Stop says nothing is wrong -- and the
        platform-level cancel branch reports the cancel itself anyway."""
        assert resilience.sweep_outcome("cancelled", False) == resilience.TRUNCATED

    def test_summarise_is_deterministic_on_ties(self):
        """This string is diffed by eye across runs and asserted on above,
        so equal counts order by code rather than by dict insertion."""
        counts = {"stalled": 2, "cap:pages": 2}
        assert (resilience.summarise_stops(counts)
                == resilience.summarise_stops({"stalled": 2, "cap:pages": 2}))
        assert resilience.summarise_stops(counts) == (
            "4 sweep(s) stopped early -- 2 hit the page limit, "
            "2 stalled with no new results")

    def test_nothing_to_report_is_an_empty_string(self):
        assert resilience.summarise_stops({}) == ""
        assert resilience.summarise_stops({"stalled": 0}) == ""


class TestTheCountersStayInStepWithTheNote:
    def _run(self) -> R._PlatformSweepRun:
        return R._PlatformSweepRun(
            platform_id=PLATFORM, queue=R.deque(), tabs=[TAB], max_results=0,
            max_seconds=None, platform_limits={}, platform_tab_limits={},
            prog=R.PlatformSweep(platform=PLATFORM, display_name="X"),
        )

    def test_satisfied_sweeps_move_no_counter(self):
        run = self._run()
        assert run.record_stop("cap:results", False) == resilience.SATISFIED
        assert run.record_stop("exhausted", True) == resilience.SATISFIED
        assert (run.truncated, run.broken, run.incomplete) == (0, 0, 0)
        assert run.note() == ""

    def test_truncated_and_broken_are_counted_apart(self):
        run = self._run()
        run.record_stop("cap:seconds", False)
        run.record_stop("stalled", False)
        assert (run.truncated, run.broken) == (1, 1)
        # `incomplete` is what still decides partial-vs-done, so splitting
        # the counters changed what is REPORTED, never which platforms are.
        assert run.incomplete == 2

    def test_the_same_error_text_is_never_repeated(self):
        """`sweep_errors` is joined into a one-line chip: ten sweeps
        failing identically must read once, not ten times."""
        run = self._run()
        for _ in range(10):
            run.record_stop("geoblocked", False, "blocked for this IP")
        assert run.sweep_errors == ["blocked for this IP"]
        assert run.note() == (
            "blocked for this IP (10 sweep(s) were geoblocked for this IP)")

    def test_a_giant_error_is_trimmed_before_it_reaches_the_chip(self):
        """A Playwright timeout's full text is hundreds of characters of
        selector, which would push the actual diagnosis off the end."""
        run = self._run()
        run.record_stop("error", False, "boom " * 200)
        assert len(run.sweep_errors[0]) <= 160
