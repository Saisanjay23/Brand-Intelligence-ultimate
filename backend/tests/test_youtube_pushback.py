"""YouTube pushback: a throttle or Google's unusual-traffic check must be
SEEN (recorded on the governor, reported in the stop code the runner cools
the key on) and must never pass for a dead key, a missing channel, or a
clean zero. No network: urllib and the API client are faked throughout.
"""

from __future__ import annotations

import io
import types
import urllib.error

import pytest

from backend.platforms.scan_options import ScanOptions
from backend.platforms.youtube import analysis_engine as YA
from backend.platforms.youtube import discovery_engine as YD
from backend.shared.models.row import Row
from backend.shared.pushback import (CHALLENGED, THROTTLED, Signal,
                                     governor_for, reset_all)
from backend.shared.resilience import classify_failure

CID = "UC" + "a" * 22


@pytest.fixture(autouse=True)
def _clean_governor():
    reset_all()
    yield
    reset_all()


@pytest.fixture
def no_sleep(monkeypatch):
    async def _instant(*_a, **_k):
        return None
    monkeypatch.setattr(YD.asyncio, "sleep", _instant)


def _http_error(code: int, body: str, url: str = "https://www.googleapis.com/youtube/v3/search"):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body.encode()))


def _api_raising(monkeypatch, code: int, body: str, url: str | None = None):
    def _urlopen(*_a, **_k):
        raise _http_error(code, body, url or "https://www.googleapis.com/youtube/v3/search")
    monkeypatch.setattr(YD.urllib.request, "urlopen", _urlopen)
    return YD.YouTubeAPI(key="test-key")


def _gov():
    return governor_for("youtube")


# ------------------------------------------------------------- _get_sync

class TestGetSyncClassifies:
    def test_a_plain_429_is_a_recorded_throttle(self, monkeypatch):
        api = _api_raising(monkeypatch, 429, '{"error": {"message": "slow"}}')
        with pytest.raises(YD.YouTubePushback) as ei:
            api._get_sync("search", {})
        assert ei.value.stop_code == "rate_limited"
        assert _gov().pushbacks == 1 and _gov().last_signal.kind == THROTTLED

    def test_the_per_minute_limit_is_a_throttle_not_daily_quota(self, monkeypatch):
        body = ('{"error": {"code": 403, "message": "Quota exceeded for quota metric '
                '\'Queries\' and limit \'Queries per minute\'", '
                '"errors": [{"reason": "rateLimitExceeded"}]}}')
        api = _api_raising(monkeypatch, 403, body)
        with pytest.raises(YD.YouTubePushback) as ei:
            api._get_sync("search", {})
        assert not isinstance(ei.value, YD.QuotaExceeded)
        assert ei.value.stop_code == "rate_limited"

    def test_daily_quota_is_still_quota_and_does_not_slow_the_platform(self, monkeypatch):
        body = '{"error": {"errors": [{"reason": "quotaExceeded"}], "message": "exceeded your quota"}}'
        api = _api_raising(monkeypatch, 403, body)
        with pytest.raises(YD.QuotaExceeded):
            api._get_sync("search", {})
        assert _gov().pushbacks == 0

    def test_a_plain_403_keeps_todays_behaviour(self, monkeypatch):
        body = '{"error": {"errors": [{"reason": "forbidden"}], "message": "invalid caller"}}'
        api = _api_raising(monkeypatch, 403, body)
        with pytest.raises(RuntimeError) as ei:
            api._get_sync("channels", {})
        assert type(ei.value) is RuntimeError
        assert "API key invalid" not in str(ei.value)   # "invalid" alone is not a key marker
        assert _gov().total == 0

    def test_unusual_traffic_is_a_challenge(self, monkeypatch):
        api = _api_raising(monkeypatch, 429, "<html>Our systems have detected unusual traffic</html>")
        with pytest.raises(YD.YouTubePushback) as ei:
            api._get_sync("search", {})
        assert ei.value.stop_code == "checkpoint"
        assert _gov().last_signal.kind == CHALLENGED

    def test_a_redirect_to_the_sorry_page_is_a_challenge(self, monkeypatch):
        api = _api_raising(monkeypatch, 429, "<html></html>",
                           url="https://www.google.com/sorry/index?continue=x")
        with pytest.raises(YD.YouTubePushback) as ei:
            api._get_sync("search", {})
        assert ei.value.stop_code == "checkpoint"

    @pytest.mark.parametrize("code,body", [
        (400, '{"error": {"message": "API key not valid. Please pass a valid API key."}}'),
        (403, '{"error": {"errors": [{"reason": "keyInvalid"}]}}'),
    ])
    def test_a_real_key_rejection_is_named(self, monkeypatch, code, body):
        api = _api_raising(monkeypatch, code, body)
        with pytest.raises(YD.YouTubeKeyInvalid) as ei:
            api._get_sync("search", {})
        assert classify_failure(ei.value) == "expired"

    def test_a_healthy_response_is_untouched(self, monkeypatch):
        class _Resp(io.BytesIO):
            def geturl(self):
                return "https://www.googleapis.com/youtube/v3/search"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        monkeypatch.setattr(YD.urllib.request, "urlopen",
                            lambda *_a, **_k: _Resp(b'{"items": [1]}'))
        assert YD.YouTubeAPI(key="k")._get_sync("search", {}) == {"items": [1]}
        assert _gov().total == 0


