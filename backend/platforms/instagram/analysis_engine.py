"""Instagram analysis engine: validation and impersonation signal extraction
-- profile URL -> scored Row.

Session/login-checking and payload parsing (InstagramUser + friends) live in
discovery_engine.py (imported below) since discovery produces/needs them
first; this file owns everything specific to a validated profile visit: URL
normalization, the DOM-header fallback, and the browser-drive loop (Scraper).

Visiting a profile *was designed* to fire `users/web_profile_info` and read its
counts, avatar and newest posts from the payload directly. Verified against two
real, very-active accounts, that assumption is wrong: the current web client
never issues that call passively for a logged-in view of someone else's
profile. So this now asks for it directly instead of waiting: `fetch_via_api()`
below calls the exact same private mobile endpoint discovery_engine.py's
search sweep already uses successfully (`PROFILE_INFO_API`, a sibling of
`MOBILE_SEARCH_API`), the same way, with the same headers, a plain
authenticated HTTP request via `ctx.request`, not a page navigation. That
sidesteps the passive-interception dead end entirely, and since it's a raw
JSON response rather than a rendered page, it does not depend on whether
images are allowed to load in the browser, fixing the logo/avatar field
being blank by default (see below). Passive network interception is kept as
a second-chance source in case the direct call is ever rate-limited or the
endpoint returns nothing for a particular account, and DOM reading remains
the last resort.

What's reliable when both the API call and interception come up empty: the
header numbers render straight into the page ("685M followers", "8,534
posts") in a fixed, stable order, username, full name, posts, followers,
following. That DOM read is the final fallback.

The avatar carries a conventional `alt="<username>'s profile picture"` in the
DOM fallback path, but Instagram unmounts that <img> entirely when its fetch
is blocked, so before this change, the logo/avatar field only populated via
DOM when images were allowed to load, i.e. with --evidence set (the same
posture Facebook already uses). The direct API call above does not have this
problem (it's JSON, not a rendered image), so the logo/avatar field now
populates from a normal analysis run too, not only an --evidence one. The DOM
fallback still requires --evidence, and its avatar match stays intentionally
scoped by an exact alt="<username>'s profile picture", not a loose selector:
a page-wide search for *any* avatar-like image or string was tried and
rejected, because Instagram embeds the session's OWN viewer avatar on every
page ("PolarisViewer"), and an unscoped match silently attributed the
analyst's own photo to whichever profile was being scored.

LAST-POST DATE: the header gives a post COUNT, not a date, and re-verified
live (2026-07-27, against a real active account) that network interception
still does not fire the target's own profile/timeline payload, the two
GraphQL calls that DO fire are the viewer's own SSO/credentials request and
the viewer's own home-feed recommendations, neither naming the profile being
scored. What DOES work: the profile's own most recent post/reel page renders
a real `<time datetime="...">` element (confirmed live: an exact UTC
timestamp, not a relative guess). `read_last_post_date()` below is one extra
page visit, to the first post link already sitting in the grid DOM, to
read that element directly. Skipped for private accounts and accounts with
no posts, where there is nothing to visit.

EVERY DATE IS ATTRIBUTED BEFORE IT IS RECORDED, and that is the rule this
engine got wrong. The reported symptom was an account with no posts coming
back dated TODAY -- not blank, not an error, a confident current date that
then scored the account as active. Three separate paths could produce it,
and all three shared one root cause: a timestamp was read from something
this profile does not own.

  1. The payload. `PROFILE_ENDPOINTS` matches the SUBSTRINGS "api/graphql"
     and "graphql/query", and the two calls named above -- the viewer's own
     feed and recommendations -- are served on exactly those paths. Their
     media carry fresh `taken_at` values, and their nested children
     (carousel items, clips metadata) carry the timestamp with no `user`
     beside it, so `timeline_latest_post`'s old "no owner named, take it
     anyway" fallback took today's date off somebody else's post. That
     fallback is now confined to a container Instagram itself labels a
     user timeline (discovery_engine.py).
  2. The write. Even where the profile record said media_count = 0, the
     assignment in `process()` had no guard, so it wrote the date AND
     overwrote `posts_seen` with "yes" -- erasing the only field that
     could have contradicted it. It now refuses, and notes the
     contradiction instead of resolving it silently.
  3. The DOM. `read_last_post_date`'s selectors ran over the whole
     document and were reached whenever the post count was merely UNKNOWN.
     A profile with no grid of its own still renders post tiles --
     suggestions, an Explore rail -- whose dates are recent by
     construction. It is now scoped to `main`, gated on a CONFIRMED post
     count, and drops any tile or permalink that names a different author.

A blank date is the honest outcome when none of that can be established.
`shared/models/row.py::active_yes` already documents why: an account merely
not dated reads inactive, and `last_post_date` is empty exactly when the
date is unknown -- which only works if nothing writes a date it cannot
attribute.

LOGO / PROFILE PICTURE: the accurate answer is Instagram's own
`has_anonymous_profile_picture`, a boolean it publishes on the same
`data.user` object as the counts, and `InstagramUser.has_custom_pic` now
leads with it. The asset-id markers in shared/avatars.py are kept as the
fallback for the paths that have no payload (the DOM header read), but they
cannot be the primary: Instagram ROTATED the anonymous avatar's id once
already, the list still held only the old one, and from that moment every
account with no picture was recorded as having a real one, silently, until
somebody noticed the cards. The verdict is also tri-state now -- None where
nothing settled it -- so "we did not look" can no longer read as "Yes".

NOT COLLECTED at all: creation date. It lives behind the interactive "About
this account" panel, so that column stays blank rather than guessed.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlparse

from backend.shared import diagnostics, evidence
from backend.shared.pushback import (CHALLENGED, EXPIRED, THROTTLED, Signal,
                                     governor_for, note_response)
from backend.shared.tasks import spawn
from backend.shared.models.row import Row
from backend.shared.walls import first_segment, is_wall_url, url_path
from backend.platforms.scan_options import cancelled, captures_screenshot
from backend.stealth.mouse_movement import humanize_interaction
from backend.shared.text import (MONTHS, _letters_only, name_score,
                                   normalized_host, parse_count,
                                   parse_normalized_url)
from backend.shared.avatars import hd_picture_url, looks_like_placeholder
from backend.platforms.instagram.discovery_engine import (ABOUT_PANEL_APPID,
                                                           MOBILE_UA,
                                                           PROFILE_ENDPOINTS,
                                                           PROFILE_INFO_API,
                                                           RE_CHECKPOINT,
                                                           RE_GONE, RE_LOGIN,
                                                           InstagramSession,
                                                           InstagramUser,
                                                           about_country,
                                                           parse_lines,
                                                           profile_from,
                                                           timeline_latest_post)

BAD_SEGMENTS = {"p", "reel", "reels", "explore", "stories", "accounts", "direct", "tv"}

# Consecutive profile visits that returned no payload, with no explicit
# pushback, before the governor is told Instagram is probably soft-blocking.
NO_PAYLOAD_STREAK_LIMIT = 3

# What Instagram's own error / rate-limit page says. Consulted only after the
# profile header could not be read, so a bio quoting these words never
# matches. This is a THROTTLE, not a verdict on the profile.
_RE_THROTTLE_PAGE = re.compile(
    r"(something went wrong|try again later|please wait a few minutes|"
    r"we limit how often|action blocked)", re.I)


# THE WARNING THAT SITS ON TOP OF A PROFILE THAT LOADED FINE.
#
# Instagram's "We suspect automated behavior on your account" arrives as a
# popup over the page, and the profile behind it usually still renders. So
# the header read succeeded, the page-text checks further down (which only
# run when the header could NOT be read) never ran, and the batch carried on
# to every remaining profile on an account Instagram had just warned. The
# popup is the one place this warning lives, so it is looked for there, on
# every visit.
#
# Only the text INSIDE open dialogs is read, never the whole page: a bio or
# caption can say anything, but a profile page has no dialog open of its own
# when it first loads, so text in one is Instagram talking to the account.
JS_DIALOG_TEXT = """
() => Array.from(document.querySelectorAll('[role="dialog"], [role="alertdialog"]'))
  .map(d => d.innerText || "").join("\\n").slice(0, 4000)
