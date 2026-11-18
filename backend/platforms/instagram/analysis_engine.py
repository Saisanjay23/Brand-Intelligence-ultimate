"""Instagram analysis engine: validation and impersonation signal extraction
-- profile URL -> scored Row.

Session/login-checking and payload parsing (InstagramUser + friends) live in
discovery_engine.py (imported below) since discovery produces/needs them
first; this file owns everything specific to a validated profile visit: URL
normalization, the DOM-header fallback, and the browser-drive loop (Scraper).

Visiting a profile *was designed* to fire `users/web_profile_info` and read its
counts, avatar and newest posts from the payload directly. The current web
client does not issue that call passively for a logged-in view of someone
else's profile, and asking for it directly (with a fake Android-app
User-Agent, from outside the browser) was tried, answered 429 for every
username, and was removed -- see the note where `fetch_via_api()` used to
be. What the page DOES fire is its own `api/graphql` profile query, and
that response, intercepted as the browser receives it, is the primary
source. DOM reading remains the last resort.

What's reliable when interception comes up empty: the
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

NO-LOGIN MODE (`Scraper(..., anonymous=True)`, added 2026-11-17). The pool's
Instagram accounts kept being suspended, and a public profile page still
loads for a visitor who is not signed in. So analysis now reads each profile
that way FIRST (analysis/runner.py's no-login phase) and only falls back to
an account -- the logged-in path above, unchanged -- when Instagram refuses
the logged-out visitor. What this mode does, and what it refuses to do:

  * ONE PAGE LOAD PER PROFILE, in a real browser on its own persistent
    profile, with NO cookies of ours in it (`LoggedOutInstagramSession`). No
    login probe, no home-feed warm-up, no permalink visits for the post date.
  * IT READS ONLY WHAT THE PAGE DELIVERS: the profile's own data, which
    Instagram embeds in the page as JSON (`<script type="application/json">`
    blocks; the profile sits under `xig_user_by_username`), the rendered
    page, and responses the page fetches by itself. It never takes a token
    off the page to call an Instagram API on its own -- that is a different
    thing from visiting a page, and it is not done here.
  * A REFUSAL IS NEVER A FINDING. A login wall, an HTTP 429, a challenge, or
    a 200 page with no profile data in it ends as `BLOCKED_LOGGED_OUT` --
    not GONE, not OK -- and the runner hands that profile to an account.
    GONE needs Instagram's own "page isn't available" page, its error-page
    marker, and no login wall (see `process_logged_out`).

Where the JSON paths come from: instaloader's logged-out fallback, merged in
PR #2765 (v4.15.4, 2026-10-12) and refined on master since -- see
`embedded_query_data` and `logged_out_user` below for the exact lines.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import random
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import quote, urlparse

from backend.shared import diagnostics, evidence
from backend.shared.pushback import (CHALLENGED, EXPIRED, THROTTLED, Signal,
                                     classify_response, governor_for,
                                     note_response)
from backend.shared.tasks import spawn
from backend.shared.models.row import Row
from backend.shared.walls import first_segment, is_wall_url, url_path
from backend.platforms.scan_options import cancelled, captures_screenshot
from backend.stealth.mouse_movement import humanize_interaction
from backend.shared.text import (MONTHS, _letters_only, name_score,
                                   normalized_host, parse_count,
                                   parse_normalized_url)
from backend.shared.avatars import hd_picture_url, looks_like_placeholder
from backend.platforms.instagram.discovery_engine import (PROFILE_ENDPOINTS,
                                                           RE_CHECKPOINT,
                                                           RE_GONE, RE_LOGIN,
                                                           InstagramSession,
                                                           InstagramUser,
                                                           parse_lines,
                                                           profile_from,
                                                           timeline_latest_post,
                                                           user_from_node)

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


# ──────────────────────────── no-login reading ──────────────────────────── #
#
# Everything below up to `class Scraper` serves NO-LOGIN MODE (see the module
# docstring). Pure functions over the page's own text, so every rule here is
# tested on fixtures without a browser (tests/test_instagram_no_login_analysis.py).

# The row status for a logged-out visit Instagram REFUSED. Deliberately not
# one of the statuses anything else already gives a meaning to: not GONE (a
# refusal says nothing about whether the profile exists), not PARTIAL (which
# the runner files as done), not CHECKPOINT / LOGIN_REQUIRED (those quarantine
# an account, and there is no account here). The runner hands a row with this
# status to the account path; nothing ever stores it as a reading.
BLOCKED_LOGGED_OUT = "BLOCKED_LOGGED_OUT"

# WHY a logged-out read was refused, kept on the row as
# `row.src["access"] = "logged-out-blocked:<kind>"`. The runner needs the
# difference: a refusal of THIS MACHINE (throttled, login wall, challenge, a
# page with the data withheld) means "stop visiting logged-out for a while",
# while a page that simply did not load means "hand this one over" and
# nothing more.
NL_THROTTLED = "throttled"
NL_LOGIN_WALL = "login-wall"
NL_CHALLENGE = "challenge"
NL_NO_DATA = "no-data"
NL_UNREACHABLE = "unreachable"
NO_LOGIN_IP_BLOCKS = frozenset({NL_THROTTLED, NL_LOGIN_WALL, NL_CHALLENGE, NL_NO_DATA})
# Instagram's own error page, WITHOUT the words that would make it a GONE.
# Seen live (2026-11-17), logged out: a handle that cannot exist answered
# HTTP 200 with a ~539 KB page carrying the error-page root, no profile data
# and no description line, rendering "Profile isn't available / The link may
# be broken, or the profile may have been removed." -- wording RE_GONE does
# not know. A second, real-looking handle answered with the identical page
# moments after a profile that read perfectly. So this is what a removed
# profile looks like -- or one Instagram hides from visitors who are not
# signed in ("may have been removed" settles neither) -- and NOT what a
# refused IP looks like (the error root is measured absent from the login
# wall). It is handed to an account like any unread profile, but it does not
# stop or pause the no-login phase: one dead link in a batch must not send
# the whole batch to the accounts. Several in a row does (analysis/runner.py,
# `_NO_LOGIN_ERROR_PAGE_STREAK`), in case Instagram ever serves this page to
# a visitor it is limiting.
NL_ERROR_PAGE = "error-page"

# The `row.src["access"]` value of a profile read without login. Also the
# provenance every field read that way is tagged with (see `_retag_logged_out`).
ACCESS_LOGGED_OUT = "logged-out-page"
_BLOCKED_PREFIX = "logged-out-blocked:"
# How every refusal note on a row begins (`Scraper._refused`). Named so the
# runner, which repeats the reason in its own sentences, can drop it.
REFUSED_NOTE_PREFIX = "could not be read without login -- "


def no_login_block_kind(row: Row) -> str:
    """The refusal kind on a `BLOCKED_LOGGED_OUT` row, or "" for any other
    row. Read by analysis/runner.py to decide whether to pause no-login."""
    src = str((getattr(row, "src", None) or {}).get("access") or "")
    return src[len(_BLOCKED_PREFIX):] if src.startswith(_BLOCKED_PREFIX) else ""


# The persistent browser profile the no-login browser lives in. Its OWN
# directory (stealth/browser.py::profile_dir_for -> "instagram_no-login"),
# never a pooled account's: no login has ever been made in it, so there is no
# account for it to remember. A fixed name, so its fingerprint stays the same
# run to run -- the same reasoning as TikTok's anonymous_context().
NO_LOGIN_PROFILE_ID = "no-login"

# The JSON blocks Instagram server-renders into a page. Instaloader's own
# pattern (instaloadercontext.py, `get_page_data`), loosened only so the
# `type` attribute may sit anywhere in the tag.
_RE_JSON_SCRIPT = re.compile(
    r'<script\b[^>]*\btype="application/json"[^>]*>(.*?)</script>', re.S)

# Instagram's own error-page root. Measured (shared/fast_http.py,
# _DEATH_SIGNALS, 2026-09-22): present on 3/3 missing profiles served
# logged-out, absent from 3/3 live ones AND from the login wall, the site
# root and /explore/. That last part is why it is safe to lean on: a wall
# does not carry it.
_GONE_MARKER = "PolarisErrorRoot"

# The page's own one-line summary, "1M Followers, 10 Following, 500 Posts -
# See Instagram photos and videos from ...". Present on every live profile
# page (the same fast_http measurement uses it as the "alive" veto), and the
# only place a logged-out visitor is shown the post count when the JSON
# withholds it. Counts there are rounded ("1M"), so they are recorded as such.
_RE_META_TAG = re.compile(r'<meta\b[^>]*\b(?:name|property)="(?:og:)?description"[^>]*>', re.I)
_RE_META_CONTENT = re.compile(r'\bcontent="([^"]*)"', re.I)
_RE_META_COUNTS = re.compile(
    r"([\d][\d,.]*[KMB]?)\s+Followers?,\s*([\d][\d,.]*[KMB]?)\s+Following,\s*"
    r"([\d][\d,.]*[KMB]?)\s+Posts?", re.I)

# The same JSON blocks, read off the live DOM -- the second source when the
# document's own response body could not be read.
JS_JSON_SCRIPTS = """
() => Array.from(document.querySelectorAll('script[type="application/json"]'))
  .map(s => s.textContent || "")
  .filter(t => t.includes("xig_user_by_username"))
