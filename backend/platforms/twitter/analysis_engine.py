"""Twitter analysis engine: validation and impersonation signal extraction
-- profile URL -> scored Row.

Session/login-checking and payload parsing (TwitterUser + friends) live in
discovery_engine.py (imported below) since discovery produces/needs them
first; this file owns everything specific to a validated profile visit: URL
normalization for the analysis entry point, and the browser-drive loop
(Scraper).

One request does it, usually: visiting a profile fires UserByScreenName, whose
`legacy` object holds every field the report wants as typed values, an
integer follower count rather than a rendered "154M", and a real join date.
So most fields never need the DOM, and unlike Facebook the Created Date
column is filled.

The one field that does fall back to the DOM is last-post date. It comes
from a SEPARATE query (UserTweets) than the profile one, and that query can
simply not have landed in the single ~1.2s window this waits for it, a
timing miss, not a parsing failure, confirmed live: two accounts stored with
no last-post date reproduced fine on a fresh visit. When that happens,
dom_last_post() reads the same information off the already-rendered
timeline instead of waiting longer or re-requesting.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urlparse

from backend.shared.models.row import Row
from backend.platforms.scan_options import captures_screenshot
from backend.shared.text import name_score, normalized_host, parse_normalized_url
from backend.platforms.twitter.discovery_engine import (ABOUT_QUERY,
                                                         RE_CHECKPOINT,
                                                         RE_GONE, RE_LOGIN,
                                                         TWEETS_QUERIES,
                                                         USER_QUERIES,
                                                         AboutAccount,
                                                         TwitterSession,
                                                         TwitterUser,
                                                         about_account_from,
                                                         iter_users,
                                                         latest_post,
                                                         parse_lines)

BAD_SEGMENTS = {
    "home",
    "search",
    "explore",
    "notifications",
    "messages",
    "i",
    "settings",
    "compose",
    "intent",
}


def normalize_url(url: str) -> str:
    """WHAT: one canonical `https://x.com/<handle>` form for any Twitter/X
    URL variant. HOW: delegates host/path parsing to
    shared/text.py::parse_normalized_url, folds both twitter.com and x.com
    onto x.com (one login spans both hosts). LINKED TO: the one
    normalizer both this file and discovery_engine.py use for every X URL
    they touch."""
    p = parse_normalized_url(url)
    if p is None:
        return ""
    host = normalized_host(p)
    if "twitter.com" in host or "x.com" in host:
        host = "x.com"
    return f"https://{host}{p.path.rstrip('/')}"


def handle_of(url: str) -> str:
    """WHAT: the account's @handle, out of a normalized profile URL. HOW:
    the first non-BAD_SEGMENTS path segment (rejecting reserved routes
    like /home, /search, /i/... that are not profile URLs at all).
    LINKED TO: Scraper.process()'s row.profile_id."""
    seg = [s for s in urlparse(normalize_url(url)).path.split("/") if s]
    if not seg:
        return ""
    h = seg[0].lstrip("@")
    return "" if h.lower() in BAD_SEGMENTS else h


# DOM last-post fallback
# latest_post() (discovery_engine.py) reads the UserTweets GraphQL response,
# and deliberately excludes reposts and pinned tweets, counting either
# would make a dormant account look active. A DOM fallback has to preserve
# that same filtering, not just grab the newest <time> on the page: verified
# live on a real account, the single newest tweet CELL on screen was a
# repost, one day newer than that account's actual last original post.
# `[data-testid="socialContext"]` is the exact element X renders that
# repost badge in ("Adani Group reposted"), excluding any cell that has
# one reproduces latest_post()'s scoping.
#
# No live example of a currently-pinned tweet was available to confirm its
# exact socialContext text, so "pinned" is matched defensively (case
# -insensitive substring) alongside the confirmed "repost", an unmatched
# pinned tweet would only make this fallback occasionally too generous by
# one tweet, never wrong in the direction that hides real inactivity.
JS_TWEET_TIMES = """
() => {
  const out = [];
  for (const cell of document.querySelectorAll('[data-testid="tweet"]')) {
    const time = cell.querySelector('time[datetime]');
    if (!time) continue;
    const ctx = cell.querySelector('[data-testid="socialContext"]');
    const label = ctx ? (ctx.textContent || '').toLowerCase() : '';
    out.push({dt: time.getAttribute('datetime'), repostOrPinned: /repost|retweet|pinned/.test(label)});
  }
  return out;
}
"""


