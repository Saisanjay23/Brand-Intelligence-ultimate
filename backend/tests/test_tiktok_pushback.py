"""TikTok pushback: detect a CAPTCHA / verify puzzle / throttle, STOP, and rest.

Never solve or bypass it -- these tests only prove the engines notice, stop,
report the shared stop code (or a status the analysis runner acts on), tell
the governor, and keep what they already gathered. And the other half of the
rule: a HEALTHY page (a caption that says "captcha", a 2xx body that mentions
it) changes nothing.

No network, no browser: the real `Discovery.sweep()` / `Scraper.process()`
driven through fake pages, the same technique as test_tiktok_end_to_end.py.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.platforms.scan_options import DiscoveryOptions, ScanOptions
from backend.platforms.tiktok import analysis_engine as A
from backend.platforms.tiktok import discovery_engine as D
from backend.shared import pushback as P
from backend.shared.resilience import classify_failure

SEARCH_XHR = json.dumps({"user_list": [
    {"user_info": {"unique_id": "adanigroup", "nickname": "Adani Group", "uid": "1"}},
    {"user_info": {"unique_id": "adani.fanpage", "nickname": "Adani Fan", "uid": "2"}},
]})
XHR_URL = "https://www.tiktok.com/api/search/general/full/?q=adani"


@pytest.fixture(autouse=True)
def _fresh_governors():
    P.reset_all()
    yield
    P.reset_all()


def _signals() -> list[str]:
    gov = P.governor_for("tiktok")
    return [gov.last_signal.kind] if gov.last_signal else []


# --------------------------------------------------------------- fakes


class _Resp:
    def __init__(self, url, body, status=None):
        self.url = url
        self._body = body
        if status is not None:
            self.status = status

    async def text(self):
        return self._body


class _Page:
    """`responses` fire on goto; `on_scroll` fire one per scroll."""

    def __init__(self, *, body="", chrome=None, dialog="", responses=(),
                 on_scroll=(), url=None, hydration="", cards=None, selector_raises=False,
                 goto_raises=None):
        self.url = url or ""
        self._body = body
        self._chrome = chrome
        self._dialog = dialog
        self._responses = list(responses)
        self._on_scroll = list(on_scroll)
        self._hydration = hydration
        self._cards = cards or []
        self._selector_raises = selector_raises
        self._goto_raises = goto_raises
        self._handler = None
        self.scrolls = 0
        self.closed = False

    def on(self, _event, handler):
        self._handler = handler

    async def _fire(self, resp):
        if self._handler:
            self._handler(resp)
            for _ in range(3):
                await asyncio.sleep(0)

    async def goto(self, url, **_kw):
        if self._goto_raises:
            raise self._goto_raises
        if not self.url:
            self.url = url
        for r in self._responses:
            await self._fire(r)

    async def inner_text(self, _sel):
        return self._body

    async def wait_for_selector(self, *_a, **_kw):
        if self._selector_raises:
            raise TimeoutError("selector")

    async def wait_for_timeout(self, _ms):
        await asyncio.sleep(0)

    async def evaluate(self, js, *args):
        if "tiktok-search-chrome" in js or "split(x).join" in js:
            return self._chrome if self._chrome is not None else self._body
        if 'role="dialog"' in js:                       # warning_dialog
            return self._dialog
        if "REHYDRATION" in js:
            return self._hydration
        if "scrollTop" in js:
            self.scrolls += 1
            if self._on_scroll:
                await self._fire(self._on_scroll.pop(0))
            return True
        if "lines.length < 2" in js:
            return self._cards                            # JS_USER_CARDS
        return []

    async def close(self):
        self.closed = True


class _Ctx:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page


def _opts(**kw) -> DiscoveryOptions:
    base = dict(timeout=5, settle=0.05, page_wait=0.05, patience=1,
                progress_every=99, max_results=0, max_pages=0, max_seconds=0)
    base.update(kw)
    return DiscoveryOptions(**base)


def _discovery(page, *, anonymous=False, **kw):
    disc = D.Discovery(_opts(**kw), _Ctx(page), anonymous=anonymous)
    merged: list[str] = []

    async def _merge(out, _probe=None):
        merged.append(out.stopped)

    disc._merge_user_accounts = _merge
    return disc, merged


# ------------------------------------------------------ regexes (A2, A8)


def test_the_3d_shape_puzzle_is_a_checkpoint():
    assert D.RE_CHECKPOINT.search("Select 2 objects that are the same shape")
    assert D.RE_CHECKPOINT.search("Select the shapes")


@pytest.mark.parametrize("text", [
    "Too many attempts. Try again later.",
    "Maximum number of attempts reached. Try again later.",
    "You are visiting too fast",
])
def test_throttle_wording_is_recognised(text):
    assert D.RE_THROTTLE.search(text)


def test_bare_generic_error_copy_is_not_a_throttle_on_the_page():
    # TikTok's generic error copy shows on healthy pages too.
    assert not D.RE_THROTTLE.search("Something went wrong. Please try again later.")


# ------------------------------------------------- discovery sweep (A1, A5)


@pytest.mark.asyncio
async def test_a_429_mid_sweep_stops_rate_limited_and_keeps_results():
    page = _Page(body="Top", responses=[_Resp(XHR_URL, SEARCH_XHR, 200)],
                 on_scroll=[_Resp(XHR_URL, '{"status_code": 0}', 429)])
    disc, merged = _discovery(page)
    out = await disc.sweep("adani")

    assert out.stopped == P.STOP_CODE[P.THROTTLED] == "rate_limited"
    assert out.error.startswith("rate_limited")
    assert classify_failure(out.error) == "rate_limited"
    assert [h.username for h in out.hits] == ["adanigroup", "adani.fanpage"]
    assert merged == [], "the Users-tab search fired after a throttle"
    assert _signals() == [P.THROTTLED]
    assert page.scrolls == 1, "kept scrolling after the throttle"


@pytest.mark.asyncio
async def test_a_verify_json_error_stops_checkpoint():
    page = _Page(body="Top", responses=[
        _Resp(XHR_URL, '{"verify": "captcha required"}', 403)])
    disc, merged = _discovery(page)
    out = await disc.sweep("adani")
    assert out.stopped == "checkpoint"
    assert classify_failure(out.error) == "checkpointed"
    assert merged == []
    assert _signals() == [P.CHALLENGED]


@pytest.mark.asyncio
async def test_a_2xx_body_mentioning_captcha_is_a_result_not_a_block():
    body = json.dumps({"user_list": [{"user_info": {
        "unique_id": "captcha_memes", "nickname": "Captcha Memes", "uid": "9"}}]})
    page = _Page(body="Top", responses=[_Resp(XHR_URL, body, 200)])
    disc, merged = _discovery(page)
    out = await disc.sweep("captcha")
    assert out.stopped == "stalled"
    assert [h.username for h in out.hits] == ["captcha_memes"]
    assert merged == ["stalled"]
    assert P.governor_for("tiktok").pushbacks == 0


@pytest.mark.asyncio
async def test_empty_bodies_are_not_pages_and_cannot_meet_the_page_cap():
    page = _Page(body="Top", responses=[_Resp(XHR_URL, "", 200)],
                 on_scroll=[_Resp(XHR_URL, "", 200)])
    disc, _ = _discovery(page, max_pages=1)
    out = await disc.sweep("adani")
    assert out.pages == 0
    assert out.stopped == "stalled", "an empty answer met cap:pages"
    assert "empty body" in out.error
    assert classify_failure(out.error) is None


# ---------------------------------------- chrome text, not captions (B2, A9)


@pytest.mark.asyncio
async def test_a_caption_saying_captcha_or_government_of_india_is_not_a_block():
    caption = "how to solve a captcha -- Government of India decided to block apps"
    page = _Page(body=f"Top\n{caption}", chrome="Top\n",
                 responses=[_Resp(XHR_URL, SEARCH_XHR, 200)])
    disc, _ = _discovery(page)
    out = await disc.sweep("adani")
    assert out.stopped == "stalled"
    assert len(out.hits) == 2
    assert P.governor_for("tiktok").pushbacks == 0


@pytest.mark.asyncio
async def test_a_full_page_captcha_is_still_a_checkpoint():
    page = _Page(body="Drag the slider to verify")      # chrome falls back to body
    disc, merged = _discovery(page)
    out = await disc.sweep("adani")
    assert out.stopped == "checkpoint"
    assert out.error.startswith("checkpoint")
    assert merged == []
    assert _signals() == [P.CHALLENGED]


@pytest.mark.asyncio
async def test_a_throttle_popup_on_load_stops_rate_limited():
    page = _Page(body="Top", dialog="You're temporarily blocked from searching")
    disc, _ = _discovery(page)
    out = await disc.sweep("adani")
    assert out.stopped == "rate_limited"
    assert classify_failure(out.error) == "rate_limited"


@pytest.mark.asyncio
async def test_a_captcha_appearing_mid_sweep_is_caught_at_the_stall():
    page = _Page(body="Top", responses=[_Resp(XHR_URL, SEARCH_XHR, 200)])
    disc, _ = _discovery(page)
    # the challenge paints only after the first page of results
    orig = page.evaluate

    async def evaluate(js, *a):
        if "tiktok-search-chrome" in js and page.scrolls:
            return "Select 2 objects that are the same shape"
        return await orig(js, *a)

    page.evaluate = evaluate
    out = await disc.sweep("adani")
    assert out.stopped == "checkpoint"
    assert len(out.hits) == 2, "results gathered before the challenge were lost"


@pytest.mark.asyncio
async def test_logged_in_zero_results_under_a_sign_in_prompt_is_login():
    page = _Page(body="Log in to TikTok")
    disc, merged = _discovery(page)
    out = await disc.sweep("adani")
    assert out.stopped == "login"
    assert classify_failure(out.error) == "expired"
    assert merged == []


@pytest.mark.asyncio
async def test_anonymous_sweeps_see_the_sign_in_prompt_and_that_is_not_a_verdict():
    page = _Page(body="Log in to TikTok")
    disc, _ = _discovery(page, anonymous=True)
    out = await disc.sweep("adani")
    assert out.stopped == "stalled"


# ------------------------------------------------ Users tab / last post (A7, B1)


@pytest.mark.asyncio
async def test_users_tab_navigation_failure_reports_the_real_error():
    page = _Page(goto_raises=RuntimeError("net::ERR_TIMED_OUT"))
    # used to raise UnboundLocalError on `ordered` inside the handler
    assert await D._users_for(_Ctx(page), "adani", timeout_s=0.01) == []


@pytest.mark.asyncio
async def test_users_tab_captcha_is_told_to_the_governor():
    page = _Page(body="Drag the slider to verify")
    assert await D._users_for(_Ctx(page), "adani", timeout_s=0.01) == []
    assert _signals() == [P.CHALLENGED]


@pytest.mark.asyncio
async def test_last_post_lookup_names_a_throttle():
    page = _Page(body="Too many attempts. Try again later.", selector_raises=True)
    assert await D.newest_post_via_search(_Ctx(page), "adanigroup", timeout_s=0.01) == ""
    assert _signals() == [P.THROTTLED]


# ------------------------------------------------------ analysis (A3, A4, A6)


def _hydration():
    return json.dumps({"__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo": {
        "user": {"id": "6812", "uniqueId": "adanigroup", "nickname": "Adani Group",
                 "signature": "Official account", "avatarLarger": "https://cdn/a.jpg"},
        "stats": {"followerCount": 120400, "followingCount": 12, "videoCount": 87},
    }, "itemList": []}}})


class _NavResp:
    def __init__(self, page, status):
        self.status = status
        self.request = self
        self.frame = page.main_frame

    def is_navigation_request(self):
        return True


class _ProfilePage(_Page):
    def __init__(self, *, goto_status=None, goto_raises=None, **kw):
        super().__init__(**kw)
        self.main_frame = object()
        self._goto_status = goto_status
        self._nav_raises = goto_raises

    async def goto(self, url, **_kw):
        if not self.url:
            self.url = url
        if self._goto_status is not None and self._handler:
            self._handler(_NavResp(self, self._goto_status))
        if self._nav_raises:
            raise self._nav_raises


def _scraper(monkeypatch, page, *, anonymous=False):
    sc = A.Scraper(ScanOptions(timeout=5, settle=0.01), [], session_id="", anonymous=anonymous)
    sc.session = None
    monkeypatch.setattr(A.Scraper, "ctx", property(lambda self: _Ctx(page)))
    calls = {"search": 0, "users": 0}

    async def _no_search(*_a, **_kw):
        calls["search"] += 1
        return ""

    async def _users(*_a, **_kw):
        calls["users"] += 1
        return {}

    async def _no_shot(*_a, **_kw):
        return None

    monkeypatch.setattr(A, "newest_post_via_search", _no_search)
    monkeypatch.setattr(A, "search_users_in", _users)
    monkeypatch.setattr(sc, "screenshot", _no_shot)
    return sc, calls


@pytest.mark.asyncio
async def test_logged_in_challenge_over_a_loaded_profile_stops_the_batch(monkeypatch):
    page = _ProfilePage(body="Adani Group", chrome="Drag the slider to verify",
                        hydration=_hydration())
    sc, calls = _scraper(monkeypatch, page)
    row = await sc.process("https://www.tiktok.com/@adanigroup", "Adani", "")
    assert row.status == "CHECKPOINT"
    assert row.followers == 120400, "fields already read were dropped"
    assert classify_failure(row.notes) == "checkpointed"
    assert calls["search"] == 0
    assert _signals() == [P.CHALLENGED]


@pytest.mark.asyncio
async def test_anonymous_challenge_over_a_loaded_profile_is_partial_and_signalled(monkeypatch):
    page = _ProfilePage(body="Adani Group", dialog="Verify to continue",
                        hydration=_hydration())
    sc, _ = _scraper(monkeypatch, page, anonymous=True)
    row = await sc.process("https://www.tiktok.com/@adanigroup", "Adani", "")
    assert row.status == "PARTIAL"
    assert _signals() == [P.CHALLENGED]


@pytest.mark.asyncio
async def test_logged_in_throttle_popup_rests_the_account_as_rate_limited(monkeypatch):
    page = _ProfilePage(body="Adani Group", dialog="Action blocked. Try again later.",
                        hydration=_hydration())
    sc, _ = _scraper(monkeypatch, page)
    row = await sc.process("https://www.tiktok.com/@adanigroup", "Adani", "")
    assert row.status == "CHECKPOINT"
    assert classify_failure(row.notes) == "rate_limited"
    assert _signals() == [P.THROTTLED]


@pytest.mark.asyncio
async def test_a_healthy_profile_is_unchanged(monkeypatch):
    page = _ProfilePage(body="Adani Group", chrome="Adani Group\n120.4K Followers",
                        hydration=_hydration())
    sc, _ = _scraper(monkeypatch, page)
    row = await sc.process("https://www.tiktok.com/@adanigroup", "Adani", "")
    assert row.status == "OK"
    assert P.governor_for("tiktok").total == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [None, RuntimeError("net::ERR_HTTP_RESPONSE_CODE_FAILURE")])
async def test_a_429_skips_the_search_fallback(monkeypatch, raises):
    page = _ProfilePage(goto_status=429, goto_raises=raises)
    sc, calls = _scraper(monkeypatch, page)
    row = await sc.process("https://www.tiktok.com/@adanigroup", "Adani", "")
    assert row.status == "ERROR"
    assert calls["users"] == 0, "a fallback search fired after a 429"
    assert classify_failure(row.notes) == "rate_limited"
    assert _signals() == [P.THROTTLED]


@pytest.mark.asyncio
async def test_anonymous_captcha_row_tells_the_governor(monkeypatch):
    page = _ProfilePage(body="Drag the slider to verify", chrome="Drag the slider to verify")
    sc, _ = _scraper(monkeypatch, page, anonymous=True)
    row = await sc.process("https://www.tiktok.com/@someone", "Adani", "")
    assert row.status == "ERROR"
    assert classify_failure(row.notes) is None      # unchanged wording
    assert _signals() == [P.CHALLENGED]


@pytest.mark.asyncio
async def test_three_anonymous_captchas_open_the_breaker(monkeypatch):
    for _ in range(3):
        page = _ProfilePage(body="Drag the slider to verify")
        sc, _ = _scraper(monkeypatch, page, anonymous=True)
        await sc.process("https://www.tiktok.com/@someone", "Adani", "")
    assert P.governor_for("tiktok").is_open()
