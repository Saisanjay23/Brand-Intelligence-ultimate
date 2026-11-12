"""X (Twitter) pushback: when X throttles, challenges, warns or logs the
account out, the engines stop, say so with the shared stop code / row status,
tell the governor once, and keep what they already read. A healthy run is
unchanged -- and a bio or tweet quoting the words is never X speaking.

Nothing here touches the network: pages, contexts and responses are fakes.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.platforms.scan_options import DiscoveryOptions
from backend.platforms.twitter import analysis_engine as XA
from backend.platforms.twitter import discovery_engine as XD
from backend.shared import pushback
from backend.shared.pushback import CHALLENGED, EXPIRED, THROTTLED, classify_response
from backend.shared.resilience import classify_failure

_real_sleep = asyncio.sleep


@pytest.fixture(autouse=True)
def _fresh_governors():
    pushback.reset_all()
    yield
    pushback.reset_all()


def _gov():
    return pushback.governor_for("twitter")


def _user(**kw):
    base = dict(entity_id="42", handle="NASA", name="NASA", followers=10,
                following=2, posts=5, created_iso="2010-01-01", location="X")
    base.update(kw)
    return XD.TwitterUser(**base)


class FakeResp:
    def __init__(self, url, status=200, text="{}"):
        self.url, self.status, self._text = url, status, text

    async def text(self):
        return self._text


# ======================================================================
# x_error_text / RE_CHECKPOINT (B2, A7)
# ======================================================================

class TestOnlyXIsReadNeverAPayload:
    def test_a_bio_in_a_users_payload_is_not_read(self):
        body = '{"data":{"bio":"Rate limit exceeded? OverCapacity \\"code\\":88"}}'
        assert XD.x_error_text(body, allow_body=False) is None

    def test_a_top_level_errors_body_is_read_even_with_users(self):
        body = '{"data":{"u":1},"errors":[{"code":88,"message":"Rate limit exceeded"}]}'
        err = XD.x_error_text(body, allow_body=False)
        assert classify_response("twitter", 200, err).kind == THROTTLED

    def test_x_error_codes_classify(self):
        for code, kind in ((326, CHALLENGED), (226, CHALLENGED), (32, EXPIRED)):
            err = XD.x_error_text(f'{{"errors":[{{"code":{code}}}]}}', allow_body=False)
            assert classify_response("twitter", 200, err).kind == kind

    def test_a_non_json_error_page_is_read(self):
        assert XD.x_error_text("Rate limit exceeded", allow_body=False) == "Rate limit exceeded"

    def test_automation_wording_is_a_checkpoint_on_the_page(self):
        assert XD.RE_CHECKPOINT.search(
            "This request looks like it might be automated. To protect our users")
        assert not XD.RE_CHECKPOINT.search("our posting might be automated soon")


# ======================================================================
# Discovery sweep (A1, A6, A8, B2)
# ======================================================================

def _sweep_page(responses_after_goto, responses_per_scroll, monkeypatch,
                chrome="Search People"):
    """A page whose goto fires `responses_after_goto` and whose every scroll
    fires the next list in `responses_per_scroll` (nothing once exhausted)."""
    cbs = []
    page = MagicMock()
    page.url = "https://x.com/search?q=acme&f=user"
    page.on = MagicMock(side_effect=lambda ev, cb: cbs.append(cb))
    page.wait_for_function = AsyncMock()
    page.close = AsyncMock()
    page.evaluate = AsyncMock(
        side_effect=lambda js, *a: chrome if "cloneNode" in js else [])
    page.inner_text = AsyncMock(return_value=chrome)

    async def goto(*a, **k):
        for r in responses_after_goto:
            for cb in cbs:
                cb(r)
        await _real_sleep(0)

    page.goto = AsyncMock(side_effect=goto)
    scrolls = list(responses_per_scroll)

    async def scroll(p, **k):
        batch = scrolls.pop(0) if scrolls else []
        for r in batch:
            for cb in cbs:
                cb(r)
        await _real_sleep(0)

    async def no_sleep(s, *a, **k):
        await _real_sleep(0)

    monkeypatch.setattr(XD, "natural_scroll_down", scroll)
    monkeypatch.setattr(XD, "humanize_interaction", AsyncMock())
    monkeypatch.setattr(XD, "hover_element_safely", AsyncMock())
    monkeypatch.setattr(XD.asyncio, "sleep", no_sleep)
    ctx = MagicMock()
    ctx.new_page = AsyncMock(return_value=page)
    return ctx


@pytest.fixture
def search_states(monkeypatch):
    """Each JSON body's "page" key picks the users/cursor search_state returns."""
    states = {}

    def fake_state(blob, probe=None):
        key = blob.get("page") if isinstance(blob, dict) else None
        users, cursor = states.get(key, ([], ""))
        return SimpleNamespace(users=users, bottom_cursor=cursor)

    monkeypatch.setattr(XD, "search_state", fake_state)
    return states


