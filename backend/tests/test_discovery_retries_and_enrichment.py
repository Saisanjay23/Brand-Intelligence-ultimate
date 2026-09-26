"""No keyword left unsearched for want of a second try, and nothing a sweep
learned after streaming a card is thrown away.

Four runner behaviours, each closing a gap found in the 2026-09-24 audit:

  * a tab that BROKE with zero results (a stall, a parse error) gets one
    more attempt in the same run instead of waiting a day for the next one;
  * a keyword re-queued after its session died re-runs only the tabs that
    did not finish -- and a give-up never overwrites a satisfied tab;
  * a platform keeps asking the pool for replacements while it has healthy
    accounts, not just twice;
  * a name the engine resolved AFTER streaming a card reaches the database.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from backend.discovery import runner as R
from backend.shared.models.row import Row
from backend.tests.test_keyword_coverage import (FakeLedger, FakePool,
                                                 FakeSweep, _job, _session,
                                                 _sweep)


def _wire(monkeypatch, platform, sessions, sweep_fn, pool_total=None):
    """Like test_keyword_coverage._wire, with the sweep itself pluggable:
    `sweep_fn(session_id, keyword, tab, on_progress)` -> FakeSweep."""
    ledger = FakeLedger()
    calls: Counter = Counter()
    saved: list[dict] = []

    class FakeDiscoverer:
        def __init__(self, options, ctx, anonymous: bool = False):
            self.session_id = str(ctx or "").removeprefix("ctx-")

        async def sweep(self, keyword, tab, on_progress=None):
            await asyncio.sleep(0)
            calls[(keyword, tab)] += 1
            return await sweep_fn(self.session_id, keyword, tab, on_progress, calls)

    class FakeSession:
        def __init__(self, options, cookies, session_id=""):
            self.ctx = f"ctx-{session_id}"
            self.on_cookies = None

        async def start(self): pass
        async def stop(self): pass
        async def check_session(self): return True
        async def sync_cookies(self): pass
        async def pause(self, mult=1.0): pass

    class FakePlatform:
        session_path = "fake:Session"
        uses_api_key = False
        env_keys = ()

        def discoverer(self): return FakeDiscoverer
        def session_cls(self): return FakeSession

    pool = FakePool(sessions, FakePlatform())
    for name in ("session_for_job", "release_claim", "mark_session_failed",
                 "mark_session_ok", "cookie_saver", "proven_fresh"):
        monkeypatch.setattr(R.sessions_engine, name, getattr(pool, name))

    async def _pool_summary(platform_id):
        return {"total": pool_total if pool_total is not None else len(sessions)}

    monkeypatch.setattr(R.sessions_engine, "pool_summary", _pool_summary)

    async def _save_many(client_id, plat_id, phase, rows):
        saved.extend(rows)
        return len(rows), len(rows)

    monkeypatch.setattr(R.profiles_db, "save_many", _save_many)
    monkeypatch.setattr(R.avatar_cache, "spawn", lambda *a, **kw: None)
    monkeypatch.setattr(R, "WORKER_STAGGER_SEC", 0.0)
    monkeypatch.setattr(R, "TAB_STAGGER_SEC", 0.0)
    monkeypatch.setattr(R, "_RETRY_BACKOFF_S", 0.0)
    monkeypatch.setitem(R._PLATFORM_INTER_KEYWORD_DELAY, platform, 0.0)
    monkeypatch.setattr(R.settings, "discovery_sequential_keywords", True)
    for name in ("plan", "record", "miss", "owed"):
        monkeypatch.setattr(R.coverage_db, name, getattr(ledger, name))
    return ledger, calls, saved


def _hit(keyword, tab, name="Someone"):
    return Row(url=f"https://x/{keyword}-{tab}", target=keyword, original_feed="",
               status="OK", profile_name=name)


class TestABrokenEmptySweepIsRetriedInTheSameRun:
    @pytest.mark.asyncio
    async def test_a_stall_with_nothing_found_gets_one_more_attempt(self, monkeypatch):
        async def sweep(sid, kw, tab, on_progress, calls):
            if calls[(kw, tab)] == 1:
                return FakeSweep(hits=[], complete=False, stopped="stalled")
            return FakeSweep(hits=[_hit(kw, tab)])

        ledger, calls, _ = _wire(monkeypatch, "twitter", [_session("a")], sweep)
        job = _job(["kw0", "kw1"], "twitter", ["people"])
        prog = await _sweep(job, "twitter")

        assert calls[("kw0", "people")] == 2
        assert ledger.owed_now() == set()
        # the retry did not double-count its progress unit
        assert prog.keywords_done == prog.keywords_total == 2

    @pytest.mark.asyncio
    async def test_only_once_and_then_it_stays_owed(self, monkeypatch):
        async def sweep(sid, kw, tab, on_progress, calls):
            return FakeSweep(hits=[], complete=False, stopped="stalled")

        ledger, calls, _ = _wire(monkeypatch, "twitter", [_session("a")], sweep)
        await _sweep(_job(["kw0"], "twitter", ["people"]), "twitter")

        assert calls[("kw0", "people")] == R._MAX_KEYWORD_ATTEMPTS
        assert ("twitter", "people", "kw0") in ledger.owed_now()

    @pytest.mark.asyncio
    async def test_a_genuinely_empty_search_is_never_retried(self, monkeypatch):
        async def sweep(sid, kw, tab, on_progress, calls):
            return FakeSweep(hits=[], complete=True, stopped="no-results")

        ledger, calls, _ = _wire(monkeypatch, "facebook", [_session("a")], sweep)
        await _sweep(_job(["kw0"], "facebook", ["people"]), "facebook")

        assert calls[("kw0", "people")] == 1
        assert ledger.owed_now() == set()


class TestARequeueKeepsWhatAlreadyWorked:
    @pytest.mark.asyncio
    async def test_only_the_unfinished_tab_is_swept_again(self, monkeypatch):
        """Session `a` dies on kw0's PAGES tab after People succeeded. The
        replacement session re-runs pages and groups -- not people."""
        async def sweep(sid, kw, tab, on_progress, calls):
            if sid == "a" and tab == "pages":
                raise RuntimeError("checkpoint detected")
            return FakeSweep(hits=[_hit(kw, tab)])

        ledger, calls, _ = _wire(
            monkeypatch, "facebook", [_session("a"), _session("b")], sweep)
        prog = await _sweep(_job(["kw0"], "facebook", ["people", "pages", "groups"]), "facebook")

        assert calls[("kw0", "people")] == 1
        assert calls[("kw0", "pages")] == 2
        assert ledger.owed_now() == set()
        assert prog.keywords_done == prog.keywords_total == 3

    @pytest.mark.asyncio
    async def test_a_session_shaped_stop_without_an_exception_is_rerun_too(self, monkeypatch):
        """A sweep can REPORT a checkpoint instead of raising one (the
        Facebook login-wall check does). That tab is not done, even though
        the engine returned normally."""
        async def sweep(sid, kw, tab, on_progress, calls):
            if sid == "a" and tab == "pages":
                return FakeSweep(hits=[], complete=False, stopped="checkpoint",
                                 error="facebook search page shows a checkpoint wall")
            return FakeSweep(hits=[_hit(kw, tab)])

        ledger, calls, _ = _wire(
            monkeypatch, "facebook", [_session("a"), _session("b")], sweep)
        await _sweep(_job(["kw0"], "facebook", ["people", "pages"]), "facebook")

        assert calls[("kw0", "pages")] == 2
        assert calls[("kw0", "people")] == 1
        assert ledger.owed_now() == set()

    @pytest.mark.asyncio
    async def test_giving_up_never_overwrites_a_satisfied_tab(self, monkeypatch):
        async def sweep(sid, kw, tab, on_progress, calls):
            if tab == "pages":
                raise RuntimeError("checkpoint detected")
            return FakeSweep(hits=[_hit(kw, tab)])

        ledger, _, _ = _wire(
            monkeypatch, "facebook", [_session("a"), _session("b")], sweep)
        await _sweep(_job(["kw0"], "facebook", ["people", "pages"]), "facebook")

        assert ("facebook", "people", "kw0") in ledger.searched()
        assert ("facebook", "pages", "kw0") in ledger.owed_now()


class TestTheWholePoolIsUsedBeforeKeywordsAreAbandoned:
    @pytest.mark.asyncio
    async def test_a_third_healthy_account_is_asked(self, monkeypatch):
        """Two accounts die one after another; the third is healthy. With a
        flat two claim rounds, every keyword after the second death was
        abandoned while `c` sat idle."""
        async def sweep(sid, kw, tab, on_progress, calls):
            if (sid, kw) in {("a", "kw0"), ("b", "kw1")}:
                raise RuntimeError("checkpoint detected")
            return FakeSweep(hits=[_hit(kw, tab)])

        ledger, _, _ = _wire(
            monkeypatch, "twitter", [_session("a"), _session("b"), _session("c")], sweep)
        prog = await _sweep(_job(["kw0", "kw1", "kw2"], "twitter", ["people"]), "twitter")

        assert ledger.owed_now() == set()
        assert prog.status == "done"


class TestNamesResolvedAfterStreamingAreSaved:
    @pytest.mark.asyncio
    async def test_the_enriched_row_is_saved_again(self, monkeypatch):
        async def sweep(sid, kw, tab, on_progress, calls):
            row = _hit(kw, tab, name="")
            await on_progress(1, 1, [row])
            row.profile_name = "Resolved Name"   # the resolve phase, later
            return FakeSweep(hits=[row])

        ledger, _, saved = _wire(monkeypatch, "facebook", [_session("a")], sweep)
        job = _job(["kw0"], "facebook", ["people"])
        await _sweep(job, "facebook")

        names = [r.get("display_name") for r in saved if r.get("url") == "https://x/kw0-people"]
        assert names == ["", "Resolved Name"]
        # counted once, not twice
        assert job.found == 1

    @pytest.mark.asyncio
    async def test_an_unchanged_streamed_row_is_not_saved_twice(self, monkeypatch):
        async def sweep(sid, kw, tab, on_progress, calls):
            row = _hit(kw, tab, name="Same")
            await on_progress(1, 1, [row])
            return FakeSweep(hits=[row])

        _, _, saved = _wire(monkeypatch, "facebook", [_session("a")], sweep)
        await _sweep(_job(["kw0"], "facebook", ["people"]), "facebook")
        assert len(saved) == 1
