"""Instagram discovery engine: search, crawling, pagination, and profile
extraction, keywords in, candidate accounts out.

Also owns the browser session (login/checkpoint detection) and the payload
parsing (InstagramUser + friends): both are produced here first and re-used
by analysis_engine.py, which imports them rather than redefining them, so
there is exactly one definition of each across the two files.

Strategy: directly hit the Instagram Mobile API via Playwright's API context
using a spoofed Android User-Agent. This bypasses the web UI and returns a
robust JSON payload with up to 100+ users per request, with pagination to
fetch all available profiles for the keyword while respecting rate limits.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional
from urllib.parse import quote

from backend.shared.extraction import ExtractionResult, run_strategies
from backend.shared.models.row import Row
from backend.shared.text import iter_dicts
from backend.stealth.browser import Session

# Session / login state

ME = "https://www.instagram.com/accounts/edit/"

RE_LOGIN = re.compile(
    # The first three are the classic logged-out pages. The rest are the
    # modern "saved login" wall, which is what a dead session actually gets
    # served now, it never says "log in" anywhere, which is precisely how
    # it slipped past this check. Belt and braces alongside expect_path in
    # check_session; neither is trusted alone.
    r"(Log in to Instagram|Sign up to see|Log In\b.*Sign Up|"
    r"Phone number, username, or email|"
    r"Use another profile|Create new account|"
    r"See everyday moments from your close friends)",
    re.I,
)
RE_CHECKPOINT = re.compile(
    r"(challenge_required|Suspicious Login|"
    r"We Detected An Unusual Login|confirm it.s you|"
    r"Help Us Confirm)",
    re.I,
)
RE_GONE = re.compile(
    r"(Sorry, this page isn.t available|" r"user not found|page not found)", re.I
)


class InstagramSession(Session):
    """The Instagram-specific half of `Session` (backend/stealth/browser.py):
    owns nothing but whether a cookie set is still logged in. Loaded
    dynamically by backend/platforms/registry.py's `session_path` entry
    for "instagram", constructed by session_for_job()
    (backend/sessions/manager.py) whenever a job needs a live Instagram
    browser context."""

    # Same reasoning as FacebookSession -- same company, same CDN, same
    # server-side signal. Measured: an Instagram profile visit requests 33
    # images from instagram.*.fna.fbcdn.net, 23% of its traffic, none of
    # which reached Meta while they were being stubbed. See
    # Session.ALWAYS_LOAD_IMAGES.
    ALWAYS_LOAD_IMAGES = True

    async def check_session(self) -> bool:  # type: ignore[override]
        """WHAT: are these cookies still logged in? HOW: visits the
        authenticated-only /accounts/edit/ page and asks
        Session.check_session() to confirm the browser actually landed
        there rather than on a login/checkpoint wall.

        expect_path is what actually catches a dead Instagram session: the
        logged-out redirect lands on `instagram.com/#`, which trips none
        of the negative patterns. LINKED TO: stealth/browser.py::
        Session.check_session; sessions/manager.py consumes the verdict."""
        return await super().check_session(
            ME, RE_LOGIN, RE_CHECKPOINT, expect_path="/accounts/edit",
        )


# Payload parsing (profile extraction)
# Reading Instagram's own API payloads.
#
# Instagram serves the web client from a private JSON API rather than a
# GraphQL document, so interception targets those endpoints directly:
#
#     /api/v1/users/web_profile_info/    the profile: counts, bio, newest posts
#     /api/v1/fbsearch/topsearch/        search results: users[].user
#     /graphql/query                     newer builds move profile data here
#
# The profile payload is unusually complete, it carries the newest twelve
# posts with unix timestamps, so the last-post date needs no extra request.
#
# NOT AVAILABLE: account creation date. Instagram exposes it only inside
# "About this account", which is an authenticated interactive panel, so that
# column stays blank rather than guessed, the same call made for Facebook.

# Every response worth reading a profile out of. Substring-matched against
# the response URL, so each entry must be a fragment that really appears.
#
# `api/graphql` was the missing one, and its absence was the single largest
# source of data loss in this engine. Confirmed live (2026-08-22, a real
# logged-in profile visit): the modern web client answers a profile
# navigation with `https://www.instagram.com/api/graphql`, whose
# `data.user` object carries the whole record as TYPED values --
# follower_count 79525 (an exact integer, not the rendered "79.5K"),
# following_count, media_count, biography, city_name, external_url,
# is_private, is_verified, profile_pic_url. `profile_from()` below parses
# that object perfectly and always could; the URL simply never matched
# anything in this tuple, so it was never handed to it.
#
# The measured cost of that one missing fragment: 310 of 310 stored
# Instagram analysis rows came from the LAST-RESORT rendered-header tier
# (`sources` = dom-header on every single one), which is why followers were
# stored as rounded strings, biography was blank on 100% of rows, and the
# last-post date was missing on 30%.
#
# `web_profile_info` is kept, but it is no longer the path anything relies
# on: re-verified live on the same date, i.instagram.com AND
# www.instagram.com both answer it HTTP 429 with an empty body for every
# username, under both the Android and desktop User-Agent. It is a
# hard-throttled endpoint now, not a flaky one, and an engine that treats
# it as primary is an engine permanently running on its fallback.
PROFILE_ENDPOINTS = (
    "api/graphql",
    "graphql/query",
    "users/web_profile_info",
    "api/v1/users/",
)

# Instagram's anonymous avatar
DEFAULT_PIC_HINTS = (
    "44884218_345707102882519_2446069589734326272_n",
    "anonymousUser",
    "default_profile",
)


@dataclass
class InstagramUser:
    """One Instagram account, flattened from whichever payload shape
    found it (search result, profile-page graphql, or timeline). LINKED
    TO: the universal per-platform result type -- produced by
    `user_from_node`/`profile_from`/`web_search_users` below, consumed by
    analysis_engine.py's Scraper.fill(), the same relationship
    facebook/discovery_engine.py's `Hit` has to its own consumers."""

    entity_id: str = ""
    username: str = ""
    full_name: str = ""
    followers: Optional[int] = None
    following: Optional[int] = None
    posts: Optional[int] = None
    avatar: str = ""
    biography: str = ""
    verified: bool = False
    private: bool = False
    last_post_iso: str = ""
    category: str = ""
    external_url: str = ""
    has_highlight_reels: Optional[bool] = None
    # Instagram's only location field, and it really is published: it sits
    # on the same `data.user` object as everything else (confirmed live
    # 2026-08-22). Professional/business accounts fill it in; personal ones
    # usually leave it empty, which is why this stays an optional field
    # rather than something a missing value counts against -- see
    # shared/completeness.py::missing_fields, which never checks location
    # on any platform for exactly this reason.
    city_name: str = ""

    @property
    def url(self) -> str:
        """The canonical profile URL, built from `username` -- Instagram
        has no separate numeric-id-only URL form the way Facebook does."""
        return f"https://www.instagram.com/{self.username}/" if self.username else ""

    @property
    def has_custom_pic(self) -> bool:
        """Is `avatar` a real upload, not Instagram's own anonymous-user
        placeholder (see DEFAULT_PIC_HINTS above)."""
        return bool(self.avatar) and not any(
            h in self.avatar for h in DEFAULT_PIC_HINTS
        )


