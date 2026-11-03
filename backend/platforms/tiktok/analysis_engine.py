"""TikTok analysis engine: validation and impersonation signal extraction
-- profile URL -> scored Row.

Session/login-checking and payload parsing (TikTokUser + friends) live in
discovery_engine.py (imported below) since discovery produces/needs them
first; this file owns everything specific to a validated profile visit: URL
normalization, the DOM-header fallback, and the browser-drive loop
(Scraper).

Same extraction order as discovery's search sweep, and for the same reason
(see discovery_engine.py's module docstring): TikTok's own hydration script
first (same page load, no XHR race), then the rendered header text, in that
order. NEEDS LIVE VERIFICATION, see that same docstring.

LAST-POST DATE, in order of preference:
  1. The newest video's id, decoded straight from the id itself (see
     discovery_engine.py::_video_id_to_iso), free, no extra navigation,
     but the profile hydration payload's own `itemList` is confirmed
     (live, 2026-08-11) to always arrive empty, so this tier realistically
     never fires today. Kept because it costs nothing to try and would
     start working for free if TikTok ever changes that.
  2. `newest_post_via_search`, one extra page visit, searching the
     account's own username on the Videos tab and reading the real
     `createTime` TikTok's search response carries. This exists because
     the obvious alternative, visiting the profile's OWN video grid --
     is live-confirmed to trip TikTok's slide-verify CAPTCHA challenge
     (repeatedly, across a clean session with no prior automated visits);
     this backend will never automate solving that, on principle, not
     just because it's hard. Search never triggered the challenge in
     testing. Not exhaustive (search relevance, not a full post history),
     so a miss here is not evidence of inactivity, see that function's
     own docstring.

NOT COLLECTED: account creation date. Not exposed anywhere on a profile
page's own rendered surface or (per public write-ups) its hydration
payload, so that column stays blank rather than guessed.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from backend.shared import diagnostics, evidence
from backend.shared.models.row import Row
from backend.platforms.scan_options import captures_screenshot
from backend.shared.text import name_score, normalized_host, parse_count, parse_normalized_url
from backend.shared.walls import chrome_text, is_wall_url
from backend.platforms.tiktok.discovery_engine import (RE_CHECKPOINT, RE_GEOBLOCK,
                                                        RE_GONE, RE_LOGIN,
                                                        TikTokSession, TikTokUser,
                                                        geoblock_redirect,
                                                        newest_post_iso,
                                                        newest_post_via_search,
                                                        profile_from, read_hydration,
                                                        search_users_in)

# Kept for anything that still imports it. username_of() no longer needs it:
# a profile is identified by an `@` first segment, and a route name can never
# start with one, so there is nothing to blocklist.
BAD_SEGMENTS = {
    "video", "live", "tag", "music", "discover", "explore", "upload",
    "messages", "following", "notification", "search", "foryou", "login",
    "t", "embed", "legal", "business", "about",
}

# TikTok handles are letters, digits, underscore and full stop. Anything else
# is not a handle, and it must not reach the page URL we build or the JS.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,64}$")

# The `Row.src["last_post"]` value that means "the newest post search happened
# to show", i.e. a floor under the real date. analysis/runner.py keys on it.
LAST_POST_LOWER_BOUND = "search-lowerbound"


def _int_status(resp) -> Optional[int]:
    """An HTTP status off a Playwright response, or None when there is none
    (no response object, or something that is not a real status)."""
    s = getattr(resp, "status", None)
    return s if isinstance(s, int) and not isinstance(s, bool) else None

# The parts of a profile page that show the ACCOUNT'S OWN content -- its bio,
# its title line and its video grid. Text inside them is the account speaking,
# not TikTok, so it is cut out before the page is read for walls: a bio that
# says "Government of India", "captcha" or "log in to TikTok" is a profile,
# not a block. See shared/walls.py.
_PROFILE_CONTENT_SELECTORS = (
    '[data-e2e="user-bio"]',
    '[data-e2e="user-title"]',
    '[data-e2e="user-subtitle"]',
    '[data-e2e="user-post-item-list"]',
    '[data-e2e="user-post-item"]',
    '[data-e2e="user-link"]',
)


def normalize_url(url: str) -> str:
    """WHAT: one canonical `https://www.tiktok.com/<path>` form for any
    TikTok reference. HOW: shared/text.py parse_normalized_url does the
    scheme/host parsing, then every tiktok-something host (m.tiktok.com,
    vm.tiktok.com, the regional variants) collapses to www.tiktok.com, so
    the same profile reached by two different hosts dedups to one row.
    LINKED TO: exposed as Scraper.normalize_url, which
    services/analysis_service.py calls before storing any URL."""
    p = parse_normalized_url(url)
    if p is None:
        return ""
    host = normalized_host(p)
    if "tiktok" in host:
        host = "www.tiktok.com"
    path = p.path.rstrip("/")
    return f"https://{host}{path}" if path else f"https://{host}"


def username_of(url: str) -> str:
    """WHAT: the account handle out of a normalized TikTok URL, or "".
    HOW: the first path segment, and ONLY when it starts with "@". A
    profile URL is tiktok.com/@handle; tiktok.com/video/..., /tag/...,
    /t/<code> (a short link), /embed, /legal, /business, /about and the rest
    are routes whose first segment is a name, not an account, and treating
    "t" or "video" as a username produced a confident row about a profile
    that does not exist. /@handle/video/123 yields "handle" (the author);
    the caller decides what to do with the rest of the path. LINKED TO:
    process() sets row.profile_id from this, and an empty result is what
    makes a URL an immediate ERROR there."""
    seg = [s for s in urlparse(normalize_url(url)).path.split("/") if s]
    if not seg or not seg[0].startswith("@"):
        return ""
    u = seg[0][1:]
    return u if _HANDLE_RE.match(u) else ""


class Scraper:
    """WHAT: one TikTok session (logged-in or anonymous), driven over a
    list of profile URLs. HOW: per profile, load the page once and read
    the hydration payload TikTok embeds in it, falling back to the
    rendered header only when that payload is absent -- the same
    ordered-fallback shape every platform engine in this package uses.

    LINKED TO: `analysis_path` in backend/platforms/registry.py names this
    class, and services/analysis_service.py constructs and drives it with
    the same (args, cookies, session_id) signature it uses for
    every platform."""

    normalize_url = staticmethod(normalize_url)

    def __init__(
        self, args, cookies: list[dict], session_id: str = "",
        anonymous: bool = False,
    ):
        """Builds either a cookie-backed TikTokSession or, in anonymous
        mode, nothing at all until start() borrows the shared persistent
        context. See the ANONYMOUS MODE note below for why losing the
        login costs only one field."""
        self.a = args
        self.evidence = args.evidence or None  # GridFS key prefix, not a path
        # ANONYMOUS MODE: no cookies, driven from the same persistent
        # browser profile discovery uses (see discovery_engine.
        # anonymous_context). Every field this scraper normally reads --
        # name, followers, following, avatar, name match -- comes out of
        # the profile's own hydration payload, which a logged-out browser
        # gets in full. The single thing a login buys is the exact
        # last-post date (the video grid renders empty when logged out),
        # and losing one field beats losing the platform because a cookie
        # expired.
        self.anonymous = anonymous
        self._anon_cm = None
        self._anon_ctx = None
        self.session = None if anonymous else TikTokSession(
            args, cookies, load_images=captures_screenshot(args), session_id=session_id,
        )

    @property
    def ctx(self):
        """The Playwright browser context, whichever mode is active. One
        accessor so nothing below has to know which."""
        return self._anon_ctx if self.anonymous else self.session.ctx

    async def start(self):
        """WHAT: opens the browser context. HOW: anonymous mode borrows
        the persistent profile discovery already uses (see
        discovery_engine.py anonymous_context), logged-in mode starts a
        TikTokSession with the supplied cookies. LINKED TO: called by
        services/analysis_service.py before the first profile."""
        if self.anonymous:
            from backend.platforms.tiktok.discovery_engine import anonymous_context

            self._anon_cm = anonymous_context()
            self._anon_ctx = await self._anon_cm.__aenter__()
            return
        await self.session.start()

    async def stop(self):
        """Closes whichever context start() opened. The anonymous branch
        exits the context manager itself, since there is no session object
        owning it."""
        if self.anonymous:
            if self._anon_cm is not None:
                try:
                    await self._anon_cm.__aexit__(None, None, None)
                finally:
                    self._anon_cm = self._anon_ctx = None
            return
        await self.session.stop()

    async def pause(self, mult: float = 1.0):
        """Between-profile pacing. Anonymous mode has no session rhythm to
        follow, so it uses a flat delay; a logged-in session defers to
        TikTokSession.pause, which paces against its own recent
        activity."""
        if self.anonymous:
            import asyncio as _asyncio

            await _asyncio.sleep(max(0.1, getattr(self.a, "delay", 2.0) * mult))
            return
        await self.session.pause(mult)

    async def check_session(self) -> bool:
        """WHAT: is this session still usable? HOW: anonymous mode is
        always True -- there are no credentials to validate, and more
        importantly nothing to QUARANTINE, so an anonymous run can never
        burn a session it does not have. LINKED TO:
        sessions/manager.py::verify_session_item, which treats False as
        conclusive evidence the stored cookies are dead."""
        if self.anonymous:
            return True
        return await self.session.check_session()

    # ─────────────────────────── DOM fallback ─────────────────────────── #
    #
    # TikTok's profile header renders, in a fixed order, the account's
    # nickname, then three counts labelled by the word that follows each
    # one ("Following" / "Followers" / "Likes"), reading by label text
    # rather than position or a CSS class survives a layout change the way
    # Instagram's own fixed-order header parse does.
    JS_HEADER = """
    (username) => {
      const lines = (document.body.innerText || "").split("\\n")
        .map(s => s.trim()).filter(Boolean);
      let following = "", followers = "", likes = "";
      for (let i = 1; i < lines.length; i++) {
        if (/^following$/i.test(lines[i])) following = lines[i - 1];
        else if (/^followers$/i.test(lines[i])) followers = lines[i - 1];
        else if (/^likes$/i.test(lines[i])) likes = lines[i - 1];
      }
      // The account's own title line. `user-title` is the @handle and
      // `user-subtitle` the display name; a bare h1/h2 can just as well be
      // an error heading ("Couldn't find this account"), a "Log in" prompt
      // or the viewer's own name, so it is only a last resort and never when
      // it is the handle itself or reads like a page message.
      const clean = (el) => el ? (el.textContent || '').trim() : '';
      const title = clean(document.querySelector('[data-e2e="user-title"]'));
      let nickname = clean(document.querySelector('[data-e2e="user-subtitle"]'));
      if (!nickname) {
        const h = document.querySelector('h1, h2');
        const t = clean(h);
        const handle = (username || '').toLowerCase();
        if (t && t.replace(/^@/, '').toLowerCase() !== handle
            && !/error|went wrong|couldn.t|can.t be found|not found|log in|sign up|private|something/i.test(t)) {
          nickname = t;
        }
      }
      // The PROFILE'S avatar only: the header's own avatar element, or an
      // image whose alt names this account. Never `img[class*=avatar]` --
      // that also matches the signed-in viewer's sidebar avatar, which would
      // be hashed and stored as this account's picture.
      let img = document.querySelector('[data-e2e="user-avatar"] img, [data-e2e="user-avatar"]');
      if (img && img.tagName !== 'IMG') img = img.querySelector('img');
      if (!img) {
        const want = (username || '').toLowerCase();
        img = Array.from(document.querySelectorAll('img')).find(
          (i) => want && (i.getAttribute('alt') || '').toLowerCase().includes(want)) || null;
      }
      const verified = !!document.querySelector('[data-e2e="user-verified"], svg[aria-label="Verified"]');
      const bodyText = document.body.innerText || "";
      return {
        following, followers, likes, nickname, title,
        avatar: img ? (img.getAttribute('src') || '') : '',
        verified,
        isPrivate: /this account is private/i.test(bodyText),
        notFound: /couldn.t find this account|this account can.t be found/i.test(bodyText),
      };
    }
    """

    async def read_dom(self, page, username: str) -> dict:
        """WHAT: runs JS_HEADER against the live page -> the header dict.
        HOW: any failure returns {} rather than raising, because this is
        already the LAST tier of the fallback chain -- an exception here
        would discard the fields the tiers above may have set. LINKED TO:
        called by process() only when the hydration payload is missing;
        its result feeds fill_from_dom()."""
        try:
            return await page.evaluate(self.JS_HEADER, username) or {}
        except Exception:
            return {}

    # ───────────────────────────── per URL ────────────────────────────── #

    async def process(
        self, raw_url: str, target: str, feed: str, known: Optional[dict] = None,
    ) -> Row:
        """WHAT: one profile URL -> a scored Row. HOW: a single page load,
        then an ordered fallback -- TikTok own hydration payload first (it
        arrives with the page, so there is no XHR to race), the rendered
        header second.

        `known` (whatever discovery already read for this URL, see
        analysis/runner.py's `seed_by_url`) is accepted for interface
        consistency with the other platforms, but there's nothing here it
        lets this profile skip: the single page visit below is mandatory
        regardless (status/screenshot/last-post all need it), TikTok
        exposes no creation date at all, and there's no separate
        location fetch on this platform the way Twitter/Instagram/
        Facebook have. Runner.py's own `_populate` fallback still covers
        `known` for whatever this visit itself comes back blank on.

        When the payload is missing, the page text is classified BEFORE
        falling back, because the four reasons it can be missing need four
        different outcomes: a CAPTCHA (CHECKPOINT -- stop the run, the
        session is burning), rejected cookies (LOGIN_REQUIRED), a deleted
        account (GONE -- terminal, no retry), or a slow render (PARTIAL --
        retryable, and the one case where another visit genuinely helps).
        Collapsing those into one ERROR is what makes a dead account and a
        dying session look identical in the results grid.

        Last-post is then filled by the two-tier scheme in the module
        docstring. LINKED TO: fill()/fill_from_dom() below do the mapping;
        read_hydration, profile_from and newest_post_via_search all come
        from discovery_engine.py."""
        url = normalize_url(raw_url)
        row = Row(url=url, target=target, original_feed=feed)
        row.profile_id = username_of(url)

        if not row.profile_id:
            row.status = "ERROR"
            row.note(
                "not a profile URL -- expected tiktok.com/@handle. Short links "
                "(vm.tiktok.com, tiktok.com/t/...) and video links with no @handle "
                "cannot be resolved without opening them; paste the account's own URL"
            )
            return row

        # ALWAYS THE PROFILE PAGE, NEVER THE PASTED PATH. `/@foo/video/123` is
        # a post and `/@foo/anything` is not the profile either; visiting it
        # and reading it as a profile would give a confident row about the
        # wrong page. The handle came out of the URL, so the profile is built
        # from the handle.
        profile_url = f"https://www.tiktok.com/@{row.profile_id}"
        if url.rstrip("/").lower() != profile_url.lower():
            row.canonical_url = profile_url
            row.note("pasted link was not a bare profile URL -- the account's profile page was analysed")

        page = await self.ctx.new_page()
        try:
            # THE STATUS OF THE PAGE LOAD, SEEN PASSIVELY. Chromium collapses
            # an HTTP error carrying an empty body into a bare exception with
            # no status on it, so a refusal and a dead network read alike. The
            # status is taken from the response events the load already
            # produces -- never from a second request, which is what this used
            # to do (`ctx.request.get`, a non-browser fetch with no page
            # behind it and no cookies of the page's own, on exactly the URL
            # that had just been refused).
            nav: dict = {"status": None}

            def _on_response(resp) -> None:
                try:
                    req = resp.request
                    if req.is_navigation_request() and req.frame == page.main_frame:
                        s = _int_status(resp)
                        if s is not None:
                            nav["status"] = s
                except Exception:
                    pass

            try:
                page.on("response", _on_response)
            except Exception:
                pass

            try:
                resp = await page.goto(
                    profile_url, wait_until="domcontentloaded", timeout=self.a.timeout * 1000)
                if (s := _int_status(resp)) is not None:
                    nav["status"] = s
            except Exception as nav_err:
                # NAME THE CAUSE. Measured live (2026-09-08, logged out):
                # TikTok answers the homepage 200 and /search/user 200, and
                # returns 403 with a ZERO-BYTE body for every /@handle -- its
                # own @tiktok account included. That is a policy on the
                # endpoint, not a problem with the profile.
                status = nav["status"]
                # REFUSED IS NOT THE END. The profile page is one source, not
                # the only one: TikTok's user search answers 200 while
                # /@handle answers 403, and its cards carry most of what this
                # scraper reads anyway. Falling back to it turns a row that
                # reported nothing into one that reports nearly everything,
                # with the two genuinely page-only fields left honestly blank.
                if await self._recover_via_search(row, page, status):
                    return row
                self._refused(row, status, nav_err)
                return row
            http_status = nav["status"]

            # give the hydration script (and, on a suspicious-traffic
            # response, whatever TikTok replaces it with) a moment to settle
            await page.wait_for_timeout(min(2500, int(self.a.settle * 1000)))

            # A blocked region serves the same notice for every profile, so
            # without this the row reads PARTIAL "profile payload not seen"
            # -- a retryable status that will keep re-spending attempts on
            # something no retry can fix. The REDIRECT is the proof (a URL
            # cannot be written by a bio); the notice text is only trusted
            # below, once no profile was read, and only outside the account's
            # own content.
            if geoblock_redirect(page.url):
                self._geoblocked(row, page.url)
                return row

            # What TikTok itself is showing, with the account's own bio, title
            # and video grid taken out -- see _PROFILE_CONTENT_SELECTORS.
            chrome = await self._chrome_text(page)

            hydration = await read_hydration(page)
            user = profile_from(hydration, row.profile_id) if hydration else None

            challenged = False
            if user:
                self.fill(row, user)
                # A CAPTCHA / slide-verify OVERLAY can sit on top of a profile
                # whose payload still loaded. The fields are real, but the
                # session is being challenged and the page is not what an
                # analyst would see, so the row is PARTIAL with the reason,
                # never a clean OK, and no further request is spent on it.
                challenged = bool(RE_CHECKPOINT.search(chrome)) or is_wall_url(
                    page.url, ("captcha",))
                if challenged:
                    row.note(
                        "a verification challenge was on screen over this profile -- "
                        "the fields were read from the page payload but the visit may be incomplete"
                    )
                if not row.last_post_iso:
                    iso = newest_post_iso(hydration, row.profile_id)
                    if iso:
                        row.last_post_iso = iso
                        row.posts_seen = "yes"
                        row.mark("last_post", "hydration")
            else:
                if RE_CHECKPOINT.search(chrome) or is_wall_url(page.url, ("captcha",)):
                    if self.anonymous:
                        # NO COOKIES, NOTHING TO CHECKPOINT. Saying "session
                        # checkpointed" about a browser with no session sends
                        # an analyst to a login that does not exist.
                        row.status = "ERROR"
                        row.note(
                            "TikTok showed a human-verification page to this logged-out "
                            "browser, so the profile was not read -- try again later, or "
                            "add a TikTok session under Sessions"
                        )
                    else:
                        row.status = "CHECKPOINT"
                        row.note("session checkpointed")
                    return row
                if is_wall_url(page.url, ("login",)) or RE_LOGIN.search(chrome):
                    if self.anonymous:
                        row.status = "ERROR"
                        row.note(
                            "TikTok asked this logged-out browser to sign in before showing "
                            "the profile, so it was not read -- add a TikTok session under Sessions"
                        )
                    else:
                        row.status = "LOGIN_REQUIRED"
                        row.note("cookies rejected/expired")
                    return row
                if RE_GONE.search(chrome):
                    row.status = "GONE"
                    row.note("removed or unavailable -- may already be down")
                    return row
                if RE_GEOBLOCK.search(chrome):
                    self._geoblocked(row, page.url)
                    return row
                if http_status is not None and http_status >= 400:
                    # The page answered with an error and carried no profile.
                    if await self._recover_via_search(row, page, http_status):
                        return row
                    self._refused(row, http_status, None)
                    return row

                dom = await self.read_dom(page, row.profile_id)
                followers_n, _ = parse_count(str(dom.get("followers") or ""))
                title_ok = (str(dom.get("title") or "").strip().lstrip("@").lower()
                            == row.profile_id.lower())
                if dom.get("notFound") and followers_n is None and not title_ok:
                    row.status = "GONE"
                    row.note("removed or unavailable -- may already be down")
                    return row
                # PROOF THE PROFILE LOADED: a followers count, or the page's
                # own title line naming THIS handle. A heading or a nickname
                # alone proves nothing -- an error page has headings too.
                if followers_n is not None or title_ok:
                    self.fill_from_dom(row, dom)
                else:
                    row.status = "PARTIAL"
                    row.note("profile payload not seen")
                    # Capture anyway, as Instagram and X do: the row whose
                    # fields could not be read is the one an analyst most
                    # needs a picture of. Login walls and not-found pages
                    # have already returned above, so this is the profile.
                    await self.screenshot(page, row)
                    return row

            if not row.last_post_iso and not challenged:
                # the free, no-extra-navigation attempt above came up empty
                # (it always does today, see module docstring), fall
                # back to the CAPTCHA-free search-based lookup, one extra
                # page visit. Best-effort: any failure here must not fail
                # the whole profile visit. Skipped while a challenge is on
                # screen: that session is already being watched.
                try:
                    iso = await newest_post_via_search(self.ctx, row.profile_id)
                except Exception:
                    iso = ""
                if iso:
                    row.last_post_iso = iso
                    row.posts_seen = "yes"
                    # A LOWER BOUND, NOT THE LAST POST. Search surfaces the
                    # newest video it happens to rank, not the newest video
                    # there is, so a newer post can exist. The runner reads
                    # this source and leaves "active" unknown unless the date
                    # alone already proves it.
                    row.mark("last_post", LAST_POST_LOWER_BOUND)
                    row.note(
                        "last post date came from search only -- a lower bound, "
                        "newer posts may exist"
                    )

            await self.screenshot(page, row)
            # NOT `or row.profile_id` -- profile_id is derived from the URL
            # at the very top of this method and is unconditionally
            # non-empty by this point (a blank one already returned ERROR
            # earlier), so `or row.profile_id` made this condition always
            # True regardless of whether anything was actually read. A weak
            # read missing the name -- the one field this whole tool is about
            # -- must not be stamped "OK".
            row.status = "OK" if row.profile_name and not challenged else "PARTIAL"
            return row
        finally:
            try:
                await page.close()
            except Exception:
                pass

    @staticmethod
    async def _chrome_text(page) -> str:
        """The page's VISIBLE text with the account's own bio, title and video
        grid removed -- what TikTok itself is saying, as opposed to what the
        profile says. Falls back to the whole body when the page cannot be
        queried, which is exactly how loose the check was before: a failure
        here can only make it looser, never stricter. (shared/walls.py's
        chrome_text does the same job on textContent, which also reads the
        page's <script> payloads and so contains the word "captcha" on every
        TikTok page; this one works on the rendered text.)"""
        try:
            text = await page.evaluate(
                """(sels) => {
                    let t = document.body ? (document.body.innerText || '') : '';
                    for (const s of sels) {
                        document.querySelectorAll(s).forEach((n) => {
                            const x = (n.innerText || '').trim();
                            if (x) t = t.split(x).join(' ');
                        });
                    }
                    return t;
                }""",
                list(_PROFILE_CONTENT_SELECTORS),
            )
            if isinstance(text, str):
                return text
        except Exception:
            pass
        try:
            return await page.inner_text("body")
        except Exception:
            return ""

    @staticmethod
    def _geoblocked(row: Row, page_url: str) -> None:
        row.status = "ERROR"
        row.note(
            "TikTok is blocked for this IP (redirected to "
            f"{page_url}) -- route this platform through a proxy"
        )

    def _refused(self, row: Row, status: Optional[int], nav_err: Optional[BaseException]) -> None:
        """An ERROR row for a profile page TikTok would not serve.

        THE WORDING IS NOT COSMETIC. The runner reads every ERROR row's note
        through resilience.classify_failure, and a bare "403" in it reads as
        "this session's cookies are dead": one not-found handle was marking a
        good session failed and stopping the worker with the rest of the batch
        still queued. A per-URL refusal says nothing about the session, so the
        403 is spelled out in words the classifier does not match.
        """
        row.status = "ERROR"
        if status == 403:
            row.note(
                "refused (access denied) -- TikTok is not serving this profile page to "
                "this browser, and the account was not found in user search either. "
                + ("Add a TikTok session under Sessions."
                   if self.anonymous else
                   "This is about this one URL and is not treated as a dead session.")
            )
        elif status and status >= 400:
            row.note(f"navigation failed -- TikTok answered HTTP {status}")
        elif nav_err is not None:
            row.note(f"navigation failed -- {type(nav_err).__name__}")
        else:
            row.note("navigation failed")

    @staticmethod
    def fill(row: Row, u: TikTokUser) -> None:
        """WHAT: copies a parsed TikTokUser onto the Row. HOW: every value
        is marked `hydration` (see shared/models/row.py::mark) so
        shared/completeness.py can later tell a field that was READ from
        one that was never reached. Two deliberate choices: the display
        NAME is preferred over the handle because that is what an
        impersonator copies, and a zero video_count is recorded as
        `posts_seen = "no"` -- genuinely postless, a real finding -- rather
        than left blank, which would read as a failed extraction. The
        closing note records that TikTok exposes no creation date at all,
        so the blank column is explained rather than mysterious. LINKED
        TO: called by process(); TikTokUser is parsed in
        discovery_engine.py::profile_from."""
        row.profile_id = u.username
        # the display name is what an impersonator copies; fall back to handle
        row.profile_name = u.nickname or u.username
        row.mark("name", "hydration")
        row.name_score = name_score(row.profile_name, row.target)

        if u.follower_count is not None:
            row.followers = u.follower_count
            row.followers_exact = "yes"
            row.mark("followers", "hydration")
        if u.following_count is not None:
            row.friends = u.following_count
            row.mark("friends", "hydration")
        if u.avatar:
            row.profile_pic_url = u.avatar
            row.has_custom_pic = u.has_custom_pic
            row.mark("logo", "hydration")
        if u.video_count is not None:
            row.posts_seen = "yes" if u.video_count > 0 else "no"
            row.mark("posts", "hydration")
        if u.bio:
            # INTO THE FIELD, not only a note. Row.bio is what the export,
            # the incident row and the analysis table all read; TikTok was
            # the only platform that never set it (Twitter, Instagram and
            # Telegram all assign row.bio directly), so a TikTok row's bio
            # column was permanently blank while the text sat in a note that
            # nothing renders as a bio. The note is kept as well -- it is
            # what an analyst reading this row's history already sees, and
            # dropping it would quietly remove context from existing rows.
            row.bio = u.bio
            row.mark("bio", "hydration")
            row.note(f"bio: {u.bio[:120]}")
        if u.verified:
            row.verified = True
            row.note("verified account")
        if u.private:
            row.note("private account -- some fields may be limited")
        row.note("creation date not exposed by TikTok")

    async def _recover_via_search(self, row: Row, page, status: Optional[int]) -> bool:
        """Rebuild this profile from TikTok's USER SEARCH when its own page
        is refused. True when something usable was recovered.

        WHY THIS WORKS AT ALL. Measured live 2026-09-08, logged out:
        /@handle answers 403 with a zero-byte body -- TikTok's own @tiktok
        account included -- while /search/user?q= answers 200 with 379KB.
        The refusal is a policy on the profile endpoint, not a block on the
        client, so the account's data is still reachable by another door.

        A search card carries name, bio, followers, following, avatar,
        verified, private and the numeric id: everything `fill()` maps,
        which is why this reuses that mapper rather than a second one that
        could drift from it. The two fields it cannot carry are location
        (TikTok publishes none anywhere) and the last-post date -- and that
        one is recovered separately by `newest_post_via_search`, the same
        CAPTCHA-free route the engine already trusts.

        PARTIAL, NOT OK. The row is real but assembled from a listing rather
        than read at the source, and `analysis_status` is what decides
        whether a profile is re-queued for another look (see
        profile_repository.RETRYABLE_ANALYSIS_STATUSES). Marking this OK
        would freeze a second-hand reading as final; PARTIAL keeps it in the
        queue for when a session exists.
        """
        handle = (row.profile_id or "").strip()
        if not handle:
            return False
        try:
            found = (await search_users_in(self.ctx, [handle])).get(handle) or []
        except Exception as e:
            from backend.shared.logging import get_logger as _gl
            _gl("tiktok").warning(
                f"tiktok/{handle}: search fallback failed -- {type(e).__name__}: {e}")
            return False

        # EXACT handle only. Search ranks by relevance, so the first card is
        # frequently a different, more popular account wearing a similar
        # name -- writing that one's followers onto this row would be worse
        # than reporting nothing.
        match = next((u for u in found if (u.username or "").lower() == handle.lower()), None)
        if match is None:
            return False

        self.fill(row, match)
        row.status = "PARTIAL"
        for field in ("name", "followers", "friends", "logo", "bio"):
            row.mark(field, "search-card")

        if not row.last_post_iso:
            try:
                if iso := await newest_post_via_search(self.ctx, handle):
                    row.last_post_iso = iso
                    row.posts_seen = "yes"
                    row.mark("last_post", LAST_POST_LOWER_BOUND)
            except Exception:
                pass  # best effort; the row is already worth keeping

        row.note(
            f"profile page refused (HTTP {status or '?'}) -- rebuilt from TikTok user search. "
            "Location is never published by TikTok"
            + ("" if row.last_post_iso else "; last-post date could not be recovered")
            + ". Add a TikTok session for a full read."
        )
        from backend.shared.logging import get_logger as _gl
        _gl("tiktok").info(
            f"tiktok/{handle}: profile refused, recovered {row.profile_name!r} from search")
        return True

    @staticmethod
    def fill_from_dom(row: Row, dom: dict) -> None:
        """WHAT: the same fields as fill(), recovered from the rendered
        header when the hydration payload was absent. HOW: marked
        `dom-header` rather than `hydration`, so the stored row records
        which tier actually produced each value and a silent slide from
        payload to DOM across a whole run is visible instead of invisible.
        Counts come back as display strings here ("1.2M"), so
        `followers_exact` reports "no" when shared/text.py::parse_count
        had to expand an abbreviation -- an approximate number labelled as
        approximate, never presented as exact.

        Last-post is NOT available from this path: decoding it needs a
        video id (see newest_post_iso) and the header carries none, so it
        stays blank rather than guessed. LINKED TO: called by process()
        as the last tier; read_dom() supplies the dict."""
        nickname = (dom.get("nickname") or "").strip()
        if nickname:
            row.profile_name = nickname
            row.mark("name", "dom-header")
            row.name_score = name_score(nickname, row.target)

        followers, exact = parse_count(dom.get("followers", ""))
        if followers is not None:
            row.followers = followers
            row.followers_exact = "yes" if exact else "no"
            row.mark("followers", "dom-header")

        following, _ = parse_count(dom.get("following", ""))
        if following is not None:
            row.friends = following
            row.mark("friends", "dom-header")

        avatar = dom.get("avatar") or ""
        if avatar:
            row.profile_pic_url = avatar
            # THE PICTURE IS RECORDED; WHETHER IT IS A REAL ONE IS NOT
            # CLAIMED. TikTok's own "no custom avatar" signal is the
            # hydration payload OMITTING the field (see TikTokUser.
            # has_custom_pic in discovery_engine.py -- presence is the
            # signal, which is why this platform needs no stock-URL regex).
            # That signal does not survive into the DOM: the rendered header
            # always carries an <img>, default avatar included, so a src
            # being present here distinguishes nothing.
            #
            # This used to set has_custom_pic = True regardless, which
            # asserted a real profile picture from evidence that cannot tell
            # -- on exactly the tier that runs when the reliable source was
            # unavailable. Leaving it unset means Row.logo_yes reports "No"
            # (it answers Yes only to a confirmed True), which is the honest
            # reading: we did not establish one. Same rule this method
            # already applies to last-post two fields above, which "stays
            # blank rather than guessed".
            row.mark("logo", "dom-header")

        if dom.get("verified"):
            row.verified = True
            row.note("verified account")
        if dom.get("isPrivate"):
            row.note("private account -- some fields may be limited")
        row.note("creation date not exposed by TikTok")

    async def screenshot(self, page, row: Row) -> None:
        """WHAT: captures the evidence PNG into GridFS. HOW: waits for
        visible content first so the capture is not a half-painted page,
        then stores under a DETERMINISTIC key derived from the handle --
        re-analysing a profile must overwrite its own previous capture
        rather than accumulate one per run. Best-effort throughout: a
        failed screenshot must never fail the profile visit, since the
        scraped fields are the actual finding. LINKED TO:
        database/repositories/evidence_repository.py owns the store;
        row.screenshot holds the key, not a filesystem path. The shared
        mechanics live in shared/evidence.py."""
        # A video tile is TikTok's "the grid has painted" signal, the same
        # role the post/tweet selectors play on the other three. This was
        # the one platform shooting with no content wait at all, so its
        # captures could show the header above an empty, loading grid.
        await evidence.capture(
            self.session, page, row, evidence=self.evidence,
            ephemeral=getattr(self.a, 'ephemeral_screenshot', False),
            content_selector='a[href*="/video/"], a[href*="/photo/"]', platform="tiktok")

    # ─────────────────────────── orchestration ────────────────────────── #

    async def one(self, u: str, tgt: str, feed: str, known: Optional[dict] = None) -> Row:
        """WHAT: process() that never raises -- always a Row. HOW: any
        exception becomes an ERROR row carrying the exception type and
        message, so one unreachable profile cannot end a job and the
        reason survives into the results grid. LINKED TO: called by run(),
        and directly by services/analysis_service.py on the API path."""
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
        """WHAT: one progress line per profile. HOW: prints the fields an
        operator needs to spot a silent extraction failure early -- a
        column reading "-" on every line means that field stopped being
        read. LINKED TO: called by run() after each row."""
        from backend.shared.logging import get_logger as _gl
        _gl("platforms.tiktok.analysis").info(
            f"[{i}/{total}] {u} | {row.status} name={row.profile_name[:22]} "
            f"followers={row.followers if row.followers is not None else '-'} "
            f"active={row.active_yes or '-'} risk={row.risk} {row.priority}"
        )

    async def run(self, jobs: list[tuple[str, str, str]]) -> list[Row]:
        """WHAT: drives a whole batch of (url, target, feed) jobs. HOW:
        sequentially, pausing between profiles, and aborting on the first
        CHECKPOINT unless `keep_going` was set -- a CAPTCHA means TikTok
        is already challenging this session, and pushing on is what turns
        a challenge into a dead account. Rows gathered before the abort
        are still returned. LINKED TO: the standalone entry point; the API
        path drives one() through services/analysis_service.py, which does
        its own session-burn handling on CHECKPOINT."""
        rows: list[Row] = []
        for i, (u, tgt, feed) in enumerate(jobs, 1):
            row = await self.one(u, tgt, feed)
            rows.append(row)
            self.report(i, len(jobs), u, row)
            if row.status == "CHECKPOINT" and not getattr(self.a, "keep_going", False):
                from backend.shared.logging import get_logger as _gl
                _gl("platforms.tiktok.analysis").warning(
                    "CHECKPOINT -- aborting to protect the session."
                )
                break
            if i < len(jobs):
                await self.pause()
        return rows