def _search(status=200, text="{}"):
    return FakeResp("https://x.com/i/api/graphql/q/SearchTimeline", status, text)


def _opts():
    return DiscoveryOptions(timeout=5, settle=0.3, page_wait=0.3, max_results=0,
                            max_seconds=30, patience=4)


class TestDiscoveryStopsOnPushback:
    @pytest.mark.asyncio
    async def test_locked_account_after_results_is_checkpoint_not_exhausted(
            self, search_states, monkeypatch):
        search_states[1] = ([_user(entity_id="1", handle="a"),
                             _user(entity_id="2", handle="b")], "c1")
        locked = '{"errors":[{"code":326,"message":"To protect our users from spam"}]}'
        ctx = _sweep_page([_search(text='{"page":1}')],
                          [[_search(403, locked)]], monkeypatch)
        sweep = await XD.Discovery(_opts(), ctx).sweep("acme")
        assert sweep.stopped == "checkpoint" and not sweep.complete
        assert sweep.error.startswith("checkpoint: x search answered HTTP 403")
        assert classify_failure(sweep.error) == "checkpointed"
        assert len(sweep.hits) == 2                     # results kept
        assert _gov().pushbacks == 1 and _gov().last_signal.kind == CHALLENGED

    @pytest.mark.asyncio
    async def test_bare_403_is_login_and_never_complete(self, search_states, monkeypatch):
        search_states[1] = ([_user(entity_id="1", handle="a")], "c1")
        ctx = _sweep_page([_search(text='{"page":1}')],
                          [[_search(403, "{}")]], monkeypatch)
        sweep = await XD.Discovery(_opts(), ctx).sweep("acme")
        assert sweep.stopped == "login" and not sweep.complete
        assert classify_failure(sweep.error) == "expired"

    @pytest.mark.asyncio
    async def test_429_still_rate_limited_and_recorded_once(self, search_states, monkeypatch):
        ctx = _sweep_page([_search(429)], [], monkeypatch)
        sweep = await XD.Discovery(_opts(), ctx).sweep("acme")
        assert sweep.stopped == "rate_limited"
        assert sweep.error.startswith("rate_limited:")
        assert _gov().pushbacks == 1

    @pytest.mark.asyncio
    async def test_a_bio_quoting_rate_limit_does_not_stop_a_healthy_sweep(
            self, search_states, monkeypatch):
        # B2: the old whole-body check ended this sweep as rate_limited.
        search_states[1] = ([_user(entity_id="1", handle="a")], "c1")
        search_states[2] = ([], "c1")                   # X's empty end answer
        ctx = _sweep_page(
            [_search(text='{"page":1,"bio":"Rate limit exceeded -- OverCapacity"}')],
            [[_search(text='{"page":2}')]], monkeypatch)
        sweep = await XD.Discovery(_opts(), ctx).sweep("acme")
        assert sweep.stopped == "exhausted" and sweep.complete
        assert len(sweep.hits) == 1
        assert _gov().pushbacks == 0

    @pytest.mark.asyncio
    async def test_a_page_wall_records_a_signal(self, search_states, monkeypatch):
        ctx = _sweep_page([], [], monkeypatch, chrome="Verify your identity")
        sweep = await XD.Discovery(_opts(), ctx).sweep("acme")
        assert sweep.stopped == "checkpoint"
        assert sweep.error.startswith("checkpoint:")
        assert _gov().last_signal.kind == CHALLENGED


class TestLoginWallMidSweep:
    @pytest.mark.asyncio
    async def test_login_wall_is_named_when_cells_were_cut_out(self):
        page = MagicMock(url="https://x.com/search?q=acme&f=user")
        page.evaluate = AsyncMock(return_value="Don't miss what's happening Sign up for X")
        assert await XD._x_session_blocked(page, have_results=True) == "login"

    @pytest.mark.asyncio
    async def test_a_bio_is_not_trusted_when_the_cut_failed(self):
        page = MagicMock(url="https://x.com/search?q=acme&f=user")
        page.evaluate = AsyncMock(side_effect=RuntimeError("closed"))
        page.inner_text = AsyncMock(return_value="Acme -- Sign in to X to see our deals")
        assert await XD._x_session_blocked(page, have_results=True) == ""


