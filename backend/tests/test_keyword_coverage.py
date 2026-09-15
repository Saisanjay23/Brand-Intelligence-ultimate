"""Every keyword an analyst saved gets searched on every platform -- and
when one cannot be, it is named rather than lost.

THE REQUIREMENT. This is brand-impersonation monitoring. A permutation that
was never searched is an impersonator nobody looked for, and the client is
told the sweep finished. So the one unacceptable outcome is a keyword that
was never searched being indistinguishable from a keyword that was searched
and found nothing -- and that has to hold across a queue of ten clients run
one after another, where each client's saved keywords are loaded when its
turn comes up and the job that sweeps them is gone an hour later.

WHY AGGREGATE PROGRESS COULD NOT PROVIDE IT. `keywords_done` vs
`keywords_total` says how many cells went unswept and nothing about which.
The queue that knew lived in one `DiscoveryJob` in an evicting in-memory
store, and the scheduler sequencing the clients is JavaScript in a browser
tab that a refresh ends. Every way a platform can stop early discarded that
queue:

  * the session pool ran dry mid-platform (`_MAX_CLAIM_ROUNDS` spent);
  * a keyword took down `_MAX_KEYWORD_ATTEMPTS` sessions and was dropped;
  * a platform had no usable session at all and was skipped whole;
  * a tab sweep raised inside the concurrent gather and was logged only;
  * Telegram's FloodWait hard-stopped the platform;
  * the analyst pressed Stop.

Every one is a legitimate thing to do. None is legitimate to do silently.

WHAT IS PINNED HERE. Each of those paths writes the exact (platform, tab,
keyword) cells it abandoned to the coverage ledger, every cell is declared
before any sweeping starts, a clean run leaves nothing owed, and a
gap-closing run sweeps only what is owed -- down to the individual Facebook
tab, because re-sweeping people and pages to recover groups is three live
searches to redo work already done.

The ledger is faked here rather than hit for real: these tests are about
what the RUNNER records, and `coverage_repository` swallows its own write
failures by design (bookkeeping must never take a sweep down), so a real
repository would let every assertion below pass by writing nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from backend.discovery import runner as R
from backend.shared import resilience
from backend.shared.models.row import Row

FATAL = "checkpoint detected"  # what classify_failure() reads as a dead session


# --------------------------------------------------------------- fake ledger


class FakeLedger:
    """The coverage ledger, in a dict, with the real one's semantics.

    `state` is keyed exactly as `coverage_repository.cell_id` keys it, and
    `owed_now` applies the same rule the real `owed()` query does -- a cell
    is owed until something actually put its term into a search box.
    """

    OWED = ("", R.coverage_db.MISSED, resilience.BROKEN)

    def __init__(self, owed_cells: list[dict] | None = None, read_error: Exception | None = None):
        # (platform, tab, kw_type, search_lower) -> (outcome, reason)
        self.state: dict[tuple, tuple[str, str]] = {}
        self.planned: set[tuple] = set()
        self.calls: list[str] = []
        self._owed = owed_cells or []
        self._read_error = read_error

    @staticmethod
    def _key(platform: str, tab: str, kw_type: str, search: str) -> tuple:
        return (platform, tab, kw_type, (search or "").strip().lower())

    async def plan(self, group_id, job_id, platform, tabs, plans):
        self.calls.append(f"plan:{platform}")
        for p in plans:
            for tab in tabs:
                key = self._key(platform, tab, p.kw_type, p.search)
                self.planned.add(key)
                self.state.setdefault(key, ("", ""))
        return len(self.planned)

    async def record(self, group_id, job_id, platform, tab, kw_type, search,
                     outcome, stop="", found=0, new=0):
        self.calls.append(f"record:{platform}/{tab}/{search}")
        self.state[self._key(platform, tab, kw_type, search)] = (outcome, stop)

    async def miss(self, group_id, job_id, platform, tabs, plans, reason):
        self.calls.append(f"miss:{platform}")
        for p in plans:
            for tab in tabs:
                self.state[self._key(platform, tab, p.kw_type, p.search)] = (
                    R.coverage_db.MISSED, reason)
        return 0

    async def owed(self, group_id, platforms=None):
        if self._read_error:
            raise self._read_error
        return list(self._owed)

    # ---------------------------------------------------------- assertions

    def owed_now(self) -> set[tuple]:
        """Every cell still needing a search, by (platform, tab, term)."""
        return {
            (p, t, term) for (p, t, _kt, term), (outcome, _r) in self.state.items()
            if outcome in self.OWED
        }

    def reason_for(self, platform: str, tab: str, search: str) -> str:
        return self.state.get(self._key(platform, tab, "individual", search), ("", ""))[1]

    def searched(self) -> set[tuple]:
        return {
            (p, t, term) for (p, t, _kt, term), (outcome, _r) in self.state.items()
            if outcome not in self.OWED
        }


# --------------------------------------------------------------------- fakes


@dataclass
class FakeSweep:
    hits: list = field(default_factory=list)
    complete: bool = True
    stopped: str = "exhausted"
    error: str = ""
    resolved_visits: int = 0
    resolve_seconds: float = 0.0


class FakePool:
    """Exclusive-until-released, marked-failed-stays-dead -- the two
    properties `_claim_sessions` depends on. Same contract as the pool fake
    in test_discovery_parallel.py."""

    def __init__(self, sessions: list[dict], plat) -> None:
        self.sessions = sessions
        self.plat = plat
        self.claimed: set[str] = set()
        self.dead: set[str] = set()

    async def session_for_job(self, platform_id: str, *, wait_s: float = 0.0):
        for s in self.sessions:
            if s["id"] not in self.claimed and s["id"] not in self.dead:
                self.claimed.add(s["id"])
                return self.plat, dict(s)
        raise RuntimeError(f"{platform_id}: no healthy sessions available")

    def release_claim(self, platform_id: str, session_id: str) -> None:
        self.claimed.discard(session_id)

    async def mark_session_failed(self, platform_id, session_id, reason="expired", detail=""):
        self.dead.add(session_id)

    async def mark_session_ok(self, platform_id, session_id) -> None:
        pass

    def cookie_saver(self, platform_id, session_id):
        return None

    def proven_fresh(self, session_item) -> bool:
        return True


def _wire(
    monkeypatch, platform: str, sessions: list[dict],
    fail_keywords: dict[str, set[str]] | None = None,
    crash_tabs: set[str] | None = None,
    ledger: FakeLedger | None = None,
) -> FakeLedger:
    """`fail_keywords` maps session id -> keywords whose sweep kills that
    session. `crash_tabs` names tabs whose sweep raises a non-session error
    from OUTSIDE the engine's own catch -- the concurrent-gather crash that
    used to lose a cell without a trace."""
    fail_keywords = fail_keywords or {}
    crash_tabs = crash_tabs or set()
    ledger = ledger or FakeLedger()

    class FakeDiscoverer:
        def __init__(self, options, ctx, anonymous: bool = False):
            self.session_id = str(ctx or "").removeprefix("ctx-")

        async def sweep(self, keyword: str, tab: str):
            await asyncio.sleep(0)
            if keyword in fail_keywords.get(self.session_id, set()):
                raise RuntimeError(FATAL)
            if tab in crash_tabs:
                # Raised where `_sweep_tab` does NOT catch it, which is what
                # made this the one loss with no record at all.
                raise BrokenPipeError("renderer went away")
            row = Row(url=f"https://x/{keyword}-{tab}", target=keyword,
                      original_feed="", status="OK", profile_name=keyword)
            return FakeSweep(hits=[row])

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

    class FakePlatform:
        session_path = "fake:Session"
        uses_api_key = False
        env_keys = ()

        def discoverer(self):
            return FakeDiscoverer

        def session_cls(self):
            return FakeSession

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
    monkeypatch.setitem(R._PLATFORM_INTER_KEYWORD_DELAY, platform, 0.0)
    monkeypatch.setattr(R.settings, "discovery_sequential_keywords", True)
    # The ledger is the subject under test, not a dependency to be exercised.
    for name in ("plan", "record", "miss", "owed"):
        monkeypatch.setattr(R.coverage_db, name, getattr(ledger, name))
    return ledger


def _session(sid: str) -> dict:
    return {"id": sid, "identifier": f"acct-{sid}", "cookies": [{"name": "c"}], "last_ok": 1.0}


def _job(keywords: list[str], platform: str, tabs: list[str],
         only_owed: bool = False) -> R.DiscoveryJob:
    plan = [
        R.kw_groups.KeywordPlan(
            search=kw, kw_type="individual",
            targets=(R.kw_groups.MatchTarget(parent=kw, terms=(kw,)),),
        )
        for kw in keywords
    ]
    job = R.DiscoveryJob(
        id="job1", group_id="client1", keyword_plan=plan,
        tabs={platform: tabs}, only_owed=only_owed)
    job.platforms[platform] = R.PlatformSweep(
        platform=platform, display_name=platform.title(),
        keywords_total=len(plan) * len(tabs))
    return job


async def _sweep(job, platform: str):
    await R.DiscoveryRunner()._sweep_platform(
        job, platform, max_results=5, max_seconds=None,
        platform_limits_individual={}, platform_limits_domain={},
        platform_tab_limits={},
    )
    return job.platforms[platform]


# ------------------------------------------------- the clean case, first


class TestAHealthyRunOwesNothing:
    @pytest.mark.asyncio
    async def test_every_keyword_on_every_tab_is_recorded_searched(self, monkeypatch):
        ledger = _wire(monkeypatch, "facebook", [_session("a")])
        job = _job(["kw0", "kw1"], "facebook", ["people", "pages", "groups"])

        prog = await _sweep(job, "facebook")

        assert prog.status == "done"
        assert ledger.owed_now() == set()
        # Two keywords across three tabs is six searches, and all six are on
        # record as having happened.
        assert len(ledger.searched()) == 6

    @pytest.mark.asyncio
    async def test_the_whole_plan_is_declared_before_any_sweeping(self, monkeypatch):
        """The declaration is what makes an abandoned job legible. A job
        that dies in its first second still has to leave behind a complete
        statement of what it was supposed to do -- otherwise "nothing was
        recorded" and "nothing was owed" look the same."""
        ledger = _wire(monkeypatch, "facebook", [_session("a")])
        job = _job(["kw0", "kw1"], "facebook", ["people", "pages"])

        await _sweep(job, "facebook")

        assert len(ledger.planned) == 4
        assert ledger.calls[0] == "plan:facebook"
        assert not any(c.startswith("record") for c in ledger.calls[:1])


