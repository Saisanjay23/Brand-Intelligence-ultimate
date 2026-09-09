"""Several sessions on one platform's keyword sweep: how the plan is split,
and what happens to a keyword whose session dies underneath it.

Mirrors test_analysis_parallel.py's approach and its two pinned defects,
one level up (keywords instead of URLs, `discoverer.sweep()` instead of
`scraper.one()`):

WHY THIS IS TESTABLE WITHOUT A BROWSER. What changed in discovery/runner.py
is SCHEDULING, not extraction: which session sweeps which keyword, what
happens to a keyword a dead session was holding, and whether the counters
(`keywords_done`, `found`, `new`) survive a keyword being swept twice. All
of that is decided against a `discoverer` interface of one method
(`sweep(keyword, tab)`) and a `session` interface of four, so fakes over
those test the real decisions.

THE TWO DEFECTS THESE EXIST TO PIN DOWN:

  double counting   a re-queued keyword had already added its
                    keywords_done/found/new to the shared, platform-wide
                    counters. Adding them again on the retry walks the
                    progress bar past 100% and inflates found/new for
                    profiles counted twice -- `_requeue_keyword` gives back
                    exactly what the failed attempt added (a RELATIVE
                    subtraction, not a snapshot reset, since a sibling
                    worker's OWN keyword is incrementing those same shared
                    counters at the same moment).

  one account,      two workers must be two ACCOUNTS. A claim that handed
  twice             the same pooled session to both would be one identity
                    driven twice as hard while reporting as two.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from backend.discovery import runner as R
from backend.shared.models.row import Row

PLATFORM = "twitter"  # single tab, matches _MAX_SESSIONS_PER_PLATFORM's default cap
FATAL = "checkpoint detected"  # what classify_failure() reads as a dead session


# THIS WHOLE MODULE TESTS THE OPT-IN PATH. `discovery_sequential_keywords`
# ships ON (see config/settings.py for why one-keyword-at-a-time is a
# correctness property, not a pacing preference), and it pins every platform
# to a single worker -- which is exactly what the multi-session behaviour
# below cannot be observed through. Turned off here for every test in the
# file so these keep testing the machinery they were written for; the
# guarantee that the shipped default really is sequential is pinned
# separately, in test_discovery_sequential_and_caps.py.
@pytest.fixture(autouse=True)
def _parallel_path(monkeypatch):
    monkeypatch.setattr(R.settings, "discovery_sequential_keywords", False)


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
    """Same contract as analysis's FakePool -- see test_analysis_parallel.py
    for the reasoning behind exclusivity-until-released and
    marked-failed-stays-dead."""

    def __init__(self, sessions: list[dict], plat) -> None:
        self.sessions = sessions
        self.plat = plat
        self.claimed: set[str] = set()
        self.dead: set[str] = set()
        self.marked_failed: list[tuple[str, str]] = []
        self.released: list[str] = []

    async def session_for_job(self, platform_id: str, *, wait_s: float = 0.0):
        # `wait_s` is accepted and ignored: a real pool waits out a BUSY
        # account so discovery and analysis can share a one-account platform
        # (see sessions/manager.py::get_healthy_session). Nothing here is
        # ever transiently busy -- a session is claimed or it is not -- so
        # waiting would only add real seconds to a scheduling test.
        for s in self.sessions:
            if s["id"] not in self.claimed and s["id"] not in self.dead:
                self.claimed.add(s["id"])
                return self.plat, dict(s)
        raise RuntimeError(
            f"{platform_id}: no healthy sessions available -- please add more cookies")

    def release_claim(self, platform_id: str, session_id: str) -> None:
        if session_id:
            self.released.append(session_id)
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


def _discoverer_class(fail_keywords: dict[str, set[str]], live: dict):
    """A Discoverer over the one method a worker actually calls.
    `fail_keywords` maps a session id to the keywords that session raises a
    session-fatal error on. `live` tracks concurrently-open discoverers so
    the process-wide worker cap can be observed."""

    class FakeDiscoverer:
        instances: list["FakeDiscoverer"] = []

        def __init__(self, options, ctx, anonymous: bool = False):
            self.options = options
            self.ctx = ctx
            self.anonymous = anonymous
            # The real discoverer classes never receive a session id
            # directly either -- they only get the browser context that is
            # already tied to one (see `_keyword_worker`'s
            # `make_discoverer = lambda o, _s=session: plat_obj.discoverer()(o, _s.ctx)`).
            # FakeSession below sets `ctx = f"ctx-{session_id}"`, so this
            # recovers the same identity a real discoverer would get for
            # free just by which context it was handed.
            self.session_id = str(ctx or "").removeprefix("ctx-")
            self.seen: list[str] = []
            FakeDiscoverer.instances.append(self)

        async def sweep(self, keyword: str, tab: str) -> FakeSweep:
            self.seen.append(keyword)
            await asyncio.sleep(0)  # let sibling workers genuinely interleave
            if keyword in fail_keywords.get(self.session_id, set()):
                raise RuntimeError(FATAL)
            row = Row(url=f"https://x.com/{keyword}", target=keyword, original_feed="",
                      status="OK", profile_name=keyword)
            return FakeSweep(hits=[row], complete=True)

    return FakeDiscoverer


def _session_class(live: dict):
    class FakeSession:
        def __init__(self, options, cookies, session_id: str = ""):
            self.session_id = session_id
            self.on_cookies = None
            self.ctx = f"ctx-{session_id}"
            self.started = False

        async def start(self) -> None:
            self.started = True
            live["now"] = live.get("now", 0) + 1
            live["max"] = max(live.get("max", 0), live["now"])

        async def stop(self) -> None:
            live["now"] = live.get("now", 0) - 1

        async def check_session(self) -> bool:
            return True

        async def sync_cookies(self) -> None:
            pass

        async def pause(self, mult: float = 1.0) -> None:
            # Recorded, not slept: the real one is jittered/fatigued
            # (stealth/human.py) and this suite is testing WHEN a gap is
            # taken and how big it was asked to be, not human.py's shaping.
            live.setdefault("pauses", []).append(mult)

    return FakeSession


def _session(sid: str) -> dict:
    return {"id": sid, "identifier": f"acct-{sid}", "cookies": [{"name": "auth"}],
            "last_ok": 1.0}


def _wire(monkeypatch, sessions: list[dict], fail_keywords: dict[str, set[str]] | None = None,
          platform: str = PLATFORM, pace: bool = False):
    live: dict = {}
    disc_cls = _discoverer_class(fail_keywords or {}, live)
    sess_cls = _session_class(live)

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
        return len(rows), len(rows)  # every hit is "new" -- simplest honest fake

    def _spawn(*a, **kw):
        return None

    monkeypatch.setattr(R.profiles_db, "save_many", _save_many)
    monkeypatch.setattr(R.avatar_cache, "spawn", _spawn)
    monkeypatch.setitem(R.PLATFORM_TABS, platform, ["people"])
    monkeypatch.setattr(R.settings, "discovery_tab_concurrency", 1)
    # A real sweep takes real seconds, so a 2s stagger between workers is
    # proportionally small. This fake sweep returns on the next tick --
    # against that, an un-zeroed stagger would let worker A drain the
    # entire queue before worker B's sleep ever returns, which would make
    # every distribution test flaky on timing rather than testing
    # distribution at all.
    monkeypatch.setattr(R, "WORKER_STAGGER_SEC", 0.0)
    if not pace:
        # Off by default for the same reason as the worker stagger: these
        # tests are about scheduling, and a real inter-keyword gap would
        # only make them slow. TestInterKeywordPacing turns it back on.
        monkeypatch.setitem(R._PLATFORM_INTER_KEYWORD_DELAY, platform, 0.0)
    return pool, disc_cls, live


def _job(keywords: list[str], platform: str = PLATFORM) -> R.DiscoveryJob:
    plan = [(kw, "individual") for kw in keywords]
    job = R.DiscoveryJob(id="job1", group_id="client1", keyword_plan=plan)
    job.platforms[platform] = R.PlatformSweep(
        platform=platform, display_name="X (Twitter)", keywords_total=len(plan),
    )
    return job


async def _sweep(job, platform=PLATFORM):
    await R.DiscoveryRunner()._sweep_platform(
        job, platform, max_results=0, max_seconds=None,
        platform_limits_individual={}, platform_limits_domain={}, platform_tab_limits={},
    )


# ------------------------------------------------------- how many, and why


class TestHowManySessionsAreWorthClaiming:
    def test_a_full_plan_takes_the_default_cap(self, monkeypatch):
        monkeypatch.setattr(R.settings, "discovery_max_parallel_sessions", 3)
        assert R._sessions_wanted(PLATFORM, 10) == 3

    def test_it_never_claims_more_sessions_than_keywords(self):
        assert R._sessions_wanted(PLATFORM, 1) == 1
        assert R._sessions_wanted(PLATFORM, 2) <= 2

    def test_youtube_is_pinned_to_one_because_its_key_has_no_second_identity(self):
        assert R._sessions_wanted("youtube", 10) == 1

    def test_telegram_is_pinned_to_one_because_of_the_session_file_lock(self):
        assert R._sessions_wanted("telegram", 10) == 1

    def test_the_cap_is_independently_tunable_from_analysiss(self, monkeypatch):
        monkeypatch.setattr(R.settings, "discovery_max_parallel_sessions", 1)
        assert R._sessions_wanted(PLATFORM, 10) == 1



# ----------------------------------------------------------- the split itself


class TestOneSession:
    @pytest.mark.asyncio
    async def test_it_sweeps_every_keyword_exactly_once(self, monkeypatch):
        """The single-session pool is not a special path -- it is this code
        with one worker -- so it has to stay exactly as complete as it
        was."""
        _, disc_cls, _ = _wire(monkeypatch, [_session("a")])
        keywords = [f"kw{n}" for n in range(4)]
        job = _job(keywords)

        await _sweep(job, PLATFORM)

        assert len(disc_cls.instances) == 1
        assert disc_cls.instances[0].seen == keywords
        prog = job.platforms[PLATFORM]
        assert prog.keywords_done == prog.keywords_total == 4
        assert prog.found == prog.new == 4
        assert job.found == job.new == 4
        assert prog.status == "done"

    @pytest.mark.asyncio
    async def test_it_sweeps_them_in_order(self, monkeypatch):
        _, disc_cls, _ = _wire(monkeypatch, [_session("a")])
        keywords = [f"kw{n}" for n in range(5)]
        job = _job(keywords)
        await _sweep(job, PLATFORM)
        assert disc_cls.instances[0].seen == keywords


class TestTwoSessions:
    @pytest.mark.asyncio
    async def test_the_plan_is_split_between_them(self, monkeypatch):
        _, disc_cls, _ = _wire(
            monkeypatch, [_session("a"), _session("b")])
        keywords = [f"kw{n}" for n in range(6)]
        job = _job(keywords)

        await _sweep(job, PLATFORM)

        assert len(disc_cls.instances) == 2
        seen = [d.seen for d in disc_cls.instances]
        assert all(s for s in seen), f"one worker did no work at all: {seen}"
        assert sorted(seen[0] + seen[1]) == sorted(keywords)
        assert len(seen[0] + seen[1]) == 6, "a keyword was swept by both workers"
        prog = job.platforms[PLATFORM]
        assert prog.keywords_done == prog.keywords_total == 6
        assert prog.status == "done"

    @pytest.mark.asyncio
    async def test_each_worker_gets_its_own_account(self, monkeypatch):
        _, disc_cls, _ = _wire(
            monkeypatch, [_session("a"), _session("b")])
        job = _job([f"kw{n}" for n in range(6)])
        await _sweep(job, PLATFORM)
        ids = {d.session_id for d in disc_cls.instances}
        ctxs = {d.ctx for d in disc_cls.instances}
        assert ids == {"a", "b"}
        assert ctxs == {"ctx-a", "ctx-b"}

    @pytest.mark.asyncio
    async def test_the_progress_chip_reports_the_worker_count_while_running(self, monkeypatch):
        seen_workers: list[int] = []
        _, disc_cls, _ = _wire(
            monkeypatch, [_session("a"), _session("b")])
        job = _job([f"kw{n}" for n in range(6)])

        orig_sweep = disc_cls.sweep

        async def _spy(self, keyword, tab):
            seen_workers.append(job.platforms[PLATFORM].workers)
            return await orig_sweep(self, keyword, tab)

        disc_cls.sweep = _spy
        await _sweep(job, PLATFORM)

        assert max(seen_workers) == 2, "the chip never reported 2 workers while they ran"
        assert job.platforms[PLATFORM].workers == 0, "left nonzero after the sweep finished"

    @pytest.mark.asyncio
    async def test_every_claimed_session_is_released(self, monkeypatch):
        pool, _, _ = _wire(
            monkeypatch, [_session("a"), _session("b")])
        job = _job([f"kw{n}" for n in range(6)])
        await _sweep(job, PLATFORM)
        assert pool.claimed == set(), "a session stayed claimed and is now invisible to every job"



# --------------------------------------------------------------- failover


class TestASessionDyingMidSweep:
    @pytest.mark.asyncio
    async def test_the_healthy_session_finishes_what_the_dead_one_was_holding(self, monkeypatch):
        keywords = [f"kw{n}" for n in range(6)]
        pool, disc_cls, _ = _wire(
            monkeypatch,
            [_session("a"), _session("b")],
            fail_keywords={"a": set(keywords)},
        )
        job = _job(keywords)

        await _sweep(job, PLATFORM)

        prog = job.platforms[PLATFORM]
        assert prog.keywords_done == prog.keywords_total == 6, (
            "a dead session still cost the sweep keywords")
        assert prog.status == "done"
        healthy = [d for d in disc_cls.instances if d.session_id == "b"][0]
        assert sorted(healthy.seen) == sorted(keywords)
        assert ("a", "checkpointed") in pool.marked_failed

    @pytest.mark.asyncio
    async def test_a_retried_keyword_is_not_counted_twice(self, monkeypatch):
        """Its first attempt already added its keywords_done/found/new to
        the shared counters. Adding them again on the retry walks the bar
        past 100% and inflates found/new for a profile counted twice."""
        keywords = [f"kw{n}" for n in range(6)]
        _wire(monkeypatch, [_session("a"), _session("b")],
              fail_keywords={"a": set(keywords)})
        job = _job(keywords)

        await _sweep(job, PLATFORM)

        prog = job.platforms[PLATFORM]
        assert prog.keywords_done == prog.keywords_total == 6
        assert prog.found == prog.new == 6
        assert job.found == job.new == 6

    @pytest.mark.asyncio
    async def test_a_replacement_session_is_claimed_when_the_only_worker_dies(self, monkeypatch):
        """The replacement-claim round, reached when every claimed
        second session can only be reached by the replacement round --
        which is the case the round exists for."""
        keywords = ["kw0", "kw1", "kw2"]
        pool, disc_cls, _ = _wire(
            monkeypatch,
            [_session("a"), _session("b")],
            fail_keywords={"a": {"kw1"}},
        )
        job = _job(keywords)

        await _sweep(job, PLATFORM)

        assert [d.session_id for d in disc_cls.instances] == ["a", "b"]
        prog = job.platforms[PLATFORM]
        assert prog.keywords_done == prog.keywords_total == 3
        assert prog.status == "done"


class TestAKeywordThatKillsEverySession:
    @pytest.mark.asyncio
    async def test_it_is_retried_once_and_then_left_as_is(self, monkeypatch):
        """Ambiguous by nature: usually the session died, sometimes the
        keyword is what trips the challenge. Both sessions here share an
        Both sessions are claimed up front now, so "bad" is attempted on
        one and then re-queued onto the other."""
        keywords = ["ok0", "bad", "ok1"]
        pool, disc_cls, _ = _wire(
            monkeypatch,
            [_session("a"), _session("b")],
            fail_keywords={"a": {"bad"}, "b": {"bad"}},
        )
        job = _job(keywords)

        await _sweep(job, PLATFORM)

        prog = job.platforms[PLATFORM]
        # ok0 and ok1 both genuinely succeeded; "bad" took down both
        # sessions and, capped at _MAX_KEYWORD_ATTEMPTS, its own final
        # (failed) attempt is what accounts for the third unit -- it is
        # counted as ATTEMPTED (matching the original single-session
        # semantics: an exhausted exception-fatal sweep always counted as
        # processed), but flagged incomplete rather than silently "done".
        assert prog.keywords_done == prog.keywords_total == 3
        assert prog.status == "partial"
        assert "bad" in prog.note
        assert [s for s, r in pool.marked_failed] == ["a", "b"]
        assert pool.marked_failed == [("a", "checkpointed"), ("b", "checkpointed")]


class TestWhenNothingCanBeClaimed:
    @pytest.mark.asyncio
    async def test_an_empty_pool_fails_the_platform_with_its_own_message(self, monkeypatch):
        _wire(monkeypatch, [])
        job = _job(["kw0", "kw1"])

        await _sweep(job, PLATFORM)

        prog = job.platforms[PLATFORM]
        assert prog.status == "failed"
        assert "no healthy sessions available" in prog.note
        assert prog.keywords_done == 0

    @pytest.mark.asyncio
    async def test_the_only_session_dying_on_the_first_keyword_reports_failed(self, monkeypatch):
        """Nothing was ever actually completed -- the one attempt that ran
        got rolled back for its retry, and the retry found no replacement
        session. Distinct from the "some keywords done, then everything
        died" case, which is "partial" (see TestAKeywordThatKillsEverySession)."""
        keywords = [f"kw{n}" for n in range(4)]
        _wire(monkeypatch, [_session("a")], fail_keywords={"a": set(keywords)})
        job = _job(keywords)

        await _sweep(job, PLATFORM)

        prog = job.platforms[PLATFORM]
        assert prog.status == "failed"
        assert prog.keywords_done == 0
        assert "session" in prog.note


