"""The evidence screenshot for one analysed profile, shared by every
browser platform (facebook, instagram, twitter, tiktok).

The four engines each carried their own copy of this, identical except for
the selector that means "a post has painted". Copies drift: one platform
gets a fix the others never see. What each engine still owns is that
selector, and the decision of WHEN in its visit to call this.

Two rules this enforces for all of them:

  FROM THE TOP     the shot is taken scrolled to the top of the profile,
                   with on-screen images loaded -- see
                   stealth/browser.py::Session.capture_evidence.
  NEVER SILENT     a capture that fails writes WHY onto the row and the
                   log. It used to be `except Exception: pass`, so the row
                   showed "missed: screenshot" with no reason anywhere.
"""

from __future__ import annotations

import re
from typing import Any

from backend.shared.logging import get_logger

log = get_logger("shared.evidence")


async def capture(session: Any, page: Any, row: Any, *, evidence: str, ephemeral: bool,
                  content_selector: str = "", platform: str = "") -> None:
    """Shoot `page` and attach it to `row`: stored under `evidence` (the
    GridFS key prefix) when set, kept in memory when `ephemeral`. Never
    raises -- a failed capture must not fail the visit, the scraped fields
    are the finding."""
    if not evidence and not ephemeral:
        return
    # DETERMINISTIC key, no timestamp: re-analysing a profile must
    # overwrite its own previous capture, not add another one. With a
    # timestamp, a daily re-sweep left one PNG per profile per run in the
    # store forever, and the profile document only ever pointed at the
    # newest, every earlier one was unreachable garbage.
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", row.profile_id or "entity")[:60]
    key = f"{evidence}/{stem}.png" if evidence else ""
    try:
        if session is not None:
            data = await session.capture_evidence(page, content_selector=content_selector)
        else:
            from backend.stealth.browser import scroll_to_top

            await scroll_to_top(page)
            data = await page.screenshot(full_page=False)
        if evidence:
            from backend.database.repositories import evidence_repository

            await evidence_repository.save(key, data)
            row.screenshot = key
        if ephemeral:
            row.screenshot_bytes = data
    except Exception as e:
        why = f"{type(e).__name__}: {e}".splitlines()[0][:200]
        row.note(f"screenshot failed -- {why}")
        log.warning(f"{platform or '?'}: screenshot failed for {row.url} -- {why}")