# ------------------------------------ every way a sweep can end early


class TestNothingIsLostWhenTheSessionPoolRunsDry:
    @pytest.mark.asyncio
    async def test_keywords_no_session_ever_reached_are_named(self, monkeypatch):
        """THE HEADLINE CASE. One account, and it dies on the first
        keyword. `_MAX_CLAIM_ROUNDS` finds no replacement, so kw0 goes
        back on the queue for a session that never arrives and kw1/kw2
        were never put into a search box by anybody. All three used to
        leave a count and a shrug; they now leave their names, and the
        reason recorded against them is the platform's own diagnosis
        rather than a generic one."""
        ledger = _wire(
            monkeypatch, "twitter", [_session("a")],
            fail_keywords={"a": {"kw0"}})
        job = _job(["kw0", "kw1", "kw2"], "twitter", ["people"])

        prog = await _sweep(job, "twitter")

        assert prog.status in ("failed", "partial")
        assert ledger.owed_now() == {
            ("twitter", "people", "kw0"),
            ("twitter", "people", "kw1"),
            ("twitter", "people", "kw2"),
        }
        assert "session checkpointed" in ledger.reason_for("twitter", "people", "kw1")

    @pytest.mark.asyncio
    async def test_a_keyword_that_kills_every_session_is_owed_by_name(self, monkeypatch):
        """The give-up path. `bad` exhausts `_MAX_KEYWORD_ATTEMPTS` and
        leaves the queue for good, so `_abandon`'s sweep of what remains
        can never see it -- it is the one route by which a configured
        keyword could go unsearched and unrecorded."""
        ledger = _wire(
            monkeypatch, "twitter", [_session("a"), _session("b")],
            fail_keywords={"a": {"bad"}, "b": {"bad"}})
        monkeypatch.setattr(R.settings, "discovery_sequential_keywords", False)
        job = _job(["ok0", "bad", "ok1"], "twitter", ["people"])

        await _sweep(job, "twitter")

        assert ("twitter", "people", "bad") in ledger.owed_now()
        assert "every available session" in ledger.reason_for("twitter", "people", "bad")
        # And the keywords that DID work are not dragged down with it.
        assert ("twitter", "people", "ok0") in ledger.searched()

    @pytest.mark.asyncio
    async def test_a_tab_that_crashes_in_the_gather_is_owed_not_lost(self, monkeypatch):
        """A tab sweep raising outside the engine's own catch produced no
        sweep record, no counter movement and no ledger write -- it
        vanished into the gap between `keywords_done` and `keywords_total`
        with a single log line as its only trace. It is simply absent from
        the item's `resolved` set, which the backstop turns into an owed
        cell like any other."""
        ledger = _wire(
            monkeypatch, "facebook", [_session("a")], crash_tabs={"groups"})
        monkeypatch.setattr(R.settings, "discovery_sequential_keywords", False)
        monkeypatch.setattr(R.settings, "discovery_tab_concurrency", 3)
        job = _job(["kw0"], "facebook", ["people", "pages", "groups"])

        await _sweep(job, "facebook")

        assert ("facebook", "groups", "kw0") in ledger.owed_now()
        # The two tabs that worked are still on record as having worked.
        assert ("facebook", "people", "kw0") in ledger.searched()
        assert ("facebook", "pages", "kw0") in ledger.searched()

    @pytest.mark.asyncio
    async def test_a_cancel_records_what_it_interrupted(self, monkeypatch):
        """Stop is a legitimate instruction and its cost is legitimate
        too. What it must not do is leave the interrupted keywords looking
        swept -- resuming this client should pick up where the analyst
        interrupted it."""
        ledger = _wire(monkeypatch, "twitter", [_session("a")])
        job = _job(["kw0", "kw1", "kw2"], "twitter", ["people"])
        job.cancel.set()

        prog = await _sweep(job, "twitter")

        # Never `done`. No sweep failed, so the old incomplete-count test
        # said this platform was clean -- about three keywords nobody
        # searched.
        assert prog.status == "partial"
        assert "unsearched" in prog.note
        assert ledger.owed_now() == {
            ("twitter", "people", "kw0"),
            ("twitter", "people", "kw1"),
            ("twitter", "people", "kw2"),
        }
        assert "cancelled" in ledger.reason_for("twitter", "people", "kw0")