def user_to_row(u: "InstagramUser", keyword: str, *, source: str = "api") -> Row:
    """An `InstagramUser` -> the shared `Row` record `Sweep.hits` now
    carries. `user_from_node()` is a GENERIC node parser -- the same
    function reads a search-result node here and a full profile-info node
    in analysis_engine.py's `fetch_via_api()` -- so this carries forward
    whatever the search response actually populated: usually just name/
    avatar/verified today (the mobile-search and web-topsearch endpoints
    are thinner than the profile-info payload), but never less than that,
    and richer if a future search response happens to embed more.

    Deliberately does NOT call the profile-info endpoint here to backfill
    followers/bio/location during discovery -- that's the trap the "One
    Pass or Two" research flagged: it's a per-profile cost, and discovery
    routinely surfaces far more candidates than an analyst approves.
    That fetch stays scoped to analysis's existing per-approved-candidate
    flow. `row.target` is seeded from the raw search term (`keyword`); a
    caller that resolves keyword-plan parents should overwrite it.
    """
    row = Row(
        url=u.url, target=keyword, entity_type="profile",
        profile_id=u.entity_id or u.username, profile_name=u.full_name or u.username,
        profile_pic_url=u.avatar, has_custom_pic=u.has_custom_pic, verified=u.verified,
        followers=u.followers, friends=u.following, bio=u.biography, location=u.city_name,
    )
    for f in ("profile_name", "profile_pic_url", "verified"):
        row.mark(f, f"discovery-search:{source}")
    for f in ("followers", "friends", "bio", "location"):
        if getattr(row, f) not in (None, ""):
            row.mark(f, f"discovery-search:{source}")
    return row