class TestPushbackMessageClassifies:
    def test_throttle_reads_as_rate_limited_never_as_a_dead_key(self):
        e = YD.YouTubePushback(Signal(THROTTLED, "HTTP 429"), "search")
        assert classify_failure(e) == "rate_limited"
        assert not any(ch.isdigit() for ch in str(e))

    def test_challenge_reads_as_checkpointed(self):
        e = YD.YouTubePushback(Signal(CHALLENGED, "body says 'detected unusual traffic'"), "search")
        assert classify_failure(e) == "checkpointed"


# ------------------------------------------------------- search_channels

class TestSearchDoesNotRetryPushback:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc", [
        YD.YouTubePushback(Signal(THROTTLED, "x")),
        YD.YouTubeKeyInvalid("youtube search 400: API key invalid"),
    ])
    async def test_raised_on_the_first_attempt(self, monkeypatch, no_sleep, exc):
        api = YD.YouTubeAPI(key="k")
        calls = []

        async def _get(endpoint, **params):
            calls.append(1)
            raise exc
        monkeypatch.setattr(api, "get", _get)
        with pytest.raises(type(exc)):
            await api.search_channels("acme")
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_an_ordinary_error_is_still_retried(self, monkeypatch, no_sleep):
        api = YD.YouTubeAPI(key="k")
        calls = []

        async def _get(endpoint, **params):
            calls.append(1)
            raise RuntimeError("youtube search 503: backend")
        monkeypatch.setattr(api, "get", _get)
        with pytest.raises(RuntimeError):
            await api.search_channels("acme")
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_total_results_rides_on_the_page(self, monkeypatch, no_sleep):
        api = YD.YouTubeAPI(key="k")

        async def _get(endpoint, **params):
            return {"items": [], "pageInfo": {"totalResults": 7}}
        monkeypatch.setattr(api, "get", _get)
        items, token = await api.search_channels("acme")
        assert items == [] and token == "" and items.total_results == 7


# ----------------------------------------------------------------- sweep

def _item(cid):
    return {"id": {"channelId": cid}, "snippet": {"channelTitle": cid}}


class _SweepAPI:
    def __init__(self, pages, handles_pushback=None):
        self.pages = list(pages)
        self.handles_pushback = handles_pushback
        self.search_calls = 0

    async def search_channels(self, keyword, token="", per_page=50):
        self.search_calls += 1
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    async def handles(self, ids):
        out = YD._Handles()
        if self.handles_pushback:
            out.pushback = self.handles_pushback
        return out


def _discovery(api):
    d = YD.Discovery.__new__(YD.Discovery)
    d.a = types.SimpleNamespace(max_results=0, max_seconds=0)
    d.api = api
    return d


