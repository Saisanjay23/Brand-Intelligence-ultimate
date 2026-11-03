"""Review fixes for the X and Telegram analysis/discovery engines.

Governing rule: a blocked, throttled or half-loaded read must never look like
a clean result, and "unknown" must never be filed as a definite value.

Nothing here touches a live platform: X pages are fakes, the one real-DOM test
loads static HTML into a local headless browser, and Telegram is driven
through fake clients.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.platforms.telegram import analysis_engine as TA
from backend.platforms.telegram import discovery_engine as TD
from backend.platforms.twitter import analysis_engine as XA
from backend.platforms.twitter import discovery_engine as XD
from backend.shared.models.row import Row


# --------------------------------------------------------------------------
# X: fake page / context
# --------------------------------------------------------------------------

class FakeResp:
    def __init__(self, url, status=200, text="{}"):
        self.url, self.status, self._text = url, status, text

    async def text(self):
        return self._text


class FakePage:
    """Just enough Playwright for Scraper.process() and its helper tabs."""

    def __init__(self, body="", cells=None, responses=None, url="", log=None):
        self.body = body
        self.cells = cells or []
        self.responses = responses or []
        self.url = url
        self.log = log if log is not None else []
        self._cb = None
        self.closed = False

    def on(self, event, cb):
        if event == "response":
            self._cb = cb

    async def goto(self, url, **kw):
        self.url = url.split("?")[0]
        self.log.append(url)
        for r in self.responses:
            if self._cb:
                self._cb(r)
        await asyncio.sleep(0)

    async def reload(self, **kw):
        return None

    async def wait_for_selector(self, *a, **k):
        return None

    async def evaluate(self, js, *args):
        if "cloneNode" in js:
            return self.body
        if js is XA.JS_TWEET_TIMES:
            return self.cells
        if js is XA.JS_DISPLAY_NAME or "UserName" in js:
            return {"name": "", "own": False}
        return {}

    async def inner_text(self, sel):
        return self.body

    async def close(self):
        self.closed = True


class FakeCtx:
    def __init__(self, first_page, others=None):
        self.pages = [first_page]
        self.others = others or []
        self.opened = 0

    async def new_page(self):
        self.opened += 1
        if self.pages:
            return self.pages.pop(0)
        return FakePage(log=self.log_ref) if hasattr(self, "log_ref") else FakePage()


def _user(**kw):
    base = dict(entity_id="42", handle="NASA", name="NASA", followers=10,
                following=2, posts=5, created_iso="2010-01-01", location="X")
    base.update(kw)
    return XD.TwitterUser(**base)


def _scraper(ctx, **opts):
    args = SimpleNamespace(timeout=5, settle=0.05, delay=0, evidence=None,
                           cancel=opts.pop("cancel", None), **opts)
    s = XA.Scraper.__new__(XA.Scraper)
    s.a = args
    s.evidence = None
    s.session = SimpleNamespace(ctx=ctx)
    s.screenshot = AsyncMock()
    return s


@pytest.fixture
def patched_parsers(monkeypatch):
    """Skip the GraphQL shape: a response's text IS what the parsers yield."""
    users = {}
    monkeypatch.setattr(XA, "parse_lines", lambda text: [text])

    def fake_iter_users(blob):
        return users.get(blob, [])

    monkeypatch.setattr(XA, "iter_users", fake_iter_users)
    monkeypatch.setattr(XA, "latest_post", lambda blob, w, e: "")
    monkeypatch.setattr(XA, "latest_repost", lambda blob, w, e: "")
    monkeypatch.setattr(XA, "about_account_from", lambda blob: None)
    return users


# --------------------------------------------------------------------------
# X 1: payload under a login wall is not OK
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_x_payload_under_login_wall_is_login_required(patched_parsers):
    patched_parsers["USERBLOB"] = [_user()]
    page = FakePage(
        body="Don't miss what's happening  Sign up for X",
        responses=[FakeResp("https://x.com/i/api/graphql/abc/UserByScreenName", text="USERBLOB")],
    )
    ctx = FakeCtx(page)
    row = await _scraper(ctx).process("https://x.com/NASA", "NASA", "f")
    assert row.status == "LOGIN_REQUIRED"
    assert "logged out" in row.notes
    assert ctx.opened == 1                      # no extra tabs on a dead session
    assert row.status != "OK"