def _count(node: Any, *keys: str) -> Optional[int]:
    """WHAT: an integer count field, checked under each of `keys` in
    order. HOW: counts appear either as {"count": N} or as a bare integer,
    both shapes handled. LINKED TO: user_from_node() below, for followers/
    following/posts."""
    for k in keys:
        v = node.get(k) if isinstance(node, dict) else None
        if isinstance(v, dict) and isinstance(v.get("count"), int):
            return v["count"]
        if isinstance(v, int):
            return v
    return None


def _latest_post(node: dict) -> str:
    """Newest timestamp among the profile's own recent media."""
    best = 0
    for d in iter_dicts(node):
        ts = d.get("taken_at_timestamp") or d.get("taken_at")
        if isinstance(ts, int) and 1_000_000_000 < ts < 4_000_000_000:
            best = max(best, ts)
    if not best:
        return ""
    return datetime.fromtimestamp(best, timezone.utc).date().isoformat()


def user_from_node(node: dict) -> Optional[InstagramUser]:
    """WHAT: one `InstagramUser` out of a raw payload node (a search
    result, a profile object, or similar), or None if `node` doesn't even
    have a username. HOW: reads every field this project cares about off
    whichever of its several known key-name variants is present (Instagram
    has shipped more than one shape for follower/following/post counts and
    bio links over time). LINKED TO: the shared builder every parser below
    (iter_search_users, iter_mobile_search_users, profile_from) calls once
    it has located the right node in its own payload shape."""
    if not isinstance(node, dict):
        return None
    username = node.get("username")
    if not isinstance(username, str) or not username:
        return None
    pk = node.get("id") or node.get("pk") or node.get("pk_id") or ""
    category = str(node.get("category_name") or node.get("category") or "").strip()
    external_url = str(node.get("external_url") or "").strip()
    if not external_url and isinstance(node.get("bio_links"), list) and node["bio_links"]:
        first_link = node["bio_links"][0]
        if isinstance(first_link, dict):
            external_url = str(first_link.get("url") or "").strip()
    has_highlight_reels = bool(node.get("has_highlight_reels") or node.get("highlight_reel_count"))

    return InstagramUser(
        entity_id=str(pk),
        username=username,
        full_name=(node.get("full_name") or "").strip(),
        followers=_count(node, "edge_followed_by", "follower_count"),
        following=_count(node, "edge_follow", "following_count"),
        posts=_count(node, "edge_owner_to_timeline_media", "media_count"),
        avatar=(node.get("profile_pic_url_hd") or node.get("profile_pic_url") or ""),
        biography=(node.get("biography") or "").strip(),
        verified=bool(node.get("is_verified")),
        private=bool(node.get("is_private")),
        last_post_iso=_latest_post(node),
        category=category,
        external_url=external_url,
        has_highlight_reels=has_highlight_reels,
        city_name=str(node.get("city_name") or "").strip(),
    )