"""


async def account_warning(page) -> str:
    """The checkpoint / automation warning showing in an open dialog on
    `page`, as the matched words -- or "" when there is none, or the page
    could not be read (an unreadable page is not a warning; the existing
    checks further down still get their turn)."""
    try:
        text = await page.evaluate(JS_DIALOG_TEXT) or ""
    except Exception:
        return ""
    m = RE_CHECKPOINT.search(text) if isinstance(text, str) else None
    return m.group(0) if m else ""


def _jitter_ms(base_ms: float) -> int:
    """A fixed wait made human: +-25% around `base_ms`, stretched by the
    pacing governor's multiplier (1.0 while Instagram is happy)."""
    mult = max(1.0, governor_for("instagram").gap_multiplier())
    return int(base_ms * random.uniform(0.75, 1.25) * mult)


def pushback_verdict(sig) -> Optional[tuple[str, str]]:
    """What Instagram's own answer says about the ACCOUNT, as (row status,
    note) -- or None when it says nothing about the account.

    A challenge or a rejected login is an account verdict (the runner
    quarantines it and hands the rest of the batch to another account). A
    throttle is NOT: it says the platform wants us slower, which is the
    governor's job (shared/pushback.py), so it returns None here and the
    caller only records it on the row."""
    if sig.kind == CHALLENGED:
        return "CHECKPOINT", f"instagram challenge ({sig.reason})"
    if sig.kind == EXPIRED:
        return "LOGIN_REQUIRED", f"instagram login rejected ({sig.reason})"
    return None


def register_no_payload(state, pushed: bool) -> str:
    """Count one profile visit that ended with no payload and, when
    `NO_PAYLOAD_STREAK_LIMIT` of them have come in a row WITHOUT an explicit
    pushback, tell the governor Instagram is probably soft-blocking.

    `state` is any object that can carry `_no_payload_streak` (the scraper).
    Returns the note to put on the row, "" when there is nothing to say.
    Explicit pushback is excluded because the response handler has already
    fed the governor with it -- counting it twice would open the breaker on
    one bad moment."""
    state._no_payload_streak = getattr(state, "_no_payload_streak", 0) + 1
    if state._no_payload_streak >= NO_PAYLOAD_STREAK_LIMIT and not pushed:
        governor_for("instagram").record(Signal(
            THROTTLED,
            f"{state._no_payload_streak} consecutive profiles returned no payload"))
        return (f"{state._no_payload_streak} profiles in a row returned no payload "
                "-- possible soft block, pacing slowed")
    return ""

# How long to let the profile's timeline response land before giving up on
# reading a post date from it. Measured live: the profile payload answers at
# ~1s and the timeline at ~9s on the same visit, so this has to clear that
# gap or the cheap tier never gets a chance. Generous on purpose and bounded
# either way -- the cost of waiting is a few seconds on profiles that have
# no date yet, and the cost of reading too early is a false "no posts",
# which feeds the activity classification and the risk score.
_TIMELINE_WAIT_S = 12.0

# The About panel is two clicks and a Bloks round-trip. Short, explicit
# budgets: it is a bonus field, and must never be what makes a profile slow.
_ABOUT_CLICK_MS = 4000
_ABOUT_RENDER_MS = 6000


def normalize_url(url: str) -> str:
    """WHAT: one canonical `https://www.instagram.com/<slug>/` form for
    any Instagram URL variant. HOW: delegates host/path parsing to
    shared/text.py::parse_normalized_url, folds every instagram host
    variant onto www.instagram.com, and always keeps the trailing slash
    (Instagram's own canonical form). LINKED TO: unlike Facebook/Twitter
    (whose discovery_engine.py defines the shared normalizer for both
    phases), Instagram's discovery_engine.py has no normalize_url of its
    own -- this is the only one, used by Scraper below and exposed as
    `Scraper.normalize_url` for callers that don't know which platform
    they're holding."""
    p = parse_normalized_url(url)
    if p is None:
        return ""
    host = normalized_host(p)
    if "instagram" in host:
        host = "www.instagram.com"
    path = p.path.rstrip("/")
    return f"https://{host}{path}/" if path else f"https://{host}/"


def username_of(url: str) -> str:
    """WHAT: the account's username, out of a normalized profile URL.
    HOW: the first non-BAD_SEGMENTS path segment (rejecting reserved
    routes like /p/, /explore/, /accounts/ that are not profile URLs at
    all). LINKED TO: Scraper.process()'s row.profile_id."""
    seg = [s for s in urlparse(normalize_url(url)).path.split("/") if s]
    if not seg:
        return ""
    u = seg[0].lstrip("@")
    return "" if u.lower() in BAD_SEGMENTS else u