@pytest.mark.asyncio
async def test_x_payload_on_login_url_is_login_required(patched_parsers):
    patched_parsers["USERBLOB"] = [_user()]

    class Redirected(FakePage):
        async def goto(self, url, **kw):
            await super().goto("https://x.com/i/flow/login", **kw)

    page = Redirected(body="", responses=[
        FakeResp("https://x.com/i/api/graphql/abc/UserByScreenName", text="USERBLOB")])
    row = await _scraper(FakeCtx(page)).process("https://x.com/NASA", "NASA", "f")
    assert row.status == "LOGIN_REQUIRED"


# --------------------------------------------------------------------------
# X 2: throttled timeline -> no extra tabs, a note
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_x_timeline_429_skips_extra_tabs_and_says_so(patched_parsers):
    patched_parsers["USERBLOB"] = [_user(location="")]
    page = FakePage(body="NASA", responses=[
        FakeResp("https://x.com/i/api/graphql/abc/UserByScreenName", text="USERBLOB"),
        FakeResp("https://x.com/i/api/graphql/abc/UserTweets", status=429),
    ])
    ctx = FakeCtx(page)
    row = await _scraper(ctx).process("https://x.com/NASA", "NASA", "f")
    assert ctx.opened == 1                      # /with_replies, /reposts, /about NOT opened
    assert "timeline rate-limited -- last post unknown" in row.notes
    assert "about tab not read -- rate limited" in row.notes
    assert row.last_post_iso == ""


# --------------------------------------------------------------------------
# X 5: tab URLs come from the handle, not the pasted URL
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_x_tab_urls_are_built_from_the_handle(patched_parsers):
    patched_parsers["USERBLOB"] = [_user(location="")]
    log: list[str] = []
    page = FakePage(body="NASA", log=log, responses=[
        FakeResp("https://x.com/i/api/graphql/abc/UserByScreenName", text="USERBLOB")])
    ctx = FakeCtx(page)
    ctx.log_ref = log
    row = await _scraper(ctx).process("https://x.com/NASA/status/1", "NASA", "f")
    assert row.url == "https://x.com/NASA"
    assert log[0] == "https://x.com/NASA?lang=en"
    assert "https://x.com/NASA/with_replies?lang=en" in log
    assert "https://x.com/NASA/reposts?lang=en" in log
    assert "https://x.com/NASA/about" in log
    assert not any("status" in u for u in log)


def test_x_normalize_url_reduces_to_the_profile():
    n = XA.normalize_url
    assert n("https://x.com/NASA/with_replies") == "https://x.com/NASA"
    assert n("https://twitter.com/NASA/status/123?s=20") == "https://x.com/NASA"
    assert n("https://x.com/@NASA/") == "https://x.com/NASA"
    assert XA.handle_of("https://x.com/NASA/status/1") == "NASA"
    # reserved routes are not turned into somebody's profile
    assert XA.handle_of(n("https://x.com/home")) == ""
    assert n("https://x.com/i/flow/login") == "https://x.com/i/flow/login"


# --------------------------------------------------------------------------
# X 7: cancel + pacing
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_x_cancel_stops_before_the_extra_tabs(patched_parsers):
    patched_parsers["USERBLOB"] = [_user(location="")]
    page = FakePage(body="NASA", responses=[
        FakeResp("https://x.com/i/api/graphql/abc/UserByScreenName", text="USERBLOB")])
    ctx = FakeCtx(page)
    s = _scraper(ctx, cancel=True)
    row = await s.process("https://x.com/NASA", "NASA", "f")
    assert ctx.opened == 1
    assert row.status == "PARTIAL"              # an unfinished read is not OK
    assert "stopped" in row.notes
    s.screenshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_x_extra_tabs_are_paced_with_jitter(monkeypatch):
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(XA.asyncio, "sleep", fake_sleep)
    s = _scraper(FakeCtx(FakePage()))
    s.a.delay = 2.0
    for _ in range(20):
        await s._between_tiers()
    assert all(1.2 <= x <= 2.8 for x in slept)
    assert len(set(round(x, 6) for x in slept)) > 1
    slept.clear()
    s.a.delay = 0
    await s._between_tiers()
    assert slept == []