class TestCancellation:
    @pytest.mark.asyncio
    async def test_a_cancelled_platform_keeps_what_it_read_and_fails_nothing(self, monkeypatch):
        _wire(monkeypatch, [_session("a")])
        job = _job([f"kw{n}" for n in range(4)])
        job.cancel.set()

        await _sweep(job, PLATFORM)

        prog = job.platforms[PLATFORM]
        assert prog.keywords_done == 0
        assert prog.status == "done"


# --------------------------------------------------- the process-wide ceiling


class TestTheProcessWideWorkerCap:
    @pytest.mark.asyncio
    async def test_it_bounds_how_many_browsers_are_open_at_once(self, monkeypatch):
        monkeypatch.setattr(R.settings, "discovery_max_browser_workers", 1)
        _, disc_cls, live = _wire(
            monkeypatch, [_session("a"), _session("b")])
        job = _job([f"kw{n}" for n in range(6)])

        await _sweep(job, PLATFORM)

        assert live["max"] == 1
        assert job.platforms[PLATFORM].keywords_done == 6

    @pytest.mark.asyncio
    async def test_two_workers_are_allowed_when_the_ceiling_permits(self, monkeypatch):
        monkeypatch.setattr(R.settings, "discovery_max_browser_workers", 4)
        _, _, live = _wire(
            monkeypatch, [_session("a"), _session("b")])
        job = _job([f"kw{n}" for n in range(6)])
        await _sweep(job, PLATFORM)
        assert live["max"] == 2

    @pytest.mark.asyncio
    async def test_the_semaphore_belongs_to_the_running_loop(self):
        first = R._worker_semaphore()
        assert R._worker_semaphore() is first
        assert asyncio.get_running_loop() in R._worker_slots


