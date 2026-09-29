"""Discovery robustness fixes from the 2026-09-29 engine audit.

  * the runner: a tab whose processing RAISED (our own save failing after
    the search worked) used to kill its worker and take the keyword it had
    already popped off the queue with it -- neither retried nor recorded;
  * X: a genuinely empty search, and a logged-out session, are named for
    what they are instead of scrolling to `stalled`;
  * Telegram: the cap applies before profile photos are downloaded, and a
    flood-wait on a photo no longer throws the search's results away;
  * YouTube: the handle lookup really never raises, so a network blip on
    it cannot discard a page of results.
"""

from __future__ import annotations

import asyncio
import types
import urllib.error

import pytest

from backend.discovery import runner as R
from backend.platforms.scan_options import DiscoveryOptions
from backend.tests.test_discovery_retries_and_enrichment import _hit, _wire
from backend.tests.test_keyword_coverage import FakeSweep, _job, _session, _sweep


# ------------------------------------------------------------------ runner


def _flaky_save(monkeypatch, saved: list, fail_for: str, times: int):
    """Make the profile save raise for `fail_for`'s rows `times` times."""
    left = {"n": times}

    async def _save_many(client_id, plat_id, phase, rows):
        if left["n"] and any(fail_for in r.get("url", "") for r in rows):
            left["n"] -= 1
            raise RuntimeError("mongo write failed")
        saved.extend(rows)
        return len(rows), len(rows)

    monkeypatch.setattr(R.profiles_db, "save_many", _save_many)


class TestACrashedTabIsNeverLost:
    @pytest.mark.asyncio
    async def test_a_one_off_crash_is_retried_and_the_keyword_completes(self, monkeypatch):
        async def sweep(sid, kw, tab, on_progress, calls):
            return FakeSweep(hits=[_hit(kw, tab)])

        ledger, calls, saved = _wire(monkeypatch, "twitter", [_session("a")], sweep)
        _flaky_save(monkeypatch, saved, "kw0", times=1)
        prog = await _sweep(_job(["kw0", "kw1"], "twitter", ["people"]), "twitter")

        assert calls[("kw0", "people")] == 2
        assert calls[("kw1", "people")] == 1
        assert ledger.owed_now() == set()
        assert prog.status == "done"

    @pytest.mark.asyncio
    async def test_a_persistent_crash_is_recorded_and_the_rest_still_runs(self, monkeypatch):
        async def sweep(sid, kw, tab, on_progress, calls):
            return FakeSweep(hits=[_hit(kw, tab)])

        ledger, calls, saved = _wire(monkeypatch, "twitter", [_session("a")], sweep)
        _flaky_save(monkeypatch, saved, "kw0", times=99)
        prog = await _sweep(_job(["kw0", "kw1", "kw2"], "twitter", ["people"]), "twitter")

        # The keywords after the crashing one were still searched...
        assert calls[("kw1", "people")] == 1
        assert calls[("kw2", "people")] == 1
        # ...and the crashing one is owed, by name, with the real error.
        assert ledger.owed_now() == {("twitter", "people", "kw0")}
        assert "mongo write failed" in ledger.reason_for("twitter", "people", "kw0")
        # Never a clean "done" over a keyword that failed.
        assert prog.status == "partial"
        assert "mongo write failed" in prog.note

    @pytest.mark.asyncio
    async def test_other_tabs_of_the_same_keyword_still_run(self, monkeypatch):
        async def sweep(sid, kw, tab, on_progress, calls):
            return FakeSweep(hits=[_hit(kw, tab)])

        ledger, calls, saved = _wire(monkeypatch, "facebook", [_session("a")], sweep)
        _flaky_save(monkeypatch, saved, "kw0-people", times=99)
        await _sweep(_job(["kw0"], "facebook", ["people", "pages", "groups"]), "facebook")

        assert calls[("kw0", "pages")] == 1
        assert calls[("kw0", "groups")] == 1
        assert ledger.owed_now() == {("facebook", "people", "kw0")}


# ---------------------------------------------------------------------- X


