"""Facebook stops, and says so, when the platform pushes back.

The pattern Instagram already follows: on a throttle, checkpoint, automation
warning or login wall the engine (1) stops instead of carrying on with the
same account, (2) reports the shared stop code / a session-blocked row status
that `classify_failure` turns into the right verdict (a throttle is COOLED,
not quarantined), (3) tells the pacing governor once per event, and (4) keeps
everything already gathered. And on a healthy platform nothing changes.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.platforms.facebook import analysis_engine as ae
from backend.platforms.facebook import discovery_engine as F
from backend.platforms.facebook.analysis_engine import Harvest, Scraper
from backend.platforms.facebook.discovery_engine import (
    QUERY_NAME, RE_CHECKPOINT, RE_TRY_LATER, Discovery, _session_blocked,
    most_severe, text_wall, url_wall)
from backend.shared import pushback
from backend.shared.models.row import Row
from backend.shared.pushback import CHALLENGED, EXPIRED, THROTTLED, Signal
from backend.shared.resilience import classify_failure
from backend.shared.warning_dialog import JS_DIALOG_TEXT

PID = "100057408731013"
URL = f"https://www.facebook.com/profile.php?id={PID}"
AUTOMATION = "We suspect automated behavior on your account"


def _gov():
    return pushback.governor_for("facebook")


# ===================================================================== fakes

class _Req:
    post_data = f'{{"fb_api_req_friendly_name":"{QUERY_NAME}"}}'


class _Resp:
    def __init__(self, status: int, body: str):
        self.url = "https://www.facebook.com/api/graphql/"
        self.status = status
        self.request = _Req()
        self._body = body

    async def text(self):
        return self._body


def _edges(*ids: str, name: str = "") -> str:
    return json.dumps({"edges": [{"rendering_strategy": {"view_model": {
        "__typename": "SearchProfileViewModel",
        "profile": {"__typename": "User", "id": i, "name": name,
                    "profile_url": f"https://www.facebook.com/{i}"}}}} for i in ids]})


class SearchPage:
    """Just enough of a Playwright page for Discovery.sweep()."""

    def __init__(self, responses=(), body="People", dialog="", landed=None,
                 nav_error=False):
        self.responses = list(responses)
        self.body = body
        self.dialog = dialog
        self.landed = landed
        self.nav_error = nav_error
        self.url = ""
        self.handler = None
        self.evaluated: list = []

    def on(self, event, handler):
        self.handler = handler

    async def goto(self, url, **kw):
        self.url = self.landed or url
        for status, body in self.responses:
            self.handler(_Resp(status, body))
        for _ in range(10):
            await asyncio.sleep(0)
        if self.nav_error:
            raise TimeoutError("Page.goto: Timeout exceeded")

    async def wait_for_function(self, *a, **k):
        raise TimeoutError()

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)

    async def inner_text(self, sel):
        return self.body

    async def evaluate(self, js, *a):
        self.evaluated.append(js)
        if js == JS_DIALOG_TEXT:
            return self.dialog
        return None

    async def close(self):
        pass


class SearchCtx:
    def __init__(self, page):
        self.page = page
        self.pages = 0

    async def new_page(self):
        self.pages += 1
        return self.page


def _args(**over):
    a = dict(timeout=5, settle=0, page_wait=0.01, patience=2, max_pages=0,
             max_seconds=0, progress_every=10, max_results=0, cancel=None)
    a.update(over)
    return SimpleNamespace(**a)


@pytest.fixture(autouse=True)
def _no_motion(monkeypatch):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(F, "natural_scroll_down", _noop)
    monkeypatch.setattr(F, "humanize_interaction", _noop)


def _discovery(page, resolve_calls=None, resolve_wall="", **over):
    d = Discovery(_args(**over), SearchCtx(page))
    d._warmed = True

    async def fake_resolve(ids, kind):
        if resolve_calls is not None:
            resolve_calls.append(list(ids))
        d._resolve_wall = resolve_wall
        return {}

    d._resolve_missing = fake_resolve
    return d


# ================================================================ discovery

class TestA1NoProfileVisitsAfterPushback:
    @pytest.mark.asyncio
    async def test_a_throttled_sweep_does_not_visit_profiles(self):
        calls: list = []
        page = SearchPage(responses=[(200, _edges("123")), (429, "")])
        out = await _discovery(page, calls).sweep("acme", "people")
        assert out.stopped == "rate_limited"
        assert calls == [], "visited profiles on an account Facebook just throttled"
        assert len(out.hits) == 1, "results gathered before the throttle were dropped"

    @pytest.mark.asyncio
    async def test_a_healthy_sweep_still_resolves_nameless_results(self):
        calls: list = []
        page = SearchPage(responses=[(200, _edges("123"))])
        out = await _discovery(page, calls).sweep("acme", "people")
        assert calls == [["123"]]
        assert out.stopped == "stalled"


class TestA2ResolveWallOverridesEveryEnding:
    @pytest.mark.asyncio
    async def test_a_wall_during_visits_on_a_capped_sweep_is_reported(self):
        page = SearchPage(responses=[(200, _edges("123", "456"))])
        out = await _discovery(page, [], resolve_wall="checkpoint",
                               max_results=1).sweep("acme", "people")
        assert out.stopped == "checkpoint"
        assert not out.complete
        assert classify_failure(out.error) == "checkpointed"

    @pytest.mark.asyncio
    async def test_a_cancel_is_not_overridden(self):
        page = SearchPage(responses=[(200, _edges("123"))])
        d = _discovery(page, [], resolve_wall="checkpoint", cancel=True)
        out = await d.sweep("acme", "people")
        assert out.stopped == "cancelled"


class TestA3OneWallEndsTheVisits:
    @pytest.mark.asyncio
    async def test_remaining_visits_are_skipped(self, monkeypatch):
        monkeypatch.setattr(F.random, "uniform", lambda a, b: 0)
        visited: list[str] = []

        class P:
            def __init__(self):
                self.url = ""

            def on(self, *a):
                pass

            async def goto(self, url, **kw):
                visited.append(url)
                self.url = ("https://www.facebook.com/checkpoint/1/"
                            if len(visited) == 1 else url)

            async def inner_text(self, sel):
                return ""

            async def evaluate(self, js, *a):
                return "" if js == JS_DIALOG_TEXT else []

            async def wait_for_function(self, *a, **k):
                raise TimeoutError()

            async def title(self):
                return ""

            async def close(self):
                pass

        async def new_page():
            return P()

        d = Discovery(_args(), SimpleNamespace(new_page=new_page))
        await d._resolve_missing(["1", "2", "3", "4", "5"], "profile")
        assert d._resolve_wall == "checkpoint"
        assert len(visited) <= F.RESOLVE_CONCURRENCY, visited
        assert _gov().pushbacks == 1 and _gov().last_signal.kind == CHALLENGED


class TestA4ThrottleIsNotACleanZero:
    @pytest.mark.asyncio
    async def test_throttled_first_response_plus_empty_panel(self):
        page = SearchPage(responses=[(429, "")],
                          body="We couldn't find anything for acme")
        out = await _discovery(page).sweep("acme", "people")
        assert out.stopped == "rate_limited"
        assert not out.complete

    @pytest.mark.asyncio
    async def test_a_real_empty_search_is_still_no_results(self):
        page = SearchPage(body="We couldn't find anything for acme")
        out = await _discovery(page).sweep("acme", "people")
        assert out.stopped == "no-results" and out.complete
        assert _gov().pushbacks == 0


class TestA5WarningPopupOnTheSearchPage:
    @pytest.mark.asyncio
    async def test_automation_warning_stops_as_checkpoint(self):
        page = SearchPage(dialog=AUTOMATION)
        out = await _discovery(page).sweep("acme", "people")
        assert out.stopped == "checkpoint"
        assert classify_failure(out.error) == "checkpointed"
        assert _gov().pushbacks == 1 and _gov().last_signal.kind == CHALLENGED

    @pytest.mark.asyncio
    async def test_a_popup_over_results_still_stops(self):
        page = SearchPage(responses=[(200, _edges("123", name="Acme"))],
                          dialog="You're temporarily blocked. Going too fast.")
        out = await _discovery(page).sweep("acme", "people")
        assert out.stopped == "rate_limited"
        assert classify_failure(out.error) == "rate_limited"
        assert len(out.hits) == 1, "results gathered before the popup were dropped"

    @pytest.mark.asyncio
    async def test_no_dialog_changes_nothing(self):
        page = SearchPage(responses=[(200, _edges("123", name="Acme"))])
        out = await _discovery(page).sweep("acme", "people")
        assert out.stopped == "stalled"
        assert _gov().pushbacks == 0


class TestA6Wording:
    def test_automation_and_throttle_wording(self):
        assert RE_CHECKPOINT.search(AUTOMATION)
        assert RE_CHECKPOINT.search("automated behaviour")
        for s in ("You can’t use this feature right now", "going too fast",
                  "misusing this feature", "Action Blocked", "Try again later"):
            assert RE_TRY_LATER.search(s), s
        assert not RE_CHECKPOINT.search("a checkpoint on the border")

    def test_a_bio_quoting_the_words_does_not_blank_a_resolved_profile(self):
        assert not F._RE_PROFILE_WALL_TEXT.search("We stop automated behavior on networks")

    def test_text_wall_kinds(self):
        assert text_wall(AUTOMATION) == "checkpoint"
        assert text_wall("Confirm Your Identity") == "checkpoint"
        assert text_wall("You’re Temporarily Blocked") == "rate_limited"
        assert text_wall("Please try again later") == "rate_limited"
        assert text_wall("You must log in to continue") == "login"
        assert text_wall("People\nAcme Corp") == ""


class TestA7WallsReachTheGovernor:
    @pytest.mark.asyncio
    async def test_search_page_wall_is_recorded(self):
        page = SearchPage(landed="https://www.facebook.com/checkpoint/1/", nav_error=True)
        out = await _discovery(page).sweep("acme", "people")
        assert out.stopped == "checkpoint"
        assert _gov().pushbacks == 1 and _gov().last_signal.kind == CHALLENGED

    @pytest.mark.asyncio
    async def test_a_checkpoint_redirect_with_a_long_body_is_not_ok(self):
        sc, page = _scraper({"*": {"html": "<html>x</html>", "text": "x" * 5000,
                                   "landed": "https://www.facebook.com/checkpoint/9/"}})
        row = await sc.process(URL, "t", "f")
        assert row.status == "CHECKPOINT"
        assert _gov().last_signal.kind == CHALLENGED and _gov().pushbacks == 1

    @pytest.mark.asyncio
    async def test_a_text_login_wall_is_recorded_once(self):
        sc, _ = _scraper({"*": {"html": "<html></html>", "text": "You must log in to continue"}})
        row = await sc.process(URL, "t", "f")
        assert row.status == "LOGIN_REQUIRED"
        assert _gov().pushbacks == 1 and _gov().last_signal.kind == EXPIRED

    @pytest.mark.asyncio
    async def test_an_about_tab_throttle_is_recorded_and_cooled(self):
        routes = {"*": _live_main(),
                  f"{URL}&sk=about": {"html": "<html></html>",
                                     "text": "You can't use this feature right now"}}
        sc, _ = _scraper(routes)
        row = await sc.process(URL, "t", "f")
        assert row.status == "CHECKPOINT", row.notes
        assert classify_failure(row.notes) == "rate_limited"
        assert _gov().last_signal.kind == THROTTLED


class TestA8ThrottleIsCooledNotQuarantined:
    @pytest.mark.asyncio
    async def test_search_page_try_again_later(self):
        page = SimpleNamespace(url="https://www.facebook.com/search/people/?q=x")

        async def inner_text(sel):
            return "Something went wrong. Please try again later."
        page.inner_text = inner_text
        code = await _session_blocked(page)
        assert code == "rate_limited"
        assert classify_failure(f"facebook search page shows a {code} wall") == "rate_limited"

    def test_analysis_throttle_note_classifies_as_rate_limited(self):
        row = Row(url=URL, target="")
        row.profile_id = PID
        h = Harvest()
        assert Scraper.blocked_status(row, URL, "Please try again later", h)
        assert row.status == "CHECKPOINT"
        assert classify_failure(row.notes) == "rate_limited"
        st, note = Scraper.tab_wall(f"{URL}/about", "Try again later")
        assert classify_failure(note) == "rate_limited"
        st, note = Scraper.tab_wall(f"{URL}/about", "",
                                    Signal(THROTTLED, "body says 'try again later'"))
        assert classify_failure(note) == "rate_limited"


class TestA9LoginRedirectAndNavFailure:
    def test_index_php_next_is_a_login_wall(self):
        assert url_wall("https://www.facebook.com/index.php?next=https%3A%2F%2Fx") == "login"
        row = Row(url=URL, target="")
        row.profile_id = PID
        assert Scraper.blocked_status(
            row, "https://www.facebook.com/index.php?next=x", "", Harvest())
        assert row.status == "LOGIN_REQUIRED"

    @pytest.mark.asyncio
    async def test_a_nav_failure_on_a_checkpoint_is_not_an_error(self):
        sc, _ = _scraper({"*": {"nav_error": True,
                                "landed": "https://www.facebook.com/checkpoint/5/"}})
        row = await sc.process(URL, "t", "f")
        assert row.status == "CHECKPOINT", row.notes
        assert _gov().pushbacks == 1

    @pytest.mark.asyncio
    async def test_an_ordinary_nav_failure_is_still_an_error(self):
        sc, _ = _scraper({"*": {"nav_error": True}})
        row = await sc.process(URL, "t", "f")
        assert row.status == "ERROR"
        assert _gov().pushbacks == 0


class TestB3MostSevere:
    def test_order(self):
        t, e, c = Signal(THROTTLED), Signal(EXPIRED), Signal(CHALLENGED)
        assert most_severe([t, c, t]) is c
        assert most_severe([t, e]) is e
        assert most_severe([]) is None

    @pytest.mark.asyncio
    async def test_a_challenge_then_a_throttle_stops_as_checkpoint(self):
        page = SearchPage(responses=[(200, '{"message":"checkpoint_required"}'), (429, "")])
        out = await _discovery(page).sweep("acme", "people")
        assert out.stopped == "checkpoint"


# ================================================================= analysis

class APage:
    """Just enough of a Playwright page for Scraper.process()."""

    def __init__(self, routes, dialog=""):
        self.routes = routes
        self.dialog = dialog
        self.url = ""
        self.cur: dict = {}
        self.visited: list[str] = []
        self.mouse = SimpleNamespace(wheel=self._noop)

    async def _noop(self, *a, **k):
        return None

    def on(self, *a, **k):
        return None

    async def goto(self, url, **kw):
        self.visited.append(url)
        r = self.routes.get(url) or self.routes["*"]
        self.cur = r
        self.url = r.get("landed", url)
        if r.get("nav_error"):
            raise TimeoutError("Page.goto: Timeout exceeded")
        return SimpleNamespace(status=r.get("status", 200))

    async def wait_for_function(self, *a, **k):
        raise TimeoutError()

    async def wait_for_selector(self, *a, **k):
        raise TimeoutError()

    async def wait_for_timeout(self, ms):
        return None

    async def content(self):
        return self.cur.get("html", "<html></html>")

    async def inner_text(self, sel):
        return self.cur.get("text", "")

    async def evaluate(self, js, *args):
        if js == JS_DIALOG_TEXT:
            return self.dialog
        if js == Scraper.JS_EMBEDDED:
            return self.cur.get("embedded", [])
        if js == Scraper.JS_HEADER:
            return self.cur.get("dom", {})
        return None if not isinstance(js, str) or "post" not in js.lower() else []

    async def close(self):
        pass


def _scraper(routes, dialog="", **over):
    sc = Scraper.__new__(Scraper)
    a = dict(timeout=5, settle=1, scrolls=0, cancel=None)
    a.update(over)
    sc.a = SimpleNamespace(**a)
    sc.evidence = None
    page = APage(routes, dialog)

    async def new_page():
        return page

    async def pause(mult=1.0):
        return None

    async def no_shot(p, row):
        return None

    sc.session = SimpleNamespace(ctx=SimpleNamespace(new_page=new_page), pause=pause)
    sc.pause = pause
    sc.screenshot = no_shot
    return sc, page


def _live_main(**over):
    ent = {"id": PID, "name": "Acme Corp", "profile_social_context": {
        "content": [{"text": {"text": "120 followers"}}]}}
    r = {
        "html": "<html><title>Acme Corp | Facebook</title></html>",
        "text": "Acme Corp\n120 followers\nPosts",
        "embedded": [json.dumps({"user": ent})],
        "dom": {"name": "Acme Corp", "counter": "120 followers"},
    }
    r.update(over)
    return r


class TestA5AnalysisWarningPopup:
    @pytest.mark.asyncio
    async def test_an_automation_popup_over_a_loaded_profile_is_a_checkpoint(self):
        sc, page = _scraper({"*": _live_main()}, dialog=AUTOMATION)
        row = await sc.process(URL, "t", "f")
        assert row.status == "CHECKPOINT"
        assert classify_failure(row.notes) == "checkpointed"
        assert page.visited == [URL], "kept loading tabs after the warning"
        assert _gov().pushbacks == 1 and _gov().last_signal.kind == CHALLENGED

    @pytest.mark.asyncio
    async def test_a_throttle_popup_slows_down_but_keeps_the_row(self):
        sc, _ = _scraper({"*": _live_main()}, dialog="You're temporarily blocked")
        row = await sc.process(URL, "t", "f", known={"location": "Pune"})
        assert row.status == "OK", row.notes
        assert "throttled" in row.notes
        assert _gov().last_signal.kind == THROTTLED

    @pytest.mark.asyncio
    async def test_a_bio_quoting_the_warning_is_not_a_popup(self):
        sc, _ = _scraper({"*": _live_main(text=f"Acme Corp\n120 followers\n{AUTOMATION}")})
        row = await sc.process(URL, "t", "f", known={"location": "Pune"})
        assert row.status == "OK", row.notes
        assert _gov().pushbacks == 0


class TestB4BatchStops:
    @staticmethod
    def _sc(statuses, concurrency=1):
        sc = Scraper.__new__(Scraper)
        sc.a = SimpleNamespace(concurrency=concurrency, keep_going=False)
        seen: list[str] = []
        it = iter(statuses)

        async def one(u, tgt, feed, known=None):
            seen.append(u)
            r = Row(url=u, target=tgt)
            r.status = next(it, "OK")
            return r

        async def pause(mult=1.0):
            return None

        sc.one = one
        sc.pause = pause
        sc.report = lambda *a, **k: None
        return sc, seen

    @pytest.mark.asyncio
    async def test_sequential_stops_on_login_required(self):
        sc, seen = self._sc(["OK", "LOGIN_REQUIRED", "OK", "OK"])
        rows = await sc.run([(f"u{i}", "t", "f") for i in range(4)])
        assert seen == ["u0", "u1"]
        assert [r.status for r in rows] == ["OK", "LOGIN_REQUIRED"]

    @pytest.mark.asyncio
    async def test_parallel_stops_launching_once_blocked(self, monkeypatch):
        real_sleep = asyncio.sleep

        async def fast(d, *a, **k):
            await real_sleep(0)
        monkeypatch.setattr(ae.asyncio, "sleep", fast)
        sc, seen = self._sc(["CHECKPOINT"], concurrency=2)
        rows = await sc.run([(f"u{i}", "t", "f") for i in range(6)])
        assert len(seen) < 6, "kept visiting after a checkpoint"
        assert rows[0].status == "CHECKPOINT"