def profile_from(blob: Any, username: str = "") -> Optional[InstagramUser]:
    """The profile this page is about, not whoever else the payload mentions."""
    want = username.lower().strip("/")
    best: Optional[InstagramUser] = None
    for d in iter_dicts(blob):
        # a profile node is the one carrying counts, not a bare mention
        if "username" not in d:
            continue
        if not any(
            k in d
            for k in (
                "edge_followed_by",
                "follower_count",
                "edge_owner_to_timeline_media",
                "media_count",
                "biography",
            )
        ):
            continue
        user = user_from_node(d)
        if not user:
            continue
        if want and user.username.lower() != want:
            continue
        if best is None or (user.followers is not None and best.followers is None):
            best = user
    return best


def timeline_latest_post(blob: Any, username: str = "") -> str:
    """Newest post date in a TIMELINE payload, as ISO. "" when there is none.

    THE GAP THIS CLOSES
        `_latest_post()` above is only ever reached from inside
        `user_from_node()`, which is only reached for a node that already
        looks like a full profile record (it must carry a follower/media
        count or a biography -- see `profile_from`'s gate). A timeline
        response carries none of those: its user objects are the slim
        `edges[].node.user` shape. So on a real profile visit the post
        timestamps were sitting in an intercepted response, fully parsed,
        and then dropped on the floor because the payload holding them
        could not pass a gate designed for a different payload.

        Measured cost: the last-post date was blank on 93 of 310 stored
        Instagram rows (30%). Confirmed live (2026-08-22): the
        `graphql/query` response for a real profile carries 12 posts under
        `data.xdt_api__v1__feed__user_timeline_graphql_connection.edges[]`,
        and this function reads 2026-08-21 off the very payload the old
        code discarded.

    WHY IT IS SCOPED BY OWNER
        That same response really does mention other accounts -- tagged
        users, co-authors and suggestions (live capture: `gautam.adani`,
        `pritiadani`, `cmo_keralam` alongside the profile's own
        `adaniparivar`). None of them owned a `taken_at` node in that
        capture, but nothing guarantees that, and attributing someone
        else's post date to this profile would make a dormant impersonator
        look active -- the exact failure mode the Twitter and Facebook
        engines already scope against. So a node counts only when its own
        `user.username` matches; the unscoped reading is kept solely as a
        fallback for payloads that carry no owner at all, and never
        overrides a scoped one.

    WHY max() AND NOT THE FIRST EDGE
        Instagram pins up to 3 posts to the top of a profile. Confirmed in
        the same capture: `edges[0]` was a pinned post from 2025-12-25
        while the account's real newest post (2026-08-21) sat further down.
        Taking the maximum is what survives pinning -- the same conclusion
        the grid-alt reader in analysis_engine.py reached independently.
    """
    want = (username or "").lower().strip("/")
    scoped, unscoped = 0, 0
    for d in iter_dicts(blob):
        ts = d.get("taken_at") or d.get("taken_at_timestamp")
        if not isinstance(ts, int) or not (1_000_000_000 < ts < 4_000_000_000):
            continue
        owner = ""
        user = d.get("user")
        if isinstance(user, dict):
            owner = str(user.get("username") or "").lower()
        if want and owner:
            if owner == want:
                scoped = max(scoped, ts)
            continue
        unscoped = max(unscoped, ts)
    best = scoped or unscoped
    if not best:
        return ""
    return datetime.fromtimestamp(best, timezone.utc).date().isoformat()


# Instagram's "About this account" panel.
#
# Reached by clicking (Options -> About this account); it is NOT directly
# fetchable. Confirmed live 2026-08-22: the panel is served by a POST to
# `/async/wbloks/fetch/?appid=com.bloks.www.ig.about_this_account` carrying
# ~1.8KB of session-derived tokens (__bkv, __hs, __rev, __s, __hsi, __dyn),
# and `/api/v1/users/<pk>/about_this_account/` answers 404 while
# `/api/v1/users/<pk>/info/` answers 200 but carries no country at all.
# Letting the page issue the request is what keeps those tokens correct
# without this file having to harvest or forge any of them.
#
# The response is a Bloks payload -- a serialised UI tree, not a data
# document -- so the country is read from its own NAMED state key rather
# than by position in the component list. The label/value Text components
# ("Account based in", then "India") are adjacent siblings whose order is a
# layout decision; the named key is the closest thing to a stable contract
# the payload offers.
ABOUT_PANEL_APPID = "com.bloks.www.ig.about_this_account"
_RE_ABOUT_COUNTRY = re.compile(
    r'"key"\s*:\s*"IG_ABOUT_THIS_ACCOUNT:about_this_account_country"'
    r'\s*,\s*"mode"\s*:\s*"[^"]*"\s*,\s*"initial"\s*:\s*"([^"]*)"'
)