# ======================================================================
# Analysis (A2-A7, B1, B2)
# ======================================================================

class FakePage:
    def __init__(self, body="", responses=None, url="", dialog="", redirect=""):
        self.body, self.responses, self.url = body, responses or [], url
        self.dialog, self.redirect = dialog, redirect
        self._cb = None
        self.reloads = 0

    def on(self, event, cb):
        if event == "response":
            self._cb = cb

    async def goto(self, url, **kw):
        self.url = self.redirect or url.split("?")[0]
        for r in self.responses:
            if self._cb:
                self._cb(r)
        await asyncio.sleep(0)

    async def reload(self, **kw):
        self.reloads += 1

    async def wait_for_selector(self, *a, **k):
        return None

    async def evaluate(self, js, *args):
        if 'role="dialog"' in js:
            return self.dialog
        if "cloneNode" in js:
            return self.body
        if js is XA.JS_TWEET_TIMES:
            return []
        if "UserName" in js:
            return {"name": "", "own": False}
        return {}

    async def inner_text(self, sel):
        return self.body

    async def close(self):
        pass


class FakeCtx:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.opened = 0

    async def new_page(self):
        self.opened += 1
        return self.pages.pop(0) if self.pages else FakePage()


def _scraper(ctx):
    s = XA.Scraper.__new__(XA.Scraper)
    s.a = SimpleNamespace(timeout=5, settle=0.05, delay=0, evidence=None, cancel=None)
    s.evidence = None
    s.session = SimpleNamespace(ctx=ctx)
    s.screenshot = AsyncMock()
    return s


@pytest.fixture
def users(monkeypatch):
    """A user-query response's text maps to the users iter_users yields."""
    table = {}
    monkeypatch.setattr(XA, "parse_lines", lambda text: [text])
    monkeypatch.setattr(XA, "iter_users", lambda blob: table.get(blob, []))
    monkeypatch.setattr(XA, "latest_post", lambda blob, w, e: "")
    monkeypatch.setattr(XA, "latest_repost", lambda blob, w, e: "")
    monkeypatch.setattr(XA, "about_account_from", lambda blob: None)
    return table


def _ubsn(status=200, text='{"u":1}'):
    return FakeResp("https://x.com/i/api/graphql/a/UserByScreenName", status, text)


async def _run(*pages):
    ctx = FakeCtx(*pages)
    row = await _scraper(ctx).process("https://x.com/NASA", "NASA", "f")
    return row, ctx


class TestAnalysisResponses:
    @pytest.mark.asyncio
    async def test_locked_account_answer_is_checkpoint_with_no_reload(self, users):
        page = FakePage(body="NASA", responses=[
            _ubsn(403, '{"errors":[{"code":326,"message":"locked"}]}')])
        row, _ = await _run(page)
        assert row.status == "CHECKPOINT" and page.reloads == 0
        assert classify_failure(row.notes) == "checkpointed"
        assert _gov().pushbacks == 1 and _gov().last_signal.kind == CHALLENGED

    @pytest.mark.asyncio
    async def test_rejected_login_on_the_user_query_is_login_required(self, users):
        page = FakePage(body="NASA", responses=[_ubsn(401, "{}")])
        row, _ = await _run(page)
        assert row.status == "LOGIN_REQUIRED" and page.reloads == 0

    @pytest.mark.asyncio
    async def test_a_bio_quoting_rate_limit_is_not_a_throttle(self, users):
        bio = '{"bio":"Rate limit exceeded \\"code\\":88"}'
        users[bio] = [_user(location="")]
        page = FakePage(body="NASA", responses=[_ubsn(text=bio)])
        row, ctx = await _run(page)
        assert row.status == "OK"
        assert ctx.opened > 1                          # extra tabs still read
        assert _gov().pushbacks == 0

    @pytest.mark.asyncio
    async def test_timeline_429_after_a_good_profile_is_recorded(self, users):
        users['{"u":1}'] = [_user(location="")]
        page = FakePage(body="NASA", responses=[
            _ubsn(), FakeResp("https://x.com/i/api/graphql/a/UserTweets", 429)])
        row, ctx = await _run(page)
        assert row.status == "OK" and ctx.opened == 1
        assert _gov().pushbacks == 1 and _gov().last_signal.kind == THROTTLED

    @pytest.mark.asyncio
    async def test_a_timeline_bare_403_does_not_touch_pacing(self, users):
        users['{"u":1}'] = [_user()]
        page = FakePage(body="NASA", responses=[
            _ubsn(), FakeResp("https://x.com/i/api/graphql/a/UserTweets", 403, "{}")])
        row, _ = await _run(page)
        assert row.status == "OK" and _gov().pushbacks == 0


