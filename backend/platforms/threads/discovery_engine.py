"""Threads discovery engine: keyword in, candidate accounts out.

Also owns the browser session (login/not-joined/checkpoint detection) and the
search-card parsing (ThreadsUser): both are produced here first and re-used by
analysis_engine.py, which imports them rather than redefining them -- the same
split instagram/ uses.

STRATEGY: drive the logged-in "Profiles" tab of threads.com's own search
(`/search?q=<keyword>&filter=profiles`) in a real browser tab and read the
result cards off the rendered page. There is no API tier to prefer here, and
that was checked rather than assumed (2026-11-04):

  * Meta's official Threads API cannot do this job. `keyword_search` finds
    POSTS, not accounts, and `profile_lookup` only answers for public accounts
    with at least 100 followers -- the small look-alike accounts an
    impersonation search exists to find are exactly the ones it skips.
  * The open-source clients (threads-api and friends) replay private endpoints
    written for the old threads.net host and are unmaintained or archived.
  * Logged OUT, the same search returns only ~22 people who POSTED about the
    keyword (never accounts named like it) and then says "Log in for more".

So search needs a signed-in session, and that session is the SAME Instagram
account the Instagram engines use: Threads signs in with Instagram. Its
cookies live on threads.com though, so it is its own pool entry.

WHAT THE LIVE PAGE DOES (measured, logged in, keyword "nike"):

  * The Profiles tab lists ~54 accounts, each card carrying handle, display
    name, a follow button, bio and an abbreviated follower count ("5,880",
    "96.1K"). The first handful are a compact block with no counts.
  * The list ENDS at ~54. There is no "load more" and scrolling further adds
    nothing. Whether that is the true end or Threads' own ceiling cannot be
    told from the page, so a stall at or above PLATFORM_RESULT_CEILING is
    reported as `cap:platform` (TRUNCATED), never as `exhausted`.
  * An empty search says "No results." -- and then shows a "Follow
    suggestions" rail of unrelated accounts. Reading cards without checking
    for that line would file strangers as hits, so the line is checked first
    and the rail is excluded by position.
  * An Instagram account that never joined Threads, or a logged-out session,
    still renders post cards behind a "Say more with Threads" / "Log in or
    sign up" dialog. Those are walls, checked BEFORE any card is trusted.
  * The page has no <main>; the viewer's own profile icon sits in the nav as a
    profile link with no text. A real card always starts with its own handle,
    which is what tells them apart.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

from backend.platforms.scan_options import cancelled
from backend.shared import diagnostics
from backend.shared.avatars import looks_like_placeholder
from backend.shared.models.row import Row
from backend.shared.pushback import governor_for
from backend.shared.text import parse_count
from backend.shared.walls import url_path
from backend.stealth import levels as stealth_levels
from backend.stealth.browser import Session

HOME = "https://www.threads.com/"
# An authenticated-only page. Logged out it redirects to /login; logged in it
# renders "Settings" (both measured 2026-11-04).
ME = "https://www.threads.com/settings"
SEARCH_URL = "https://www.threads.com/search?q={q}&filter=profiles&exact_match=false"

# Where Threads' own Profiles list stopped on the one keyword measured (54). A
# stall at or above this is treated as the platform's ceiling, not as having
# reached the end of the matches.
PLATFORM_RESULT_CEILING = 50
DEFAULT_MAX_SCROLLS = 30
# Scroll rounds in a row that add nobody before the list is called finished.
STALL_ROUNDS = 3

RE_LOGIN = re.compile(
    r"(Log in or sign up for Threads|Join using your Meta account|"
    r"Say more with Threads|Log in with username instead)",
    re.I,
)
RE_CHECKPOINT = re.compile(
    r"(challenge_required|Suspicious Login|We Detected An Unusual Login|"
    r"confirm it.s you|Help Us Confirm)",
    re.I,
)

# Whole LINES the page prints when it is a wall. Matched as an entire line,
# never as a substring, so a bio that quotes these words cannot read as one.
_LOGIN_LINES = frozenset({
    "log in or sign up for threads",
    "log in with username instead",
    "say more with threads",
    "log in for more threads about this topic.",
})
_NOT_JOINED_LINES = frozenset({"join using your meta account"})
_NO_RESULTS_LINE = "no results."

# Words that label a card's button or state rather than describe the account.
_CARD_LABELS = frozenset({"follow", "following", "follow back", "requested"})
_RE_FOLLOWERS_LINE = re.compile(r"^([\d][\d.,]*\s*[KMB]?)\s+followers?$", re.I)


class ThreadsSession(Session):
    """The Threads-specific half of `Session` (backend/stealth/browser.py):
    owns nothing but whether a cookie set is still signed in to threads.com.
    Loaded dynamically by backend/platforms/registry.py's `session_path`
    entry for "threads"."""

    # Meta's CDN, same reasoning as InstagramSession: stubbing images is
    # invisible to a fingerprint script and obvious server-side.
    ALWAYS_LOAD_IMAGES = True

    # Discovery opens threads.com itself, on the tab it searches from, and
    # does its own dwell there (Discovery._search_tab), so the generic
    # warm-up would be a second home-feed load seconds before it.
    WARMUP_BY_ENGINE = True

    async def check_session(self) -> bool:  # type: ignore[override]
        """Are these cookies still signed in to Threads? Visits the
        authenticated-only /settings and confirms the browser actually
        landed there. `expect_path` is the positive proof: a signed-out or
        never-joined session is bounced to /login, and absence of a known
        failure string is not evidence of success."""
        return await super().check_session(
            ME, RE_LOGIN, RE_CHECKPOINT, expect_path="/settings",
        )


# --------------------------------------------------------------- search cards


@dataclass
class ThreadsUser:
    """One Threads account, flattened from a search card or a profile record.
    LINKED TO: the per-platform result type -- produced here (search cards)
    and by analysis_engine.py (profile payload), consumed by that file's
    Scraper.fill()."""

    entity_id: str = ""
    username: str = ""
    full_name: str = ""
    followers: Optional[int] = None
    followers_exact: bool = False
    avatar: str = ""
    biography: str = ""
    verified: bool = False
    private: bool = False
    last_post_iso: str = ""
    posts_seen: str = ""  # yes | no | "" -- see analysis_engine.profile_from

    @property
    def url(self) -> str:
        """Canonical profile URL, built from the handle."""
        return f"https://www.threads.com/@{self.username}" if self.username else ""

    @property
    def has_custom_pic(self) -> Optional[bool]:
        """Does this account use a picture somebody chose? Tri-state: None
        when nothing was read, never "it has one". Threads serves avatars from
        Instagram's CDN, so Instagram's stock-avatar markers apply."""
        if not self.avatar:
            return None
        return not looks_like_placeholder("threads", self.avatar)


def user_from_card(card: dict) -> Optional[ThreadsUser]:
    """One search card -> a ThreadsUser, or None when it is not a card.

    `card` is what _JS_CARDS returns: {u: handle, lines: [...], img, ver}.
    A real card's first line is its own handle -- the viewer's nav icon, an
    avatar-only link or a stray mention all fail that, which is the whole
    defence against filing the analyst's own account as a hit.
    """
    if not isinstance(card, dict):
        return None
    handle = str(card.get("u") or "").strip()
    lines = [str(s).strip() for s in (card.get("lines") or []) if str(s).strip()]
    if not handle or not lines or lines[0].lower() != handle.lower():
        return None

    followers, exact, count_at = None, False, -1
    for i in range(len(lines) - 1, 0, -1):
        m = _RE_FOLLOWERS_LINE.match(lines[i])
        if m:
            followers, exact = parse_count(m.group(1))
            count_at = i
            break

    rest = [s for i, s in enumerate(lines[1:], 1) if i != count_at]
    name = ""
    if rest and rest[0].lower() not in _CARD_LABELS:
        name = rest.pop(0)
    bio = "\n".join(s for s in rest if s.lower() not in _CARD_LABELS)

    return ThreadsUser(
        username=handle,
        full_name=name,
        followers=followers,
        followers_exact=bool(exact) and followers is not None,
        avatar=str(card.get("img") or ""),
        biography=bio,
        verified=bool(card.get("ver")),
    )


def user_to_row(u: ThreadsUser, keyword: str, *, source: str = "dom") -> Row:
    """A search ThreadsUser -> the shared `Row` record `Sweep.hits` carries.
    Search cards are thin (no post dates, abbreviated counts), so this carries
    exactly what the card showed and no more; the per-profile read stays in
    analysis, scoped to the profiles an analyst actually approves."""
    row = Row(
        url=u.url, target=keyword, entity_type="profile",
        profile_id=u.entity_id or u.username, username=u.username,
        profile_name=u.full_name or u.username,
        profile_pic_url=u.avatar, has_custom_pic=u.has_custom_pic, verified=u.verified,
        followers=u.followers, bio=u.biography,
    )
    if u.followers is not None:
        row.followers_exact = "yes" if u.followers_exact else "no"
    for f in ("profile_name", "profile_pic_url", "verified"):
        row.mark(f, f"discovery-search:{source}")
    for f in ("followers", "bio"):
        if getattr(row, f) not in (None, ""):
            row.mark(f, f"discovery-search:{source}")
    return row


def wall_kind(url: str, lines: list[str]) -> str:
    """What stands between this page and its results: "" (nothing),
    "login", "not-joined" or "checkpoint".

    The URL PATH is read first (a redirect to /login is unambiguous); page
    text is read only as whole lines and only for the specific wall copy
    Threads prints, so ordinary content that happens to quote those words is
    never read as a wall (the class of mistake shared/walls.py exists for).
    """
    path = url_path(url)
    if path.startswith(("/login", "/accounts/login")):
        return "login"
    if path.startswith(("/challenge", "/checkpoint")):
        return "checkpoint"
    seen = {str(s).strip().lower() for s in lines}
    if seen & _NOT_JOINED_LINES:
        return "not-joined"
    if seen & _LOGIN_LINES:
        return "login"
    return ""


# Runs in the page. Returns the result cards plus everything the sweep needs
# to decide what the page actually is, in ONE evaluate so the cards and the
# wall/empty verdict always describe the same moment.
#
# A card is the largest ancestor of a profile link that holds exactly one
# distinct handle. Kept only when it starts with its own handle (drops the nav
# icon) and is not part of the "Follow suggestions" rail -- the rail an empty
# search shows in place of results, and the right-hand column beside them.
#
# THE RAIL IS TOLD APART BY ITS HEADING, NOT BY HOW FAR LEFT A CARD SITS.
# That was the first draft ("leftmost column") and the live page refuted it:
# the top block of results is a grid of wide tiles at x = 249, 401, 552 and
# 703, so a narrow-column rule silently dropped four real accounts of 54. The
# heading sat at x = 977. Only if the heading is ever missing does the
# viewport-width guess stand in for it.
_JS_CARDS = r"""
() => {
  const prof = a => /^\/@[^\/?#]+\/?(\?.*)?$/.test(a.getAttribute('href') || '');
  const handle = a => (a.getAttribute('href').match(/^\/@([^\/?#]+)/) || [])[1];
  const lines = (document.body.innerText || '').split('\n')
    .map(s => s.trim()).filter(Boolean);
  let rail = null;
  for (const el of document.querySelectorAll('span,div,h2,h3')) {
    if (el.children.length === 0 && (el.textContent || '').trim() === 'Follow suggestions') {
      rail = el.getBoundingClientRect(); break;
    }
  }
  const distinct = el => new Set(
    [...el.querySelectorAll('a')].filter(prof).map(handle)).size;
  const found = [], seen = new Set();
  for (const a of document.querySelectorAll('a')) {
    if (!prof(a)) continue;
    const r = a.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (rail && r.x >= rail.x - 20 && r.y >= rail.y - 5) continue;
    if (!rail && r.x >= window.innerWidth * 0.72) continue;
    const u = handle(a);
    if (seen.has(u)) continue;
    let n = a, i = 0;
    while (n.parentElement && distinct(n.parentElement) <= 1 && i < 12) { n = n.parentElement; i++; }
    const cl = n.innerText.split('\n').map(s => s.trim()).filter(Boolean).slice(0, 10);
    if (!cl.length || cl[0].toLowerCase() !== u.toLowerCase()) continue;
    seen.add(u);
    const img = n.querySelector('img');
    found.push({u, lines: cl, img: img ? (img.src || '') : '',
                ver: !!n.querySelector('svg[aria-label*="erified" i]')});
  }
  return {url: location.href, lines: lines.slice(0, 400), cards: found};
}
"""


def _says_no_results(lines: list[str]) -> bool:
    """Did Threads print its own "No results." line? Whole line only: a bio
    saying it is not the platform answering."""
    return any(str(s).strip().lower() == _NO_RESULTS_LINE for s in lines)


async def read_page(tab) -> dict:
    """One snapshot of the search page: {url, lines, cards}."""
    try:
        snap = await tab.evaluate(_JS_CARDS)
    except Exception:
        return {"url": "", "lines": [], "cards": []}
    return snap if isinstance(snap, dict) else {"url": "", "lines": [], "cards": []}


async def _page_gap(options) -> None:
    """Wait between two scroll rounds: a jittered gap, interruptible by Stop
    (sliced, like instagram's, so a cancelled sweep does not sit out the gap)."""
    wait = stealth_levels.jittered(stealth_levels.value("threads", "page_gap", 2.5))
    wait *= max(1.0, governor_for("threads").gap_multiplier())
    deadline = time.monotonic() + wait
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or cancelled(options):
            return
        await asyncio.sleep(min(0.2, remaining))


async def _close_quietly(page) -> None:
    """Close a Playwright page, never raising (it may already be gone)."""
    if page is None:
        return
    try:
        await page.close()
    except Exception:
        pass


@dataclass
class Sweep:
    """One keyword's search sweep, and how it ended. Same shape as every other
    platform's Sweep (hits, pages, stopped, complete, source, schema) so
    discovery/runner.py reads them all alike; there is no shared base class."""

    keyword: str
    tab: str = "people"
    hits: list[Row] = field(default_factory=list)
    users: list[ThreadsUser] = field(default_factory=list)
    pages: int = 0
    stopped: str = ""
    complete: bool = False
    seconds: float = 0.0
    error: str = ""
    # Where in OUR code the sweep failed, when it raised -- see
    # shared/diagnostics.py::where. Blank for a sweep that ended itself.
    where: str = ""
    source: str = "dom"
    # Threads has no payload schema to probe (cards are read off the page), so
    # this stays empty; it exists so the runner can read it like any Sweep.
    schema: dict = field(default_factory=dict)

    def summary(self) -> str:
        return f"{len(self.hits)} hits, {self.pages} scrolls, {self.stopped}"


class Discovery:
    """Runs keyword sweeps against Threads' Profiles search.

    LINKED TO: `discovery_path` in backend/platforms/registry.py names this
    class (loaded dynamically); discovery/runner.py is the caller."""

    def __init__(self, args, ctx):
        """`args` is a DiscoveryOptions-shaped object (scan_options.py);
        `ctx` is the already-started Playwright BrowserContext
        (stealth/browser.py::Session.start())."""
        self.a = args
        self.ctx = ctx
        # One threads.com tab per session, reused for every keyword.
        self._tab = None

    async def _search_tab(self):
        """The threads.com tab searches are made from, opened on first use
        (landing on the home feed, like a person) and reused after. None when
        it cannot be had."""
        if self._tab is not None:
            try:
                if not self._tab.is_closed():
                    return self._tab
            except Exception:
                pass
            self._tab = None
        tab = None
        try:
            tab = await self.ctx.new_page()
            await tab.goto(HOME, wait_until="domcontentloaded",
                           timeout=self.a.timeout * 1000)
            # THE WARM-UP, AND THE ONLY ONE (see ThreadsSession.WARMUP_BY_ENGINE).
            # Best effort and never fatal: a courtesy must not fail a search.
            try:
                if not cancelled(self.a):
                    await asyncio.sleep(random.uniform(2.0, 3.8))
                    await tab.mouse.wheel(0, random.randint(150, 320))
                    await asyncio.sleep(random.uniform(0.8, 1.5))
            except Exception:
                pass
            self._tab = tab
            return tab
        except Exception:
            await _close_quietly(tab)
            return None

    async def sweep(self, keyword: str, tab: str = "people", on_progress=None) -> Sweep:
        """One keyword, start to finish.

        HOW: open the Profiles search, wait for the page to say what it is
        (cards, "No results.", or a wall), then scroll with the real wheel
        until a few rounds in a row add nobody. EVERY ending names itself --
        see shared/resilience.py::sweep_outcome for how each is classified:

          login / checkpoint   a wall; the runner marks the session
          no-results           Threads said "No results." (confirmed, satisfied)
          exhausted            the list stopped growing below the ceiling
          cap:platform         it stopped at/above PLATFORM_RESULT_CEILING
          cap:results          our own max_results was met
          cap:pages            the scroll budget ran out while still growing
          empty-unconfirmed    nothing read AND no "No results." and no wall:
                               an unanswered search, never a clean zero
        """
        out = Sweep(keyword=keyword, tab=tab)
        started = time.time()
        by_name: dict[str, ThreadsUser] = {}
        notified = 0

        async def _emit() -> None:
            """Hand the caller every user found since the last call, so the
            grid fills as the scroll goes. Capped like the final trim, and a
            broken callback never aborts the sweep."""
            nonlocal notified
            if on_progress is None:
                return
            users = list(by_name.values())
            if self.a.max_results:
                users = users[: self.a.max_results]
            fresh = users[notified:]
            if not fresh:
                return
            notified = len(users)
            rows = [user_to_row(u, keyword, source=out.source) for u in fresh if u.url]
            if not rows:
                return
            try:
                res = on_progress(len(users), out.pages, rows)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass

        try:
            page = await self._search_tab()
            if page is None:
                out.stopped = "error"
                out.error = "could not open a threads.com tab"
                return out

            max_scrolls = int(getattr(self.a, "max_pages", 0) or 0) or DEFAULT_MAX_SCROLLS
            await page.goto(SEARCH_URL.format(q=quote(keyword.strip(), safe="")),
                            wait_until="domcontentloaded",
                            timeout=self.a.timeout * 1000)

            stable = 0
            snap: dict = {}
            for rnd in range(max_scrolls + 1):
                if cancelled(self.a):
                    out.stopped = "cancelled"
                    break
                # Give the first read time to render; later rounds pace
                # themselves through _page_gap.
                await asyncio.sleep(random.uniform(3.5, 5.0) if rnd == 0 else 0.4)
                snap = await read_page(page)
                lines = snap.get("lines") or []

                if rnd == 0 or not by_name:
                    # A wall is decided BEFORE any card is trusted: a signed-out
                    # or never-joined session still renders post cards behind
                    # its dialog.
                    wall = wall_kind(snap.get("url") or page.url, lines)
                    if wall:
                        out.stopped = "checkpoint" if wall == "checkpoint" else "login"
                        out.error = {
                            "login": "login required: threads.com is asking this session "
                                     "to log in",
                            "not-joined": "login required: this Instagram account has not "
                                          "joined Threads (accept the Threads sign-up once "
                                          "on threads.com)",
                            "checkpoint": "checkpoint: Threads is challenging this session",
                        }[wall]
                        break
                    if _says_no_results(lines):
                        # Read it twice: a page still rendering must not be
                        # filed as an empty keyword on one early glance.
                        await asyncio.sleep(1.5)
                        snap = await read_page(page)
                        lines = snap.get("lines") or []
                        if _says_no_results(lines) and not snap.get("cards"):
                            out.stopped, out.complete = "no-results", True
                            break

                before = len(by_name)
                for card in snap.get("cards") or []:
                    user = user_from_card(card)
                    if user and user.username.lower() not in by_name:
                        by_name[user.username.lower()] = user
                if len(by_name) > before:
                    out.pages += 1
                    stable = 0
                    await _emit()
                else:
                    stable += 1

                if self.a.max_results and len(by_name) >= self.a.max_results:
                    out.stopped = "cap:results"
                    break
                # `stable` only counts once something was read: the first
                # rounds of an unrendered page are not "the end of the list".
                if by_name and stable >= STALL_ROUNDS:
                    if len(by_name) >= PLATFORM_RESULT_CEILING:
                        out.stopped, out.complete = "cap:platform", False
                    else:
                        out.stopped, out.complete = "exhausted", True
                    break
                if not by_name and stable >= STALL_ROUNDS + 2:
                    break  # nothing ever rendered; classified below

                try:
                    await page.mouse.wheel(0, random.randint(700, 1100))
                except Exception:
                    pass
                await _page_gap(self.a)

            if not out.stopped:
                if by_name:
                    out.stopped, out.complete = "cap:pages", False
                else:
                    # Nothing read, no wall, no "No results.": the page never
                    # answered. Filing that as an empty keyword is exactly the
                    # clean zero this tool refuses.
                    out.stopped, out.complete = "empty-unconfirmed", False

            out.users = list(by_name.values())
            out.hits = [user_to_row(u, keyword, source=out.source)
                        for u in out.users if u.url]
            if self.a.max_results:
                out.hits = out.hits[: self.a.max_results]
        except Exception as e:
            out.stopped, out.error = "error", f"{type(e).__name__}: {e}"
            out.where = diagnostics.where(e)
        finally:
            out.seconds = time.time() - started

        return out

