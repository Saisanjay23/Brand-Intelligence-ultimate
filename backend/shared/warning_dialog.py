"""The warning a platform pops up OVER a page that otherwise loaded fine.

THE DEFECT THIS CLOSES. Instagram's "We suspect automated behavior on your
account" arrives as a popup over a profile that still renders, so every
check that only ran when the page could NOT be read missed it, and the batch
carried on visiting profiles on an account the platform had just warned
(fixed for Instagram in 0e4af96). Threads (the same Meta account), X and
Facebook show the same kind of popup -- an automation warning, a "you're
temporarily blocked" notice, an account-locked sheet -- so the check lives
here once instead of being re-learned per platform.

ONLY TEXT INSIDE OPEN DIALOGS IS READ, never the whole page. A bio, a post
or a search result can say anything; a dialog that is open when a page first
loads is the platform talking to the account. The tokens are the same ones
shared/pushback.py uses for response bodies, plus a few that only ever
appear as on-screen copy.

Reading costs one `evaluate` and no network request, and a page that cannot
be read is "no warning" -- the existing checks still get their turn.
"""

from __future__ import annotations

from typing import Optional

from backend.shared import pushback
from backend.shared.pushback import CHALLENGED, THROTTLED, Signal

# Every element a platform renders a modal into: ARIA dialogs (Meta, X,
# TikTok) and X's own bottom sheet.
JS_DIALOG_TEXT = """
() => Array.from(document.querySelectorAll(
    '[role="dialog"], [role="alertdialog"], [data-testid="sheetDialog"]'))
  .map(d => d.innerText || "").join("\\n").slice(0, 4000)
"""

# On-screen wording that never arrives as an API body.
_EXTRA_CHALLENGE = (
    "your account has been locked", "your account is temporarily locked",
    "we've temporarily locked your account", "verify to continue",
)
_EXTRA_THROTTLE = (
    "action blocked", "misusing this feature", "you've been temporarily blocked",
    "you’ve been temporarily blocked",
)


def classify_dialog_text(text: str) -> Optional[Signal]:
    """Dialog text -> a CHALLENGED / THROTTLED `Signal`, or None when the
    dialog says nothing about the account (or there is none)."""
    t = (text or "").lower() if isinstance(text, str) else ""
    if not t.strip():
        return None
    for tok in pushback._CHALLENGE_TOKENS + _EXTRA_CHALLENGE:
        if tok in t:
            return Signal(CHALLENGED, f"warning popup says {tok!r}")
    for tok in pushback._THROTTLE_TOKENS + _EXTRA_THROTTLE:
        if tok in t:
            return Signal(THROTTLED, f"warning popup says {tok!r}")
    return None


async def dialog_text(page) -> str:
    """The text of every open dialog on `page`, "" when none or unreadable."""
    try:
        text = await page.evaluate(JS_DIALOG_TEXT)
    except Exception:
        return ""
    return text if isinstance(text, str) else ""


async def dialog_warning(page) -> Optional[Signal]:
    """The account warning showing in an open dialog on `page`, or None."""
    return classify_dialog_text(await dialog_text(page))
