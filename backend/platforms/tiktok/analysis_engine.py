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

from backend.shared.models.row import Row
from backend.platforms.scan_options import captures_screenshot
from backend.shared.text import name_score, normalized_host, parse_count, parse_normalized_url
from backend.platforms.tiktok.discovery_engine import (RE_CHECKPOINT, RE_GONE,
                                                        RE_LOGIN, TikTokSession,
                                                        TikTokUser, geoblocked,
                                                        newest_post_iso,
                                                        newest_post_via_search,
                                                        profile_from, read_hydration)

BAD_SEGMENTS = {
    "video", "live", "tag", "music", "discover", "explore", "upload",
    "messages", "following", "notification", "search", "foryou", "login",
}


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
    HOW: the first path segment with its leading @ stripped, rejected if
    it is one of BAD_SEGMENTS -- tiktok.com/video/... and
    tiktok.com/tag/... are real URLs whose first segment is a route name,
    not an account, and treating "video" as a username would produce a
    confident row about a profile that does not exist. LINKED TO:
    process() sets row.profile_id from this, and an empty result is what
    makes a URL an immediate ERROR there."""
    seg = [s for s in urlparse(normalize_url(url)).path.split("/") if s]
    if not seg:
        return ""
    u = seg[0].lstrip("@")
    return "" if u.lower() in BAD_SEGMENTS else u


class Scraper:
    """WHAT: one TikTok session (logged-in or anonymous), driven over a
    list of profile URLs. HOW: per profile, load the page once and read
    the hydration payload TikTok embeds in it, falling back to the
    rendered header only when that payload is absent -- the same
    ordered-fallback shape every platform engine in this package uses.

    LINKED TO: `analysis_path` in backend/platforms/registry.py names this
    class, and services/analysis_service.py constructs and drives it with
    the same (args, cookies, session_id, proxy) signature it uses for
    every platform."""

    normalize_url = staticmethod(normalize_url)

    def __init__(
        self, args, cookies: list[dict], session_id: str = "", proxy: Optional[dict] = None,
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
            args, cookies, load_images=captures_screenshot(args), session_id=session_id, proxy=proxy,
        )
        self._proxy = proxy

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

            self._anon_cm = anonymous_context(self._proxy)
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
      const h = document.querySelector('h1, h2');
      const nickname = h ? (h.textContent || '').trim() : '';
      const img = document.querySelector(
        `img[alt*="${username}"], header img, img[class*="avatar" i]`);
      const verified = !!document.querySelector('[data-e2e="user-verified"], svg[aria-label="Verified"]');
      const bodyText = document.body.innerText || "";
      return {
        following, followers, likes, nickname,
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
            row.note("no @username in the URL -- private/video links cannot be resolved")
            return row

        page = await self.ctx.new_page()
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.a.timeout * 1000)
            except Exception:
                row.status = "ERROR"
                row.note("navigation failed")
                return row

            # give the hydration script (and, on a suspicious-traffic
            # response, whatever TikTok replaces it with) a moment to settle
            await page.wait_for_timeout(min(2500, int(self.a.settle * 1000)))

            body = ""
            try:
                body = await page.inner_text("body")
            except Exception:
                pass

            # A blocked region serves the same notice for every profile, so
            # without this the row reads PARTIAL "profile payload not seen"
            # -- a retryable status that will keep re-spending attempts on
            # something no retry can fix. See RE_GEOBLOCK in
            # discovery_engine.py.
            if geoblocked(page.url, body):
                row.status = "ERROR"
                row.note(
                    "TikTok is blocked for this IP (redirected to "
                    f"{page.url}) -- route this platform through a proxy"
                )
                return row

            hydration = await read_hydration(page)
            user = profile_from(hydration, row.profile_id) if hydration else None

            if user:
                self.fill(row, user)
                if not row.last_post_iso:
                    iso = newest_post_iso(hydration, row.profile_id)
                    if iso:
                        row.last_post_iso = iso
                        row.posts_seen = "yes"
                        row.mark("last_post", "hydration")
            else:
                if RE_CHECKPOINT.search(body) or "/captcha" in page.url:
                    row.status = "CHECKPOINT"
                    row.note("session checkpointed")
                    return row
                if "/login" in page.url or RE_LOGIN.search(body):
                    row.status = "LOGIN_REQUIRED"
                    row.note("cookies rejected/expired")
                    return row
                if RE_GONE.search(body):
                    row.status = "GONE"
                    row.note("removed or unavailable -- may already be down")
                    return row

                dom = await self.read_dom(page, row.profile_id)
                if dom.get("notFound"):
                    row.status = "GONE"
                    row.note("removed or unavailable -- may already be down")
                    return row
                if dom.get("followers") or dom.get("nickname"):
                    self.fill_from_dom(row, dom)
                else:
                    row.status = "PARTIAL"
                    row.note("profile payload not seen")
                    return row

            if not row.last_post_iso:
                # the free, no-extra-navigation attempt above came up empty
                # (it always does today, see module docstring), fall
                # back to the CAPTCHA-free search-based lookup, one extra
                # page visit. Best-effort: any failure here must not fail
                # the whole profile visit.
                try:
                    iso = await newest_post_via_search(self.ctx, row.profile_id)
                except Exception:
                    iso = ""
                if iso:
                    row.last_post_iso = iso
                    row.posts_seen = "yes"
                    row.mark("last_post", "search")

            await self.screenshot(page, row)
            # NOT `or row.profile_id` -- profile_id is derived from the URL
            # at the very top of this method and is unconditionally
            # non-empty by this point (a blank one already returned ERROR
            # earlier), so `or row.profile_id` made this condition always
            # True regardless of whether anything was actually read.
            # Concrete case this let through as "OK": the DOM fallback tier
            # runs when `dom.get("followers") or dom.get("nickname")` is
            # truthy (line 341) -- if only `followers` came back and
            # `nickname` didn't, `fill_from_dom` leaves `row.profile_name`
            # blank (it's only ever set from `dom.get("nickname")`), a
            # genuinely weak read missing the one field this whole tool is
            # about (the name to compare against the keyword) -- and this
            # line still stamped it "OK", hiding that from the analyst.
            row.status = "OK" if row.profile_name else "PARTIAL"
            return row
        finally:
            try:
                await page.close()
            except Exception:
                pass

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
            row.note(f"bio: {u.bio[:120]}")
        if u.verified:
            row.verified = True
            row.note("verified account")
        if u.private:
            row.note("private account -- some fields may be limited")
        row.note("creation date not exposed by TikTok")

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
            row.has_custom_pic = True
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
        row.screenshot holds the key, not a filesystem path."""
        if not self.evidence and not getattr(self.a, 'ephemeral_screenshot', False):
            return
        # DETERMINISTIC key, no timestamp, re-analysing a profile must
        # overwrite its own previous capture, not add another one.
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", row.profile_id or "entity")[:60]
        key = f"{self.evidence}/{stem}.png" if self.evidence else ""
        try:
            if self.session is not None:
                await self.session.wait_for_visible_content(page)
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