# ------------------------------------------------ real accuracy is unchanged


class TestTheSweepItselfIsUnchanged:
    @pytest.mark.asyncio
    async def test_a_hit_is_saved_through_the_same_row_to_fields_path(self, monkeypatch):
        """Confirms multi-session distribution did not touch WHAT gets
        saved -- only which session did the sweeping."""
        captured: list[dict] = []

        async def _save_many(client_id, plat_id, phase, rows):
            captured.extend(rows)
            return len(rows), len(rows)

        _wire(monkeypatch, [_session("a")])
        monkeypatch.setattr(R.profiles_db, "save_many", _save_many)
        job = _job(["acme"])

        await _sweep(job, PLATFORM)

        assert len(captured) == 1
        assert captured[0]["url"] == "https://x.com/acme"
        assert captured[0]["display_name"] == "acme"


# ------------------------------------------------------- inter-keyword pacing


class TestInterKeywordPacing:
    """The gap between one keyword and the next on a live account. Discovery
    had none: keywords ran back-to-back, which is the request cadence that
    reads as automation on the platform most likely to disable an account."""

    def test_facebook_waits_longest_because_it_costs_the_most(self):
        """Three tabs per keyword plus profile visits to reconcile names."""
        fb = R._PLATFORM_INTER_KEYWORD_DELAY["facebook"]
        assert fb > R._DEFAULT_INTER_KEYWORD_DELAY
        assert fb >= R._PLATFORM_INTER_KEYWORD_DELAY["instagram"]

    def test_the_api_platforms_take_no_gap_at_all(self):
        """Not browser sessions driving a logged-in account -- Telegram has
        FloodWait, YouTube has a metered quota."""
        assert R._PLATFORM_INTER_KEYWORD_DELAY["youtube"] == 0.0
        assert R._PLATFORM_INTER_KEYWORD_DELAY["telegram"] == 0.0

    @pytest.mark.asyncio
    async def test_a_gap_is_taken_between_keywords_but_not_after_the_last(
        self, monkeypatch,
    ):
        """A pause before releasing the session buys nothing and only makes
        the sweep look slower."""
        _, _, live = _wire(monkeypatch, [_session("a")], pace=True)
        monkeypatch.setitem(R._PLATFORM_INTER_KEYWORD_DELAY, PLATFORM, 6.0)
        job = _job(["kw0", "kw1", "kw2"])

        await _sweep(job, PLATFORM)

        assert len(live.get("pauses", [])) == 2, "3 keywords means 2 gaps, not 3"

    @pytest.mark.asyncio
    async def test_the_gap_asks_for_the_configured_number_of_seconds(self, monkeypatch):
        """`pause()` takes a MULTIPLIER on the session's own median, so the
        target seconds have to be converted into one -- a multiplier of 1.0
        would silently be the median, not the number configured here."""
        monkeypatch.setattr(R.settings, "discovery_delay_sec", 2.5)
        _, _, live = _wire(monkeypatch, [_session("a")], pace=True)
        monkeypatch.setitem(R._PLATFORM_INTER_KEYWORD_DELAY, PLATFORM, 10.0)
        job = _job(["kw0", "kw1"])

        await _sweep(job, PLATFORM)

        assert live["pauses"] == [4.0], "10s against a 2.5s median is a 4x multiplier"

    @pytest.mark.asyncio
    async def test_a_zero_gap_platform_never_pauses(self, monkeypatch):
        _, _, live = _wire(monkeypatch, [_session("a")], pace=True)
        monkeypatch.setitem(R._PLATFORM_INTER_KEYWORD_DELAY, PLATFORM, 0.0)
        job = _job(["kw0", "kw1", "kw2"])

        await _sweep(job, PLATFORM)

        assert live.get("pauses", []) == []

    @pytest.mark.asyncio
    async def test_a_cancelled_sweep_does_not_sit_out_its_gap(self, monkeypatch):
        """Cancel should stop promptly, not after one more pacing wait."""
        _, disc_cls, live = _wire(monkeypatch, [_session("a")], pace=True)
        monkeypatch.setitem(R._PLATFORM_INTER_KEYWORD_DELAY, PLATFORM, 6.0)
        job = _job(["kw0", "kw1", "kw2"])

        orig = disc_cls.sweep

        async def _cancel_after_first(self, keyword, tab):
            out = await orig(self, keyword, tab)
            job.cancel.set()
            return out

        disc_cls.sweep = _cancel_after_first
        await _sweep(job, PLATFORM)

        assert live.get("pauses", []) == []