"""


def embedded_query_data(obj: Any) -> Iterator[dict]:
    """Every GraphQL query result a page embeds: the `data` object under each
    `__bbox.result`, anywhere in one parsed JSON block.

    A port of instaloader's `_embedded_query_data` (instaloadercontext.py,
    lines 34-45 at the PR #2765 merge commit 0cf00a6), iterative instead of
    recursive so a deep block cannot exhaust the stack."""
    stack: list[Any] = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            box = cur.get("__bbox")
            result = box.get("result") if isinstance(box, dict) else None
            if isinstance(result, dict) and isinstance(result.get("data"), dict):
                yield result["data"]
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def json_scripts(html: str) -> list[str]:
    """The text of every `<script type="application/json">` block in `html`."""
    return [m.group(1) for m in _RE_JSON_SCRIPT.finditer(html or "")]


def logged_out_user(scripts: Iterable[str], username: str = "") -> Optional[dict]:
    """The profile a logged-out page embeds, as Instagram's own dict, or None.

    The rule is instaloader's (structures.py `Profile._from_page`, master
    lines 1005-1030; first merged in PR #2765): the page embeds the profile's
    details and its first posts as TWO query results, both under
    `xig_user_by_username`, so they are merged; the profile exists only when
    the merged record carries a `pk`. One addition here: a block that NAMES a
    different account is skipped rather than merged in, so nothing about
    somebody else can be attributed to this profile.

    None means "the page did not give us this profile" -- which is a refusal
    or a missing profile, never an empty one. The caller decides which.
    """
    want = (username or "").strip("/").lower()
    merged: dict = {}
    for text in scripts:
        # A profile page carries dozens of these blocks; only the one or two
        # holding the profile are worth parsing.
        if not text or "xig_user_by_username" not in text:
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        for data in embedded_query_data(obj):
            user = data.get("xig_user_by_username")
            if not isinstance(user, dict):
                continue
            name = str(user.get("username") or "").lower()
            if want and name and name != want:
                continue
            merged.update(user)
    return merged if merged.get("pk") else None


def user_from_logged_out(user: dict) -> Optional[InstagramUser]:
    """`logged_out_user`'s dict -> the same `InstagramUser` the logged-in path
    fills a row from, through the same `user_from_node`.

    Two adjustments, both from instaloader: the page's `id` is NOT the user
    id -- `pk` is (master structures.py line 1022) -- and a logged-out page
    states the post count as `all_media_count` where the API says
    `media_count` (master lines 1151-1153). Everything else -- follower and
    following counts, bio, verified, private, the picture, and the newest
    post date out of `polaris_ordered_timeline_connection` -- is read by
    `user_from_node` exactly as it reads any other profile record, including
    its guard against counting somebody else's post as this profile's.
    """
    node = dict(user)
    node["id"] = node.get("pk")
    count = node.get("all_media_count")
    if "media_count" not in node and isinstance(count, int) and not isinstance(count, bool):
        node["media_count"] = count
    return user_from_node(node)


def timeline_edge_count(user: Optional[dict]) -> Optional[int]:
    """How many of its own posts the page embedded, or None when it embedded
    no timeline at all (a private account, or a page that withheld it)."""
    conn = (user or {}).get("polaris_ordered_timeline_connection")
    edges = conn.get("edges") if isinstance(conn, dict) else None
    return len(edges) if isinstance(edges, list) else None


def meta_description(html: str) -> str:
    """The page's own description line, unescaped, or ""."""
    for tag in _RE_META_TAG.finditer(html or ""):
        m = _RE_META_CONTENT.search(tag.group(0))
        if m and m.group(1).strip():
            return html_lib.unescape(m.group(1))
    return ""