class _XPage:
    """Just enough of a Playwright page for X's sweep to reach its
    pre-scroll verdict."""

    def __init__(self, url: str, body: str):
        self.url = url
        self._body = body
        self.scrolled = False

    def on(self, *_a, **_kw): pass
    async def goto(self, *_a, **_kw): pass
    async def wait_for_function(self, *_a, **_kw): raise TimeoutError("no")
    async def inner_text(self, _sel): return self._body
    async def close(self): pass
    async def evaluate(self, *_a, **_kw): return []
    async def query_selector_all(self, *_a, **_kw): return []

    @property
    def mouse(self):
        self.scrolled = True
        raise RuntimeError("the loop should not scroll")


def _x_sweep(monkeypatch, page: _XPage, keyword="nobody"):
    from backend.platforms.twitter import discovery_engine as TW

    monkeypatch.setattr(TW.random, "uniform", lambda a, b: 0.0)

    async def _noop(*_a, **_kw): pass
    monkeypatch.setattr(TW, "humanize_interaction", _noop)

    async def _new_page():
        return page

    ctx = types.SimpleNamespace(new_page=_new_page)
    opts = DiscoveryOptions(settle=0.01, page_wait=0.01, max_results=5)
    return asyncio.run(TW.Discovery(opts, ctx).sweep(keyword))


class TestXNamesAnEmptyOrDeadSearch:
    def test_x_saying_no_results_is_a_satisfied_empty_search(self, monkeypatch):
        page = _XPage("https://x.com/search?q=nobody", 'No results for "nobody"')
        out = _x_sweep(monkeypatch, page)
        assert (out.stopped, out.complete) == ("no-results", True)
        assert not page.scrolled

    def test_a_login_redirect_is_session_shaped(self, monkeypatch):
        from backend.shared.resilience import classify_failure

        page = _XPage("https://x.com/i/flow/login?redirect_after_login=%2Fsearch", "")
        out = _x_sweep(monkeypatch, page)
        assert out.stopped == "login"
        assert classify_failure(out.error or out.stopped) == "expired"

    def test_a_logged_out_landing_page_is_session_shaped(self, monkeypatch):
        page = _XPage("https://x.com/search?q=nobody", "Sign in to X  Don't miss what's happening")
        out = _x_sweep(monkeypatch, page)
        assert out.stopped == "login"


# ---------------------------------------------------------------- Telegram


class _TGObj:
    def __init__(self, i: int):
        self.id = i
        self.username = f"user{i}"
        self.first_name = f"User {i}"
        self.photo = object()


def _tg(monkeypatch, n: int, on_photo):
    from backend.platforms.telegram import discovery_engine as TG

    if not TG.HAVE_TELETHON:
        pytest.skip("telethon not installed")
    tg = TG.Telegram.__new__(TG.Telegram)
    tg._photos_paused_until = 0.0
    downloads = []

    async def _download(obj, file=None):
        downloads.append(obj.id)
        return on_photo(obj)

    async def _call(req):
        return types.SimpleNamespace(users=[_TGObj(i) for i in range(n)], chats=[])

    tg.client = types.SimpleNamespace(download_profile_photo=_download)
    tg._call = _call
    monkeypatch.setattr(TG, "entity_from", lambda obj, probe=None: TG.TelegramEntity(
        entity_id=str(obj.id), username=obj.username, title=obj.first_name,
        kind="profile", has_photo=True))
    return TG, tg, downloads


class TestTelegramPhotos:
    def test_only_capped_results_have_their_photo_downloaded(self, monkeypatch):
        _TG, tg, downloads = _tg(monkeypatch, 40, lambda obj: b"img")
        out = asyncio.run(tg.search("kw", 100, max_results=5))
        assert len(out) == 5
        assert len(downloads) == 5

    def test_a_photo_flood_wait_keeps_the_results(self, monkeypatch):
        from backend.platforms.telegram import discovery_engine as TG

        def _flood(obj):
            err = TG.FloodWaitError(request=None)
            err.seconds = 60
            raise err

        _TG, tg, downloads = _tg(monkeypatch, 4, _flood)
        out = asyncio.run(tg.search("kw", 100))
        assert len(out) == 4                 # nothing thrown away
        assert len(downloads) == 1           # and no more photos tried
        assert tg._photos_paused_until > 0


# ----------------------------------------------------------------- YouTube