# ----------------------------------------------- closing the gaps cheaply


class TestAGapClosingRunSweepsOnlyWhatIsOwed:
    @pytest.mark.asyncio
    async def test_it_narrows_to_the_owed_tabs_of_the_owed_keywords(self, monkeypatch):
        """Down to the individual tab. Re-sweeping people and pages to
        recover groups is two extra live searches per keyword to redo work
        already done -- on a fifteen-keyword client that is the difference
        between a short pass and a full re-run."""
        ledger = FakeLedger(owed_cells=[
            {"kw_type": "individual", "search": "kw1", "tab": "groups"},
        ])
        _wire(monkeypatch, "facebook", [_session("a")], ledger=ledger)
        job = _job(["kw0", "kw1", "kw2"], "facebook",
                   ["people", "pages", "groups"], only_owed=True)

        prog = await _sweep(job, "facebook")

        searched = {(t, s) for _p, t, s in ledger.searched()}
        assert searched == {("groups", "kw1")}
        # One search, not nine -- and the progress total says so rather
        # than reporting eight sweeps as missing.
        assert prog.keywords_total == 1
        assert prog.keywords_done == 1

    @pytest.mark.asyncio
    async def test_a_fully_covered_platform_spends_no_session(self, monkeypatch):
        """Nothing owed means nothing to do, and the session not claimed is
        the whole point -- a gap-closing lap over ten clients must not cost
        ten logins to discover there is nothing to fix."""
        ledger = FakeLedger(owed_cells=[])
        _wire(monkeypatch, "twitter", [_session("a")], ledger=ledger)
        job = _job(["kw0", "kw1"], "twitter", ["people"], only_owed=True)

        prog = await _sweep(job, "twitter")

        assert prog.status == "done"
        assert "nothing owed" in prog.note
        assert not any(c.startswith("record") for c in ledger.calls)

    @pytest.mark.asyncio
    async def test_an_unreadable_ledger_sweeps_everything_rather_than_nothing(
        self, monkeypatch,
    ):
        """THE FAILURE DIRECTION MATTERS. Sweeping work already done is
        wasteful. Concluding "nothing is owed" from a database that could
        not be read would be the exact false clean bill of health this
        ledger exists to prevent, so the fallback is the whole plan."""
        ledger = FakeLedger(read_error=RuntimeError("mongo unreachable"))
        _wire(monkeypatch, "twitter", [_session("a")], ledger=ledger)
        job = _job(["kw0", "kw1"], "twitter", ["people"], only_owed=True)

        prog = await _sweep(job, "twitter")

        assert prog.status == "done"
        assert len(ledger.searched()) == 2