class TestSweepStops:
    @pytest.mark.asyncio
    async def test_a_throttle_mid_sweep_keeps_the_hits_and_names_itself(self):
        pb = YD.YouTubePushback(Signal(THROTTLED, "per-minute rate limit"), "search")
        d = _discovery(_SweepAPI([([_item("UC1"), _item("UC2")], "tok"), pb]))
        sw = await d.sweep("acme")
        assert sw.stopped == "rate_limited" and sw.complete is False
        assert len(sw.hits) == 2
        assert sw.error.startswith("rate_limited: youtube")
        assert classify_failure(sw.error) == "rate_limited"
        assert "403" not in sw.error and "429" not in sw.error

    @pytest.mark.asyncio
    async def test_a_challenge_stops_as_checkpoint(self):
        pb = YD.YouTubePushback(Signal(CHALLENGED, "body says 'detected unusual traffic'"))
        sw = await _discovery(_SweepAPI([pb])).sweep("acme")
        assert sw.stopped == "checkpoint" and classify_failure(sw.error) == "checkpointed"

    @pytest.mark.asyncio
    async def test_a_throttled_handle_lookup_keeps_the_page_and_stops(self):
        pb = YD.YouTubePushback(Signal(THROTTLED, "x"), "channels")
        api = _SweepAPI([([_item("UC1")], "tok"), ([_item("UC2")], "")], handles_pushback=pb)
        sw = await _discovery(api).sweep("acme")
        assert sw.stopped == "rate_limited" and len(sw.hits) == 1
        assert api.search_calls == 1

    @pytest.mark.asyncio
    async def test_an_empty_first_page_the_api_says_has_results_is_not_complete(self):
        page = YD._Page()
        page.total_results = 12
        sw = await _discovery(_SweepAPI([(page, "")])).sweep("acme")
        assert sw.stopped == "empty-unconfirmed" and sw.complete is False
        assert classify_failure(sw.error) is None

    @pytest.mark.asyncio
    async def test_a_genuinely_empty_search_is_still_exhausted(self):
        sw = await _discovery(_SweepAPI([(YD._Page(), "")])).sweep("acme")
        assert sw.stopped == "exhausted" and sw.complete is True

    @pytest.mark.asyncio
    async def test_a_failing_progress_callback_is_logged(self, monkeypatch):
        warned = []
        monkeypatch.setattr(YD, "log", types.SimpleNamespace(
            warning=warned.append, info=lambda *_: None))

        def _boom(*_a):
            raise ValueError("save failed")
        sw = await _discovery(_SweepAPI([([_item("UC1")], "")])).sweep("acme", on_progress=_boom)
        assert sw.stopped == "exhausted" and len(sw.hits) == 1
        assert any("progress callback failed" in w for w in warned)


# -------------------------------------------------------------- analysis

def _channel(cid=CID, pic="https://yt3.ggpht.com/real=s88"):
    return {
        "id": cid,
        "snippet": {"title": "Acme", "customUrl": "@acme",
                    "thumbnails": {"high": {"url": pic}}},
        "statistics": {"subscriberCount": "10", "videoCount": "3"},
        "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}},
    }


class _RunAPI:
    def __init__(self, channels_exc=None, handle_exc=None, latest_exc=None):
        self.channels_exc = channels_exc
        self.handle_exc = handle_exc
        self.latest_exc = latest_exc
        self.channels_calls = 0
        self.handle_calls = 0

    async def channels(self, ids):
        self.channels_calls += 1
        if self.channels_exc:
            raise self.channels_exc
        return [_channel(i) for i in ids]

    async def channel_by_handle(self, ref):
        self.handle_calls += 1
        if self.handle_exc:
            raise self.handle_exc
        return _channel()

    async def latest_upload(self, _pl):
        if self.latest_exc:
            raise self.latest_exc
        return "2026-01-02"


def _scraper(api):
    sc = YA.Scraper.__new__(YA.Scraper)
    sc.a = ScanOptions(timeout=5, settle=0.01)
    sc.api = api
    return sc


IDS = [f"https://www.youtube.com/channel/UC{c * 22}" for c in "bcd"]