# How long to let a profile timeline paint before giving up on reading a
# date off it. Generous on purpose: the cost of waiting is a few seconds
# per profile, the cost of reading too early is a false "this account has
# no posts", which feeds the activity classification and the risk score.
_DOM_TIMELINE_WAIT_MS = 3000


async def dom_last_post(page) -> str:
    """Newest ORGANIC post date read off the already-rendered timeline.
    '' when nothing usable is on screen, never a guess. LINKED TO: called
    from Scraper.process() when the UserTweets/UserOriginalsTimeline
    payload missed its window, and from `replies_tab_last_post()` below
    as its own last-resort tier on the /with_replies tab."""
    try:
        cells = await page.evaluate(JS_TWEET_TIMES)
    except Exception:
        return ""
    dates = [
        c["dt"][:10] for c in (cells or [])
        if c.get("dt") and not c.get("repostOrPinned")
    ]
    return max(dates) if dates else ""


class Scraper:
    """One logged-in X session, driven over a list of profiles.

    LINKED TO: `analysis_path` in backend/platforms/registry.py names
    this class (loaded dynamically, by import path string), and
    backend/services/analysis_service.py is the actual caller.
    """

    normalize_url = staticmethod(normalize_url)

    def __init__(
        self,
        args,
        cookies: list[dict],
        session_id: str = "",
        proxy: dict | None = None,
    ):
        """WHAT: binds this Scraper to one TwitterSession built from
        `cookies`. HOW: `args` is a ScanOptions-shaped object
        (scan_options.py); images load only when evidence capture is on."""
        self.a = args
        self.evidence = args.evidence or None  # GridFS key prefix, not a path
        self.session = TwitterSession(
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

    # ───────────────────────────── per URL ────────────────────────────── #

    async def process(
        self, raw_url: str, target: str, feed: str, known: Optional[dict] = None,
    ) -> Row:
        """One profile URL, start to finish -- the counterpart to
        facebook/analysis_engine.py's Scraper.process().

        `known`, when given (only a profile sent here via "Analyse
        Validated Profiles" carries one -- see analysis/runner.py's
        `seed_by_url`), is whatever discovery already read for this same
        URL. Most fields still come from this visit's own
        UserByScreenName response regardless -- one page load answers
        them all in the same round trip a status/screenshot/last-post
        read needs anyway, so pre-filling from `known` would save nothing
        and would risk a stale value surviving over a fresher one. The one
        field this DOES change the behavior of is `location`: when
        discovery already has one, the separate `read_about_panel()`
        navigation below (step 5) is skipped entirely rather than paying
        for a second page load to re-confirm what's already known.

        WHAT IT RETURNS: a scored `Row`, status OK/PARTIAL/GONE/ERROR/
        CHECKPOINT/LOGIN_REQUIRED, every field tagged with `row.mark()`.

        HOW, roughly in order:
          1. Navigate to the profile; wait for the UserByScreenName/
             UserByRestId response (`found`) and, opportunistically, the
             UserTweets/UserOriginalsTimeline response (`posts`).
          2. If no user payload landed, classify the page as CHECKPOINT/
             LOGIN_REQUIRED/GONE/PARTIAL from its rendered text.
          3. `fill()` the row from the matched user object.
          4. If no last-post date came from the timeline payload, wait
             for the timeline to actually paint and fall back to
             `dom_last_post()`; if the account is known to have posts and
             still has no date, try `replies_tab_last_post()` (an
             all-replies account has a genuinely empty originals
             timeline).
          5. If still no location AND discovery didn't already have one:
             `read_about_panel()`.
          6. Capture the evidence screenshot.

        LINKED TO: called by `one()` below.
        """
        url = normalize_url(raw_url)
        row = Row(url=url, target=target, original_feed=feed)
        row.profile_id = handle_of(url)
        if known and known.get("location"):
            row.location = known["location"]
            row.mark("location", "discovery")

        page = await self.ctx.new_page()
        found: list[TwitterUser] = []
        posts: list[str] = []
        got = asyncio.Event()
        wanted = row.profile_id.lower()

        async def on_response(resp):
            """Captures the profile visit's GraphQL payloads. Watches BOTH
            the user query (identity, counts, join date) and the timeline
            queries (the post dates), because a single profile load fires
            both and missing either costs a whole column. Silent on
            failure -- an unreadable response costs one tier of one field,
            never the visit."""
            try:
                is_user = any(q in resp.url for q in USER_QUERIES)
                is_tweets = any(q in resp.url for q in TWEETS_QUERIES)
                if not (is_user or is_tweets):
                    return
                text = await resp.text()
            except Exception:
                return
            for blob in parse_lines(text):
                if is_user:
                    for u in iter_users(blob):
                        # a profile page also loads other users (who to follow);
                        # only the one whose handle matches is this profile
                        if not wanted or u.handle.lower() == wanted:
                            found.append(u)
                            got.set()
                if is_tweets:
                    ident = found[0] if found else None
                    if iso := latest_post(
                        blob, wanted, ident.entity_id if ident else ""
                    ):
                        posts.append(iso)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        try:
            try:
                await page.goto(
                    f"{url}?lang=en",
                    wait_until="domcontentloaded",
                    timeout=self.a.timeout * 1000,
                )
            except Exception:
                row.status = "ERROR"
                row.note("navigation failed")
                return row

            # the profile query fires within a second of the document loading
            try:
                await asyncio.wait_for(got.wait(), timeout=self.a.settle)
            except asyncio.TimeoutError:
                pass

            body = ""
            try:
                body = await page.inner_text("body")
            except Exception:
                pass

            if not found:
                if RE_CHECKPOINT.search(body):
                    row.status = "CHECKPOINT"
                    row.note("session checkpointed")
                elif (
                    "/login" in page.url
                    or "/i/flow/login" in page.url
                    or RE_LOGIN.search(body)
                ):
                    row.status = "LOGIN_REQUIRED"
                    row.note("cookies rejected/expired")
                elif RE_GONE.search(body):
                    row.status = "GONE"
                    row.note("suspended or does not exist -- may already be down")
                else:
                    row.status = "PARTIAL"
                    row.note("profile payload not seen")
                # See instagram/analysis_engine.py: a row we could not
                # read is the one most worth having a picture of, and
                # returning before screenshot() threw that away.
                await self.screenshot(page, row)
                return row

            # the timeline query lands just after the profile one
            if not posts:
                # Wait for the timeline to actually paint, rather than a
                # flat 1.2s. Measured live: the profile query answers at
                # ~4.1s but the first tweet cells only exist at ~5.7s, so
                # a fixed sleep read an empty timeline for some profiles
                # and a full one for others -- exactly the "17 of 33 have
                # no date" pattern that looked like a parser break.
                try:
                    await page.wait_for_selector(
                        '[data-testid="tweet"]', timeout=_DOM_TIMELINE_WAIT_MS,
                    )
                except Exception:
                    # genuinely no tweets on screen (a zero-post account,
                    # or a protected/empty timeline) -- fall through, the
                    # DOM read below will return "" and the caller keeps
                    # posts_seen as it found it
                    pass
            self.fill(row, found[0])
            if posts:
                row.last_post_iso = max(posts)
                row.posts_seen = "yes"
                row.mark("last_post", "graphql")
            elif row.posts_seen != "no":
                # the UserTweets query (captured into `posts` above) missed
                # its window, fill() already determined "no posts at all"
                # is not the case (posts_seen != "no"), so read the same
                # information off the timeline that's already rendered on
                # screen instead of waiting longer or re-requesting
                iso = await dom_last_post(page)
                if iso:
                    row.last_post_iso = iso
                    row.posts_seen = "yes"
                    row.mark("last_post", "dom-time")

            # Last resort: the /with_replies tab, for an account whose posts
            # are all replies and whose originals timeline is therefore
            # genuinely empty (see replies_tab_last_post). Only reached when
            # the profile tab produced nothing at all AND the account is
            # known to have posts, so a zero-post or protected account never
            # pays for it. Runs on its own page; `page` stays parked on the
            # profile for the screenshot below.
            if (
                not row.last_post_iso
                and row.posts_seen == "yes"
                and "protected" not in row.notes.lower()
            ):
                iso = await self.replies_tab_last_post(
                    url, wanted, found[0].entity_id if found else "")
                if iso:
                    row.last_post_iso = iso
                    row.mark("last_post", "replies-tab")

            # The About panel. Visited when it can actually add something:
            # the profile carried no free-text location of its own (blank on
            # 52% of stored rows), so X's inferred country is the only
            # location available for this row.
            #
            # The account holder's OWN location always wins when present --
            # it is what the impersonator typed, which is the more specific
            # signal for matching a brand; `account_based_in` is X's
            # inference about where the account operates from. Tagged with
            # its own source so the two never read as the same field.
            if not row.location:
                if about := await self.read_about_panel(url):
                    if about.account_based_in:
                        row.location = about.account_based_in
                        row.mark("location", "about-panel")
                    if about.username_changes:
                        # A recycled account is exactly what a rename count
                        # exposes, and nothing else this engine reads
                        # carries it.
                        seen = f"{about.username_changes} username change(s)"
                        if about.last_username_change_iso:
                            seen += f", last {about.last_username_change_iso}"
                        row.note(seen)

            await self.screenshot(page, row)
            row.status = "OK" if row.profile_name else "PARTIAL"
            return row
        finally:
            try:
                await page.close()
            except Exception:
                pass

    @staticmethod
    def fill(row: Row, u: TwitterUser) -> None:
        """WHAT: every field the matched TwitterUser carries, written onto
        `row` and tagged "graphql". HOW: a straight field-by-field copy,
        each guarded so a field the payload didn't carry is left
        untouched. LINKED TO: called from process() once the matching
        UserByScreenName/UserByRestId response has landed."""
        row.profile_id = u.entity_id or u.handle
        row.profile_name = u.name
        row.mark("name", "graphql")
        row.name_score = name_score(u.name, row.target)

        if u.followers is not None:
            row.followers = u.followers
            row.followers_exact = "yes"  # a typed integer, never rounded
            row.mark("followers", "graphql")
        if u.following is not None:
            row.friends = u.following
            row.mark("friends", "graphql")
        if u.created_iso:
            row.created_iso = u.created_iso
            row.mark("created", "graphql")
        if u.location:
            row.location = u.location
            row.mark("location", "graphql")
        if u.description:
            row.bio = u.description
            row.mark("bio", "graphql")
        if u.avatar:
            row.profile_pic_url = u.avatar
            row.has_custom_pic = u.has_custom_pic
            row.mark("logo", "graphql")
        if u.verified:
            row.verified = True
            row.note("verified account")
        if u.protected:
            row.note("protected account -- posts not visible")

        # X exposes no last-post date on the profile payload; posts_count is the
        # honest activity signal, and "no posts at all" is a real answer
        if u.posts is not None:
            row.mark("posts", "graphql")
            if u.posts == 0:
                row.posts_seen = "no"
            else:
                # Recorded, not just noted. `posts_seen` is the signal
                # shared/completeness.py reads to tell "this account never
                # posted" from "we failed to read when it last did", and
                # leaving it unset on a posting account made a real miss
                # indistinguishable from an empty account -- all 18 stored
                # rows in that state carried posts_seen=None. It is also the
                # gate on the /with_replies tier in process(), which must
                # never fire for an account that genuinely has nothing.
                row.posts_seen = "yes"
                row.note(f"{u.posts:,} posts")

    async def read_about_panel(self, url: str) -> Optional[AboutAccount]:
        """X's "About this account" panel -> the country it believes the
        account operates from, plus how many times it has been renamed.

        `https://x.com/<handle>/about` is a REAL page, not only the modal
        the join-date link opens -- confirmed live 2026-08-22: navigating
        straight to it fires AboutAccountQuery and renders "Account based
        in". So this is one plain navigation, with no click to simulate and
        no menu to keep working when X reshuffles it.

        Worth the visit because it is the only source for two things:
          * a location for the 52% of stored rows where the account holder
            left the profile's own free-text location blank, and
          * the rename count, which is the single clearest tell that one
            account has been recycled through several identities and which
            no other payload this project reads exposes at all.

        Runs on its own page so the caller's page stays parked on the
        profile for the evidence screenshot.
        """
        page = await self.ctx.new_page()
        found: list[AboutAccount] = []
        landed = asyncio.Event()

        async def on_response(resp):
            """Captures only the About-panel payload, which carries the
            account's country and creation date -- fields the profile
            header itself does not render."""
            try:
                if ABOUT_QUERY not in resp.url:
                    return
                text = await resp.text()
            except Exception:
                return
            for blob in parse_lines(text):
                if about := about_account_from(blob):
                    found.append(about)
                    landed.set()

        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        try:
            await page.goto(
                f"{url}/about", wait_until="domcontentloaded",
                timeout=self.a.timeout * 1000,
            )
            try:
                await asyncio.wait_for(landed.wait(), timeout=self.a.settle)
            except asyncio.TimeoutError:
                pass
            return found[0] if found else None
        except Exception:
            return None
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def replies_tab_last_post(self, url: str, wanted: str, entity_id: str) -> str:
        """Newest post date off the /with_replies tab. "" when there is none.

        WHY THIS TIER EXISTS
            The profile tab's `UserOriginalsTimeline` serves ORIGINAL posts
            only, but `tweet_counts.tweets` counts replies too. An account
            that only ever replies therefore reports a non-zero post count
            and hands back an empty timeline -- which is indistinguishable,
            from the profile tab alone, from a scraping failure.

            Confirmed live (2026-08-22) against the real stored rows that
            had no date: @MBS_4_U reports 7 posts, its profile tab's
            timeline response carries no tweet objects at all (only
            who-to-follow entries and cursors) and renders zero tweet cells,
            and its /with_replies tab answers 2020-01-17 from
            `UserRepliesTimeline` with 7 cells on screen. 18 of 179 stored
            Twitter rows were sitting in exactly this state.

        WHY A SEPARATE PAGE
            The caller still needs its own page parked on the profile for
            the evidence screenshot. Navigating that page here would capture
            the replies tab instead of the profile -- silently, on every row
            that reached this tier. instagram/analysis_engine.py documents
            having been bitten by precisely that.

        A reply IS the account's own content, so counting it as activity is
        correct: `latest_post` still excludes retweets and pinned tweets,
        which are the two things that would make a dormant account look
        busy.
        """
        page = await self.ctx.new_page()
        found: list[str] = []
        landed = asyncio.Event()

        async def on_response(resp):
            """Captures the replies-timeline payload. Separate from the
            profile visit because an account that ONLY replies has an
            empty originals timeline and would otherwise look postless --
            see read_last_post's fallback order."""
            try:
                if not any(q in resp.url for q in TWEETS_QUERIES):
                    return
                text = await resp.text()
            except Exception:
                return
            for blob in parse_lines(text):
                if iso := latest_post(blob, wanted, entity_id):
                    found.append(iso)
                    landed.set()

        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        try:
            await page.goto(
                f"{url}/with_replies?lang=en",
                wait_until="domcontentloaded",
                timeout=self.a.timeout * 1000,
            )
            try:
                await asyncio.wait_for(landed.wait(), timeout=self.a.settle)
            except asyncio.TimeoutError:
                pass
            if found:
                return max(found)
            # the payload missed its window; the same information is on the
            # rendered tab, read exactly the way the profile tab's fallback
            # reads it (reposts and pinned excluded)
            try:
                await page.wait_for_selector(
                    '[data-testid="tweet"]', timeout=_DOM_TIMELINE_WAIT_MS,
                )
            except Exception:
                pass
            return await dom_last_post(page)
        except Exception:
            return ""
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def screenshot(self, page, row: Row) -> None:
        """WHAT: captures evidence (a GridFS-stored PNG) for `row`, when
        evidence capture is enabled. HOW: waits for a real tweet cell to
        paint before shooting, not just the profile header -- see
        stealth/browser.py::Session.wait_for_visible_content. LINKED TO:
        called from process(), the final step once every field tier has
        run."""
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
            # A rendered tweet cell is X's "the timeline has painted"
            # signal. Without it the capture is a correct header above a
            # spinner where the posts belong.
            await self.session.wait_for_visible_content(
                page, content_selector='[data-testid="tweet"]')
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
        batch `run()` is driving."""
        try:
            return await self.process(u, tgt, feed, known)
        except Exception as e:
            row = Row(url=normalize_url(u), target=tgt, original_feed=feed)
            row.profile_id = handle_of(row.url)
            row.status = "ERROR"
            row.note(f"{type(e).__name__}: {e}")
            return row

    @staticmethod
    def report(i: int, total: int, u: str, row: Row) -> None:
        """One progress line to the platform's own logger after each
        profile."""
        from backend.shared.logging import get_logger as _gl
        _gl("platforms.twitter.analysis").info(
            f"[{i}/{total}] {u} | {row.status} name={row.profile_name[:22]} "
            f"followers={row.followers if row.followers is not None else '-'} "
            f"active={row.active_yes or '-'} risk={row.risk} {row.priority}"
        )

    async def run(self, jobs: list[tuple[str, str, str]]) -> list[Row]:
        """Every (url, target, feed) job in `jobs`, sequentially, pausing
        between profiles and aborting the batch on a CHECKPOINT (unless
        `self.a.keep_going`) to protect the session. LINKED TO: called by
        analysis_service.py."""
        rows: list[Row] = []
        for i, (u, tgt, feed) in enumerate(jobs, 1):
            row = await self.one(u, tgt, feed)
            rows.append(row)
            self.report(i, len(jobs), u, row)
            if row.status == "CHECKPOINT" and not getattr(self.a, "keep_going", False):
                from backend.shared.logging import get_logger as _gl
                _gl("platforms.twitter.analysis").warning(
                    "CHECKPOINT -- aborting to protect the session."
                )
                break
            if i < len(jobs):
                await self.pause()
        return rows