class TestAnalysisWarningDialog:
    @pytest.mark.asyncio
    async def test_automation_popup_over_a_loaded_profile_is_checkpoint(self, users):
        users['{"u":1}'] = [_user(location="")]
        page = FakePage(body="NASA", responses=[_ubsn()],
                        dialog="This request looks like it might be automated.")
        row, ctx = await _run(page)
        assert row.status == "CHECKPOINT" and ctx.opened == 1
        assert classify_failure(row.notes) == "checkpointed"
        assert _gov().last_signal.kind == CHALLENGED

    @pytest.mark.asyncio
    async def test_throttle_popup_keeps_the_row_and_reads_no_more_tabs(self, users):
        users['{"u":1}'] = [_user(location="")]
        page = FakePage(body="NASA", responses=[_ubsn()],
                        dialog="You've been temporarily blocked. Try again later.")
        row, ctx = await _run(page)
        assert row.status == "OK" and ctx.opened == 1
        assert _gov().pushbacks == 1 and _gov().last_signal.kind == THROTTLED

    @pytest.mark.asyncio
    async def test_a_bio_quoting_the_words_is_not_a_popup(self, users):
        users['{"u":1}'] = [_user()]
        page = FakePage(body="NASA might be automated? Verify to continue", responses=[_ubsn()])
        row, _ = await _run(page)
        assert row.status == "OK"


class TestAnalysisHelperTabWalls:
    @pytest.mark.asyncio
    async def test_login_wall_on_replies_tab_is_login_required_with_fields(self, users):
        users['{"u":1}'] = [_user(location="")]
        main = FakePage(body="NASA", responses=[_ubsn()])
        replies = FakePage(body="Sign in to X")
        row, ctx = await _run(main, replies)
        assert row.status == "LOGIN_REQUIRED" and ctx.opened == 2
        assert row.followers == 10                     # fields kept
        assert _gov().last_signal.kind == EXPIRED

    @pytest.mark.asyncio
    async def test_account_access_on_about_tab_is_checkpoint(self, users):
        users['{"u":1}'] = [_user(location="", posts=0)]
        main = FakePage(body="NASA", responses=[_ubsn()])
        about = FakePage(redirect="https://x.com/account/access")
        row, _ = await _run(main, about)
        assert row.status == "CHECKPOINT"
        assert classify_failure(row.notes) == "checkpointed"
        assert _gov().last_signal.kind == CHALLENGED

    @pytest.mark.asyncio
    async def test_rate_limit_on_replies_tab_keeps_row_and_stops_tabs(self, users):
        users['{"u":1}'] = [_user(location="")]
        main = FakePage(body="NASA", responses=[_ubsn()])
        replies = FakePage(body="Something went wrong. Rate limit exceeded")
        row, ctx = await _run(main, replies)
        assert row.status == "OK" and ctx.opened == 2  # /reposts, /about skipped
        assert "about tab not read -- rate limited" in row.notes
        assert _gov().last_signal.kind == THROTTLED

    def test_account_access_is_a_wall(self):
        page = SimpleNamespace(url="https://x.com/account/access")
        assert XA.Scraper._is_wall(page, "")
        assert not XA.Scraper._is_wall(SimpleNamespace(url="https://x.com/account"), "")


class TestDomReadsUseTheHandle:
    @pytest.mark.asyncio
    async def test_counts_and_name_are_read_by_handle_not_numeric_id(self, users, monkeypatch):
        users['{"u":1}'] = [_user(followers=None, name="")]
        seen = []

        async def fake_counts(page, handle):
            seen.append(("counts", handle))
            return {}

        async def fake_name(page, handle=""):
            seen.append(("name", handle))
            return ""

        monkeypatch.setattr(XA, "dom_counts", fake_counts)
        monkeypatch.setattr(XA, "dom_display_name", fake_name)
        await _run(FakePage(body="NASA", responses=[_ubsn()]))
        assert ("counts", "NASA") in seen and ("name", "NASA") in seen
        assert all(h != "42" for _, h in seen)
