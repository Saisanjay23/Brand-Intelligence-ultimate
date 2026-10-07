"""The evidence screenshot: taken from the top, and never lost in silence.

Measured live 2026-10-07: every Facebook capture was taken 425px down the
page, cutting the cover photo -- often the strongest impersonation evidence
-- to a strip. And all four browser engines ended their capture in
`except Exception: pass`, so a failed capture left no reason anywhere.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.shared import evidence
from backend.shared.models.row import Row
from backend.stealth import browser as B


class FakePage:
    """Records the scroll position at the moment of the shot. `rescroll`
    is how many times the page moves itself back down after a scrollTo,
    which is what Facebook did."""

    def __init__(self, y: int = 425, rescroll: int = 0, shot_raises: Exception | None = None):
        self.y = y
        self.rescroll = rescroll
        self.shot_raises = shot_raises
        self.shot_at_y: int | None = None

    async def evaluate(self, js: str):
        if "scrollTo(0, 0)" not in js and "scrollTo" in js:
            self.y = int(js.split(",")[1].strip(" )"))
            return None
        if "scrollTo" in js:
            if self.rescroll:
                self.rescroll -= 1
                self.y = 425
            else:
                self.y = 0
            return None
        if "scrollY" in js:
            return self.y
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def screenshot(self, full_page=False):
        if self.shot_raises:
            raise self.shot_raises
        self.shot_at_y = self.y
        return b"png"


@pytest.mark.asyncio
async def test_scroll_to_top_retries_until_it_holds():
    page = FakePage(rescroll=2)
    assert await B.scroll_to_top(page) == 0


@pytest.mark.asyncio
async def test_capture_evidence_shoots_from_the_top_after_waiting():
    page = FakePage()
    order: list[str] = []

    async def wait(p, content_selector=""):
        order.append(f"wait:{content_selector}")

    async def settle(p, ms):
        order.append(f"settle@{p.y}")

    fake_session = SimpleNamespace(wait_for_visible_content=wait, _settle_images=settle)
    data = await B.Session.capture_evidence(fake_session, page, content_selector="a.post")
    assert data == b"png"
    assert page.shot_at_y == 0
    # Waits for the content, THEN scrolls up, THEN lets the images that
    # just came on screen load.
    assert order == ["wait:a.post", "settle@0"]
    # ...and puts the page back where it was, because engines keep reading
    # it after the shot (Facebook's last-post fallback needs its posts on
    # screen).
    assert page.y == 425


def _row():
    row = Row(url="https://x.com/someone", target="Acme")
    row.profile_id = "someone"
    return row


@pytest.mark.asyncio
async def test_capture_keeps_bytes_when_ephemeral():
    row = _row()
    await evidence.capture(None, FakePage(), row, evidence="", ephemeral=True)
    assert row.screenshot_bytes == b"png"


@pytest.mark.asyncio
async def test_capture_without_a_session_still_scrolls_to_top():
    page = FakePage()
    await evidence.capture(None, page, _row(), evidence="", ephemeral=True)
    assert page.shot_at_y == 0


@pytest.mark.asyncio
async def test_failed_capture_says_why_and_does_not_raise():
    row = _row()
    page = FakePage(shot_raises=TimeoutError("Page.screenshot: Timeout 30000ms exceeded."))
    await evidence.capture(None, page, row, evidence="", ephemeral=True)
    assert not row.screenshot_bytes
    assert "screenshot failed" in row.notes
    assert "TimeoutError" in row.notes


@pytest.mark.asyncio
async def test_capture_is_off_when_neither_store_is_wanted():
    page = FakePage()
    await evidence.capture(None, page, _row(), evidence="", ephemeral=False)
    assert page.shot_at_y is None