class TestAnalysisRun:
    @pytest.mark.asyncio
    async def test_batch_pushback_stops_without_per_channel_fallback(self):
        api = _RunAPI(channels_exc=YD.YouTubePushback(Signal(THROTTLED, "x"), "channels"))
        sink: list[Row] = []
        await _scraper(api).run([(u, "Acme", "") for u in IDS], rows_out=sink)
        assert api.channels_calls == 1
        assert [r.status for r in sink] == ["CHECKPOINT", "ERROR", "ERROR"]
        assert all("not attempted" in r.notes for r in sink[1:])

    @pytest.mark.asyncio
    async def test_batch_quota_rows_reach_rows_out(self):
        api = _RunAPI(channels_exc=YD.QuotaExceeded("YouTube daily quota exhausted"))
        sink: list[Row] = []
        await _scraper(api).run([(u, "Acme", "") for u in IDS], rows_out=sink)
        assert len(sink) == 3 and sink[0].status == "CHECKPOINT"
        assert sum(r.status == "CHECKPOINT" for r in sink) == 1

    @pytest.mark.asyncio
    async def test_per_channel_pushback_is_a_checkpoint_and_stops_the_loop(self):
        api = _RunAPI(handle_exc=YD.YouTubePushback(Signal(THROTTLED, "x"), "channels"))
        sink: list[Row] = []
        jobs = [(f"https://www.youtube.com/@acme{i}", "Acme", "") for i in range(3)]
        await _scraper(api).run(jobs, rows_out=sink)
        assert api.handle_calls == 1
        assert [r.status for r in sink] == ["CHECKPOINT", "ERROR", "ERROR"]
        assert "throttled" in sink[0].notes

    @pytest.mark.asyncio
    async def test_a_latest_upload_error_keeps_the_channel(self):
        api = _RunAPI(latest_exc=RuntimeError("youtube playlistItems 500: backend"))
        rows = await _scraper(api).run([(IDS[0], "Acme", "")])
        assert rows[0].status == "OK" and rows[0].profile_name == "Acme"
        assert rows[0].posts_seen == "" and "latest upload unread" in rows[0].notes

    @pytest.mark.asyncio
    async def test_a_latest_upload_throttle_still_stops(self):
        api = _RunAPI(latest_exc=YD.YouTubePushback(Signal(THROTTLED, "x"), "playlistItems"))
        rows = await _scraper(api).run([(IDS[0], "Acme", "")])
        assert rows[0].status == "CHECKPOINT"

    @pytest.mark.asyncio
    async def test_one_turns_pushback_into_checkpoint(self):
        api = _RunAPI(handle_exc=YD.YouTubePushback(Signal(THROTTLED, "x")))
        row = await _scraper(api).one("https://www.youtube.com/@acme", "Acme", "")
        assert row.status == "CHECKPOINT"


class TestCheckSession:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", [THROTTLED, CHALLENGED])
    async def test_pushback_is_inconclusive_not_a_rejected_key(self, kind):
        sc = _scraper(None)
        sc.api = types.SimpleNamespace()

        async def _get(*_a, **_k):
            raise YD.YouTubePushback(Signal(kind, "x"), "channels")
        sc.api.get = _get
        with pytest.raises(YD.YouTubePushback):
            await sc.check_session()


GENERATED = "https://yt3.ggpht.com/ytc/AIdro_abc=s88"


class TestAvatarThrottle:
    @pytest.mark.asyncio
    async def test_a_throttled_avatar_is_unknown_and_stops_further_fetches(self, monkeypatch):
        fetches = []

        def _read(uri):
            fetches.append(uri)
            raise YA._AvatarThrottled("avatar fetch answered too many requests")
        sc = _scraper(None)
        monkeypatch.setattr(sc, "_read_avatar", _read)
        rows = []
        for _ in range(2):
            r = Row(url="u", target="t")
            YA.Scraper.fill(r, _channel(pic=GENERATED))
            assert r.has_custom_pic is True        # fill()'s URL-only guess
            await sc._confirm_avatar(r)
            rows.append(r)
        assert len(fetches) == 1
        assert all(r.has_custom_pic is None and r.logo_yes == "No" for r in rows)
        assert all("avatar check unavailable" in r.notes for r in rows)
        assert _gov().last_signal.kind == THROTTLED

    @pytest.mark.asyncio
    async def test_an_ordinary_avatar_failure_keeps_fill(self, monkeypatch):
        def _read(uri):
            raise TimeoutError("slow")
        sc = _scraper(None)
        monkeypatch.setattr(sc, "_read_avatar", _read)
        r = Row(url="u", target="t")
        YA.Scraper.fill(r, _channel(pic=GENERATED))
        await sc._confirm_avatar(r)
        assert r.has_custom_pic is True and _gov().total == 0

    @pytest.mark.parametrize("code,url", [
        (429, GENERATED),
        (403, "https://www.google.com/sorry/index?continue=x"),
    ])
    def test_read_avatar_names_a_throttle(self, monkeypatch, code, url):
        def _urlopen(*_a, **_k):
            raise _http_error(code, "", url)
        monkeypatch.setattr(YA.urllib.request, "urlopen", _urlopen)
        with pytest.raises(YA._AvatarThrottled):
            YA.Scraper._read_avatar(GENERATED)

    def test_read_avatar_passes_other_errors_through(self, monkeypatch):
        def _urlopen(*_a, **_k):
            raise _http_error(404, "", GENERATED)
        monkeypatch.setattr(YA.urllib.request, "urlopen", _urlopen)
        with pytest.raises(urllib.error.HTTPError):
            YA.Scraper._read_avatar(GENERATED)
