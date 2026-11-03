"""How a page proves it is a wall (login, checkpoint, challenge) rather than
content that merely mentions one.

THE DEFECT THIS CLOSES. Every platform's wall check was a substring test:
`"/login" in url`, `"challenge" in page.url`, and a regex over the WHOLE page
body. All three fire on ordinary content -- a profile called
`facebook.com/loginradius`, `instagram.com/fitnesschallenge`, a keyword like
`bank.com/login` that lands in the search URL's query, a bank's bio saying
"report suspicious activity", a post that says "Confirm your booking". A
false wall is not harmless here: the runner reads it as a dead SESSION,
quarantines the account and re-queues the keyword on the next one, which
sees the same page, so one ordinary result can burn the whole pool.

Two rules replace the substring tests:

  * A URL is a wall only by its PATH, and only by its FIRST path segment.
    The query string (where a keyword lives) and later segments (where a
    username lives) can never make a page a wall.
  * Body text is only trusted OUTSIDE the content the platform is showing.
    `chrome_text` returns the page's text with result/post containers cut
    out, so a bio or a post can no longer be mistaken for the platform
    speaking.
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlparse


def url_path(url: str) -> str:
    """The lower-cased path of `url`, or "" when it cannot be parsed."""
    try:
        return (urlparse(url or "").path or "").lower()
    except Exception:                            # noqa: BLE001 - never fatal
        return ""


def first_segment(url: str) -> str:
    """The first path segment of `url` ("/login/?next=x" -> "login")."""
    return url_path(url).lstrip("/").split("/", 1)[0]


def is_wall_url(url: str, segments: Iterable[str]) -> bool:
    """Is `url`'s FIRST path segment one of `segments`?

    `facebook.com/login/?next=..` and `facebook.com/login.php` are walls;
    `facebook.com/loginradius` (a profile) and
    `facebook.com/search/people/?q=bank.com/login` (a keyword) are not.
    """
    return first_segment(url) in {s.lower() for s in segments}


async def chrome_text(page, strip: Iterable[str] = ()) -> str:
    """The page's visible text with the `strip` selectors' elements removed.

    What is left is the platform's own interface -- its walls, its error
    panels -- rather than the people and posts it is displaying. Falls back
    to the whole body when the page cannot be cloned (a closed page, a
    mock), which is the behaviour every caller had before this existed, so
    a failure here can only make a check as loose as it was, never stricter.
    """
    selectors = [s for s in strip if s]
    if selectors:
        try:
            text = await page.evaluate(
                """(sels) => {
                    const c = document.body.cloneNode(true);
                    for (const s of sels) {
                        c.querySelectorAll(s).forEach((n) => n.remove());
                    }
                    return c.textContent || "";
                }""",
                selectors,
            )
            if isinstance(text, str):
                return text
        except Exception:                        # noqa: BLE001 - fall back
            pass
    try:
        return await page.inner_text("body")
    except Exception:                            # noqa: BLE001 - never fatal
        return ""