# --------------------------------------------------------------------------
# X 2b: helpers say so when they come back empty
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_x_helper_tabs_leave_a_note_when_empty(patched_parsers):
    s = _scraper(FakeCtx(FakePage(body="Sign in to X", url="https://x.com/NASA/about")))
    row = Row(url="https://x.com/NASA", target="NASA")
    assert await s.read_about_panel("https://x.com/NASA", row=row) is None
    assert "about tab not read" in row.notes and "login wall" in row.notes

    s = _scraper(FakeCtx(FakePage(body="")))
    row = Row(url="https://x.com/NASA", target="NASA")
    assert await s.reposts_tab_last_post("https://x.com/NASA", "nasa", "42", row=row) == ""
    assert "reposts tab not read" in row.notes

    s = _scraper(FakeCtx(FakePage(body="Something went wrong. Rate limit exceeded")))
    row = Row(url="https://x.com/NASA", target="NASA")
    assert await s.replies_tab_last_post("https://x.com/NASA", "nasa", "42", row=row) == ""
    assert "replies tab not read" in row.notes and "rate limit" in row.notes


@pytest.mark.asyncio
async def test_x_helper_exception_is_noted_not_swallowed():
    class Boom(FakePage):
        async def goto(self, url, **kw):
            raise RuntimeError("net::ERR")

    s = _scraper(FakeCtx(Boom()))
    row = Row(url="https://x.com/NASA", target="NASA")
    assert await s.read_about_panel("https://x.com/NASA", row=row) is None
    assert "about tab not read -- RuntimeError" in row.notes


# --------------------------------------------------------------------------
# X 3: DOM last post ignores ads and other authors
# --------------------------------------------------------------------------

_TIMELINE_HTML = """
<div data-testid="tweet"><div data-testid="User-Name"><a href="/NASA">NASA</a><a href="/nasa">@NASA</a></div>
  <time datetime="2020-01-05T10:00:00.000Z"></time></div>
<div data-testid="tweet"><div data-testid="User-Name"><a href="/Someone">S</a></div>
  <time datetime="2026-10-01T10:00:00.000Z"></time><a href="/NASA">Replying to @NASA</a></div>
<div data-testid="placementTracking"><div data-testid="tweet"><div data-testid="User-Name"><a href="/NASA">NASA</a></div>
  <time datetime="2026-11-01T10:00:00.000Z"></time></div></div>
<div data-testid="tweet"><div data-testid="User-Name"><a href="/NASA">NASA</a></div>
  <time datetime="2026-09-01T10:00:00.000Z"></time><div data-testid="socialContext">NASA reposted</div></div>
<div data-testid="tweet"><time datetime="2026-08-01T10:00:00.000Z"></time></div>
"""


@pytest.mark.asyncio
async def test_x_dom_last_post_keeps_only_the_accounts_own_organic_cells():
    pw = pytest.importorskip("playwright.async_api")
    try:
        ctxm = pw.async_playwright()
        p = await ctxm.__aenter__()
        browser = await p.chromium.launch()
    except Exception as e:                      # no local browser in this env
        pytest.skip(f"no local chromium: {e}")
    try:
        page = await browser.new_page()
        await page.set_content(_TIMELINE_HTML)
        assert await XA.dom_last_post(page, "nasa") == "2020-01-05"
        assert await XA.dom_last_post(page, "@NASA") == "2020-01-05"
        # the other author's reply context and the ad are never counted
        assert await XA.dom_last_post(page, "Someone") == "2026-10-01"
        await page.close()
    finally:
        await browser.close()
        await ctxm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_x_dom_last_post_passes_the_handle_to_the_page():
    seen = {}

    class P:
        async def evaluate(self, js, *args):
            seen["args"] = args
            return [{"dt": "2024-03-04T00:00:00Z", "repostOrPinned": False}]

    assert await XA.dom_last_post(P(), "@NASA") == "2024-03-04"
    assert seen["args"] == ("NASA",)


# --------------------------------------------------------------------------
# X 4: counts
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("12.3萬 位追蹤者", (None, False)),
    ("1,5 Mio. Follower", (None, False)),
    ("3 mil seguidores", (None, False)),
    ("1.2M Followers", (1_200_000, False)),
    ("1.2 M Followers", (1_200_000, False)),
    ("154K Followers", (154_000, False)),
    ("1,234 Followers", (1234, True)),
    ("0 Followers", (0, True)),
    ("12 Following", (12, True)),
    ("5x", (None, False)),
    ("Followers", (None, False)),
    ("", (None, False)),
])
def test_x_count_from_text_never_returns_a_wrong_number(text, expected):
    assert XA.count_from_text(text) == expected