def meta_counts(html: str) -> dict[str, tuple[Optional[int], bool]]:
    """{"followers"|"following"|"posts": (value, was_exact)} from the page's
    description line, only for the counts it actually states."""
    m = _RE_META_COUNTS.search(meta_description(html))
    if not m:
        return {}
    out = {}
    for key, raw in zip(("followers", "following", "posts"), m.groups()):
        val, exact = parse_count(raw)
        if val is not None:
            out[key] = (val, exact)
    return out


def note_logged_out(status: Optional[int], body: Optional[str] = None) -> Signal:
    """Classify one response a logged-out visit received and feed the
    pacing governor -- the same `classify_response` every visit uses.

    One exception: an EXPIRED verdict ("log in", HTTP 401/403) is not fed.
    It means a lapsed login everywhere else, and there is no login here --
    a logged-out visitor being asked to log in is a refusal of this visit,
    which the caller records as one, not evidence that slows down the
    accounts."""
    sig = classify_response("instagram", status, body)
    if sig.kind != EXPIRED:
        governor_for("instagram").record(sig)
    return sig


def _retag_logged_out(row: Row) -> None:
    """Re-label what `Scraper.fill()` tagged "api" as read off the logged-out
    page, so an analyst can see a field's provenance. Instagram's own
    `has_anonymous_profile_picture` keeps its "api-anonymous-flag" tag on
    purpose: that tag is what gives the logo verdict its strength
    (shared/logo_verdict.py), and the flag is Instagram's own statement
    whether the visitor is signed in or not."""
    for fld, src in list(row.src.items()):
        if src == "api":
            row.src[fld] = ACCESS_LOGGED_OUT
        elif src == "api-no-posts":
            row.src[fld] = f"{ACCESS_LOGGED_OUT}-no-posts"