def about_country(body: str) -> str:
    """The country Instagram says an account is based in, out of the Bloks
    payload behind "About this account". "" when the payload does not carry
    one (the key is genuinely absent for some accounts)."""
    m = _RE_ABOUT_COUNTRY.search(body or "")
    return (m.group(1).strip() if m else "")


def iter_search_users(blob: Any) -> Iterator[InstagramUser]:
    """Users from a search payload, in result order."""
    seen: set[str] = set()
    for d in iter_dicts(blob):
        node = d.get("user") if isinstance(d.get("user"), dict) else None
        if node is None:
            continue
        user = user_from_node(node)
        if user and user.username.lower() not in seen:
            seen.add(user.username.lower())
            yield user

def iter_mobile_search_users(blob: Any) -> Iterator[InstagramUser]:
    """Users from a mobile search payload (`api/v1/users/search/`)."""
    seen: set[str] = set()
    users = blob.get("users", []) if isinstance(blob, dict) else []
    for node in users:
        if not isinstance(node, dict):
            continue
        user = user_from_node(node)
        if user and user.username.lower() not in seen:
            seen.add(user.username.lower())
            yield user


def parse_lines(text: str) -> Iterator[Any]:
    """WHAT: yields every JSON object found in `text`, tolerating both a
    single JSON document and newline-delimited JSON. HOW: tries a whole-
    text parse first (the common case for a plain API response), falls
    back to per-line parsing for a streamed response. LINKED TO: every
    caller in this file and analysis_engine.py that reads a raw HTTP/XHR
    response body -- the same shape facebook/twitter/telegram's own
    `parse_lines` functions provide for their platforms."""
    text = (text or "").strip()
    if not text:
        return
    try:
        yield json.loads(text)
        return
    except (json.JSONDecodeError, ValueError):
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue


# Crawling / pagination

MOBILE_SEARCH_API = "https://i.instagram.com/api/v1/users/search/?q={q}&count={count}"
# Same private mobile API, one profile by exact username, analysis_engine.py
# uses this directly instead of waiting for the browser's own JS to fire it,
# which it no longer does for a logged-in view of someone else's profile
# (verified live; see analysis_engine.py's module docstring).
PROFILE_INFO_API = "https://i.instagram.com/api/v1/users/web_profile_info/?username={u}"
# This MUST stay an Instagram APP user-agent. It is not a stylistic choice
# and it is not stale -- re-verified live 2026-09-01 against
# /api/v1/users/search/?q=nasa, which answered 200 with 30 users on this
# exact string. Three rules were measured, all of them traps:
#
#   1. A BROWSER user-agent is rejected outright. Both desktop Chrome and
#      mobile Chrome return HTTP 400 {"message":"useragent mismatch"} on
#      i.instagram.com AND www.instagram.com. Anyone "modernising" this
#      constant to a current Chrome UA silently kills Instagram discovery --
#      the sweep would report 0 results as a clean success.
#
#   2. The x-ig-app-id sent alongside it must be the WEB id
#      (936619743392459), NOT the Instagram Android id (567067343352427).
#      Pairing this app UA with the "matching" Android app id returns
#      {"message":"challenge_required"} -- i.e. it asks the ACCOUNT to pass
#      a challenge, which is the one outcome worth avoiding. The mismatched-
#      looking pair is the one that works.
#
#   3. This call cannot be moved to an in-page fetch(). Doing so was
#      investigated to give it Chrome's TLS/HTTP2 fingerprint instead of the
#      Node stack ctx.request uses (the JA3 and ALPN genuinely do differ --
#      ctx.request does not even negotiate h2). It is architecturally
#      impossible: User-Agent is a forbidden header for fetch(), so an
#      in-page call cannot send an app UA and lands straight on rule 1. The
#      TLS difference is real but demonstrably NOT gating this endpoint,
#      which returns 200 over ctx.request today. ctx.request is the correct
#      tool here.
#
# Bumping the version string is safe (275.0.0.27.98 also returns 200) but
# buys nothing measured; the old build is not what would break this.
MOBILE_UA = "Instagram 219.0.0.12.117 Android (29/10; 480dpi; 1080x2151; OnePlus; GM1913; OnePlus7Pro; qcom; en_US; 314660328)"