# --------------------------------------------------------------------------
# X 6: verified means a platform-issued badge
# --------------------------------------------------------------------------

def _payload(top=None, **legacy):
    res = {"__typename": "User", "rest_id": "1",
           "core": {"screen_name": "nasa", "name": "NASA"}, "legacy": dict(legacy)}
    res.update(top or {})
    return {"data": {"user": {"result": res}}}


def test_x_blue_check_is_not_verified():
    u = next(XD.iter_users(_payload({"is_blue_verified": True})))
    assert u.verified is False and u.verified_type == "blue"
    row = Row(url="https://x.com/nasa", target="NASA")
    XA.Scraper.fill(row, u)
    assert row.verified is None                 # unknown/not badge, never True
    assert "blue (paid) check" in row.notes
    assert "verified account" not in row.notes


def test_x_business_and_government_types_are_verified():
    for t in ("Business", "Government"):
        u = next(XD.iter_users(_payload({"is_blue_verified": True}, verified_type=t)))
        assert u.verified is True
        row = Row(url="https://x.com/nasa", target="NASA")
        XA.Scraper.fill(row, u)
        assert row.verified is True
    legacy = next(XD.iter_users(_payload(verified=True)))
    assert legacy.verified is True


def test_x_discovery_row_does_not_inherit_a_paid_check():
    u = next(XD.iter_users(_payload({"is_blue_verified": True})))
    row = XD.user_to_row(u, "nasa")
    assert row.verified is False
    assert "blue (paid) check" in row.notes
    dom = XD.TwitterUser(handle="nasa", verified=False, verified_type="badge")
    assert XD.user_to_row(dom, "nasa", source="dom").verified is None


# --------------------------------------------------------------------------
# Telegram 8: distinct states
# --------------------------------------------------------------------------

def _tg(client):
    tg = TD.Telegram()
    tg.client = client
    return tg


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,reason", [
    (TD.ChannelPrivateError(request=None), "private"),
    (TD.UsernameInvalidError(request=None), "invalid"),
])
async def test_tg_private_and_invalid_are_not_gone(exc, reason):
    tg = _tg(SimpleNamespace(get_entity=AsyncMock(side_effect=exc)))
    with pytest.raises(TD.EntityInaccessible) as ei:
        await tg.resolve("name")
    assert ei.value.reason == reason


@pytest.mark.asyncio
async def test_tg_not_occupied_stays_gone():
    tg = _tg(SimpleNamespace(
        get_entity=AsyncMock(side_effect=TD.UsernameNotOccupiedError(request=None))))
    assert await tg.resolve("name") is None
    tg = _tg(SimpleNamespace(
        get_entity=AsyncMock(side_effect=ValueError('No user has "x" as username'))))
    assert await tg.resolve("x") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,word", [
    (TD.ChannelPrivateError(request=None), "private/inaccessible"),
    (TD.UsernameInvalidError(request=None), "invalid handle"),
])
async def test_tg_scraper_reports_private_and_invalid_as_error_not_gone(exc, word):
    s = TA.Scraper(SimpleNamespace(delay=0))
    s.tg = TD.Telegram()
    s.tg.client = SimpleNamespace(get_entity=AsyncMock(side_effect=exc))
    row = await s.one("https://t.me/somewhere", "T", "")
    assert row.status == "ERROR" and row.status != "GONE"
    assert word in row.notes


@pytest.mark.asyncio
async def test_tg_scraper_gone_only_for_not_occupied():
    s = TA.Scraper(SimpleNamespace(delay=0))
    s.tg = SimpleNamespace(resolve=AsyncMock(return_value=None))
    assert (await s.one("https://t.me/nobody", "T", "")).status == "GONE"


def test_tg_deep_links():
    assert TA.normalize_url("tg://resolve?domain=Durov") == "https://t.me/Durov"
    assert TA.username_of("tg://resolve?domain=Durov") == "Durov"
    assert TA.normalize_url("tg://resolve?domain=Durov&post=5") == "https://t.me/Durov"
    inv = TA.normalize_url("tg://join?invite=AbCd123")
    assert inv == "https://t.me/+AbCd123"
    assert TA.username_of(inv) == ""
    assert "resolve" not in TA.normalize_url("tg://resolve?domain=x").split("//")[1].split("/")[0]
    assert TA.normalize_url("tg://user?id=1") == ""