class LoggedOutInstagramSession(InstagramSession):
    """A browser with NO login in it, for no-login analysis.

    Built on `InstagramSession` so it gets everything every Instagram visit
    gets -- the stealth launch, the persistent profile, real image loads --
    and differs in exactly the four places a login would come in:

      * cookies    always empty, whatever a caller passes. The site sets its
                   own logged-out cookies as it would for any visitor; none
                   of ours are ever put in.
      * warm-up    none. A logged-out "home feed" is Instagram's login page,
                   and loading it before every batch would be one more page
                   against this machine's logged-out allowance for nothing.
      * login check  always "usable": there is no login to check, and the
                   check itself is an authenticated page load.
      * cookie save  never. Nothing here belongs in the session pool.
    """

    def __init__(self, options, cookies=None, load_images: bool = False,
                 session_id: str = NO_LOGIN_PROFILE_ID, **_ignored):
        super().__init__(options, [], load_images=load_images,
                         session_id=session_id or NO_LOGIN_PROFILE_ID)

    async def _warmup(self) -> None:  # type: ignore[override]
        return None

    async def check_session(self) -> bool:  # type: ignore[override]
        return True

    async def sync_cookies(self) -> None:  # type: ignore[override]
        return None


class Scraper:
    """One Instagram session, driven over a list of profiles: a logged-in
    pooled account, or -- with `anonymous=True` -- a browser with no login
    at all (NO-LOGIN MODE, see the module docstring).

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
        anonymous: bool = False,
    ):
        """WHAT: binds this Scraper to one InstagramSession built from
        `cookies`. HOW: `args` is a ScanOptions-shaped object
        (scan_options.py); images load only when evidence capture is on
        (a screenshot with images blocked is useless as proof).

        `anonymous=True` is no-login mode: the session is a
        `LoggedOutInstagramSession`, `cookies` and `session_id` are ignored
        (nothing of an account's may reach that browser), and `process()`
        reads the public page instead -- see `process_logged_out`. Same
        keyword and same meaning as TikTok's Scraper, which is what
        analysis/runner.py already passes for an anonymous worker."""
        self.a = args
        self.evidence = args.evidence or None  # GridFS key prefix, not a path
        self.anonymous = bool(anonymous)
        if self.anonymous:
            self.session = LoggedOutInstagramSession(
                args, [], load_images=captures_screenshot(args))
        else:
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

    # NO DIRECT API CALL. A `fetch_via_api()` lived here: Instagram's private
    # `web_profile_info` endpoint, asked with a fake Instagram-for-Android
    # User-Agent through Playwright's own HTTP client -- a different network
    # fingerprint from the browser the same login uses for everything else,
    # which is the shape of a stolen session cookie being replayed by a
    # script. It stopped being called on 2026-09-24 (the endpoint answered
    # 429 for every username) and was deleted on 2026-11-17, after the
    # pool's Instagram account was suspended, so it cannot be switched back
    # on by accident. Profile data comes from the page's own `api/graphql`
    # response, the request a person's browser makes when it opens the
    # profile.

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
        process() only when the intercepted
        response came up empty; result consumed by `fill_from_dom()`
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

    @classmethod
    def _timeline_alt_dates(cls, user: Optional[dict], owners: tuple[str, ...]) -> list[str]:
        """One entry per post a logged-out page embedded, in page order: the
        date its accessibility caption states, or "" when it states none
        (a custom caption, an unfamiliar shape) or the post names another
        account as its author. NO-LOGIN MODE's last-post source -- see
        process_logged_out.

        Two owner checks, both rejecting only on positive evidence: the
        node's own `user.username`, and the author named in the caption
        (`_parse_alt_date` / `_owner_mismatch`)."""
        conn = (user or {}).get("polaris_ordered_timeline_connection")
        edges = conn.get("edges") if isinstance(conn, dict) else None
        handles = {(o or "").strip("/").lower() for o in owners[:1] if o}
        out: list[str] = []
        for edge in edges if isinstance(edges, list) else []:
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict):
                continue
            who = node.get("user")
            author = str(who.get("username") or "").lower() if isinstance(who, dict) else ""
            if author and handles and author not in handles:
                out.append("")
                continue
            out.append(cls._parse_alt_date(str(node.get("accessibility_caption") or ""), owners))
        return out

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

    # ───────────────────────────── per URL ────────────────────────────── #

    async def process(
        self, raw_url: str, target: str, feed: str, known: Optional[dict] = None,
    ) -> Row:
        """One profile URL, start to finish -- the counterpart to
        facebook/analysis_engine.py's Scraper.process().

        `known`, when given (only a profile sent here via "Analyse
        Validated Profiles" carries one -- see analysis/runner.py's
        `seed_by_url`), pre-fills `row.location`/`row.created_iso`. Every
        OTHER field still comes from this visit's own
        interception/DOM chain regardless of what
        discovery had, since they all land in the SAME payload that a
        status/screenshot/last-post read needs this visit to make anyway
        -- skipping them would save nothing.

        NO JOIN DATE. Instagram publishes an account's join date only in
        the "About this account" panel, which has to be clicked open from
        the profile's menu. A person opens it on almost no profiles; this
        engine used to open it on nearly every one, a pattern Instagram can
        score as automation. It was removed on purpose (2026-11-10): the
        join date stays blank on Instagram rows unless discovery seeded
        one, and location comes only from the profile payload's
        `city_name`.

        WHAT IT RETURNS: a scored `Row`, status OK/PARTIAL/GONE/ERROR/
        CHECKPOINT/LOGIN_REQUIRED, every field tagged with `row.mark()`.

        HOW, roughly in order:
          1. (No direct API call -- removed 2026-11-17, see the note
             where `fetch_via_api()` used to be.)
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
          6. If still no last-post date: `read_last_post_date()`'s grid-alt
             read, then up to three permalink visits (these navigate the
             page away, which is why they run last).

        LINKED TO: called by `one()` below.
        """
        if getattr(self, "anonymous", False):
            # NO-LOGIN MODE has its own, much shorter path: one page, read
            # as served. Everything below is the logged-in path, unchanged.
            # (getattr: a Scraper built without __init__ -- as tests do --
            # is a logged-in one.)
            return await self.process_logged_out(raw_url, target, feed, known)
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
        # primary path. `fetch_via_api` itself was deleted on 2026-11-17
        # (see the note where it used to be).
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
                # prefer the richest payload seen: an earlier capture is
                # usually best, but a later interception
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

    # ─────────────────────────── no-login path ─────────────────────────── #

    @staticmethod
    def _refused(row: Row, kind: str, why: str) -> Row:
        """End a logged-out visit as REFUSED: `BLOCKED_LOGGED_OUT`, the kind
        on `row.src["access"]`, and a note that says it is not a verdict.
        Nothing a refused page showed is kept on the row."""
        row.status = BLOCKED_LOGGED_OUT
        row.mark("access", f"{_BLOCKED_PREFIX}{kind}")
        row.note(f"{REFUSED_NOTE_PREFIX}{why}. This is not a verdict on the profile")
        return row

    async def process_logged_out(
        self, raw_url: str, target: str, feed: str, known: Optional[dict] = None,
    ) -> Row:
        """One profile, read from its PUBLIC page with no login.

        ONE PAGE LOAD. The profile's own data is server-rendered into the
        page (see `logged_out_user`), so the document Instagram sends for
        the profile URL is the whole read. No login probe before it, no
        permalink visits after it: a logged-out visitor's allowance is per
        IP address and every extra page spends it.

        WHERE THE DATA COMES FROM, in order:
          1. the document's own response body -- the HTML as delivered;
          2. the same JSON blocks read off the live DOM, if the body could
             not be read;
          3. a profile record in a response the page fetched by itself
             (passively intercepted, exactly as the logged-in path does).

        THE OUTCOMES, and the one rule behind them -- a page that did not
        give us the profile is never filed as a finding about the profile:
          OK       the profile's data was on the page.
          GONE     Instagram's own "Sorry, this page isn't available" page:
                   the rendered text says so (RE_GONE), the HTML carries
                   Instagram's error-page root (`_GONE_MARKER`, measured
                   absent from the login wall), the page has no description
                   line (every live profile has one), and the visit was not
                   sent to the login page.
          BLOCKED_LOGGED_OUT  everything else: a login wall, HTTP 429 or any
                   other refusal status, a challenge, a "try again later"
                   page, a page that did not load, or a 200 page with no
                   profile data in it -- the shape Instagram gives a
                   logged-out visitor it has decided to limit -- and the
                   error page without the words that settle it
                   (`NL_ERROR_PAGE`). The runner hands these to an account;
                   `row.src["access"]` says which kind it was.
          ERROR    only a URL that is not a profile at all (no username),
                   refused before any page load, exactly as logged in.

        LAST-POST DATE: the newest of the page's embedded posts, read from
        each post's accessibility caption ("... on November 17, 2026"), which
        is the only date a logged-out page carries (see `_timeline_alt_dates`).
        A `taken_at`, should Instagram ever embed one, is used first
        (`user_from_node`). Otherwise blank, with a note saying why -- never
        a guess, and never a second page to find it.

        429s and challenges seen on the way feed the pacing governor, as on
        every visit (`note_logged_out`).
        """
        url = normalize_url(raw_url)
        row = Row(url=url, target=target, original_feed=feed)
        row.profile_id = username_of(url)
        if not row.profile_id:
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

        # Pinned before any listener runs, for the reason process() gives.
        wanted = row.profile_id
        page = await self.ctx.new_page()
        found: list[InstagramUser] = []
        post_dates: list[str] = []
        pushed: list[Signal] = []

        async def on_response(resp):
            """Responses the page fetched by itself: read, never asked for."""
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
                    parsed_any = True
                if iso := timeline_latest_post(blob, wanted):
                    post_dates.append(iso)
                    parsed_any = True
            sig = note_logged_out(
                getattr(resp, "status", None),
                text if (not parsed_any and len(text) < 4000) else None)
            if sig.kind in (THROTTLED, CHALLENGED):
                pushed.append(sig)

        page.on("response", lambda r: spawn(on_response(r)))

        try:
            doc_status: Optional[int] = None
            html = ""
            try:
                resp = await page.goto(
                    url, wait_until="domcontentloaded", timeout=self.a.timeout * 1000)
            except Exception as e:
                first = (str(e).splitlines() or [""])[0][:200]
                return self._refused(row, NL_UNREACHABLE,
                                     f"the page did not load ({type(e).__name__}: {first})")
            st = getattr(resp, "status", None)
            if isinstance(st, int) and not isinstance(st, bool):
                doc_status = st
            try:
                html = (await resp.text()) if resp is not None else ""
            except Exception:
                html = ""
            if doc_status is not None:
                # Status always; body only for an error status (small, and
                # the only kind that can carry "challenge_required").
                sig = note_logged_out(doc_status, html[:4000] if doc_status >= 400 else None)
                if sig.kind in (THROTTLED, CHALLENGED):
                    pushed.append(sig)

            # A person's pause on the page: lets it render for the
            # screenshot and the text checks below, and lets anything the
            # page fetches on its own land. Scaled by the pacing governor.
            await page.wait_for_timeout(_jitter_ms(1500))

            if not html:
                try:
                    html = await page.content()
                except Exception:
                    html = ""
            body = ""
            try:
                body = await page.inner_text("body")
            except Exception:
                pass

            raw_user: Optional[dict] = None
            try:
                raw_user = logged_out_user(json_scripts(html), wanted)
            except Exception:
                raw_user = None
            if raw_user is None:
                try:
                    scripts = await page.evaluate(JS_JSON_SCRIPTS) or []
                    raw_user = logged_out_user(
                        [s for s in scripts if isinstance(s, str)], wanted)
                except Exception:
                    raw_user = None
            user: Optional[InstagramUser] = None
            if raw_user is not None:
                try:
                    user = user_from_logged_out(raw_user)
                except Exception:
                    user = None
            if user is None and found:
                user = max(found, key=lambda u: (u.followers is not None, bool(u.avatar)))

            if user is None:
                return self._classify_logged_out_miss(
                    row, page.url, doc_status, html, body, pushed)

            # ── the profile was on the page ──
            self.fill(row, user)
            _retag_logged_out(row)
            row.mark("access", ACCESS_LOGGED_OUT)
            row.note("read without login, from the public profile page")
            private = user.private

            # What the JSON withheld, from the page's own description line.
            # Its counts are rounded ("1.2M"), so they are recorded as such.
            counts = meta_counts(html)
            if row.followers is None and "followers" in counts:
                row.followers, exact = counts["followers"]
                row.followers_exact = "yes" if exact else "no"
                row.mark("followers", f"{ACCESS_LOGGED_OUT}-description")
            if row.friends is None and "following" in counts:
                row.friends = counts["following"][0]
                row.mark("friends", f"{ACCESS_LOGGED_OUT}-description")
            if user.posts is None:
                # The exact post count is hidden from a logged-out visitor
                # (`all_media_count` is null -- seen live 2026-11-17). Whether
                # there ARE posts is what the activity rules need, and the
                # description line says so ("8,649 Posts"); failing that,
                # the page's own embedded posts do.
                if "posts" in counts:
                    row.posts_seen = "yes" if counts["posts"][0] > 0 else "no"
                    row.mark("posts", f"{ACCESS_LOGGED_OUT}-description")
                elif (timeline_edge_count(raw_user) or 0) > 0:
                    row.posts_seen = "yes"
                    row.mark("posts", ACCESS_LOGGED_OUT)

            # THE LAST-POST DATE, from the page's own embedded posts. Their
            # nodes carry no timestamp logged-out (seen live: no `taken_at`),
            # but each carries Instagram's accessibility caption -- "Video by
            # Instagram on November 17, 2026. May be ..." -- the same
            # sentence the logged-in grid tier reads off its tiles, read here
            # by the same owner-checked parser. Never over a known "no posts",
            # never for a private account (it shows none).
            if not row.last_post_iso and not private and row.posts_seen != "no":
                dated = self._timeline_alt_dates(raw_user, (wanted, row.profile_name))
                found_dates = [d for d in dated if d]
                if found_dates:
                    row.last_post_iso = max(found_dates)
                    row.posts_seen = "yes"
                    row.mark("last_post", f"{ACCESS_LOGGED_OUT}-alt-text")
                    if not all(dated[:3]):
                        row.note("last-post date read from dated posts only: one of the "
                                 "newest carries no date in its caption, so a newer post "
                                 "may exist")

            # A date the page fetched by itself, owner-checked, under the
            # same no-posts guard the logged-in path applies.
            date, why = self.timeline_verdict(
                row.posts_seen, bool(row.last_post_iso), post_dates)
            if date:
                row.last_post_iso = date
                row.posts_seen = "yes"
                row.mark("last_post", f"{ACCESS_LOGGED_OUT}-xhr")
            if why:
                row.note(why)
            if not row.last_post_iso and not private and row.posts_seen != "no":
                row.note("last-post date not read: the logged-out page carried no "
                         "dated post, and a no-login read opens no second page to "
                         "find one -- left blank, not guessed")

            await self.screenshot(page, row)
            row.status = "OK"
            return row
        finally:
            try:
                await page.close()
            except Exception:
                pass

    def _classify_logged_out_miss(
        self, row: Row, final_url: str, doc_status: Optional[int], html: str,
        body: str, pushed: list,
    ) -> Row:
        """A logged-out page that did not give us the profile: refused, or
        genuinely gone? REFUSED unless the page proves gone -- see
        `process_logged_out` for the evidence GONE needs and why."""
        if url_path(final_url).startswith("/accounts/login"):
            return self._refused(row, NL_LOGIN_WALL,
                                 "Instagram sent the logged-out visitor to its login page")
        if is_wall_url(final_url, ("challenge",)):
            if not pushed:
                governor_for("instagram").record(Signal(
                    CHALLENGED, "logged-out visit sent to a challenge page"))
            return self._refused(row, NL_CHALLENGE,
                                 "Instagram sent the logged-out visitor to a challenge page")
        if doc_status is not None and doc_status >= 400 and doc_status != 404:
            kind = (NL_THROTTLED if doc_status == 429
                    else NL_UNREACHABLE if doc_status >= 500 else NL_LOGIN_WALL)
            return self._refused(row, kind, f"the page answered HTTP {doc_status}")
        if pushed:
            sig = pushed[-1]
            return self._refused(
                row, NL_CHALLENGE if sig.kind == CHALLENGED else NL_THROTTLED,
                f"Instagram pushed back on this visit ({sig.reason})")
        if _RE_THROTTLE_PAGE.search(body):
            governor_for("instagram").record(Signal(
                THROTTLED, "logged-out error page: something went wrong / try again later"))
            return self._refused(row, NL_THROTTLED,
                                 "Instagram served its error / try-again-later page")
        error_page = _GONE_MARKER in (html or "") and not meta_description(html)
        if error_page and RE_GONE.search(body):
            row.status = "GONE"
            row.mark("access", ACCESS_LOGGED_OUT)
            row.note("removed or unavailable -- Instagram's own \"page isn't available\" "
                     "page, seen without login" + (" (HTTP 404)" if doc_status == 404 else ""))
            return row
        if error_page:
            # Instagram's error page, but not in words that settle it. See
            # NL_ERROR_PAGE: an account looks, and no-login carries on.
            return self._refused(
                row, NL_ERROR_PAGE,
                "Instagram showed a logged-out visitor its error page for this profile "
                "without saying it is gone (removed, or hidden from visitors who are not "
                "signed in) -- an account can tell which")
        if RE_LOGIN.search(body):
            return self._refused(row, NL_LOGIN_WALL,
                                 "the page showed Instagram's login prompt and no profile data")
        return self._refused(
            row, NL_NO_DATA,
            "the page loaded" + (f" (HTTP {doc_status})" if doc_status else "")
            + " but carried no profile data -- what Instagram serves a logged-out "
              "visitor it is limiting")

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
            if row.status == BLOCKED_LOGGED_OUT:
                # Instagram is refusing this machine's logged-out visits;
                # every further one would be refused too and would only make
                # the refusal last longer. The rest is for an account.
                from backend.shared.logging import get_logger as _gl
                _gl("platforms.instagram.analysis").warning(
                    "logged-out visit refused -- stopping the no-login run.")
                break
            if i < len(jobs):
                await self.pause()
        return rows