# ------------------------------------------- parallelism is by session count


class TestParallelismIsPurelyBySessionCount:
    """WHAT CHANGED, AND WHY THIS IS PINNED. Claiming used to refuse a second
    session unless it left the host through a different egress, which meant a
    proxy-less pool could never sweep with more than one worker. Proxy support
    has been removed from the tool, so the only question left is how many
    pooled ACCOUNTS a platform has: two sessions means two workers splitting
    the keyword list, regardless of the address they share."""

    @pytest.mark.asyncio
    async def test_two_proxyless_sessions_split_the_keywords(self, monkeypatch):
        _, disc_cls, live = _wire(monkeypatch, [_session("a"), _session("b")])
        keywords = [f"kw{n}" for n in range(6)]
        job = _job(keywords)

        await _sweep(job, PLATFORM)

        assert len(disc_cls.instances) == 2
        assert live["max"] == 2, "both workers must actually be open at once"
        seen = [d.seen for d in disc_cls.instances]
        assert all(s for s in seen), f"one worker did no work: {seen}"
        assert sorted(seen[0] + seen[1]) == sorted(keywords)
        prog = job.platforms[PLATFORM]
        assert prog.keywords_done == prog.keywords_total == 6

    @pytest.mark.asyncio
    async def test_three_sessions_give_three_workers(self, monkeypatch):
        monkeypatch.setattr(R.settings, "discovery_max_parallel_sessions", 3)
        _, disc_cls, _ = _wire(
            monkeypatch, [_session("a"), _session("b"), _session("c")])
        job = _job([f"kw{n}" for n in range(9)])

        await _sweep(job, PLATFORM)

        assert len(disc_cls.instances) == 3
        assert job.platforms[PLATFORM].keywords_done == 9

    @pytest.mark.asyncio
    async def test_one_session_still_sweeps_the_whole_plan(self, monkeypatch):
        _, disc_cls, _ = _wire(monkeypatch, [_session("a")])
        keywords = [f"kw{n}" for n in range(4)]
        job = _job(keywords)
        await _sweep(job, PLATFORM)
        assert len(disc_cls.instances) == 1
        assert disc_cls.instances[0].seen == keywords
