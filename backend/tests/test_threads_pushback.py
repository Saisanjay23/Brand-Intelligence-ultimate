"""Threads and the shared popup check: pushback must stop the work and rest
the account, never read as an ordinary result.

THE DEFECT THESE GUARD. Threads signs in with the same Meta account as
Instagram, and it shows the same "We suspect automated behavior" popup over
a page that loaded fine. Threads had none of Instagram's protections: the
warning was only looked for when a profile could NOT be read, discovery
stopped looking for walls once the first cards were read, a refused search
document was thrown away, and nothing told the governor (shared with
Instagram) that Threads had pushed back. See backend/shared/warning_dialog.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.platforms.threads import analysis_engine as ae
from backend.platforms.threads import discovery_engine as de
from backend.shared import pushback, resilience, warning_dialog
from backend.shared.pushback import CHALLENGED, THROTTLED


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pushback.reset_all()

    async def _instant(*_a, **_k):
        return None

    monkeypatch.setattr(de.asyncio, "sleep", _instant)
    monkeypatch.setattr(de, "_page_gap", AsyncMock())
    yield
    pushback.reset_all()


def _gov():
    return pushback.governor_for("threads")


# ------------------------------------------------------- the shared check


class TestDialogText:
    @pytest.mark.parametrize("text", [
        "We suspect automated behavior on your account",
        "We suspect automated behaviour on your account",
        "Your account has been locked",
    ])
    def test_a_warning_in_a_dialog_is_a_challenge(self, text):
        assert warning_dialog.classify_dialog_text(text).kind == CHALLENGED

    @pytest.mark.parametrize("text", [
        "Please wait a few minutes before you try again",
        "Action Blocked",
        "It looks like you were misusing this feature by going too fast",
    ])
    def test_a_slow_down_notice_is_a_throttle(self, text):
        assert warning_dialog.classify_dialog_text(text).kind == THROTTLED

    @pytest.mark.parametrize("text", ["", None, "Say more with Threads", "Follow", 42])
    def test_everything_else_says_nothing_about_the_account(self, text):
        assert warning_dialog.classify_dialog_text(text) is None

    @pytest.mark.asyncio
    async def test_an_unreadable_page_is_not_a_warning(self):
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=RuntimeError("tab closed"))
        assert await warning_dialog.dialog_warning(page) is None


class TestSharedWording:
    def test_x_error_226_is_a_challenge(self):
        sig = pushback.classify_response("twitter", 403, '{"errors":[{"code": 226}]}')
        assert sig.kind == CHALLENGED

    def test_the_automation_sentence_is_a_challenge(self):
        sig = pushback.classify_response(
            "twitter", 403, "This request looks like it might be automated.")
        assert sig.kind == CHALLENGED

    def test_googles_unusual_traffic_page_is_a_challenge(self):
        sig = pushback.classify_response(
            "youtube", 429, "Our systems have detected unusual traffic from your network")
        assert sig.kind == CHALLENGED

    def test_ordinary_bodies_are_untouched(self):
        assert pushback.classify_response("twitter", 200, "hello").kind == pushback.OK


# ---------------------------------------------------------------- analysis


def _record(handle="nike", **over):
    rec = {"username": handle, "pk": "63454848350", "full_name": "Nike", "biography": "Just Do It.",
           "follower_count": 15_093_123, "is_verified": True, "text_post_app_is_private": False,
           "profile_pic_url": "https://instagram.fx.fna.fbcdn.net/v/a_n.jpg"}
    rec.update(over)
    return rec


def _scraper(*, url="https://www.threads.com/@nike", texts=(), lines=(), status=200,
             body="", record=True, dialog="", cancel=None):
    snap = {"url": url, "texts": list(texts), "lines": list(lines)}

    async def evaluate(script, *_a):
        if script is ae._JS_HAS_RECORD:
            return record
        if script is warning_dialog.JS_DIALOG_TEXT:
            return dialog
        return snap

    page = MagicMock()
    page.url = url
    page.goto = AsyncMock(return_value=SimpleNamespace(
        status=status, text=AsyncMock(return_value=body)))
    page.evaluate = AsyncMock(side_effect=evaluate)
    page.close = AsyncMock()
    s = ae.Scraper.__new__(ae.Scraper)
    s.a = SimpleNamespace(timeout=5, settle=0.05, evidence=None, cancel=cancel)
    s.evidence = None
    s.session = MagicMock()
    s.session.ctx.new_page = AsyncMock(return_value=page)
    s._no_payload_streak = 0
    s.screenshot = AsyncMock()
    return s


GOOD = [json.dumps({"r": _record()})]


class TestAnalysisWarnings:
    @pytest.mark.asyncio
    async def test_an_automation_popup_over_a_loaded_profile_stops_the_batch(self):
        s = _scraper(texts=GOOD, dialog="We suspect automated behavior on your account")
        row = await s.process("https://www.threads.com/@nike", "Nike", "feed")
        assert row.status == "CHECKPOINT"
        assert resilience.classify_failure(row.notes) == "checkpointed"
        assert _gov().snapshot()["pushbacks"] == 1

    @pytest.mark.asyncio
    async def test_a_challenge_in_the_documents_own_answer_wins_over_a_record(self):
        s = _scraper(texts=GOOD, status=403, body='{"error":"challenge_required"}')
        row = await s.process("https://www.threads.com/@nike", "Nike", "feed")
        assert row.status == "CHECKPOINT"

    @pytest.mark.asyncio
    async def test_a_throttle_popup_keeps_the_row_but_slows_the_platform(self):
        s = _scraper(texts=GOOD, dialog="Please wait a few minutes before you try again")
        row = await s.process("https://www.threads.com/@nike", "Nike", "feed")
        assert row.status == "OK"
        assert any("throttled" in n for n in row.notes.split("|")) or "throttled" in row.notes
        assert _gov().snapshot()["pushbacks"] == 1

    @pytest.mark.asyncio
    async def test_a_dialog_that_says_nothing_about_the_account_changes_nothing(self):
        s = _scraper(texts=GOOD, dialog="Say more with Threads")
        row = await s.process("https://www.threads.com/@nike", "Nike", "feed")
        assert row.status == "OK"
        assert _gov().snapshot()["pushbacks"] == 0

    @pytest.mark.asyncio
    async def test_a_try_again_later_page_tells_the_governor_on_the_first_one(self):
        s = _scraper(record=False, lines=["Something went wrong", "Please try again later"])
        row = await s.process("https://www.threads.com/@nike", "Nike", "feed")
        assert row.status == "PARTIAL"
        assert _gov().snapshot()["pushbacks"] == 1

    @pytest.mark.asyncio
    async def test_a_stop_is_not_a_soft_block(self):
        cancel = MagicMock()
        cancel.is_set = MagicMock(return_value=True)
        s = _scraper(record=False, cancel=cancel)
        for _ in range(ae.NO_PAYLOAD_STREAK_LIMIT + 1):
            row = await s.process("https://www.threads.com/@nike", "Nike", "feed")
            assert row.status == "PARTIAL" and "cancelled" in row.notes
        assert _gov().snapshot()["pushbacks"] == 0
        assert s._no_payload_streak == 0


# --------------------------------------------------------------- discovery


def _card(handle):
    return {"u": handle, "lines": [handle, "Some Name", "Follow", "12 followers"],
            "img": "https://instagram.fx.fna.fbcdn.net/a.jpg", "ver": False}


def _snap(cards=(), lines=("Top", "Recent", "Profiles"), dialog="",
          url="https://www.threads.com/search?q=k"):
    return {"url": url, "lines": list(lines), "cards": list(cards), "dialog": dialog}


def _discovery(snaps, *, goto=None):
    args = SimpleNamespace(timeout=5, max_results=0, max_pages=0, cancel=None)
    seq = list(snaps)

    async def evaluate(_script, *_a):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    page.goto = goto or AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate)
    page.mouse.wheel = AsyncMock()
    page.url = "https://www.threads.com/search?q=k"
    d = de.Discovery(args, MagicMock())
    d._tab = page
    return d


class TestDiscoveryPushback:
    @pytest.mark.asyncio
    async def test_a_refused_search_document_stops_at_once_and_cools_the_account(self):
        goto = AsyncMock(return_value=SimpleNamespace(
            status=429, text=AsyncMock(return_value="too many requests")))
        d = _discovery([_snap([_card("a")])], goto=goto)
        out = await d.sweep("kw")
        assert out.stopped == "rate_limited"
        assert resilience.classify_failure(out.error) == "rate_limited"
        assert resilience.sweep_outcome(out.stopped, out.complete) == resilience.BROKEN
        assert d._tab.evaluate.await_count == 0          # nothing scrolled on
        assert _gov().snapshot()["pushbacks"] == 1

    @pytest.mark.asyncio
    async def test_a_healthy_200_is_unchanged(self):
        goto = AsyncMock(return_value=SimpleNamespace(status=200, text=AsyncMock()))
        out = await _discovery([_snap([_card("a"), _card("b")])], goto=goto).sweep("kw")
        assert (out.stopped, out.complete, len(out.hits)) == ("exhausted", True, 2)
        assert _gov().snapshot()["pushbacks"] == 0

    @pytest.mark.asyncio
    async def test_a_popup_on_a_later_round_stops_and_keeps_what_was_read(self):
        snaps = [_snap([_card("a"), _card("b")]),
                 _snap([_card("a"), _card("b")],
                       dialog="We suspect automated behavior on your account")]
        out = await _discovery(snaps).sweep("kw")
        assert out.stopped == "checkpoint"
        assert resilience.classify_failure(out.error) == "checkpointed"
        assert len(out.hits) == 2
        assert _gov().snapshot()["pushbacks"] == 1

    @pytest.mark.asyncio
    async def test_a_throttle_popup_is_rate_limited_not_exhausted(self):
        snaps = [_snap([_card("a")]),
                 _snap([_card("a")], dialog="Please wait a few minutes before you try again")]
        out = await _discovery(snaps).sweep("kw")
        assert out.stopped == "rate_limited" and out.complete is False
        assert len(out.hits) == 1

    @pytest.mark.asyncio
    async def test_a_challenge_redirect_mid_scroll_is_a_checkpoint(self):
        snaps = [_snap([_card("a")]),
                 _snap([], url="https://www.threads.com/challenge/abc/")]
        out = await _discovery(snaps).sweep("kw")
        assert out.stopped == "checkpoint" and len(out.hits) == 1
        assert _gov().snapshot()["pushbacks"] == 1

    @pytest.mark.asyncio
    async def test_a_checkpoint_before_any_card_now_reaches_the_governor_too(self):
        out = await _discovery(
            [_snap([], url="https://www.threads.com/challenge/abc/")]).sweep("kw")
        assert out.stopped == "checkpoint"
        assert _gov().snapshot()["pushbacks"] == 1

    @pytest.mark.asyncio
    async def test_a_card_bio_quoting_the_warning_is_not_a_warning(self):
        card = _card("a")
        card["lines"].append("We suspect automated behavior on your account")
        out = await _discovery([_snap([card])]).sweep("kw")
        assert out.stopped == "exhausted"
        assert _gov().snapshot()["pushbacks"] == 0

    @pytest.mark.asyncio
    async def test_a_dead_tab_is_not_the_end_of_the_list(self):
        snaps = [_snap([_card("a"), _card("b")]), {"url": "", "lines": [], "cards": []}]
        out = await _discovery(snaps).sweep("kw")
        assert out.stopped == "error"
        assert resilience.sweep_outcome(out.stopped, out.complete) == resilience.BROKEN
        assert len(out.hits) == 2
