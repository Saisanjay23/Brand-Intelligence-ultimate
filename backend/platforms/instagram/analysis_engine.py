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

NOT COLLECTED at all: creation date. It lives behind the interactive "About
this account" panel, so that column stays blank rather than guessed.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlparse

from backend.shared.models.row import Row
from backend.platforms.scan_options import captures_screenshot
from backend.shared.text import (MONTHS, name_score,
                                   normalized_host, parse_count,
                                   parse_normalized_url)
from backend.shared.avatars import looks_like_placeholder
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
        proxy: dict | None = None,
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
            proxy=proxy,
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
    JS_GRID_ALT_DATES = """
    () => {
      const out = [];
      for (const a of document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]')) {
        const img = a.querySelector('img[alt]');
        if (img) out.push(img.getAttribute('alt') || '');
      }
      return out.slice(0, 12);
    }
    """

    # Instagram's own pin cap is 3, visiting this many candidate links
    # guarantees at least one genuinely-newest, non-pinned post is checked
    # regardless of how many (0 to 3) are actually pinned right now.
    JS_GRID_POST_LINKS = """
    () => Array.from(document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]'))
      .slice(0, 3).map(a => a.getAttribute('href'))
    """

    JS_POST_TIME = """
    () => {
      const t = document.querySelector('time[datetime]');
      return t ? t.getAttribute('datetime') : null;
    }
    """

    # "Photo by X on September 01, 2025." / "...on August 09, 2026. May be
    # an image..." / "Photo shared by X on August 08, 2026 tagging @Y."
    #, confirmed live across several accounts, always this "on <Month>
    # <Day>, <Year>" shape for a PHOTO post's own accessibility alt text.
    # Reels carry only their caption as alt text, no date, yields nothing
    # here, which is why tier 2 below still exists.
    _RE_ALT_DATE = re.compile(r"\bon\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\b")

    @classmethod
    def _parse_alt_date(cls, text: str) -> str:
        """WHAT: the date out of a post thumbnail's alt text. HOW:
        Instagram writes a human-readable "Photo by X on August 19, 2026."
        into the alt attribute, which is the only date the grid renders
        anywhere. LINKED TO: the DOM tier of read_last_post(), used when
        no timeline payload arrived."""
        m = cls._RE_ALT_DATE.search(text or "")
        if not m:
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

    async def read_last_post_date(self, page, private: bool, has_posts: bool) -> str:
        """The real last-post date, robust to grid pinning (see
        JS_GRID_ALT_DATES' comment above for the live-confirmed gap this
        closes).

        Tier 1, free, no extra navigation: every currently-rendered grid
        tile's own photo already carries its publish date in its
        accessibility alt text. Reading every tile (not just the first)
        and taking the real max is what survives pinning, at zero added
        cost over the page visit already made to reach this profile.

        Tier 2, up to 3 extra page visits, only when tier 1 found no
        parseable date at all (an all-Reels account, most often). Visits
        the first 3 grid links. Instagram's own pin cap, and reads each
        one's real `<time datetime>` element directly, taking the max.
        Confirmed live: that page renders a
        `<time datetime="2026-07-23T16:00:21.000Z">` element with an exact
        UTC timestamp.

        Returns "" on anything short of a clean read: a private/postless
        account, no candidates, or failed navigations, never a guess.
        """
        if private or not has_posts:
            return ""

        try:
            alts = await page.evaluate(self.JS_GRID_ALT_DATES) or []
        except Exception:
            alts = []
        dates = [d for d in (self._parse_alt_date(a) for a in alts) if d]
        if dates:
            return max(dates)

        try:
            hrefs = await page.evaluate(self.JS_GRID_POST_LINKS) or []
        except Exception:
            hrefs = []
        found: list[str] = []
        for href in hrefs:
            if not href:
                continue
            try:
                await page.goto(
                    f"https://www.instagram.com{href}",
                    wait_until="domcontentloaded",
                    timeout=self.a.timeout * 1000,
                )
                await page.wait_for_timeout(1500)
                iso = await page.evaluate(self.JS_POST_TIME)
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

    async def read_about_panel(self, username: str) -> dict:
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

        Runs on its OWN page: the panel is a modal over the profile, so
        opening it on the caller's page would put a dialog across the
        evidence screenshot.

        Returns {} on anything short of a clean read -- never a guess.
        """
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

        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        try:
            await page.goto(
                f"https://www.instagram.com/{quote(username)}/",
                wait_until="domcontentloaded", timeout=self.a.timeout * 1000,
            )
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
             timeline response first (`timeline_latest_post`, free), then
             `read_last_post_date()`'s grid-alt/permalink-visit tiers.
          6. If location or join date are still blank: `read_about_panel()`
             (the About-this-account panel, a click-driven visit).

        LINKED TO: called by `one()` below.
        """
        url = normalize_url(raw_url)
        row = Row(url=url, target=target, original_feed=feed)
        row.profile_id = username_of(url)
        if known:
            if known.get("location"):
                row.location = known["location"]
                row.mark("location", "discovery")
            if known.get("created_at"):
                row.created_iso = known["created_at"]
                row.mark("created", "discovery")

        # Try the direct API call first (see fetch_via_api's docstring),
        # independent of the page visit below, so it costs nothing extra
        # even when it comes up empty and we fall through to interception/DOM.
        api_user = await self.fetch_via_api(row.profile_id) if row.profile_id else None

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
            for blob in parse_lines(text):
                if user := profile_from(blob, wanted):
                    found.append(user)
                    got.set()
                if iso := timeline_latest_post(blob, wanted):
                    post_dates.append(iso)
                    timeline_got.set()

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        try:
            try:
                await page.goto(
                    url, wait_until="domcontentloaded", timeout=self.a.timeout * 1000
                )
            except Exception:
                row.status = "ERROR"
                row.note("navigation failed")
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
            else:
                if RE_CHECKPOINT.search(body) or "challenge" in page.url:
                    row.status = "CHECKPOINT"
                    row.note("session checkpointed")
                    return row
                if "/accounts/login" in page.url or RE_LOGIN.search(body):
                    row.status = "LOGIN_REQUIRED"
                    row.note("cookies rejected/expired")
                    return row
                if RE_GONE.search(body):
                    row.status = "GONE"
                    row.note("removed or unavailable -- may already be down")
                    return row

                # the payload interception has not been observed to fire in
                # practice (see module docstring), read the rendered header
                dom = await self.read_dom(page, row.profile_id)
                if dom.get("posts") or dom.get("followers") or dom.get("name"):
                    self.fill_from_dom(row, dom)
                    private = bool(dom.get("isPrivate"))
                else:
                    row.status = "PARTIAL"
                    row.note("profile payload not seen")
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

            if not row.last_post_iso and post_dates:
                row.last_post_iso = max(post_dates)
                row.posts_seen = "yes"
                row.mark("last_post", "graphql-timeline")

            if not row.last_post_iso:
                last_post = await self.read_last_post_date(
                    page, private, row.posts_seen != "no"
                )
                if last_post:
                    row.last_post_iso = last_post
                    row.mark("last_post", "post-page")
            # "About this account" -- the only source for two fields this
            # engine could not otherwise report at all. Runs when at least
            # one of them is still missing, and never for a row we could
            # not read in the first place (no name and no id means there is
            # no profile there to ask about).
            if (not row.location or not row.created_iso) and (row.profile_name or row.profile_id):
                about = await self.read_about_panel(wanted)
                if about.get("country") and not row.location:
                    row.location = about["country"]
                    row.mark("location", "about-panel")
                if about.get("joined") and not row.created_iso:
                    row.created_iso = about["joined"]
                    row.mark("created", "about-panel")
                    # Month precision, because that is all the panel gives.
                    # Said out loud so a YYYY-MM here is never mistaken for
                    # a truncated full date.
                    row.note("join date is month-precision (Instagram publishes no day)")

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
            row.has_custom_pic = u.has_custom_pic
            row.mark("logo", "api")
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

        val, exact = count("followers", "followers")
        if val is not None:
            row.followers = val
            row.followers_exact = "yes" if exact else "no"
            row.mark("followers", "dom-header")

        val, _ = count("following", "following")
        if val is not None:
            row.friends = val
            row.mark("friends", "dom-header")

        val, _ = count("posts", "posts")
        if val is not None:
            row.posts_seen = "yes" if val > 0 else "no"
            row.mark("posts", "dom-header")

        avatar = dom.get("avatar") or ""
        if avatar:
            row.profile_pic_url = avatar
            row.has_custom_pic = not looks_like_placeholder("instagram", avatar)
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
        last-post fallback tiers (which can navigate this page away)."""
        if not self.evidence and not getattr(self.a, 'ephemeral_screenshot', False):
            return
        # DETERMINISTIC key, no timestamp: re-analysing a profile must
        # overwrite its own previous capture, not add another one. With a
        # timestamp, a daily re-sweep left one PNG per profile per run in
        # the store forever, and the profile document only ever pointed at
        # the newest, every earlier one was unreachable garbage.
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", row.profile_id or "entity")[:60]
        key = f"{self.evidence}/{stem}.png" if self.evidence else ""
        try:
            # See Session.wait_for_visible_content: field extraction here
            # comes from intercepted API responses, which can land well
            # before the page has visually painted anything, a screenshot
            # taken right after would capture the loading state, not the
            # profile.
            # A grid tile is Instagram's "the posts have painted" signal.
            # Without it the capture is a correct header above a spinner.
            await self.session.wait_for_visible_content(
                page, content_selector='a[href*="/p/"], a[href*="/reel/"]')
            data = await page.screenshot(full_page=False)
            
            if self.evidence:
                from backend.database.repositories import evidence_repository
                await evidence_repository.save(key, data)
                row.screenshot = key
                
            if getattr(self.a, 'ephemeral_screenshot', False):
                row.screenshot_bytes = data
        except Exception:
            pass

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