class TestYouTubeHandles:
    def test_a_network_error_on_the_handle_lookup_never_raises(self, monkeypatch):
        from backend.platforms.youtube import discovery_engine as YT

        api = YT.YouTubeAPI(key="test-key")

        async def _boom(ids):
            raise urllib.error.URLError("connection reset")

        monkeypatch.setattr(api, "channels", _boom)
        assert asyncio.run(api.handles(["UC1", "UC2"])) == {}


# ------------------------------------------------- YouTube analysis batch


class _FakeYTAPI:
    """channels.list by id, and a handle lookup that knows nothing -- the
    state of a channel whose @handle changed after discovery found it."""

    def __init__(self, channels: dict[str, dict], fail_batch: bool = False):
        self._channels = channels
        self.fail_batch = fail_batch
        self.calls: list[list[str]] = []

    async def channels(self, ids):
        self.calls.append(list(ids))
        if self.fail_batch and len(ids) > 1:
            raise urllib.error.URLError("connection reset")
        return [self._channels[i] for i in ids if i in self._channels]

    async def channel_by_handle(self, ref):
        return None

    async def latest_upload(self, playlist):
        return "2026-09-01"


def _yt_channel(cid: str, title: str) -> dict:
    return {"id": cid, "snippet": {"title": title, "customUrl": "@newhandle"},
            "statistics": {"subscriberCount": "10", "videoCount": "3"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}}


def _yt_scraper(api):
    from backend.platforms.youtube import analysis_engine as YA

    s = YA.Scraper.__new__(YA.Scraper)
    s.a = types.SimpleNamespace()
    s.api = api
    return s


class TestYouTubeBatchUsesDiscoverysRecord:
    def test_a_renamed_handle_resolves_by_the_stored_channel_id(self):
        api = _FakeYTAPI({"UCabc": _yt_channel("UCabc", "Acme Official")})
        url = "https://www.youtube.com/@oldhandle"
        rows = asyncio.run(_yt_scraper(api).run(
            [(url, "Acme", "")],
            known_by_url={url: {"entity_id": "UCabc", "username": "oldhandle"}}))

        assert rows[0].status == "OK"               # not GONE
        assert rows[0].profile_name == "Acme Official"

    def test_without_the_record_it_is_still_gone(self):
        api = _FakeYTAPI({"UCabc": _yt_channel("UCabc", "Acme Official")})
        rows = asyncio.run(_yt_scraper(api).run(
            [("https://www.youtube.com/@oldhandle", "Acme", "")]))
        assert rows[0].status == "GONE"

    def test_a_failed_batch_lookup_falls_back_per_channel(self):
        api = _FakeYTAPI({"UCa": _yt_channel("UCa", "A"), "UCb": _yt_channel("UCb", "B")},
                         fail_batch=True)
        rows = asyncio.run(_yt_scraper(api).run([
            ("https://www.youtube.com/channel/UCa", "Acme", ""),
            ("https://www.youtube.com/channel/UCb", "Acme", ""),
        ]))
        assert [r.status for r in rows] == ["OK", "OK"]


class TestACrashedTabsRetryIsNotCountedTwice:
    @pytest.mark.asyncio
    async def test_rows_streamed_before_the_crash_are_counted_once(self, monkeypatch):
        async def sweep(sid, kw, tab, on_progress, calls):
            # streams one row, then returns a second the final save handles
            await on_progress(1, 1, [_hit(kw, tab, "streamed")])
            extra = _hit(kw, tab)
            extra.url += "-final"
            return FakeSweep(hits=[extra])

        ledger, calls, saved = _wire(monkeypatch, "twitter", [_session("a")], sweep)
        # the streamed save works; the post-sweep save fails once
        left = {"n": 1}

        async def _save_many(client_id, plat_id, phase, rows):
            if left["n"] and any(r["url"].endswith("-final") for r in rows):
                left["n"] -= 1
                raise RuntimeError("mongo write failed")
            saved.extend(rows)
            return len(rows), len(rows)

        monkeypatch.setattr(R.profiles_db, "save_many", _save_many)
        job = _job(["kw0"], "twitter", ["people"])
        prog = await _sweep(job, "twitter")

        assert calls[("kw0", "people")] == 2
        assert prog.found == job.found == 2       # not 3
        assert prog.keywords_done == prog.keywords_total == 1