class Scraper:
    """One logged-in Instagram session, driven over a list of profiles.

    LINKED TO: `analysis_path` in backend/platforms/registry.py names
    this class (loaded dynamically, by import path string), and
    backend/services/analysis_service.py is the actual caller, one
    Scraper per analysis run.
    """

    normalize_url = staticmethod(normalize_url)

    def __init__(
        self,
        args,
        cookies: list[dict],
        session_id: str = "",
    ):
        """WHAT: binds this Scraper to one InstagramSession built from
        `cookies`. HOW: `args` is a ScanOptions-shaped object
        (scan_options.py); images load only when evidence capture is on
        (a screenshot with images blocked is useless as proof)."""
        self.a = args
        self.evidence = args.evidence or None  # GridFS key prefix, not a path
        self.session = InstagramSession(
            args,
            cookies,
            load_images=captures_screenshot(args),
            session_id=session_id,
        )

    @property
    def ctx(self):
        """The live Playwright BrowserContext, once `start()` has run."""
        return self.session.ctx

    async def start(self):
        """Launches the browser context."""
        await self.session.start()

    async def stop(self):
        """Closes the browser context and its Playwright driver."""
        await self.session.stop()

    async def pause(self, mult: float = 1.0):
        """Between-profile pacing (jittered, fatigue-aware) -- see
        stealth/human.py."""
        await self.session.pause(mult)

    async def check_session(self) -> bool:
        """Is this cookie set still logged in and unchallenged?"""
        return await self.session.check_session()

    async def sync_cookies(self) -> None:
        """Persists the live context cookie jar mid-session."""
        if hasattr(self.session, "sync_cookies"):
            await self.session.sync_cookies()

    # ─────────────────────────── direct API call ───────────────────────── #

    async def fetch_via_api(self, username: str) -> Optional[InstagramUser]:
        """Ask Instagram's own profile-info endpoint directly, the same
        request discovery_engine.py's search sweep already makes
        successfully (PROFILE_INFO_API, a sibling of MOBILE_SEARCH_API),
        rather than waiting for the browser's own JS to fire it passively,
        which it no longer does for a logged-in view of someone else's
        profile (see module docstring). A plain authenticated HTTP call, so
        unlike the DOM fallback it does not depend on whether images are
        allowed to load in the browser. Returns None on anything short of a
        clean parse, callers fall through to interception/DOM."""
        try:
            res = await self.ctx.request.get(
                PROFILE_INFO_API.format(u=quote(username)),
                headers={
                    "User-Agent": MOBILE_UA,
                    "x-ig-app-id": "936619743392459",
                    "accept": "application/json",
                },
                timeout=self.a.timeout * 1000,
            )
            if res.status != 200:
                return None
            text = await res.text()
        except Exception:
            return None
        for blob in parse_lines(text):
            if user := profile_from(blob, username):
                return user
        return None

    # ─────────────────────────── DOM fallback ─────────────────────────── #

    # Confirmed on two unrelated real accounts: the header always renders as
    # username, full name, "N posts", "N followers", "N following", bio, in
    # that fixed order, so the name is simply "whatever precedes the posts
    # line" rather than a guess at a CSS class that Instagram will rename.
    #
    # The avatar is intentionally scoped by an exact alt="<username>'s profile
    # picture" match, not a loose selector. A loose page-wide search for any
    # image or JSON string that looks like an avatar was tried and rejected:
    # Instagram embeds the VIEWER's own avatar (whoever the session cookies
    # belong to, under a "PolarisViewer" block) on every single page, and an
    # unscoped match returns that one, silently attributing the analyst's own
    # photo to whatever profile is being scored. Exact alt-matching only
    # returns the header photo belonging to the account actually being viewed.
    JS_HEADER = """
    (username) => {
      const lines = (document.body.innerText || "").split("\\n")
        .map(s => s.trim()).filter(Boolean);
      let name = "", posts = "", followers = "", following = "";
      for (let i = 0; i < lines.length; i++) {
        if (/^[\\d][\\d,.]*[KMB]?\\s*posts?$/i.test(lines[i])) {
          posts = lines[i];
          followers = lines[i + 1] || "";
          following = lines[i + 2] || "";
          if (i > 0) name = lines[i - 1];
          break;
        }
      }
      // Exact alt match first ("<username>'s profile picture/photo", what
      // this function's own docstring promises), scoped to `header` --
      // `username` is passed in for exactly this. Falls back to a looser
      // substring match ONLY if the exact one misses (a different phrasing
      // Instagram might render), but that fallback stays header-scoped too:
      // it never drops to "any image in the header" the way this used to,
      // which could return an unrelated icon/decoration image sitting
      // beside the real avatar inside the same <header> element.
      const headerImgs = Array.from(document.querySelectorAll('header img[alt]'));
      const wantedAlts = username
        ? [`${username}'s profile picture`, `${username}'s profile photo`]
        : [];
      let avatarEl = headerImgs.find(el => wantedAlts.includes(el.getAttribute('alt') || ''));
      if (!avatarEl) {
        avatarEl = headerImgs.find(el => /profile (picture|photo)/i.test(el.getAttribute('alt') || ''));
      }
      const img = avatarEl || null;
      // header-scoped ONLY -- the unscoped second alternative this used to
      // carry (`svg[aria-label*="Verified" i]` with no `header` prefix)
      // made the header-scoped clause dead: a comma-joined CSS selector
      // matches either side, so ANY verified badge anywhere on the page
      // (a suggested-accounts rail, a related-profile carousel) satisfied
      // the match and got attributed to the profile being scored.
      const verified = !!document.querySelector('header svg[aria-label*="Verified" i]');
      const bodyText = document.body.innerText || "";
      return {
        name, posts, followers, following,
        avatar: img ? (img.src || "") : "",
        verified,
        isPrivate: /this account is private/i.test(bodyText),
      };
    }
    """

    async def read_dom(self, page, username: str) -> dict:
        """WHAT: the last-resort header read (name/posts/followers/
        following/avatar/verified/isPrivate) when neither the direct API
        call nor passive interception produced a profile. HOW: runs
        JS_HEADER above against the rendered page. LINKED TO: called from
        process() only when `fetch_via_api()` and the intercepted
        response both came up empty; result consumed by `fill_from_dom()`
        below."""
        try:
            return await page.evaluate(self.JS_HEADER, username) or {}
        except Exception:
            return {}

    # The grid's own post/reel links are NOT reliably newest-first,
    # confirmed live (adanifoundationschools, 2026-08-10): the first three
    # tiles were all dated 2025-09-01 while a genuinely newer post sat in
    # 4th position. Instagram's "pin to grid" feature, which holds up to
    # 3 posts at the top regardless of date. No pin marker is visible in a
    # third party's view of the DOM at all (checked the full ancestor chain
    # of the pinned tiles up 4 levels, no icon, no aria-label, nothing to
    # key off of the way Twitter's TimelinePinEntry or a "Pinned" badge
    # would give), so this can't be fixed by detecting "is this one
    # pinned." Fixed the same way underneath as Twitter/Facebook were,
    # though: read several candidates and take the real max instead of
    # trusting grid position.
    # SCOPED TO `main`, the profile's own content region. The unscoped
    # version of this selector was a cross-account leak waiting to happen:
    # a profile page also renders post links that are NOT this profile's --
    # an Explore rail, a related-posts carousel, the "Suggested for you"
    # block Instagram shows where an empty grid would be -- and every one
    # of those tiles carries its own owner's publish date. Reading them and
    # taking max() is one of the two ways a postless account was reported
    # as having posted today. The alt-text owner check in
    # `_parse_alt_date` is the second half of the same guard.
    JS_GRID_ALT_DATES = """
    () => {
      const scope = document.querySelector('main') || document.body;
      const out = [];
      for (const a of scope.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]')) {
        const img = a.querySelector('img[alt]');
        if (img) out.push(img.getAttribute('alt') || '');
      }
      return out.slice(0, 12);
    }
    """

    # Instagram's own pin cap is 3. Visiting this many candidate links
    # reaches a non-pinned post only when AT MOST TWO are pinned; with all
    # three pinned, every candidate here is a pinned post and a newer
    # unpinned one in 4th position is not visited. That is a deliberate
    # stealth trade-off (owner decision: no more than 3 extra page loads per
    # profile), not a guarantee -- see read_last_post_date's docstring.
    JS_GRID_POST_LINKS = """
    () => {
      const scope = document.querySelector('main') || document.body;
      return Array.from(scope.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]'))
        .slice(0, 3).map(a => a.getAttribute('href'));
    }
    """

    # The post's timestamp AND who published it. The second half is not
    # decoration: this navigates to a permalink harvested from a page that
    # can also show other people's posts, so the date is only usable once
    # the page confirms whose post it is. Every `/<handle>/` link on the
    # page is collected rather than one header selector, deliberately --
    # Instagram restyles that header often, and a loose collection makes
    # the check FAIL OPEN (no handles read means no rejection) instead of
    # silently rejecting every real post the day the markup changes.
    JS_POST_TIME = """
    () => {
      const t = document.querySelector('time[datetime]');
      const handles = new Set();
      for (const a of document.querySelectorAll('a[href]')) {
        const m = (a.getAttribute('href') || '').match(/^\\/([A-Za-z0-9._]+)\\/?$/);
        if (m) handles.add(m[1].toLowerCase());
      }
      return { time: t ? t.getAttribute('datetime') : null,
               handles: Array.from(handles).slice(0, 40) };
    }
    """

    # "Photo by X on September 01, 2025." / "...on August 09, 2026. May be
    # an image..." / "Photo shared by X on August 08, 2026 tagging @Y."
    #, confirmed live across several accounts, always this "on <Month>
    # <Day>, <Year>" shape for a PHOTO post's own accessibility alt text.
    # Reels carry only their caption as alt text, no date, yields nothing
    # here, which is why tier 2 below still exists.
    _RE_ALT_DATE = re.compile(r"\bon\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\b")

    # The same alt text names the author ahead of the date. Captured so a
    # tile that provably belongs to somebody else can be dropped.
    _RE_ALT_AUTHOR = re.compile(
        r"\b(?:shared\s+)?by\s+(.+?)\s+on\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}\b")

    @staticmethod
    def _owner_mismatch(author: str, owners: tuple[str, ...]) -> bool:
        """True only when `author` is a name we can read AND it matches
        none of this profile's own names.

        REJECTS ONLY ON POSITIVE EVIDENCE, and that asymmetry is the whole
        design. An unreadable author, an alt text in another language, a
        display name that has changed since the profile record was read --
        each of those returns False, so the date is still used. The check
        can therefore only ever remove a date belonging to a DIFFERENT
        account; it can never blank out one that is genuinely this
        account's because the markup moved.
        """
        got = _letters_only(author)
        if not got:
            return False
        known = {_letters_only(o) for o in owners if o}
        known.discard("")
        if not known:
            return False
        return got not in known

    @classmethod
    def _parse_alt_date(cls, text: str, owners: tuple[str, ...] = ()) -> str:
        """WHAT: the date out of a post thumbnail's alt text, when that
        thumbnail is this profile's own post. HOW: Instagram writes a
        human-readable "Photo by X on August 19, 2026." into the alt
        attribute, which is the only date the grid renders anywhere --
        both halves are read, and a tile whose X names a different account
        is dropped (see `_owner_mismatch`). LINKED TO: the DOM tier of
        read_last_post_date(), used when no timeline payload arrived."""
        text = text or ""
        m = cls._RE_ALT_DATE.search(text)
        if not m:
            return ""
        author = cls._RE_ALT_AUTHOR.search(text)
        if author:
            # "Photo by X in Ahmedabad, India on ..." -- the lazy capture
            # swallows the location tag into the author, so the owner check
            # called the account's own newest posts somebody else's. Accept
            # the capture as written OR with a trailing " in <place>" /
            # " with <tagged>" removed.
            raw = author.group(1)
            heads = {raw, re.split(r"\s+(?:in|with)\s+", raw, maxsplit=1)[0]}
            if all(cls._owner_mismatch(h, owners) for h in heads):
                return ""
        mon_name, day, year = m.groups()
        month = next(
            (i for i, name in enumerate(MONTHS, start=1)
             if name.lower() == mon_name.lower()),
            None,
        )
        if not month:
            return ""
        try:
            dt = datetime(int(year), month, int(day), tzinfo=timezone.utc)
        except ValueError:
            return ""
        now = datetime.now(timezone.utc)
        # Instagram launched in 2010; a stamp before that or in the future
        # is not a real post date
        return dt.date().isoformat() if 2010 <= dt.year and dt <= now else ""

    @staticmethod
    def timeline_verdict(
        posts_seen: str, have_date: bool, post_dates: list[str],
    ) -> tuple[str, str]:
        """Should a date read off the intercepted timeline be recorded?
        -> (the date to record or "", a note to put on the row or "").

        NEVER OVER A KNOWN "no posts", which is the whole point. Instagram's
        own record said this account has published nothing, so a timestamp
        that turned up on the same visit belongs to something else --
        writing it is the reported bug, a postless account coming back
        dated TODAY, scored active, sitting in an analyst's queue. The old
        code also overwrote `posts_seen` with "yes" on the way past,
        erasing the one field that could afterwards have contradicted the
        date.

        The contradiction is NOTED rather than swallowed. Two sources
        disagreeing is a fact about this profile and belongs on the row; a
        silent preference for one of them is how the disagreement stops
        being visible at all. `timeline_latest_post` is the layer that
        stops the stray timestamps arriving (discovery_engine.py) -- this
        is the layer that refuses to write one even if a future payload
        shape gets past it.

        No `private` guard, deliberately. A private account this session
        FOLLOWS really does serve its own timeline, and that date is
        genuine. Privacy was never what made the old reading unsafe;
        missing attribution was.
        """
        if not post_dates:
            return "", ""
        if posts_seen == "no":
            return "", ("post timestamps seen on a profile Instagram reports "
                        "as having no posts -- date not recorded")
        if have_date:
            return "", ""
        return max(post_dates), ""

    async def read_last_post_date(
        self, page, private: bool, has_posts: bool,
        owners: tuple[str, ...] = (),
        notes: Optional[list] = None,
    ) -> str:
        """The real last-post date, robust to grid pinning (see
        JS_GRID_ALT_DATES' comment above for the live-confirmed gap this
        closes).

        `has_posts` must be a CONFIRMED yes, not "not known to be no". It
        used to be passed as `posts_seen != "no"`, which sent every profile
        whose post count could not be read down these tiers -- and these
        tiers read whatever post tiles the page happens to show. On an
        account with nothing of its own to show, those tiles belong to
        Instagram's suggestions, and their dates are recent by
        construction. That is "last post = today on an account that never
        posted", arriving through the DOM rather than the payload.

        `owners` is every name this profile is known by (its handle and its
        display name), used to drop a tile or a permalink that names a
        different author. See `_owner_mismatch` for why it only ever
        rejects on positive evidence.

        Tier 1, free, no extra navigation: every currently-rendered grid
        tile's own photo already carries its publish date in its
        accessibility alt text. Reading every tile (not just the first)
        and taking the real max is what survives pinning, at zero added
        cost over the page visit already made to reach this profile.

        Tier 2, up to 3 extra page visits, only when tier 1 found no
        parseable date at all (an all-Reels account, most often). Visits
        the first 3 grid links. Instagram's own pin cap, and reads each
        one's real `<time datetime>` element directly, taking the max --
        but only from a page that does not name somebody else as the
        author. Confirmed live: that page renders a
        `<time datetime="2026-07-23T16:00:21.000Z">` element with an exact
        UTC timestamp.

        KNOWN LIMITATION, said on the row instead of hidden: a Reel tile's
        alt text is only its caption, so when one of the first tiles is
        undated and a later PHOTO tile is dated, tier 1 can report that older
        photo's date although a newer Reel exists. Running tier 2 for those
        profiles would add up to 3 page loads that are skipped today, which
        the stealth budget does not allow, so instead a note is appended to
        `notes` (when given) saying the date may be stale.

        Stops at once when the job is cancelled and returns what was read so
        far, and skips the permalink visits while the platform is being
        throttled (the row says so) -- an extra page load is the last thing a
        throttled platform should get.

        Returns "" on anything short of a clean read: a private account, an
        account not confirmed to have posts, no candidates, or failed
        navigations, never a guess.
        """
        if private or not has_posts:
            return ""

        try:
            alts = await page.evaluate(self.JS_GRID_ALT_DATES) or []
        except Exception:
            alts = []
        parsed = [self._parse_alt_date(a, owners) for a in alts]
        dates = [d for d in parsed if d]
        if dates:
            if notes is not None and not all(parsed[:3]):
                notes.append(
                    "last-post date read from dated photo tiles only: one of "
                    "the newest tiles (likely a Reel) carries no date, so a "
                    "newer post may exist")
            return max(dates)

        if cancelled(self.a):
            return ""
        if not governor_for("instagram").healthy():
            if notes is not None:
                notes.append("last-post permalink visits skipped: Instagram "
                             "is throttling this session, date not read")
            return ""

        try:
            hrefs = await page.evaluate(self.JS_GRID_POST_LINKS) or []
        except Exception:
            hrefs = []
        wanted = {_letters_only(o) for o in owners if o}
        wanted.discard("")
        found: list[str] = []
        visited = 0
        for href in hrefs:
            if not href:
                continue
            if cancelled(self.a):
                break
            if visited:
                # A small jittered pause between permalink visits, never
                # back-to-back; scaled by the pacing governor.
                await page.wait_for_timeout(_jitter_ms(2000))
            visited += 1
            try:
                await page.goto(
                    f"https://www.instagram.com{href}",
                    wait_until="domcontentloaded",
                    timeout=self.a.timeout * 1000,
                )
                await humanize_interaction(page, scroll=False, moves=1)
                await page.wait_for_timeout(_jitter_ms(1500))
                res = await page.evaluate(self.JS_POST_TIME) or {}
                iso = res.get("time") or ""
                handles = {_letters_only(h) for h in (res.get("handles") or [])}
                handles.discard("")
                # A post published by this account always links back to it.
                # No handles read at all -> cannot tell -> keep the date
                # (fail open, the same rule as _owner_mismatch).
                if wanted and handles and not (wanted & handles):
                    continue
                # the element's own datetime attribute is already a UTC ISO
                # string ("...T...Z"), the date is just its first 10
                # characters, no parsing needed
                if iso and len(iso) >= 10:
                    found.append(iso[:10])
            except Exception:
                continue
        return max(found) if found else ""

    # ────────────────────── About this account ────────────────────────── #

    # The panel renders each field as a label line followed by its value
    # line: "Date joined" then "April 2025", "Account based in" then
    # "India". Matching the label and taking the NEXT line is what survives
    # Instagram restyling the panel, which it does often; the labels
    # themselves are the stable part.
    JS_ABOUT_PANEL = """
    () => {
      const lines = (document.body.innerText || "").split("\\n")
        .map(s => s.trim()).filter(Boolean);
      const after = (label) => {
        const i = lines.findIndex(l => l.toLowerCase() === label.toLowerCase());
        return i >= 0 && i + 1 < lines.length ? lines[i + 1] : "";
      };
      return {
        joined: after("Date joined"),
        based_in: after("Account based in"),
        open: lines.some(l => /^about this account$/i.test(l)),
      };
    }
    """

    # "April 2025" -> "2025-04". MONTH precision, and deliberately not
    # padded out to a day: the panel simply does not publish one, and
    # writing "2025-04-01" would put a date in the record that Instagram
    # never said.
    _RE_MONTH_YEAR = re.compile(r"^([A-Za-z]+)\s+(\d{4})$")

    @classmethod
    def _parse_joined(cls, text: str) -> str:
        """WHAT: the join date out of the About-this-account panel. HOW:
        that panel renders a month and year ("April 2025") with no day, so
        this returns MONTH precision (YYYY-MM) rather than inventing a
        day-of-month that would read as a precise date nobody published.
        LINKED TO: read_about_panel(); the value lands on
        row.created_iso."""
        m = cls._RE_MONTH_YEAR.match((text or "").strip())
        if not m:
            return ""
        name, year = m.groups()
        month = next((i for i, mon in enumerate(MONTHS, start=1)
                      if mon.lower() == name.lower()), None)
        if not month:
            return ""
        y = int(year)
        if not (2010 <= y <= datetime.now(timezone.utc).year):
            return ""  # Instagram launched in 2010
        return f"{y:04d}-{month:02d}"

    async def read_about_panel(self, username: str, page=None) -> dict:
        """Instagram's "About this account" -> {country, joined}.

        Two fields this engine could not otherwise report AT ALL: location
        is blank on 100% of stored Instagram rows, and the creation date
        has always been documented here as not exposed. The panel publishes
        both.

        It has to be CLICKED open -- it is not fetchable. Confirmed live
        2026-08-22: `/api/v1/users/<pk>/about_this_account/` is a 404,
        `/api/v1/users/<pk>/info/` answers 200 but carries no country, and
        the panel itself is a POST to a Bloks endpoint carrying ~1.8KB of
        session-derived tokens. Driving the page is what keeps those tokens
        correct without this engine forging any of them.

        WHEN `page` IS GIVEN it is the profile page the caller has already
        loaded, and it is reused: no second tab, no second load of the same
        profile URL (two loads of one profile seconds apart is a pattern no
        person produces). The caller must have taken its evidence screenshot
        first, because the panel is a modal over the profile; the modal is
        dismissed before returning. Without `page`, the old behaviour: its
        own tab, its own load.

        Returns {} on anything short of a clean read -- never a guess.
        """
        reuse = page is not None
        if not reuse:
            page = await self.ctx.new_page()
        bodies: list[str] = []

        async def on_response(resp):
            """Collects the About-this-account payload, which carries the
            join date and country -- neither of which the profile header
            itself renders."""
            try:
                if ABOUT_PANEL_APPID not in resp.url:
                    return
                bodies.append(await resp.text())
            except Exception:
                pass

        listener = lambda r: spawn(on_response(r))  # noqa: E731
        page.on("response", listener)
        try:
            if not reuse:
                await page.goto(
                    f"https://www.instagram.com/{quote(username)}/",
                    wait_until="domcontentloaded", timeout=self.a.timeout * 1000,
                )
            await humanize_interaction(page, scroll=False, moves=1 if reuse else 2)
            # The Options button is NOT on the page at domcontentloaded --
            # measured live, it first exists somewhere between 0 and 3
            # seconds in. Clicking without this wait is why the panel
            # silently produced nothing on every profile: the locator
            # matched zero elements, the loop fell through to `else` and
            # returned {} without any error to explain it.
            try:
                await page.wait_for_selector(
                    'svg[aria-label="Options"], svg[aria-label="More options"]',
                    timeout=_ABOUT_RENDER_MS,
                )
            except Exception:
                return {}
            for selector in (
                'svg[aria-label="Options"]',
                'div[role="button"]:has(svg[aria-label="Options"])',
                'svg[aria-label="More options"]',
            ):
                try:
                    el = page.locator(selector).first
                    if await el.count():
                        await el.click(timeout=_ABOUT_CLICK_MS)
                        break
                except Exception:
                    continue
            else:
                return {}
            try:
                await page.get_by_text("About this account", exact=False).first.click(
                    timeout=_ABOUT_CLICK_MS)
            except Exception:
                return {}
            # the panel paints from the Bloks response, so wait on the
            # rendered label rather than a fixed sleep
            try:
                await page.wait_for_function(
                    "() => /account based in|date joined/i.test(document.body.innerText)",
                    timeout=_ABOUT_RENDER_MS,
                )
            except Exception:
                pass

            dom = {}
            try:
                dom = await page.evaluate(self.JS_ABOUT_PANEL) or {}
            except Exception:
                dom = {}

            # The payload's own named state key first (a contract), the
            # rendered label/value pair second (a layout decision).
            country = ""
            for body in bodies:
                if country := about_country(body):
                    break
            if not country:
                country = (dom.get("based_in") or "").strip()

            return {
                "country": country,
                "joined": self._parse_joined(dom.get("joined") or ""),
            }
        except Exception:
            return {}
        finally:
            if reuse:
                # The caller's page lives on: stop listening and dismiss the
                # modal so whatever it does next sees the profile, not a dialog.
                try:
                    page.remove_listener("response", listener)
                except Exception:
                    pass
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
            else:
                try:
                    await page.close()
                except Exception:
                    pass

    # ───────────────────────────── per URL ────────────────────────────── #

    async def process(
        self, raw_url: str, target: str, feed: str, known: Optional[dict] = None,
    ) -> Row:
        """One profile URL, start to finish -- the counterpart to
        facebook/analysis_engine.py's Scraper.process().

        `known`, when given (only a profile sent here via "Analyse
        Validated Profiles" carries one -- see analysis/runner.py's
        `seed_by_url`), pre-fills `row.location`/`row.created_iso` before
        step 6 below. Every OTHER field still comes from this visit's own
        `fetch_via_api()`/interception/DOM chain regardless of what
        discovery had, since they all land in the SAME payload that a
        status/screenshot/last-post read needs this visit to make anyway
        -- skipping them would save nothing. Location and join date are
        different: both come only from the About-this-account panel, a
        separate click-driven visit (step 6), so pre-filling them from
        `known` lets that visit be skipped entirely when discovery already
        has both.

        WHAT IT RETURNS: a scored `Row`, status OK/PARTIAL/GONE/ERROR/
        CHECKPOINT/LOGIN_REQUIRED, every field tagged with `row.mark()`.

        HOW, roughly in order:
          1. Call `fetch_via_api()` directly (fires in parallel with the
             page visit below, since it costs nothing when it fails).
          2. Navigate to the profile; if the direct API call already had a
             usable result, skip waiting for passive interception.
          3. Pick the richest of (api result, intercepted result) via
             `fill()`, or fall through to session/gone detection and then
             the DOM header (`read_dom()`/`fill_from_dom()`) as the last
             resort.
          4. Capture the evidence screenshot BEFORE the last-post
             fallback below, since that fallback can navigate this same
             page to a permalink (see the inline comment at that call
             site for the live incident this ordering fixes).
          5. If no last-post date yet: try the already-intercepted
             timeline response first (`timeline_latest_post`, free).
          6. If location or join date are still blank: `read_about_panel()`
             on THIS page (the About-this-account panel, click-driven, no
             second page load; skipped while throttled or cancelled).
          7. If still no last-post date: `read_last_post_date()`'s grid-alt
             read, then up to three permalink visits (these navigate the
             page away, which is why 6 runs first).

        LINKED TO: called by `one()` below.
        """
        url = normalize_url(raw_url)
        row = Row(url=url, target=target, original_feed=feed)
        row.profile_id = username_of(url)
        if not row.profile_id:
            # A post, reel or explore link has no username. `wanted` would be
            # empty, which switches OFF the payload's owner filter -- the
            # first record the page happened to carry (the analyst's own
            # account) would be written to this row as an OK profile.
            row.status = "ERROR"
            row.note("not an Instagram profile URL (no username) -- not visited")
            return row
        if known:
            if known.get("location"):
                row.location = known["location"]
                row.mark("location", "discovery")
            if known.get("created_at"):
                row.created_iso = known["created_at"]
                row.mark("created", "discovery")

        # NO DIRECT API CALL ANY MORE. `fetch_via_api` asks
        # `web_profile_info`, and re-verified live 2026-09-24 that endpoint
        # answers HTTP 429 for every username (as discovery_engine.py already
        # recorded on 2026-08-22), while the pk-based `users/<pk>/info/`
        # answers 403. So every profile visit opened with a request that was
        # certain to be refused as rate-limited -- no data, and a steady
        # stream of 429s on the account is exactly the pattern that gets one
        # throttled for real. The profile record arrives anyway, on the
        # page's own `api/graphql` response (measured: every one of 10 live
        # profiles was filled from it), so the interception below is the
        # primary path. `fetch_via_api` is kept for the day the endpoint
        # answers again; it is not called.
        api_user = None

        # Pinned ONCE, before any listener can run. `fill()` reassigns
        # row.profile_id to the numeric pk (`u.entity_id or u.username`),
        # and the response callbacks below close over the row -- so a
        # listener that read row.profile_id would silently start matching
        # against "73877322700" instead of "adaniparivar" the moment the
        # profile payload landed, which is mid-visit and races the timeline
        # response. Confirmed live: that is exactly what stopped
        # timeline_latest_post() from ever matching an owner, sending every
        # profile down to the expensive permalink tier instead. The URL's
        # username is the stable thing here; the same reason
        # twitter/analysis_engine.py pins `wanted` up front.
        wanted = row.profile_id
        page = await self.ctx.new_page()
        found: list[InstagramUser] = []
        # Post dates read straight off the timeline response. Kept separate
        # from `found` on purpose: the payload carrying the timestamps is
        # NOT the payload carrying the profile record, and it cannot pass
        # profile_from()'s gate (see timeline_latest_post's docstring).
        # Collecting it here is what stopped the last-post date being
        # discarded from a response we had already intercepted and parsed.
        post_dates: list[str] = []
        got = asyncio.Event()
        timeline_got = asyncio.Event()
        # What Instagram said when it pushed back during THIS visit (a 429, a
        # challenge body). Until now the response status of these endpoints
        # was never read, so a throttled visit looked identical to a profile
        # that simply had no payload -- see the PARTIAL branch below.
        pushed: list = []

        async def on_response(resp):
            """Collects the profile and timeline payloads. Watches every
            endpoint in PROFILE_ENDPOINTS because Instagram serves the
            same record from several of them and which one answers varies
            per visit; signals `timeline_got` so the visit can stop
            waiting as soon as post dates land rather than always sleeping
            out the full timeout."""
            try:
                if not any(e in resp.url for e in PROFILE_ENDPOINTS):
                    return
                text = await resp.text()
            except Exception:
                return
            parsed_any = False
            for blob in parse_lines(text):
                if user := profile_from(blob, wanted):
                    found.append(user)
                    got.set()
                    parsed_any = True
                if iso := timeline_latest_post(blob, wanted):
                    post_dates.append(iso)
                    timeline_got.set()
                    parsed_any = True
            # The STATUS is always read. The BODY only when it yielded
            # nothing and is small: a real profile payload can quote any
            # words (a bio saying "captcha"), an error body cannot be
            # mistaken for one.
            sig = note_response(
                "instagram", getattr(resp, "status", None),
                text if (not parsed_any and len(text) < 4000) else None)
            if sig.is_pushback:
                pushed.append(sig)

        page.on("response", lambda r: spawn(on_response(r)))

        try:
            doc_status: Optional[int] = None
            try:
                resp = await page.goto(
                    url, wait_until="domcontentloaded", timeout=self.a.timeout * 1000
                )
                # THE PROFILE PAGE'S OWN HTTP STATUS. It used to be thrown
                # away, so a 429/403 on the document itself looked like a
                # profile with no payload. Read here, fed to the governor,
                # and pushed like any other pushback. The body is fetched
                # only for an error status (those pages are tiny) -- never
                # for a 200, which is the whole profile HTML.
                st = getattr(resp, "status", None)
                if isinstance(st, int) and not isinstance(st, bool):
                    doc_status = st
                    err_body = None
                    if st >= 400:
                        try:
                            err_body = (await resp.text())[:4000]
                        except Exception:
                            err_body = None
                    sig = note_response("instagram", st, err_body)
                    if sig.is_pushback:
                        pushed.append(sig)
            except Exception as e:
                # The error itself, first line only (Playwright appends a
                # call log): without it the runner could not tell a dead
                # browser or a blocked account from a slow page.
                first = (str(e).splitlines() or [""])[0][:200]
                row.status = "ERROR"
                row.note(f"navigation failed: {type(e).__name__}: {first}")
                return row

            # No need to wait for passive interception if the direct API
            # call already got us a usable result.
            if api_user is None:
                try:
                    await asyncio.wait_for(got.wait(), timeout=self.a.settle)
                except asyncio.TimeoutError:
                    pass

            body = ""
            try:
                body = await page.inner_text("body")
            except Exception:
                pass

            # Checked BEFORE the profile is read, and whether or not it can
            # be: a warning over a perfectly readable profile is still a
            # warning (see account_warning). CHECKPOINT stops this batch
            # (run() / the analysis runner) and takes the account out of
            # rotation until a person has looked at it.
            if warned := await account_warning(page):
                row.status = "CHECKPOINT"
                row.note(f"instagram warned this account in a popup ({warned!r}) -- "
                         f"batch stopped; open the account, dismiss the warning and "
                         f"let it rest before using it again")
                if not pushed:
                    governor_for("instagram").record(Signal(
                        CHALLENGED, f"warning popup: {warned}"))
                return row

            private = False
            candidates = ([api_user] if api_user else []) + found
            if candidates:
                # prefer the richest payload seen: the direct API call is
                # usually best (see fetch_via_api), but a later interception
                # catch can still be more complete for a given account.
                best = max(
                    candidates,
                    key=lambda u: (u.followers is not None, bool(u.avatar), bool(u.last_post_iso)),
                )
                self.fill(row, best)
                private = best.private
                self._no_payload_streak = 0
            else:
                # By PATH, not substring: `instagram.com/fitnesschallenge`
                # is a profile, and used to be filed CHECKPOINT and
                # quarantine the account (shared/walls.py).
                if is_wall_url(page.url, ("challenge",)):
                    row.status = "CHECKPOINT"
                    row.note("session checkpointed")
                    return row
                if url_path(page.url).startswith("/accounts/login"):
                    row.status = "LOGIN_REQUIRED"
                    row.note("cookies rejected/expired")
                    return row
                # A SUSPENDED / DISABLED ACCOUNT is not a missing profile. It
                # used to fall through to PARTIAL "payload not seen", which
                # the runner files as done: the dead account was never taken
                # out of the pool, every remaining URL was visited with it,
                # and the pacing governor was told the PLATFORM was soft
                # blocking. Re-logging in cannot fix this one, so say so.
                if (first_segment(page.url) == "accounts" and wanted != "accounts"
                        and any(w in url_path(page.url)
                                for w in ("/suspended", "/disabled"))):
                    row.status = "CHECKPOINT"
                    row.note("account suspended or disabled by Instagram -- "
                             "re-login will not fix it, use another account")
                    return row

                # WHAT INSTAGRAM ITSELF SAID, read from the response rather
                # than guessed from the page text. A challenge or an expired
                # login that arrives as an API answer (and leaves the page
                # looking ordinary) used to fall through to "payload not
                # seen". A throttle is recorded on the row but is NOT an
                # account verdict: the governor (shared/pushback.py) slows the
                # platform down and opens its breaker if it keeps happening.
                if pushed:
                    sig = pushed[-1]
                    if verdict := pushback_verdict(sig):
                        row.status, note = verdict
                        row.note(note)
                        return row
                    row.note(f"instagram throttled this visit ({sig.reason})")

                # the payload interception has not been observed to fire in
                # practice (see module docstring), read the rendered header
                dom = await self.read_dom(page, row.profile_id)
                if dom.get("posts") or dom.get("followers") or dom.get("name"):
                    self.fill_from_dom(row, dom)
                    private = bool(dom.get("isPrivate"))
                    self._no_payload_streak = 0
                else:
                    # The page TEXT is only consulted once nothing could be
                    # read from the profile header. These patterns ("Sign up
                    # to see", "page isn't available", "confirm it's you")
                    # match ordinary bios and captions, so run first they read
                    # a real profile as a wall and quarantined a good account.
                    if RE_CHECKPOINT.search(body):
                        row.status = "CHECKPOINT"
                        row.note("session checkpointed")
                        return row
                    if RE_LOGIN.search(body):
                        row.status = "LOGIN_REQUIRED"
                        row.note("cookies rejected/expired")
                        return row
                    # "Something went wrong" / "try again later" is Instagram
                    # refusing to serve, NOT a statement about this profile.
                    # It is a throttle: recorded on the row and on the
                    # governor (once -- an explicit pushback this visit has
                    # already been), never filed as GONE, and never counted
                    # toward the no-payload streak (which would double-count
                    # the same refusal).
                    if _RE_THROTTLE_PAGE.search(body):
                        row.status = "PARTIAL"
                        row.note("instagram served an error / try-again-later "
                                 "page -- throttled, not a verdict on this "
                                 "profile; pacing slowed")
                        if not pushed:
                            governor_for("instagram").record(Signal(
                                THROTTLED, "error page: something went wrong / try again later"))
                        await self.screenshot(page, row)
                        return row
                    if RE_GONE.search(body):
                        row.status = "GONE"
                        # A real 404 corroborates the text, never replaces it.
                        row.note("removed or unavailable -- may already be down"
                                 + (" (HTTP 404)" if doc_status == 404 else ""))
                        return row
                    row.status = "PARTIAL"
                    row.note("profile payload not seen"
                             + (f" (page answered HTTP {doc_status})"
                                if doc_status and doc_status != 200 else ""))
                    # THREE IN A ROW IS NOT A COINCIDENCE. One profile with
                    # nothing to read is ordinary (deleted, odd layout).
                    # Several consecutive ones, with no explicit pushback,
                    # is what a soft block looks like from the inside -- and
                    # PARTIAL carries no session verdict, so nothing else
                    # would ever notice. Fed to the governor as a throttle.
                    if streak_note := register_no_payload(self, bool(pushed)):
                        row.note(streak_note)
                    # Capture anyway. A row whose fields could not be
                    # read is exactly the one an analyst most needs a
                    # picture of, and the screenshot is often the only
                    # surviving proof the account existed. This used to
                    # return here, so every PARTIAL row lost its
                    # evidence as well as its fields.
                    await self.screenshot(page, row)
                    return row

            # BEFORE the last-post fallback, not after: that fallback's
            # tier 2 navigates THIS page to up to three /p/ permalinks
            # to read their <time> elements, so a screenshot taken
            # afterwards captured a post, not the profile it belongs
            # to -- silently, on every row that fell through to it.
            await self.screenshot(page, row)

            # Tier order for the last-post date, cheapest first:
            #   1. the profile payload itself (fill(), above)
            #   2. the timeline response already intercepted during this
            #      same visit -- free, no extra navigation
            #   3. read_last_post_date()'s grid-alt read, then up to three
            #      permalink visits
            # Tier 2 is new and is the one that matters: it was previously
            # parsed and thrown away, so every profile whose payload
            # carried no date paid for tier 3's page visits (or came away
            # blank when those failed too -- 93 of 310 stored rows).
            # The profile payload and the timeline are two SEPARATE
            # responses, and the profile one wins the race every time:
            # measured live, `api/graphql` answers in about a second while
            # the timeline's `graphql/query` (a 421KB body) lands around
            # nine seconds in. `got` is set by the profile payload, so
            # without this second wait process() had always moved on before
            # the timestamps existed -- the date was extractable and simply
            # was not there yet when it was looked for.
            #
            # Only paid for when it can actually help: the profile said
            # this account HAS posts, and no date has been established from
            # any earlier source. An account with no posts, a private one,
            # or one whose payload already carried a date never waits.
            if not row.last_post_iso and not post_dates and row.posts_seen != "no" and not private:
                try:
                    await asyncio.wait_for(
                        timeline_got.wait(), timeout=_TIMELINE_WAIT_S)
                except asyncio.TimeoutError:
                    pass

            date, why = self.timeline_verdict(
                row.posts_seen, bool(row.last_post_iso), post_dates)
            if date:
                row.last_post_iso = date
                row.posts_seen = "yes"
                row.mark("last_post", "graphql-timeline")
            if why:
                row.note(why)

            # A CONFIRMED "yes", not "not known to be no" -- see
            # read_last_post_date's own docstring. `owners` lets it drop a
            # tile or permalink that names a different author: `wanted` is
            # the handle from the URL (row.profile_id has been reassigned
            # to the numeric pk by fill() at this point, which is why the
            # pinned copy is used here too).
            #
            # "ABOUT THIS ACCOUNT" RUNS HERE, ON THIS SAME PAGE, before the
            # last-post tiers below navigate it away. It used to open a new
            # tab and load the same profile a second time and click through on
            # nearly every row. The screenshot is already taken (the panel is
            # a modal), the profile is still on screen, and the modal is
            # dismissed afterwards. Skipped while Instagram is throttling us
            # and once the job is cancelled -- it is a bonus field and never
            # worth an extra interaction then.
            if (not row.location or not row.created_iso) and (row.profile_name or row.profile_id):
                if cancelled(self.a):
                    pass
                elif not governor_for("instagram").healthy():
                    row.note("About-this-account panel skipped: Instagram is "
                             "throttling this session (location / join date left blank)")
                else:
                    about = await self.read_about_panel(wanted, page)
                    if about.get("country") and not row.location:
                        row.location = about["country"]
                        row.mark("location", "about-panel")
                    if about.get("joined") and not row.created_iso:
                        row.created_iso = about["joined"]
                        row.mark("created", "about-panel")
                        # Month precision, because that is all the panel
                        # gives. Said out loud so a YYYY-MM here is never
                        # mistaken for a truncated full date.
                        row.note("join date is month-precision (Instagram publishes no day)")

            if not row.last_post_iso:
                if row.posts_seen == "":
                    # SAID OUT LOUD, not skipped quietly. These tiers read
                    # whatever post tiles the page shows, so they are only
                    # safe once we know the account has tiles of its own.
                    # Without that, a blank date here would be
                    # indistinguishable from "we looked and there is
                    # nothing", which is the confusion that let a
                    # stranger's post become this profile's in the first
                    # place.
                    row.note("post count unreadable -- last-post date not "
                             "attempted (an unowned tile cannot be dated "
                             "to this account)")
                tier_notes: list[str] = []
                last_post = await self.read_last_post_date(
                    page, private, row.posts_seen == "yes",
                    (wanted, row.profile_name), tier_notes,
                )
                for n in tier_notes:
                    row.note(n)
                if last_post:
                    row.last_post_iso = last_post
                    row.mark("last_post", "post-page")

            row.status = "OK" if row.profile_name or row.profile_id else "PARTIAL"
            return row
        finally:
            try:
                await page.close()
            except Exception:
                pass

    @staticmethod
    def fill(row: Row, u: InstagramUser) -> None:
        """WHAT: every field an InstagramUser (from the direct API call
        or passive interception) carries, written onto `row` and tagged
        "api". HOW: a straight field-by-field copy, each guarded so a
        field the payload didn't carry is left untouched rather than
        blanked. LINKED TO: called from process() once it has picked the
        richest available InstagramUser candidate; `fill_from_dom()`
        below is the equivalent for the DOM-header last resort."""
        row.profile_id = u.entity_id or u.username
        # the display name is what an impersonator copies; fall back to handle
        row.profile_name = u.full_name or u.username
        row.mark("name", "api")
        row.name_score = name_score(row.profile_name, row.target)

        if u.followers is not None:
            row.followers = u.followers
            row.followers_exact = "yes"
            row.mark("followers", "api")
        if u.following is not None:
            row.friends = u.following
            row.mark("friends", "api")
        if u.avatar:
            row.profile_pic_url = u.avatar
        # The VERDICT is written whenever the payload settles it, which is
        # not the same question as whether a URL came back. Instagram
        # states `has_anonymous_profile_picture` directly (see
        # InstagramUser.has_custom_pic), and it states it for accounts
        # whose picture URL this visit never received -- that statement is
        # the accurate answer and used to be discarded because it arrived
        # without a URL beside it. None is left alone: unknown must not
        # overwrite a verdict an earlier pass established.
        if u.has_custom_pic is not None:
            row.has_custom_pic = u.has_custom_pic
            row.mark("logo", "api-anonymous-flag" if u.anonymous_pic is not None
                     else "api")
        if u.last_post_iso:
            row.last_post_iso = u.last_post_iso
            row.posts_seen = "yes"
            row.mark("last_post", "api")
        elif u.posts == 0:
            row.posts_seen = "no"
            row.mark("last_post", "api-no-posts")
        elif u.posts:
            # The payload states a post count but carries no timestamps.
            # Recording "yes" is what makes a later blank date honest:
            # shared/completeness.py reads posts_seen to tell "this account
            # never posted" apart from "we failed to read when it last
            # did", and leaving this unset made a real miss look like the
            # former. The date itself comes from the timeline tier below.
            row.posts_seen = "yes"
        if u.city_name:
            row.location = u.city_name
            row.mark("location", "api")
        if u.biography:
            row.bio = u.biography
            row.mark("bio", "api")
        if u.verified:
            row.verified = True
            row.note("verified account")
        if u.private:
            row.note("private account -- posts not visible")

    @staticmethod
    def fill_from_dom(row: Row, dom: dict) -> None:
        """Same fields as fill(), read from the rendered header instead.

        The header itself gives a post COUNT, not a date, so `posts_seen`
        is set here for read_last_post_date() to act on (see process()),
        the actual date comes from that separate post-page visit, not from
        this header read.
        """
        def count(field: str, word: str) -> tuple[Optional[int], bool]:
            """One header count -> (value, was_exact). Anchored to the
            LABEL that follows the number ("1,234 followers"), so it
            cannot pick up a different statistic that happens to sit
            nearby, and reports whether the figure was abbreviated rather
            than presenting "1.2M" as an exact count."""
            m = re.match(rf"^([\d][\d,.]*[KMB]?)\s*{word}\b", dom.get(field, ""), re.I)
            return parse_count(m.group(1)) if m else (None, False)

        name = (dom.get("name") or "").strip()
        if name:
            row.profile_name = name
            row.mark("name", "dom-header")
            row.name_score = name_score(name, row.target)

        val, exact = count("followers", "followers?")
        if val is not None:
            row.followers = val
            row.followers_exact = "yes" if exact else "no"
            row.mark("followers", "dom-header")

        val, _ = count("following", "following")
        if val is not None:
            row.friends = val
            row.mark("friends", "dom-header")

        val, _ = count("posts", "posts?")
        if val is not None:
            row.posts_seen = "yes" if val > 0 else "no"
            row.mark("posts", "dom-header")

        # THE FALLBACK TIER, and the fragile one: a rendered header carries
        # no `has_anonymous_profile_picture`, so this is left matching
        # asset ids -- the check that has already gone stale once when
        # Instagram rotated the anonymous avatar. Reached only when neither
        # the direct API call nor interception produced a payload, and
        # recorded as `dom-header` so a row decided this way is
        # distinguishable from one Instagram answered itself.
        avatar = dom.get("avatar") or ""
        if avatar:
            row.profile_pic_url = hd_picture_url(avatar)
            row.has_custom_pic = not looks_like_placeholder("instagram", row.profile_pic_url)
            row.mark("logo", "dom-header")

        if dom.get("verified"):
            row.verified = True
            row.note("verified account")
        if dom.get("isPrivate"):
            row.note("private account -- posts not visible")

    async def screenshot(self, page, row: Row) -> None:
        """WHAT: captures evidence (a GridFS-stored PNG) for `row`, when
        evidence capture is enabled for this run. HOW: waits for a real
        grid tile to paint (not just the header) before shooting, so the
        capture proves the account is in use, not just that it exists --
        see stealth/browser.py::Session.wait_for_visible_content. LINKED
        TO: called from process() after fields are filled but before the
        last-post fallback tiers (which can navigate this page away). The
        shared mechanics live in shared/evidence.py."""
        # See Session.wait_for_visible_content: field extraction here
        # comes from intercepted API responses, which can land well
        # before the page has visually painted anything, a screenshot
        # taken right after would capture the loading state, not the
        # profile.
        # A grid tile is Instagram's "the posts have painted" signal.
        # Without it the capture is a correct header above a spinner.
        await evidence.capture(
            self.session, page, row, evidence=self.evidence,
            ephemeral=getattr(self.a, 'ephemeral_screenshot', False),
            content_selector='a[href*="/p/"], a[href*="/reel/"]', platform="instagram")

    # ─────────────────────────── orchestration ────────────────────────── #

    async def one(self, u: str, tgt: str, feed: str, known: Optional[dict] = None) -> Row:
        """process() with a failed profile turned into a reportable ERROR
        row instead of raising -- so one bad URL never crashes the whole
        batch `run()`/`run_parallel()` is driving."""
        try:
            return await self.process(u, tgt, feed, known)
        except Exception as e:
            row = Row(url=normalize_url(u), target=tgt, original_feed=feed)
            row.profile_id = username_of(row.url)
            row.status = "ERROR"
            row.note(f"{type(e).__name__}: {e}")
            row.where = diagnostics.where(e)
            return row

    @staticmethod
    def report(i: int, total: int, u: str, row: Row) -> None:
        """One progress line to the platform's own logger after each
        profile -- name/followers/active/risk, enough to eyeball a run
        without opening the database."""
        from backend.shared.logging import get_logger as _gl
        _gl("platforms.instagram.analysis").info(
            f"[{i}/{total}] {u} | {row.status} name={row.profile_name[:22]} "
            f"followers={row.followers if row.followers is not None else '-'} "
            f"active={row.active_yes or '-'} risk={row.risk} {row.priority}"
        )

    async def run(self, jobs: list[tuple[str, str, str]]) -> list[Row]:
        """Every (url, target, feed) job in `jobs`, sequentially, pausing
        between profiles and aborting the batch on a CHECKPOINT (unless
        `self.a.keep_going`) to protect the session. LINKED TO: called by
        analysis_service.py; one Row per job, in order."""
        rows: list[Row] = []
        for i, (u, tgt, feed) in enumerate(jobs, 1):
            row = await self.one(u, tgt, feed)
            rows.append(row)
            self.report(i, len(jobs), u, row)
            if row.status == "CHECKPOINT" and not getattr(self.a, "keep_going", False):
                from backend.shared.logging import get_logger as _gl
                _gl("platforms.instagram.analysis").warning(
                    "CHECKPOINT -- aborting to protect the session."
                )
                break
            if i < len(jobs):
                await self.pause()
        return rows