# Fallback page budget, used only when the caller configured no `max_pages`
# of its own. A bound is needed either way: the loop below advances on a
# `page_token` the API hands back, and a token that never stops coming
# would otherwise sweep one keyword forever. Every other platform's engine
# reads this cap from DiscoveryOptions rather than hardcoding it, and this
# one now does too, so a client asking for more depth actually gets it
# instead of being silently held at ten pages.
DEFAULT_MAX_PAGES = 10


# Secondary endpoint fallback
# The sweep above talks to Instagram's private MOBILE search API. That is
# fast and field-rich, and it is also a single point of failure: the
# endpoint answers 403 for a session Instagram dislikes, and a shape change
# to `users[].user` would leave `by_name` empty. Either way the sweep
# reports 0 results as a clean success.
#
# The fallback is a DIFFERENT endpoint, not the rendered page. A DOM
# fallback was tried first and rejected on evidence: Instagram's web search
# is a slide-out panel in an SPA, the /explore/search/keyword/ URL renders
# no results at all when loaded directly (verified live, it returns only
# nav chrome), and the only "profiles" a DOM scrape found were the logged-in
# user's own account and a "popular" nav link. A fallback that invents
# results is worse than no fallback, because those rows reach an analyst's
# queue as real findings.
#
# `web/search/topsearch` is the web client's own endpoint: a separate path
# on Instagram's side, so it survives the mobile one being refused or
# reshaped, and it returns the same well-formed `users[]` structure.
WEB_SEARCH_API = "https://www.instagram.com/web/search/topsearch/?query={q}"


async def web_search_users(ctx, keyword: str, timeout_s: int = 45) -> list[InstagramUser]:
    """Query the web client's search endpoint. Only runs when the mobile API
    produced nothing, see Discovery.sweep."""
    res = await ctx.request.get(
        WEB_SEARCH_API.format(q=quote(keyword)),
        headers={
            "User-Agent": MOBILE_UA,
            "x-ig-app-id": "936619743392459",
            "accept": "application/json",
        },
        timeout=timeout_s * 1000,
    )
    if res.status != 200:
        # surfaced by run_strategies as this strategy's failure, with this
        # line as the blame site
        raise RuntimeError(f"web/search/topsearch returned HTTP {res.status}")

    data = json.loads(await res.text())
    users: list[InstagramUser] = []
    seen: set[str] = set()
    # the envelope nests the account under `user`; tolerate both shapes so a
    # future flattening does not break this the way it broke the mobile one
    for entry in data.get("users") or []:
        u = entry.get("user") if isinstance(entry, dict) else None
        u = u if isinstance(u, dict) else entry
        if not isinstance(u, dict):
            continue
        username = (u.get("username") or "").strip()
        if not username or username.lower() in seen:
            continue
        seen.add(username.lower())
        users.append(InstagramUser(
            entity_id=str(u.get("pk") or u.get("id") or ""),
            username=username,
            full_name=(u.get("full_name") or "").strip(),
            avatar=(u.get("profile_pic_url") or "").strip(),
            verified=bool(u.get("is_verified")),
            private=bool(u.get("is_private")),
        ))
    return users