@pytest.mark.asyncio
async def test_tg_invite_deep_link_is_error_not_gone():
    s = TA.Scraper(SimpleNamespace(delay=0))
    s.tg = SimpleNamespace(resolve=AsyncMock())
    row = await s.one("tg://join?invite=AbCd123", "T", "")
    assert row.status == "ERROR"
    s.tg.resolve.assert_not_awaited()


# --------------------------------------------------------------------------
# Telegram 9: every flood wait reaches the handlers
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tg_flood_threshold_is_zero_once_authorised(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    seen = {}
    client = MagicMock()
    client.flood_sleep_threshold = 60
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()

    async def authorised():
        # the auth probe swallows RPC errors as "not logged in", so the
        # threshold must still be the default while it runs
        seen["during_auth"] = client.flood_sleep_threshold
        return True

    client.is_user_authorized = authorised
    with patch.object(TD, "TelegramClient", return_value=client):
        tg = TD.Telegram()
        await tg.start()
    assert seen["during_auth"] == 60
    assert client.flood_sleep_threshold == 0
    await tg.stop()


@pytest.mark.asyncio
async def test_tg_flood_wait_seconds_reach_the_checkpoint_note():
    s = TA.Scraper(SimpleNamespace(delay=0))
    s.tg = SimpleNamespace(resolve=AsyncMock(side_effect=TD.FloodWait(30)))
    row = await s.one("https://t.me/x", "T", "")
    assert row.status == "CHECKPOINT"
    assert "30s" in row.notes


@pytest.mark.asyncio
async def test_tg_flood_during_connect_is_a_flood_not_a_dead_session(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    client = MagicMock()
    client.connect = AsyncMock(side_effect=TD.FloodWaitError(request=None, capture=7))
    client.disconnect = AsyncMock()
    with patch.object(TD, "TelegramClient", return_value=client):
        tg = TD.Telegram()
        with pytest.raises(TD.FloodWait) as ei:
            await tg.start()
    assert ei.value.seconds == 7


# --------------------------------------------------------------------------
# Telegram 10: created date vs join date
# --------------------------------------------------------------------------

class Channel:
    def __init__(self, **kw):
        self.id = 1
        self.username = "chan"
        self.title = "Chan"
        self.broadcast = True
        self.participants_count = 10
        self.photo = None
        self.__dict__.update(kw)


_DATE = datetime(2021, 5, 6, tzinfo=timezone.utc)


def test_tg_created_blank_when_session_is_a_member():
    member = TD.entity_from(Channel(date=_DATE, left=False))
    assert member.created_iso == "" and member.created_withheld
    unknown = TD.entity_from(Channel(date=_DATE))          # no `left` at all
    assert unknown.created_iso == ""
    outsider = TD.entity_from(Channel(date=_DATE, left=True))
    assert outsider.created_iso == "2021-05-06"

    row = Row(url="https://t.me/chan", target="T")
    TA.Scraper.fill(row, member)
    assert row.created_iso == "" and "join date" in row.notes


# --------------------------------------------------------------------------
# Telegram 11: last post and members
# --------------------------------------------------------------------------

class Message:
    def __init__(self, d):
        self.date = d


class MessageService:
    def __init__(self, d):
        self.date = d


def _d(day):
    return datetime(2026, 1, day, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_tg_last_post_skips_service_messages():
    tg = _tg(SimpleNamespace(get_messages=AsyncMock(
        return_value=[MessageService(_d(20)), Message(_d(9)), Message(_d(3))])))
    assert await tg.last_post(object()) == "2026-01-09"


@pytest.mark.asyncio
async def test_tg_last_post_failure_is_noted_flood_propagates():
    notes: list = []
    tg = _tg(SimpleNamespace(get_messages=AsyncMock(side_effect=RuntimeError("boom"))))
    assert await tg.last_post(object(), notes) == ""
    assert notes == ["last post not read"]
    tg = _tg(SimpleNamespace(get_messages=AsyncMock(
        side_effect=TD.FloodWaitError(request=None, capture=9))))
    with pytest.raises(TD.FloodWait):
        await tg.last_post(object(), [])


class User:
    def __init__(self, **kw):
        self.id = 5
        self.username = "bob"
        self.first_name = "Bob"
        self.last_name = ""
        self.photo = None
        self.__dict__.update(kw)


@pytest.mark.asyncio
async def test_tg_user_never_gets_its_dm_history_as_last_post():
    client = MagicMock()
    client.get_entity = AsyncMock(return_value=User())
    client.get_messages = AsyncMock(return_value=[Message(_d(1))])
    client.side_effect = AsyncMock(side_effect=RuntimeError("no full"))
    tg = _tg(client)
    ent = await tg.resolve("bob")
    client.get_messages.assert_not_awaited()
    assert ent.last_post_iso == ""
    assert "bio not read" in ent.notes          # gap recorded, not silent


@pytest.mark.asyncio
async def test_tg_channel_gaps_become_row_notes():
    client = MagicMock()
    client.get_entity = AsyncMock(return_value=Channel(date=_DATE, left=True))

    async def call(req):
        raise RuntimeError("no full")

    client.side_effect = None
    tg = _tg(client)
    tg._call = call
    client.get_messages = AsyncMock(side_effect=RuntimeError("no msgs"))
    ent = await tg.resolve("chan")
    row = Row(url="https://t.me/chan", target="T")
    TA.Scraper.fill(row, ent)
    assert "members/bio not read" in row.notes
    assert "last post not read" in row.notes


@pytest.mark.asyncio
async def test_tg_flood_in_full_detail_still_propagates():
    client = MagicMock()
    client.get_entity = AsyncMock(return_value=Channel(date=_DATE, left=True))
    tg = _tg(client)
    tg._call = AsyncMock(side_effect=TD.FloodWait(11))
    with pytest.raises(TD.FloodWait):
        await tg.resolve("chan")


# --------------------------------------------------------------------------
# Telegram 12: fill() flags, photo unknown, forbidden stubs
# --------------------------------------------------------------------------

def test_tg_fill_carries_fake_and_bot_flags():
    ent = TD.entity_from(User(fake=True, bot=True))
    row = Row(url="https://t.me/bob", target="T")
    TA.Scraper.fill(row, ent)
    assert "FLAGGED BY TELEGRAM AS FAKE" in row.notes
    assert "bot account" in row.notes


def test_tg_photo_is_unknown_when_the_object_has_no_photo_attribute():
    class Bare:
        id = 3
        username = "bare"
        title = "Bare"
        broadcast = True

    assert TD.entity_from(Bare()).has_photo is None
    assert TD.entity_from(Channel()).has_photo is False         # photo=None: none set
    assert TD.entity_from(Channel(photo=SimpleNamespace())).has_photo is True
    row = Row(url="https://t.me/bare", target="T")
    TA.Scraper.fill(row, TD.entity_from(Bare()))
    assert row.has_custom_pic is None and "logo" not in row.src
    assert TD.entity_to_row(TD.entity_from(Bare()), "k").has_custom_pic is None


class ChannelForbidden:
    def __init__(self):
        self.id = 9
        self.title = "Secret"
        self.broadcast = True


@pytest.mark.asyncio
async def test_tg_channel_forbidden_is_not_ok():
    ent = TD.entity_from(ChannelForbidden())
    assert ent.forbidden and ent.has_photo is None
    s = TA.Scraper(SimpleNamespace(delay=0))
    s.tg = SimpleNamespace(resolve=AsyncMock(return_value=ent))
    row = await s.one("https://t.me/secret", "T", "")
    assert row.status == "ERROR" and row.status != "OK"
    assert "private/inaccessible" in row.notes
    assert row.has_custom_pic is None


@pytest.mark.asyncio
async def test_tg_forbidden_stub_skips_detail_requests():
    client = MagicMock()
    client.get_entity = AsyncMock(return_value=ChannelForbidden())
    client.get_messages = AsyncMock()
    tg = _tg(client)
    tg._call = AsyncMock()
    ent = await tg.resolve("secret")
    assert ent.forbidden
    tg._call.assert_not_awaited()
    client.get_messages.assert_not_awaited()


# --------------------------------------------------------------------------
# Telegram 13: pacing jitter
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tg_pause_is_jittered(monkeypatch):
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(TD.asyncio, "sleep", fake_sleep)
    tg = TD.Telegram()
    random.seed(7)
    for _ in range(30):
        await tg.pause(10.0)
    assert all(7.0 <= x <= 13.0 for x in slept)
    assert len(set(round(x, 6) for x in slept)) > 1
    slept.clear()
    await tg.pause(0)
    assert slept == []
