"""Several sessions on one platform: how the batch is split, and what happens
to a URL whose session dies underneath it.

WHY THIS IS TESTABLE WITHOUT A BROWSER. The thing worth testing here is not
how a profile is read -- that is unchanged, and every worker calls the same
`scraper.one()` it always did. What is new is SCHEDULING: which session gets
which URL, what happens to the URLs a dead session was holding, and whether the
counters survive a URL being read twice. All of that is decided in
analysis/runner.py against a `scraper` interface of five methods, so a fake
scraper that records the URLs it was asked for tests the real decisions.

THE TWO DEFECTS THESE EXIST TO PIN DOWN, both found while building this:

  double counting   a re-queued URL had already been counted (and already
                    persisted as an error) by its first attempt. Counting it
                    again on the retry walks the progress bar past 100% --
                    `_requeue` gives the count back, and
                    `test_a_retried_url_is_not_counted_twice` is what keeps it
                    doing that.

  one account,      two workers must be two ACCOUNTS. A claim that handed
  twice             the same pooled session to both would be one identity
                    driven twice as hard while reporting as two -- the
                    opposite of what splitting a batch is for.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.analysis import runner as R
from backend.shared.models.row import Row

PLATFORM = "twitter"
FATAL = "checkpoint detected"  # what classify_failure() reads as a dead session


# --------------------------------------------------------------------- fakes


class FakeInnerSession:
    """The `scraper.session` the worker reaches through for cookie sync."""

    def __init__(self) -> None:
        self.on_cookies = None
        self.syncs = 0

    async def sync_cookies(self) -> None:
        self.syncs += 1


class FakePool:
    """A pooled-session manager with the two behaviours the runner depends on:
    a claim is exclusive until released, and a session marked failed is not
    handed out again. Both matter -- exclusivity is what makes two workers two
    ACCOUNTS, and the second is what makes the replacement-session round
    (`_MAX_CLAIM_ROUNDS`) reach a different session than the one that died."""

    def __init__(self, sessions: list[dict], plat) -> None:
        self.sessions = sessions
        self.plat = plat
        self.claimed: set[str] = set()
        self.dead: set[str] = set()
        self.marked_failed: list[tuple[str, str]] = []
        self.released: list[str] = []

    async def session_for_job(self, platform_id: str):
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


def _scraper_class(fail_urls: dict[str, set[str]], live: dict):
    """A Scraper class over the five methods a worker actually calls.

    `fail_urls` maps a session id to the URLs that session raises a
    session-fatal error on, which is how a test says "this account is the one
    that checkpoints". `live` records how many scrapers are open at once, so
    the process-wide worker cap can be observed rather than assumed."""

    class FakeScraper:
        instances: list["FakeScraper"] = []

        def __init__(self, options, cookies, session_id="", anonymous=False):
            self.options = options
            self.session_id = session_id
            self.anonymous = anonymous
            self.session = FakeInnerSession()
            self.seen: list[str] = []
            self.started = False
            self.stopped = False
            FakeScraper.instances.append(self)

        async def start(self) -> None:
            self.started = True
            live["now"] = live.get("now", 0) + 1
            live["max"] = max(live.get("max", 0), live["now"])

        async def stop(self) -> None:
            self.stopped = True
            live["now"] = live.get("now", 0) - 1

        async def check_session(self) -> bool:
            return True

        async def pause(self, mult: float = 1.0) -> None:
            pass

        async def one(self, url, target, feed, known=None) -> Row:
            self.seen.append(url)
            # yield, so two workers genuinely interleave rather than one
            # draining the whole queue in a single uninterrupted step
            await asyncio.sleep(0)
            if url in fail_urls.get(self.session_id, set()):
                raise RuntimeError(FATAL)
            return Row(url=url, target=target, status="OK", profile_name="Someone")

    return FakeScraper


def _session(sid: str) -> dict:
    return {"id": sid, "identifier": f"acct-{sid}", "cookies": [{"name": "auth"}],
            "last_ok": 1.0}


def _wire(monkeypatch, sessions: list[dict], fail_urls: dict[str, set[str]] | None = None):
    """Point the runner at a fake pool and a fake scraper, and take the pacing
    out of the way (one URL at a time, no inter-chunk pause) so what a test
    asserts is the scheduling and not a sleep."""
    live: dict = {}
    scraper_cls = _scraper_class(fail_urls or {}, live)

    class FakePlatform:
        def scraper(self):
            return scraper_cls

    pool = FakePool(sessions, FakePlatform())
    for name in ("session_for_job", "release_claim", "mark_session_failed",
                 "mark_session_ok", "cookie_saver", "proven_fresh"):
        monkeypatch.setattr(R.sessions_engine, name, getattr(pool, name))

    async def _no_save(*a, **kw):
        return None

    monkeypatch.setattr(R.results_db, "save", _no_save)
    monkeypatch.setitem(R._PLATFORM_CONCURRENCY, PLATFORM, 1)
    monkeypatch.setitem(R._PLATFORM_INTER_BATCH_DELAY, PLATFORM, 0.0)
    return pool, scraper_cls, live


def _job(urls: list[str]) -> tuple[R.AnalysisJob, list[R.AnalysisItem]]:
    items = [R.AnalysisItem(id=f"i{n}", raw_url=u, url=u, platform=PLATFORM,
                            entity_id=f"e{n}")
             for n, u in enumerate(urls)]
    job = R.AnalysisJob(id="job1", items=items, total=len(items), target_name="Acme")
    job.platform_progress[PLATFORM] = {
        "status": "pending", "total": len(items), "completed": 0,
        "display_name": "X (Twitter)",
    }
    return job, items


async def _scrape(job, items):
    await R.AnalysisRunner()._scrape_platform(job, PLATFORM, items)


# ------------------------------------------------------- how many, and why


class TestHowManySessionsAreWorthClaiming:
    def test_a_full_batch_takes_the_cap(self):
        assert R._sessions_wanted(PLATFORM, 40, 3) == R._DEFAULT_MAX_SESSIONS_PER_PLATFORM

    def test_a_batch_that_fits_in_one_chunk_wants_one_session(self):
        """Three URLs and three tabs is one chunk. A second session would open
        a browser to sit idle, and a claimed session is invisible to every
        other job while it does."""
        assert R._sessions_wanted(PLATFORM, 3, 3) == 1

    def test_one_more_url_than_a_chunk_wants_a_second_session(self):
        assert R._sessions_wanted(PLATFORM, 4, 3) == 2

    def test_it_never_claims_more_sessions_than_urls(self):
        assert R._sessions_wanted(PLATFORM, 1, 1) == 1
        assert R._sessions_wanted(PLATFORM, 2, 1) == 2

    def test_youtube_is_pinned_to_one_because_the_whole_batch_is_one_call(self):
        assert R._sessions_wanted("youtube", 40, 6) == 1

    def test_telegram_is_pinned_to_one_because_of_the_session_file_lock(self):
        assert R._sessions_wanted("telegram", 40, 6) == 1



# ----------------------------------------------------------- the split itself


class TestOneSession:
    @pytest.mark.asyncio
    async def test_it_reads_every_url_exactly_once(self, monkeypatch):
        """The single-session pool is not a special path -- it is this code
        with one worker -- so it has to stay exactly as complete as it was."""
        _, scraper_cls, _ = _wire(monkeypatch, [_session("a")])
        job, items = _job([f"https://x.com/u{n}" for n in range(4)])

        await _scrape(job, items)

        assert len(scraper_cls.instances) == 1
        assert scraper_cls.instances[0].seen == [i.url for i in items]
        assert [i.status for i in items] == ["done"] * 4
        assert job.completed == job.total == 4
        assert job.platform_progress[PLATFORM]["status"] == "done"

    @pytest.mark.asyncio
    async def test_it_reads_them_in_order(self, monkeypatch):
        _, scraper_cls, _ = _wire(monkeypatch, [_session("a")])
        job, items = _job([f"https://x.com/u{n}" for n in range(5)])
        await _scrape(job, items)
        assert scraper_cls.instances[0].seen == [i.url for i in items]


class TestTwoSessions:
    @pytest.mark.asyncio
    async def test_the_batch_is_split_between_them(self, monkeypatch):
        _, scraper_cls, _ = _wire(
            monkeypatch, [_session("a"), _session("b")])
        job, items = _job([f"https://x.com/u{n}" for n in range(6)])

        await _scrape(job, items)

        assert len(scraper_cls.instances) == 2
        seen = [s.seen for s in scraper_cls.instances]
        assert all(s for s in seen), f"one worker did no work at all: {seen}"
        assert sorted(seen[0] + seen[1]) == sorted(i.url for i in items)
        assert len(seen[0] + seen[1]) == 6, "a URL was read by both workers"
        assert [i.status for i in items] == ["done"] * 6
        assert job.completed == job.total == 6

    @pytest.mark.asyncio
    async def test_each_worker_gets_its_own_account(self, monkeypatch):
        """The isolation claim, checked rather than asserted in a comment: two
        workers must never be two contexts on ONE account."""
        _, scraper_cls, _ = _wire(
            monkeypatch, [_session("a"), _session("b")])
        job, items = _job([f"https://x.com/u{n}" for n in range(6)])

        await _scrape(job, items)

        ids = {s.session_id for s in scraper_cls.instances}
        assert ids == {"a", "b"}, "the same session was handed to both workers"

    @pytest.mark.asyncio
    async def test_the_progress_chip_reports_the_worker_count(self, monkeypatch):
        _, _, _ = _wire(
            monkeypatch, [_session("a"), _session("b")])
        job, items = _job([f"https://x.com/u{n}" for n in range(6)])
        await _scrape(job, items)
        # back to 0 once they are done -- the field says what is running NOW
        assert job.platform_progress[PLATFORM]["workers"] == 0

    @pytest.mark.asyncio
    async def test_every_claimed_session_is_released(self, monkeypatch):
        pool, _, _ = _wire(
            monkeypatch, [_session("a"), _session("b")])
        job, items = _job([f"https://x.com/u{n}" for n in range(6)])
        await _scrape(job, items)
        assert pool.claimed == set(), "a session stayed claimed and is now invisible to every job"



# --------------------------------------------------------------- failover


class TestASessionDyingMidRun:
    @pytest.mark.asyncio
    async def test_the_healthy_session_finishes_what_the_dead_one_was_holding(
        self, monkeypatch,
    ):
        """The whole point. Session `a` checkpoints on the first URL it is
        given; every URL still has to come back read."""
        urls = [f"https://x.com/u{n}" for n in range(6)]
        pool, scraper_cls, _ = _wire(
            monkeypatch,
            [_session("a"), _session("b")],
            fail_urls={"a": set(urls)},
        )
        job, items = _job(urls)

        await _scrape(job, items)

        assert [i.status for i in items] == ["done"] * 6, (
            "a dead session still cost the batch URLs")
        healthy = [s for s in scraper_cls.instances if s.session_id == "b"][0]
        assert sorted(healthy.seen) == sorted(urls)
        assert ("a", "checkpointed") in pool.marked_failed
        assert "a" in pool.dead

    @pytest.mark.asyncio
    async def test_a_retried_url_is_not_counted_twice(self, monkeypatch):
        """Its first attempt was already counted and already saved as an
        error. Counting the retry as well walks the bar past 100%."""
        urls = [f"https://x.com/u{n}" for n in range(6)]
        _wire(monkeypatch, [_session("a"), _session("b")],
              fail_urls={"a": set(urls)})
        job, items = _job(urls)

        await _scrape(job, items)

        assert job.completed == job.total == 6
        assert job.platform_progress[PLATFORM]["completed"] == 6
        assert sum(1 for i in items if i.attempts == 2) == 1, (
            "exactly one URL should have been read by a second session")

    @pytest.mark.asyncio
    async def test_a_platform_that_recovered_completely_is_not_reported_failed(
        self, monkeypatch,
    ):
        urls = [f"https://x.com/u{n}" for n in range(6)]
        _wire(monkeypatch, [_session("a"), _session("b")],
              fail_urls={"a": set(urls)})
        job, items = _job(urls)
        await _scrape(job, items)
        assert job.platform_progress[PLATFORM]["status"] == "done"

    @pytest.mark.asyncio
    async def test_a_replacement_session_is_claimed_when_the_only_worker_dies(
        self, monkeypatch,
    ):
        """The replacement-claim round, reached when every claimed session
        session can only be reached by the replacement round -- which is the
        case the round exists for."""
        urls = ["https://x.com/u0", "https://x.com/u1", "https://x.com/u2"]
        pool, scraper_cls, _ = _wire(
            monkeypatch,
            [_session("a"), _session("b")],
            fail_urls={"a": {"https://x.com/u1"}},
        )
        job, items = _job(urls)

        await _scrape(job, items)

        assert [s.session_id for s in scraper_cls.instances] == ["a", "b"]
        assert [i.status for i in items] == ["done"] * 3
        assert job.completed == job.total == 3


class TestAUrlThatKillsEverySession:
    @pytest.mark.asyncio
    async def test_it_is_retried_once_and_then_reported(self, monkeypatch):
        """Ambiguous by nature: usually the session died, sometimes the URL is
        what trips the challenge. Bounded so the second case cannot walk the
        pool."""
        urls = ["https://x.com/ok0", "https://x.com/bad", "https://x.com/ok1"]
        pool, scraper_cls, _ = _wire(
            monkeypatch,
            [_session("a"), _session("b")],
            fail_urls={"a": {"https://x.com/bad"}, "b": {"https://x.com/bad"}},
        )
        job, items = _job(urls)

        await _scrape(job, items)

        bad = [i for i in items if i.url == "https://x.com/bad"][0]
        assert bad.status == "error"
        assert bad.attempts == R._MAX_ITEM_ATTEMPTS == 2
        assert [i.status for i in items if i is not bad] == ["done", "done"]
        assert job.completed == job.total == 3
        assert job.platform_progress[PLATFORM]["status"] == "failed"


class TestWhenNothingCanBeClaimed:
    @pytest.mark.asyncio
    async def test_an_empty_pool_fails_the_platform_with_its_own_message(
        self, monkeypatch,
    ):
        """The message an analyst can act on has to survive to the rows."""
        _wire(monkeypatch, [])
        job, items = _job(["https://x.com/u0", "https://x.com/u1"])

        await _scrape(job, items)

        assert [i.status for i in items] == ["error"] * 2
        assert "no healthy sessions available" in items[0].error
        assert job.completed == job.total == 2
        assert job.platform_progress[PLATFORM]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_the_only_session_dying_reports_what_it_never_reached(
        self, monkeypatch,
    ):
        urls = [f"https://x.com/u{n}" for n in range(4)]
        _wire(monkeypatch, [_session("a")], fail_urls={"a": set(urls)})
        job, items = _job(urls)

        await _scrape(job, items)

        assert [i.status for i in items] == ["error"] * 4
        assert all("failed or checkpointed" in i.error for i in items[1:])
        assert job.completed == job.total == 4
        assert job.platform_progress[PLATFORM]["status"] == "failed"


class TestASessionThatWasNeverUsable:
    @pytest.mark.asyncio
    async def test_the_actionable_message_still_reaches_the_rows(self, monkeypatch):
        """A failed login probe used to raise straight out of the platform and
        so became every item's error. It now raises inside a worker, where
        another worker or another round could still cover for it -- when none
        does, the analyst has to end up reading the same thing."""
        pool, scraper_cls, _ = _wire(monkeypatch, [_session("a")])
        monkeypatch.setattr(R.sessions_engine, "proven_fresh", lambda item: False)

        async def _dead(self):
            return False

        monkeypatch.setattr(scraper_cls, "check_session", _dead)
        job, items = _job(["https://x.com/u0", "https://x.com/u1"])

        await _scrape(job, items)

        assert [i.status for i in items] == ["error"] * 2
        assert "check credentials under Sessions" in items[0].error
        assert ("a", "expired") in pool.marked_failed
        assert job.completed == job.total == 2
        assert job.platform_progress[PLATFORM]["status"] == "failed"


class TestCancellation:
    @pytest.mark.asyncio
    async def test_a_cancelled_platform_keeps_what_it_read_and_fails_nothing(
        self, monkeypatch,
    ):
        """Failing the rest would turn the analyst's own cancel into a
        screenful of errors."""
        _wire(monkeypatch, [_session("a")])
        job, items = _job([f"https://x.com/u{n}" for n in range(4)])
        job.cancel.set()

        await _scrape(job, items)

        assert [i.status for i in items] == ["pending"] * 4
        assert job.completed == 0


# --------------------------------------------------- the process-wide ceiling


class TestTheProcessWideWorkerCap:
    @pytest.mark.asyncio
    async def test_it_bounds_how_many_browsers_are_open_at_once(self, monkeypatch):
        """`_run` scrapes every platform of a job concurrently, so a
        per-platform cap cannot see the total. This is the number that can."""
        monkeypatch.setattr(R.settings, "analysis_max_browser_workers", 1)
        _, scraper_cls, live = _wire(
            monkeypatch, [_session("a"), _session("b")])
        job, items = _job([f"https://x.com/u{n}" for n in range(6)])

        await _scrape(job, items)

        assert live["max"] == 1
        assert [i.status for i in items] == ["done"] * 6

    @pytest.mark.asyncio
    async def test_a_worker_that_waited_out_the_batch_never_opens_a_browser(
        self, monkeypatch,
    ):
        monkeypatch.setattr(R.settings, "analysis_max_browser_workers", 1)
        pool, scraper_cls, _ = _wire(
            monkeypatch, [_session("a"), _session("b")])
        job, items = _job([f"https://x.com/u{n}" for n in range(6)])

        await _scrape(job, items)

        assert len(scraper_cls.instances) == 1
        assert pool.claimed == set()

    @pytest.mark.asyncio
    async def test_two_workers_are_allowed_when_the_ceiling_permits(self, monkeypatch):
        monkeypatch.setattr(R.settings, "analysis_max_browser_workers", 4)
        _, scraper_cls, live = _wire(
            monkeypatch, [_session("a"), _session("b")])
        job, items = _job([f"https://x.com/u{n}" for n in range(6)])

        await _scrape(job, items)

        assert live["max"] == 2

    @pytest.mark.asyncio
    async def test_the_semaphore_belongs_to_the_running_loop(self):
        """One module-level Semaphore would bind to whichever loop first
        awaited it, which in a test suite is a loop that is already closed."""
        first = R._worker_semaphore()
        assert R._worker_semaphore() is first
        assert asyncio.get_running_loop() in R._worker_slots


# ------------------------------------------- parallelism is by session count


class TestParallelismIsPurelyBySessionCount:
    """WHAT CHANGED, AND WHY THIS IS PINNED. Claiming used to refuse a second
    session unless it left the host through a different egress, which meant a
    proxy-less pool could never run more than one worker. Proxy support has
    been removed from the tool, so the only question left is how many pooled
    ACCOUNTS a platform has: two sessions means two workers, splitting the
    batch, regardless of the address they share."""

    @pytest.mark.asyncio
    async def test_two_proxyless_sessions_run_in_parallel(self, monkeypatch):
        _, scraper_cls, live = _wire(monkeypatch, [_session("a"), _session("b")])
        job, items = _job([f"https://x.com/u{n}" for n in range(6)])

        await _scrape(job, items)

        assert len(scraper_cls.instances) == 2
        assert live["max"] == 2, "both workers must actually be open at once"
        seen = [s.seen for s in scraper_cls.instances]
        assert all(s for s in seen), f"one worker did no work: {seen}"
        assert sorted(seen[0] + seen[1]) == sorted(i.url for i in items)
        assert [i.status for i in items] == ["done"] * 6

    @pytest.mark.asyncio
    async def test_three_sessions_give_three_workers(self, monkeypatch):
        """Capped only by _sessions_wanted -- the pool size and the work."""
        _, scraper_cls, _ = _wire(
            monkeypatch, [_session("a"), _session("b"), _session("c")])
        job, items = _job([f"https://x.com/u{n}" for n in range(9)])

        await _scrape(job, items)

        assert len(scraper_cls.instances) == 3
        assert [i.status for i in items] == ["done"] * 9

    @pytest.mark.asyncio
    async def test_one_session_still_does_the_whole_batch(self, monkeypatch):
        _, scraper_cls, _ = _wire(monkeypatch, [_session("a")])
        job, items = _job([f"https://x.com/u{n}" for n in range(4)])
        await _scrape(job, items)
        assert len(scraper_cls.instances) == 1
        assert scraper_cls.instances[0].seen == [i.url for i in items]