@dataclass
class Sweep:
    """One keyword's search sweep, and how it ended. LINKED TO: built and
    returned by Discovery.sweep() below; the facebook/twitter/telegram/
    tiktok/youtube discovery engines each define their own Sweep with the
    same shape (hits, pages, stopped, complete, source, extraction) --
    there is no shared base class, each platform's own completeness
    signals and pagination model differ enough that a shared type would
    be mostly unused fields."""

    keyword: str
    tab: str = "people"
    hits: list[Row] = field(default_factory=list)
    users: list[InstagramUser] = field(default_factory=list)
    pages: int = 0
    stopped: str = ""
    complete: bool = False
    seconds: float = 0.0
    error: str = ""
    # "api" normally; "web-api" when the private mobile endpoint refused us
    # or stopped being parseable and the web client's endpoint stood in
    source: str = "api"
    extraction: Optional["ExtractionResult"] = None

    def summary(self) -> str:
        """One-line log form. Names `source` whenever it is not the
        preferred API tier, so a silent slide down the fallback chain
        shows up in the logs instead of passing as a normal result."""
        base = f"{len(self.hits)} hits, {self.pages} responses, {self.stopped}"
        return f"{base}, via {self.source}" if self.source != "api" else base


class Discovery:
    """Runs keyword sweeps against Instagram's private mobile search API.

    LINKED TO: `discovery_path` in backend/platforms/registry.py names
    this class (loaded dynamically, by import path string), and
    backend/services/discovery_service.py is the actual caller.
    """

    def __init__(self, args, ctx):
        """`args` is a DiscoveryOptions-shaped object (scan_options.py)
        carrying pacing/cap knobs; `ctx` is the already-started Playwright
        BrowserContext (stealth/browser.py::Session.start()) this class
        issues raw API requests through via `ctx.request`."""
        self.a = args
        self.ctx = ctx

    async def sweep(self, keyword: str, tab: str = "people") -> Sweep:
        """One keyword, start to finish -- the counterpart to
        facebook/discovery_engine.py's Discovery.sweep(), but page-token
        paginated over a direct API call instead of scroll-paginated over
        a browser page (see this module's own top docstring for why: a
        spoofed-UA request to Instagram's mobile search endpoint, not a
        rendered page visit).

        WHAT IT RETURNS: a `Sweep` carrying every hit found, which
        completeness signal stopped it (exhausted / cap:pages / http-NNN /
        error), and the extraction chain's own record of which API
        answered (api:mobile-topsearch or the api:web-topsearch fallback).

        HOW, roughly in order:
          1. Page through MOBILE_SEARCH_API up to `max_pages` times (or
             DEFAULT_MAX_PAGES), following `page_token`/`rank_token`,
             absorbing new users into `by_name` until the API says
             `has_more: false` with no next token, or the page budget
             runs out (marked incomplete, not silently "done").
          2. Run the extraction chain (mobile API result first, the web
             client's own search endpoint as fallback -- see
             `run_strategies` in shared/extraction.py) to decide the
             final user list and record which one actually produced it.
          3. Build `Hit`s from the winning user list.

        LINKED TO: called by `run()` below (one call per keyword).
        """
        out = Sweep(keyword=keyword, tab=tab)
        started = time.time()
        by_name: dict[str, InstagramUser] = {}

        page_token = None
        rank_token = None

        try:
            max_pages = int(getattr(self.a, "max_pages", 0) or 0) or DEFAULT_MAX_PAGES
            for _ in range(max_pages):
                url = MOBILE_SEARCH_API.format(q=quote(keyword), count=100)
                if page_token:
                    url += f"&page_token={page_token}"
                if rank_token:
                    url += f"&rank_token={rank_token}"

                res = await self.ctx.request.get(
                    url,
                    headers={
                        "User-Agent": MOBILE_UA,
                        "x-ig-app-id": "936619743392459",
                        "accept": "application/json"
                    },
                    timeout=self.a.timeout * 1000
                )

                if res.status != 200:
                    out.stopped = f"http-{res.status}"
                    break

                text = await res.text()
                data = json.loads(text)

                new_users = 0
                for user in iter_mobile_search_users(data):
                    if user.username.lower() not in by_name:
                        by_name[user.username.lower()] = user
                        new_users += 1

                if new_users > 0:
                    out.pages += 1

                # Check for pagination
                has_more = data.get("has_more")
                rank_token = data.get("rank_token")
                page_token = data.get("page_token") or data.get("next_max_id")

                if not has_more and not page_token:
                    out.stopped = "exhausted"
                    out.complete = True
                    break

                # Respect limits between pages
                await asyncio.sleep(2.5)

            if not out.stopped:
                # Ran the whole page budget without the API ever saying it
                # had run out (`has_more` false / no next token): there are
                # more results we did not fetch. This is INCOMPLETE, and
                # saying so is the entire point -- `_sweep_platform`'s
                # incomplete accounting (services/discovery_service.py)
                # reads exactly this flag to decide whether to warn the
                # analyst, so marking a truncated sweep complete was the one
                # place this engine reported "we reached the end" about
                # results it had never asked for. Uses the same "cap:pages"
                # vocabulary every other platform's engine already emits for
                # the identical outcome; no token in it is session-shaped, so
                # shared/resilience.py::classify_failure correctly leaves the
                # session pool alone.
                out.stopped = "cap:pages"
                out.complete = False

            if not by_name and out.stopped == "exhausted":
                # The mobile API paged all the way to its own "no more
                # results" signal and genuinely found nobody -- a common,
                # valid outcome for a keyword with zero real Instagram
                # matches, NOT an extraction failure. Routing this through
                # run_strategies() would misreport it as "every strategy
                # failed" (an ERROR-level log for a normal empty search)
                # and pay for a pointless web-topsearch fallback call on
                # every such keyword. Only a mobile pass that did NOT
                # cleanly complete (http-403, cap:pages, an exception)
                # falls through to the web fallback below.
                out.users = []
            else:
                # Private mobile API first (richer), rendered search page
                # second. The DOM pass only runs when the API produced
                # nothing at all, a 403, or a payload whose shape we no
                # longer recognise, so a healthy sweep never pays for a
                # browser page.
                chain = await run_strategies(
                    f"instagram/search[{keyword!r}]",
                    [
                        ("api:mobile-topsearch", lambda: list(by_name.values())),
                        ("api:web-topsearch", lambda: web_search_users(self.ctx, keyword, self.a.timeout)),
                    ],
                )
                out.users = chain.value or []
                out.extraction = chain
                if chain.degraded:
                    out.source = "web-api"
                    # the mobile leg failed, so whatever `stopped` recorded
                    # about it (http-403) no longer describes the sweep's
                    # outcome
                    out.stopped = "mobile-api-failed-web-recovered"

            out.hits = [
                user_to_row(u, keyword, source=out.source)
                for u in out.users
                if u.url
            ]
            if self.a.max_results:
                out.hits = out.hits[: self.a.max_results]
        except Exception as e:
            out.stopped, out.error = "error", f"{type(e).__name__}: {e}"
        finally:
            out.seconds = time.time() - started

        return out

    async def run(self, keywords: list[str], tabs=None) -> list[Sweep]:
        """Every keyword, `self.a.concurrency` at a time, each staggered
        by a couple seconds (see the sleep just below) to avoid firing a
        burst of near-simultaneous search requests off one session --
        `tabs` is accepted and ignored, Instagram search has no tab
        concept the way Facebook's People/Pages/Groups does. LINKED TO:
        called by discovery_service.py once per client's keyword batch."""
        sem = asyncio.Semaphore(max(1, self.a.concurrency))

        async def one(i: int, keyword: str) -> tuple[int, Sweep]:
            """One keyword sweep, holding a concurrency slot. Returns its
            index alongside the Sweep so the caller can restore order."""
            async with sem:
                # Initial delay to space out concurrent requests
                await asyncio.sleep(i % max(1, self.a.concurrency) * 2.0)
                s = await self.sweep(keyword)
                print(
                    f"  [instagram] {keyword!r}: {s.summary()} ({s.seconds:.1f}s)",
                    file=sys.stderr,
                )
                return i, s

        pairs = await asyncio.gather(*(one(i, k) for i, k in enumerate(keywords)))
        return [s for _, s in sorted(pairs, key=lambda p: p[0])]