# ------------------------------------------------ a platform never tried


class TestASkippedPlatformTakesEveryKeywordWithIt:
    @pytest.mark.asyncio
    async def test_it_records_every_keyword_it_could_not_sweep(self, monkeypatch):
        """The loudest version of the silence. One platform with no usable
        session took a client's whole keyword list with it and left a
        single "skipped" chip as the only evidence -- on a queue of ten
        clients, ten times over, with nothing afterwards able to say which
        searches never happened."""
        ledger = FakeLedger()
        for name in ("plan", "record", "miss", "owed"):
            monkeypatch.setattr(R.coverage_db, name, getattr(ledger, name))

        runner = R.DiscoveryRunner()

        async def _no_client(group_id):
            return None

        async def _readiness(only=None):
            return [], {"facebook": "no usable session -- add cookies under /sessions"}

        monkeypatch.setattr(runner, "_client", _no_client)
        monkeypatch.setattr(runner, "platform_readiness", _readiness)

        job, skipped = await runner.start(
            group_id="client1",
            individual_keywords=["kw0", "kw1"],
            domain_keywords=[],
        )

        assert skipped == {"facebook": "no usable session -- add cookies under /sessions"}
        owed = ledger.owed_now()
        assert ("facebook", "people", "kw0") in owed
        assert ("facebook", "people", "kw1") in owed
        assert "no usable session" in ledger.reason_for("facebook", "people", "kw0")
        assert job.platforms["facebook"].status == "skipped"
