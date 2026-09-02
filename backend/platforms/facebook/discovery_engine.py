"""Facebook discovery engine: search, crawling, pagination, profile/URL
extraction, keywords in, candidate profile URLs out.

Also owns the browser session (login/checkpoint detection) and URL/identity
normalization: both are produced here first and re-used by
analysis_engine.py, which imports them rather than redefining them, so
there is exactly one definition of each across the two files.

COMPLETENESS
    Three independent stopping signals, and the run records which one fired:
      * has_next_page = false          -- Facebook says there is no more
      * is_end_of_serp = true          -- the results feed is exhausted
      * no new ids after `--patience` scrolls
    Anything else (a cap, an error) is reported as an incomplete sweep rather
    than being silently treated as the end.

    People, Pages, and Groups are all held to the SAME rule, see _tab_cap:
    a tab runs to one of the three signals above (no result-count cap at
    all) unless the client has configured an explicit cap for that exact
    (tab, individual/domain keyword-type) combination, in which case it
    stops at EXACTLY that number, never fewer and never more (the
    per-response check in absorb() plus the post-backfill trim near the end
    of sweep() both enforce this, not just the coarser between-scrolls
    check). A common name can otherwise keep serving loosely-matching
    profiles well past anything an analyst would act on. Pages/Groups
    results tend to run out on their own for any real keyword, but nothing
    stops an analyst from capping them too if a particular keyword proves
    noisy there as well.

    The cursor also carries `result_ids_shown`: every id Facebook has rendered
    so far. After the sweep those are reconciled against what was extracted,
    and any id we never saw as an edge is backfilled from its id alone. That
    is the guard against a layout change quietly dropping results.

SPEED
    Pagination is cursor-driven, so pages must be fetched in order, but the
    scanner waits on the pagination response itself rather than sleeping, and
    images are never downloaded. Different keywords are independent, so they
    run in parallel tabs.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, quote, urlparse

from backend.shared.extraction import ExtractionResult, run_strategies
from backend.shared.avatars import looks_like_placeholder
from backend.shared.models.hit import Hit, hit_to_row
from backend.shared.models.row import Row
from backend.shared.text import iter_dicts, normalized_host, parse_normalized_url
from backend.stealth.browser import Session

# Session / login state

ME = "https://www.facebook.com/me"

# Matches the classic logged-out pages only. It deliberately does NOT try
# to match Meta's modern account-chooser wall ("Explore the things you
# love." over a Continue/Use another profile/Create new account card),
# which is what a dead Facebook session is actually served now, c_user
# still names the account, so the wall even greets it by name, while xs no
# longer authenticates anything. That wall says "Log in" only as a
# two-word link label and none of the three phrases here, which is exactly
# how it walked past this check (confirmed live 2026-08-12: /me, /settings,
# /friends, /bookmarks and /notifications all rendered it, and all five
# were reported healthy). Catching it is FacebookSession.check_session's
# `deny_paths` job instead, because this regex is not only a session
# signal, analysis_engine.py and _resolve_missing below both run it over
# a visited profile page to decide LOGIN_REQUIRED, so a string that also
# occurs anywhere in logged-in chrome would mark real profiles unreadable.
RE_LOGIN = re.compile(r"(You must log in|Log in to Facebook|Log In or Sign Up)", re.I)
RE_CHECKPOINT = re.compile(
    r"(checkpoint|suspicious activity|Confirm Your Identity|"
    r"account has been locked|We've temporarily)",
    re.I,
)
RE_GONE = re.compile(
    r"(isn't available|Page Not Found|content is currently unavailable)", re.I
)

# real profile photos live on scontent*.fbcdn.net; rsrc.php / static.xx is
# Facebook's own chrome, which is where the silhouette placeholder comes from.
# Shared with analysis_engine.py: discovery uses it to decide has_custom_pic
# on a fresh Hit, analysis uses it again once it re-visits the profile.
#
# TWO ALTERNATIVES THAT LOOK RIGHT AND ARE NOT -- do not re-add either:
#
#   * a bare `30497-1`: a 7-digit substring match with no surrounding
#     context, which can collide with digits appearing incidentally in a
#     real photo's URL (asset ids, cache-busting tokens, size params).
#   * `t1\.30497-1`: verified wrong against live data. That tag is just a
#     different CDN rendering-context for a genuine upload -- a
#     profile-PAGE visit serves the SAME real photo under it that a search
#     result serves as `t39.30808-1`/`t1.6435-1`. Matching it discards the
#     photo URL outright (see _extract_entity: a match means the uri is
#     never stored), which is much of why a resolved profile-page visit
#     returned no picture despite finding the right name.
RE_DEFAULT_PIC = re.compile(
    r"(static.*silhouette|default.*avatar|/rsrc\.php/|static\.xx\.fbcdn\.net)",
    re.I,
)


class FacebookSession(Session):
    """The Facebook-specific half of `Session` (backend/stealth/browser.py):
    owns nothing but the one thing that differs per platform, whether a
    cookie set is still logged in. Loaded dynamically by
    backend/platforms/registry.py's `session_path` entry for "facebook",
    and constructed by session_for_job() (backend/sessions/manager.py)
    whenever a discovery or analysis job needs a live Facebook browser
    context."""

    # Fetch images for real against Meta. Measured: a Facebook profile visit
    # requests 74 images from scontent.*.fna.fbcdn.net / static.xx.fbcdn.net,
    # 29% of all its traffic, and stubbing them locally means Meta's own CDN
    # logs show a logged-in session that pulled the page and every script but
    # never one image byte. See Session.ALWAYS_LOAD_IMAGES for the full
    # measurement, including why the speed argument against this is wrong
    # (+0.1s per visit).
    ALWAYS_LOAD_IMAGES = True

    async def check_session(self) -> bool:  # type: ignore[override]
        """WHAT: are these cookies still logged in? HOW: visits /me (an
        authenticated-only destination) and asks the shared
        Session.check_session() to confirm the browser landed on an
        account page rather than a login/checkpoint wall. LINKED TO:
        stealth/browser.py::Session.check_session does the work;
        sessions/manager.py::verify_session_item consumes the verdict.

        deny_paths is the positive confirmation here, the job expect_path
        does for Instagram/TikTok. /me cannot use expect_path because its
        authenticated destination is the account's OWN profile path, which
        differs per account. Logged out it lands on the site root (/me) or
        on /index.php?next=... (every other authenticated path), and
        neither is somewhere a logged-in /me can end up.

        Without it a logged-out session reports healthy, every search it
        then runs is served a bare 404, and both discovery strategies
        correctly find nothing -- which reads identically to a GraphQL
        doc-id rotation and sends the investigation at the parsers instead
        of the cookies."""
        return await super().check_session(
            ME, RE_LOGIN, RE_CHECKPOINT, deny_paths=("/", "/index.php"),
        )


# URL / identity


def normalize_url(url: str) -> str:
    """WHAT: one canonical `https://www.facebook.com/...` form for any
    Facebook URL variant (fb.com, fb.me, m.facebook.com, with or without a
    scheme). HOW: delegates the generic host/path parsing to
    shared/text.py::parse_normalized_url, then folds every Facebook host
    onto "www.facebook.com". LINKED TO: the one normalizer both this file
    and analysis_engine.py use for every Facebook URL they touch --
    analysis_engine.py imports this directly rather than redefining it, so
    there is exactly one definition across both phases (see this module's
    own top-of-file docstring)."""
    p = parse_normalized_url(url)
    if p is None:
        return ""
    host = normalized_host(p)
    if "facebook" in host or host in {"fb.com", "fb.me"}:
        host = "www.facebook.com"
    q = f"?{p.query}" if p.query else ""
    return f"https://{host}{p.path}{q}"


def profile_id(url: str) -> str:
    """Returns the numeric id or the vanity slug, always as a string.

    Normalises first: without a scheme, urlparse puts the host in the path and
    the first segment comes back as "facebook.com".
    """
    url = normalize_url(url)
    if m := re.search(r"profile\.php\?id=(\d+)", url):
        return m.group(1)
    if m := re.search(r"/people/[^/]+/(\d+)", url):
        return m.group(1)
    if m := re.search(r"/groups/(\d+)", url):
        return m.group(1)
    seg = [s for s in urlparse(url).path.split("/") if s]
    if not seg:
        return ""
    first = seg[0].split("?")[0]
    bad = {"pages", "groups", "profile.php", "people", "watch", "reel", "share"}
    return "" if first.lower() in bad else first


def tab_url(base: str, sk: str) -> str:
    """WHAT: the URL for one sub-tab (`sk`, e.g. "about", "friends") of the
    profile `base` points at. HOW: profile.php ids take the sub-tab as a
    `&sk=` query param; a vanity-slug profile takes it as a path segment.
    LINKED TO: analysis_engine.py's Scraper.process(), which visits the
    About and About-transparency tabs this way after the main timeline."""
    p = urlparse(base)
    if "profile.php" in p.path:
        uid = parse_qs(p.query).get("id", [""])[0]
        return f"https://www.facebook.com/profile.php?id={uid}&sk={sk}"
    return f"https://www.facebook.com{p.path.rstrip('/')}/{sk}"


# fbcdn signs the whole crop range up to `cstp`'s bound, not the specific
# `ctp` size actually requested, every profile-picture URL Facebook hands
# out (search snippet or profile header alike) asks for a tiny ctp (40-60px)
# while cstp carries the real uploaded photo's native size. Raising ctp to
# match cstp, with the same signature tokens untouched, is what actually
# yields the full-resolution upload rather than the thumbnail, verified
# live: a 50x50 crop (2.8KB) vs. the same URL bumped to 638x638 (33KB), both
# 200 OK, no new request needed.
_CTP = re.compile(r"ctp=s\d+x\d+")
_CSTP = re.compile(r"cstp=mx(\d+)x(\d+)")
_STP_SIZE = re.compile(r"([sp])\d+x\d+")


def hd_picture_url(url: str) -> str:
    """WHAT: the same fbcdn photo URL, rewritten to request the full
    upload resolution instead of the tiny thumbnail every profile-picture
    field hands out by default. HOW: see the two module-level comments
    just above (_CTP/_CSTP/_STP_SIZE) for the exact crop-parameter
    rewrite and the live measurement behind it. LINKED TO: iter_results
    and _extract_entity below, discovery's two photo-reading paths."""
    if not url:
        return url
    cstp = _CSTP.search(url)
    if not cstp:
        return url
    w, h = cstp.group(1), cstp.group(2)
    if _CTP.search(url):
        url = _CTP.sub(f"ctp=s{w}x{h}", url)
    url = _STP_SIZE.sub(lambda m: f"{m.group(1)}{w}x{h}", url)
    return url


# Search payload parsing (profile extraction)
# Reading search results out of Facebook's own search payloads.
#
# WHY NOT SCRAPE THE LINKS OFF THE PAGE
#     A search page is full of profile links that are not results: the chat
#     sidebar, the notification flyout, "people you may know". Harvesting
#     a[href*=facebook.com] on the People tab returned 52 ids that were not
#     results at all. Results live in one place, the edges of the search
#     connection, and that is the only place this reads.
#
# WHERE THE RESULTS ARE
#     data.serpResponse.results.edges[]
#         .rendering_strategy.view_model            __typename SearchProfileViewModel
#             .profile                              __typename User | Page
#                 id, name, profile_url, url

VIEW_MODELS = {"SearchProfileViewModel"}
# LIVE-CONFIRMED (2026-08-11, real search/groups/?q= response): a Group
# result rides through the exact same SearchProfileViewModel/.profile
# shape as User/Page, just with __typename "Group" and profile_url already
# pointing at /groups/<id>/, no separate parsing branch needed, this
# dict entry is the whole difference.
ENTITY_TYPES = {"User": "profile", "Page": "page", "Group": "group"}
QUERY_NAME = "SearchCometResultsPaginatedResultsQuery"


# `Hit` and `hit_to_row` live in shared/models/hit.py, not here -- YouTube's
# and TikTok's discovery engines use the exact same shape for their own,
# unrelated sweeps, and importing a type out of Facebook's own file was the
# wrong home for something three platforms depend on. Facebook itself still
# uses it constantly below (id-backfill, reconciliation, the DOM fallback)
# -- only the definition moved, not the usage.


@dataclass
class PageState:
    """What one pagination response says about how far the results go.

    `ids_shown` is what Facebook rendered. `ids_processed` is the wider set the
    backend considered, on the Pages tab it held 32 ids while only 30 were
    ever rendered. Those extras are real entities the search matched but chose
    not to display, so discovery keeps them, tagged separately.
    """

    has_next: bool = False
    ids_shown: list[str] = field(default_factory=list)
    ids_processed: list[str] = field(default_factory=list)
    total_results: Optional[int] = None
    end_of_serp: bool = False


# the tab is authoritative about what was searched; __typename is not.
# Pages (and Groups) render through the same profile stack and can report
# themselves inconsistently depending on which fallback path resolved them
TAB_KIND = {"people": "profile", "pages": "page", "groups": "group"}


def kind_for_tab(tab: str) -> str:
    """WHAT: the entity_type a search TAB implies ("people" -> "profile",
    etc). HOW: a straight lookup in TAB_KIND above, defaulting to
    "profile" for an unrecognised tab rather than raising. LINKED TO:
    Discovery.sweep() (tags every Hit it finds) and dom_search_hits()
    below (the DOM fallback, same tagging)."""
    return TAB_KIND.get(tab, "profile")


def profile_url_for(entity_id: str, kind: str = "profile") -> str:
    """`profile.php?id=` only ever resolves a personal profile, visiting it
    with a Page's numeric id (Pages use plain https://facebook.com/<id> or
    their vanity slug) lands on Facebook's own "content isn't available"
    page, not the Page. Every candidate this fires for then reads as
    blocked/unresolved forever, permanently showing the bare numeric id as
    its name (see _resolve_missing/sweep()'s missing+unshown backfill,
    which both route through here), this is the actual cause of a
    Facebook Pages-tab card stuck showing its raw id/no photo no matter how
    many times it's re-swept.
    """
    if kind == "page":
        return f"https://www.facebook.com/{entity_id}"
    if kind == "group":
        return f"https://www.facebook.com/groups/{entity_id}/"
    return f"https://www.facebook.com/profile.php?id={entity_id}"


def iter_results(blob: Any) -> Iterator[Hit]:
    """Every profile/page result in one pagination payload, in page order."""
    for edge_holder in iter_dicts(blob):
        edges = edge_holder.get("edges")
        if not isinstance(edges, list):
            continue
        for i, edge in enumerate(edges):
            if not isinstance(edge, dict):
                continue
            vm = (edge.get("rendering_strategy") or {}).get("view_model")
            if not isinstance(vm, dict) or vm.get("__typename") not in VIEW_MODELS:
                continue
            prof = vm.get("profile")
            if not isinstance(prof, dict):
                continue
            eid = prof.get("id")
            if not (isinstance(eid, str) and eid.isdigit()):
                continue
            url = prof.get("profile_url") or prof.get("url") or profile_url_for(eid, ENTITY_TYPES.get(prof.get("__typename"), "profile"))
            pic = prof.get("profile_picture")
            raw_uri = pic.get("uri", "") if isinstance(pic, dict) else ""
            has_custom = bool(raw_uri) and not looks_like_placeholder("facebook", raw_uri)
            avatar = hd_picture_url(raw_uri) if has_custom else ""

            verified = bool(
                prof.get("is_verified")
                or prof.get("verification_status") == "VERIFIED"
                or prof.get("is_profile_verified")
            )

            yield Hit(
                entity_id=eid,
                name=(prof.get("name") or "").strip(),
                url=url.split("?__")[0],
                avatar=avatar,
                has_custom_pic=has_custom,
                verified=verified,
                entity_type=ENTITY_TYPES.get(prof.get("__typename"), "profile"),
                rank=i,
            )


def page_state(blob: Any) -> Optional[PageState]:
    """The pagination cursor for the search connection, if this payload has one.

    Facebook puts a page_info on several connections per response (the
    notification dropdown has one too), so this only accepts a cursor that
    decodes to a search cursor, one carrying result_ids_shown.
    """
    for d in iter_dicts(blob):
        pi = d.get("page_info")
        if not isinstance(pi, dict) or "has_next_page" not in pi:
            continue
        cursor = pi.get("end_cursor")
        if not isinstance(cursor, str):
            continue
        try:
            c = json.loads(cursor)
        except (json.JSONDecodeError, ValueError):
            continue
        if "result_ids_shown" not in c:
            continue
        totals = c.get("unit_id_logging_fields") or {}
        return PageState(
            has_next=bool(pi["has_next_page"]),
            ids_shown=[str(i) for i in (c.get("result_ids_shown") or [])],
            ids_processed=_processed_ids(c),
            total_results=totals.get("num_total_results"),
            end_of_serp=bool(c.get("is_end_of_serp")),
        )
    return None


def _processed_ids(cursor: dict) -> list[str]:
    """Ids the backend matched, including ones it never rendered.

    They sit in the per-tab flow cursor, which is itself a JSON string.
    """
    out: list[str] = []
    for v in (cursor.get("flow_cursors_serialized") or {}).values():
        if not isinstance(v, str) or "processed_unicorn_ids" not in v:
            continue
        try:
            inner = json.loads(v)
        except (json.JSONDecodeError, ValueError):
            continue
        out += [str(i) for i in (inner.get("processed_unicorn_ids") or [])]
    out += [str(i) for i in (cursor.get("processed_unicorn_ids") or [])]
    return out


def is_search_response(post_body: str) -> bool:
    """WHAT: does this /api/graphql request's own POST body identify it as
    a search-results query (as opposed to the dozen other unrelated
    GraphQL calls a Facebook page fires on every load -- notifications,
    chat, the sidebar). HOW: a plain substring check against the query's
    own name, QUERY_NAME above. LINKED TO: Discovery.sweep()'s
    on_response() handler, the response filter that keeps the sweep from
    trying to parse irrelevant GraphQL traffic as search results."""
    return QUERY_NAME in (post_body or "")


def parse_lines(text: str) -> Iterator[Any]:
    """Search responses stream as newline-delimited JSON chunks."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue


RE_EMBEDDED = re.compile(r"^\s*\{")


def parse_embedded(texts) -> Iterator[Any]:
    """The first page of results is server-rendered, not fetched over XHR."""
    for t in texts or []:
        if not t or "SearchProfileViewModel" not in t:
            continue
        if not RE_EMBEDDED.match(t):
            continue
        try:
            yield json.loads(t)
        except (json.JSONDecodeError, ValueError):
            continue


# Crawling / pagination
# Searches the People and Pages tabs for each keyword and collects every
# result Facebook will serve, then hands the URLs to the analysis phase.

TABS = {
    "people": "https://www.facebook.com/search/people/?q={q}",
    "pages": "https://www.facebook.com/search/pages/?q={q}",
    "groups": "https://www.facebook.com/search/groups/?q={q}",
}

# People search is effectively unbounded. Facebook keeps serving pages of
# loosely-matching profiles long past anything useful for a common keyword.
# A default hard cap was originally enforced, but has been removed to allow
# endless discovery matching the analyst's manual process, unless explicitly
# capped by the client configuration. Pages and Groups are naturally small,
# finite result sets for any real keyword, so the same rule, "capped only
# when the client configured a number for THIS exact tab", applies to all
# three identically; see _tab_cap.


def _tab_cap(opts) -> int:
    """The result cap this sweep was configured with, resolved per
    (keyword-type, tab) by discovery_service.py's cap grouping and baked
    into `opts.max_results` before a discoverer is even constructed (see
    _sweep_platform's `_options_for`). 0 means uncapped, scrape until one
    of the three natural completeness signals fires (has_next_page=false,
    end_of_serp, or no new ids after `--patience` scrolls), for every tab,
    not just Pages/Groups; People simply tends to hit that point far later
    for a common name."""
    return int(getattr(opts, "max_results", 0) or 0)


JS_EMBEDDED = (
    "() => Array.from(document.querySelectorAll("
    "'script[type=\"application/json\"]')).map(s => s.textContent)"
    ".filter(t => t && t.length > 40)"
)


async def _notify(on_progress, found_count: int, page_num: int, new_hits: list) -> None:
    """Best-effort progress callback, a broken callback must never abort
    the scrape itself, so any exception here is swallowed by the caller."""
    if asyncio.iscoroutinefunction(on_progress):
        await on_progress(found_count, page_num, new_hits)
    else:
        on_progress(found_count, page_num, new_hits)


# DOM fallback
# `iter_results` reads the /api/graphql search response, which is the rich
# path and the one this engine is built around. Facebook rotates its search
# doc ids on its own schedule, and when it does the parser recognises no
# edges, `by_id` stays empty, and the sweep reports 0 hits as a clean
# success, silent, and indistinguishable from "nobody matches".
#
# The id-backfill path already here is NOT a substitute: it works from ids
# Facebook mentioned in a payload we could still parse, so it fails in the
# same breath as the payload parser it depends on.
#
# This reads profile links straight off the rendered results page, which
# keeps working when the payload shape moves. Anchors are the target
# because they are the one thing a results page cannot obfuscate away: a
# result you can click is a link to a profile.
JS_DOM_SEARCH_HITS = """
() => {
  const out = [];
  const seen = new Set();
  const RESERVED = /^(friends|groups|pages|marketplace|watch|gaming|events|settings|policies|help|privacy|terms|login|reg|search|photo|photos|notes|bookmarks|business|ads|notifications|messages|me|profile\\.php)$/i;

  // Scope to the results region only. Scanning the whole document also
  // sweeps up the chrome around it -- the notifications dropdown alone
  // contributed "See all -> /notifications/" and several "You have a new
  // friend" rows, each a perfectly well-formed profile link that is not a
  // search result. Verified against the live page.
  const root = document.querySelector('[role="main"]') || document.body;

  for (const a of root.querySelectorAll('a[href]')) {
    let href = a.getAttribute('href') || '';
    if (!href) continue;
    if (href.startsWith('/')) href = 'https://www.facebook.com' + href;
    if (!href.includes('facebook.com')) continue;
    // notification permalinks are profile links wearing tracking params,
    // real, but not results of this search
    if (/[?&](notif_id|notif_t|ref=notif)/.test(href)) continue;

    let id = '', clean = '';
    const byId = href.match(/[?&]id=(\\d+)/);
    // a real group result is /groups/<numeric id>/ -- two path segments,
    // checked BEFORE the RESERVED filter below (which exists to reject the
    // bare /groups directory-nav link, a single segment with no id; this
    // is the opposite shape and IS a genuine result)
    const groupMatch = href.match(/facebook\\.com\\/groups\\/(\\d+)\\/?(?:$|[?#])/);
    if (byId) {
      id = byId[1];
      clean = 'https://www.facebook.com/profile.php?id=' + id;
    } else if (groupMatch) {
      id = groupMatch[1];
      clean = 'https://www.facebook.com/groups/' + id + '/';
    } else {
      const m = href.match(/facebook\\.com\\/([A-Za-z0-9.\\-_]+)\\/?(?:$|[?#])/);
      if (!m) continue;
      if (RESERVED.test(m[1])) continue;
      id = m[1];
      clean = 'https://www.facebook.com/' + id;
    }
    if (!id || seen.has(id)) continue;

    // the visible label of the result row -- an anchor wrapping an avatar
    // has no text of its own, so fall back to the row around it
    let name = (a.innerText || '').trim().split('\\n')[0];
    if (!name) {
      const row = a.closest('div[role="article"]') || a.parentElement;
      if (row) name = (row.innerText || '').trim().split('\\n')[0];
    }
    name = (name || '').replace(/\\uFEFF/g, '').trim();
    if (!name) continue;
    // UI affordances that live inside the results region
    if (/^(see all|see more|view all|add friend|follow|message)$/i.test(name)) continue;

    const img = a.querySelector('img') ||
                (a.parentElement && a.parentElement.querySelector('img'));
    seen.add(id);
    out.push({
      entity_id: id,
      name: name.slice(0, 120),
      url: clean,
      avatar: img ? (img.getAttribute('src') || '') : '',
    });
  }
  return out;
}
"""


async def dom_search_hits(page, keyword: str, tab: str) -> list["Hit"]:
    """Scrape the rendered search results page. Only runs when the GraphQL
    payload produced nothing at all, see Discovery.sweep."""
    rows = await page.evaluate(JS_DOM_SEARCH_HITS)
    kind = kind_for_tab(tab)
    hits: list[Hit] = []
    for i, r in enumerate(rows or []):
        eid = (r.get("entity_id") or "").strip()
        url = (r.get("url") or "").strip()
        if not eid or not url:
            continue
        avatar = (r.get("avatar") or "").strip()
        hits.append(Hit(
            entity_id=eid,
            name=(r.get("name") or "").strip(),
            url=url,
            avatar=avatar,
            has_custom_pic=bool(avatar) and not looks_like_placeholder("facebook", avatar),
            entity_type=kind,
            keyword=keyword,
            tab=tab,
            rank=i,
            source="dom",
        ))
    return hits


@dataclass
class Sweep:
    """One keyword on one tab, and how the search ended."""

    keyword: str
    tab: str
    hits: list[Row] = field(default_factory=list)
    pages: int = 0
    stopped: str = ""  # exhausted | end-of-serp | stalled | cap | error
    complete: bool = False
    reported_total: Optional[int] = None
    backfilled: int = 0
    unshown: int = 0
    seconds: float = 0.0
    error: str = ""
    # "graphql" normally; "dom" when the search payload yielded nothing and
    # the rendered results page had to stand in
    source: str = "graphql"
    extraction: Optional["ExtractionResult"] = None

    def summary(self) -> str:
        """One-line log form. Reports `backfilled` and `unshown` counts
        separately because they mean different things: backfilled hits
        needed a second lookup to become usable, and matched-but-unshown
        are results Facebook counted but never rendered -- a gap between
        `reported_total` and what was actually harvested is the signal
        that a sweep is being throttled rather than exhausted."""
        note = f"{len(self.hits)} hits, {self.pages} pages, {self.stopped}"
        if self.backfilled:
            note += f", {self.backfilled} backfilled"
        if self.unshown:
            note += f", {self.unshown} matched-but-unshown"
        if self.reported_total is not None and self.reported_total != len(self.hits):
            note += f", facebook counted {self.reported_total}"
        return note


# ids Facebook's search connection matched but never rendered as a full
# edge (see `missing`/`unshown` in sweep()) get one cheap profile-page
# visit each to recover a name/photo. Deliberately uncapped BY COUNT: a
# count-based cap here (this used to be `ids[:60]`) silently left every
# candidate past the cutoff showing whatever incomplete/blank data the
# search snippet happened to return, and since `ids` is sorted, it was
# the SAME candidates left broken on every single re-sweep, forever, not a
# random sample. A card must never show something a normal user browsing
# Facebook wouldn't see.
#
# Concurrency, though, deliberately stays LOW, this is not a plain
# performance knob, it's request pacing under the same live session as
# everything else this module does (see settings.py's discovery_concurrency:
# "the single most important knob for staying unremarkable"). A first pass
# at this fix raised it 3->8 to compensate for removing the count cap; that
# was a mistake, more simultaneous page visits under one account is
# exactly the kind of burst activity the rest of this codebase is built to
# avoid, and it's what let a single sweep's resolve phase run long enough
# to starve a second keyword's sweep of its own turn (see RESOLVE_TIME_
# BUDGET_SEC below for how that's actually addressed instead). Restored to
# a modest step up from the original, not a multiplier.
RESOLVE_CONCURRENCY = 4

# The pagination loop above has its own max_seconds budget; this phase
# didn't, so removing the count cap could in principle let ONE sweep's
# resolve step run for as long as its (now possibly hundreds of candidates,
# only RESOLVE_CONCURRENCY at a time) reconciliation takes. Since a sweep
# holds one of the (deliberately few) outer discovery-concurrency slots for
# its entire duration, an unbounded resolve phase could starve a later
# keyword's sweep of ever getting a turn, which reads exactly like "the
# second keyword was never searched", even though it's actually just queued
# behind a resolve step still running. This is a genuine best-effort time
# budget, not a count truncation: whatever hasn't been resolved when it
# elapses keeps its already-known (possibly blank) data for THIS sweep, the
# same graceful "blank beats wrong" degradation this function already
# documents, and unlike the old count cap, WHICH candidates get skipped
# isn't the same fixed set every time (real-world timing varies), so a
# later re-sweep has a genuinely different chance at any of them, not a
# permanently-doomed subset.
RESOLVE_TIME_BUDGET_SEC = 180

# How long ONE profile-page visit may wait for the profile's own data to
# actually render before giving up on it and extracting whatever arrived.
#
# This replaced a flat `await page.wait_for_timeout(1200)`. A fixed sleep is
# a race, not a wait: it says "1.2 seconds is how long Facebook takes",
# which is true on a fast connection and false on a slow one, on a heavy
# profile, or when the avatar is lazy-loaded a moment after first paint.
# Whenever the page needed longer, extraction ran against a half-built DOM
# with no GraphQL payload for the entity yet, so the candidate resolved
# blank and the card kept showing its bare numeric id, even though the same
# profile opened by hand a second later plainly showed a name and photo.
# That is exactly the "I can see the logo and username when I open it"
# complaint, and it is a TIMING failure, not a parsing one.
#
# The fix is to wait for the actual signal (JS_PROFILE_READY below) rather
# than for the clock, with this as the ceiling. Waiting for a real
# condition also means the common fast case returns as soon as the data is
# there, typically quicker than the old flat 1.2s, so a much more
# generous ceiling costs nothing on profiles that load normally.
RESOLVE_SETTLE_SEC = 12

# The condition that actually matters on a profile page: EITHER the page
# has committed to an identity (canonical/og:url, what the identity gate
# below reads) OR it has rendered at least one real fbcdn image (what the
# DOM fallback wants) OR the page has embedded a real application/json
# payload (what _extract_entity reads). Any one of these means there is
# now something worth extracting.
#
# Originally AND-chained on canonical being present first. That was wrong,
# confirmed live: some profile-page renders never set a canonical link or
# og:url meta tag at all, not a load failure, just a render variant,
# so requiring it meant this wait ALWAYS ran out its full
# RESOLVE_SETTLE_SEC ceiling on those pages even though the actual data
# (name, photo, embedded JSON) had rendered in well under a second. With
# RESOLVE_CONCURRENCY workers all doing this, that wasted ceiling per
# candidate was large enough to starve the batch's own RESOLVE_TIME_
# BUDGET_SEC before later candidates ever got a turn.
JS_PROFILE_READY = """
() => {
    const url = (document.querySelector('link[rel="canonical"]')?.href)
        || (document.querySelector('meta[property="og:url"]')?.content) || '';
    if (url) return true;
    for (const im of document.querySelectorAll('svg image')) {
        const href = im.getAttribute('xlink:href') || im.getAttribute('href') || '';
        if (href.includes('fbcdn')) return true;
    }
    for (const img of document.querySelectorAll('img')) {
        if ((img.getAttribute('src') || '').includes('fbcdn')) return true;
    }
    for (const s of document.querySelectorAll('script[type="application/json"]')) {
        if ((s.textContent || '').length > 200) return true;
    }
    return false;
}
"""

# Chrome/placeholder strings that are never a real profile's own name:
# a login wall, checkpoint, or "Facebook" itself leaking through a loosely
# matched dict would otherwise read as a plausible (wrong) name. Mirrors
# analysis_engine.py's own GENERIC_NAMES guard; kept as a separate local
# copy rather than an import to avoid the circular dependency (see
# _extract_entity's docstring).
GENERIC_NAMES = {"facebook", "notifications", "log in to facebook"}

# `profile_picture` is what a SEARCH RESULT snippet uses (see iter_results,
# a completely different code path from this one, it reads straight off
# a known field position in the search response, never through this
# function at all, and has never shown any of the problems documented
# below across 500+ sampled real photos).
#
# A profile PAGE's own embedded JSON never carries that key at all in the
# same shape, the identical uploaded photo shows up under one of these
# instead when read via _extract_entity during a resolve visit.
PAGE_CONTEXT_PICTURE_KEYS = (
    "profile_picture", "profilePicLarge", "profilePicMedium", "profilePicSmall",
    "profile_picture_for_sticky_bar", "profilePhoto", "user_avatar",
)

# THE reason NONE of PAGE_CONTEXT_PICTURE_KEYS are safe to trust from a
# profile-PAGE resolve visit, confirmed via direct, reproducible live
# testing across three separate would-be defenses (each one individually
# insufficient, in order of discovery):
#
#   1. id-scoping alone, an id-scoped dict (id == eid) can still be a
#      viewer-relative "RenderedProfile" projection whose picture fields
#      describe what renders TO THE CURRENT VIEWER, not the profile's own
#      actual photo.
#   2. on_target_profile (canonical/og:url identity confirmation), some
#      profile pages never render a canonical link at all, so this can't
#      even be evaluated; and even when it IS confirmed correct, the
#      substitution still happens (see point 4).
#   3. RenderedProfile structural markers (__isRenderedProfile,
#      is_viewer_friend, wem_private_sharing_bundle, ...), confirmed the
#      SAME leaked photo can also appear in a dict with NONE of these
#      markers present, under the plain `profile_picture` key itself.
#   4. THE ACTUAL MECHANISM: this is not a scoping bug at all. For a
#      privacy-restricted profile (is_viewer_friend: false), Facebook's
#      client substitutes the VIEWER'S OWN photo into EVERY picture field
#      it renders for that profile. JSON, DOM, all of them, because it
#      genuinely cannot show a non-friend the real, restricted photo.
#      Being on the confirmed-correct page does not prevent this: identity
#      confirmation and privacy-restriction are orthogonal, and no
#      combination of the signals above reliably tells them apart from a
#      genuine, safe-to-trust photo in every case tested.
#
# Given no reliably positive signal was found after three real attempts,
# and every attempt was empirically caught leaking the scraper's own
# identity onto a candidate (the single worst class of bug this whole
# codebase is built to prevent, see the original identity-leak incident
# this module's docstrings already describe), this module follows its own
# stated rule to its logical conclusion: blank beats wrong, always, no
# exceptions. A profile-page resolve visit recovers NAME only. Avatar
# recovery for a candidate that needs it stays exactly what it's always
# been for a genuinely unresolvable profile: blank, with the UI's own
# entity_id/"Unnamed Profile" fallback rendering it honestly, not a
# fabricated, wrong photo that reads as a confirmed real impersonator when
# it's neither.
TRUST_PAGE_CONTEXT_AVATAR = False


def _extract_entity(blobs: list[Any], eid: str, *, trust_page_context_avatar: bool = TRUST_PAGE_CONTEXT_AVATAR) -> tuple[str, str, bool]:
    """The (name, avatar, has_custom_pic) for entity `eid` found across a
    profile page's own embedded/XHR JSON payloads, scoped by exact id
    match only, the same "an unverifiable id scopes to nothing" rule
    analysis_engine.py's Harvest.scoped() uses, so a page carrying other
    entities (suggested friends, sidebar widgets, sponsored content)
    can't leak a wrong name/photo onto this candidate: blank beats wrong,
    every time, because a wrong name here would surface as a false-
    positive impersonation match (a bogus high name-score badge) rather
    than the honest "no name available" it actually is.

    `trust_page_context_avatar` defaults OFF, see
    PAGE_CONTEXT_PICTURE_KEYS's docstring for why none of these keys are
    considered safe to read from a profile-page visit at all, regardless
    of id-scoping or identity confirmation. Overridable for tests that
    need to exercise the (currently unused in production) extraction path
    directly, and left in place rather than deleted so a FUTURE reliable
    positive signal can re-enable it without rebuilding the plumbing.

    Pure and synchronous on purpose, exercised directly in
    test_facebook_discovery.py against fixture payloads, no browser
    needed to catch a scoping regression.
    """
    name, avatar, has_custom = "", "", False
    for blob in blobs:
        for d in iter_dicts(blob):
            if d.get("id") != eid:
                continue
            n = d.get("name")
            if not name and isinstance(n, str) and n.strip() and n.strip().lower() not in GENERIC_NAMES:
                name = n.strip()
            if not avatar and trust_page_context_avatar:
                for key in PAGE_CONTEXT_PICTURE_KEYS:
                    pic = d.get(key)
                    if not isinstance(pic, dict):
                        continue
                    raw_uri = pic.get("uri", "")
                    if raw_uri and not RE_DEFAULT_PIC.search(raw_uri):
                        avatar = hd_picture_url(raw_uri)
                        has_custom = True
                        break
        if name and avatar:
            break
    return name, avatar, has_custom


def _entity_url_path(blobs: list[Any], eid: str) -> str:
    """The URL path this page's OWN payloads declare for entity `eid`, or "".

    Same exact-id scoping rule as _extract_entity: only a dict whose `id`
    IS `eid` is ever read, so this can't pick up a neighbouring entity's
    link. Exists for the identity gate in _resolve_missing: a Facebook
    PAGE reached via its numeric id redirects to its vanity URL, so the
    page's canonical (`/AdaniGroup`) doesn't textually contain the numeric
    eid at all and `profile_id(canonical)` returns the slug, not the id.
    Gating on canonical-vs-eid alone therefore rejected EVERY Page, which
    silently disabled the name/photo fallbacks for exactly the entity type
    that most needed them. Matching the canonical against the id-scoped
    link the payload itself publishes for `eid` confirms "this page is
    eid's page" just as strictly, without the unscoped guesswork the gate
    exists to prevent.
    """
    for blob in blobs:
        for d in iter_dicts(blob):
            if d.get("id") != eid:
                continue
            for key in ("profile_url", "url"):
                v = d.get(key)
                if isinstance(v, str) and v:
                    path = urlparse(normalize_url(v)).path.rstrip("/").lower()
                    if path and path not in ("", "/"):
                        return path
    return ""


_ASSET_ID_RE = re.compile(r"/(\d+_\d+_\d+_n\.\w+)")


def _photo_asset_id(url: str) -> str:
    """The stable filename component of an fbcdn photo URL, which stays
    constant across repeated fetches of the same underlying asset even
    though the surrounding signed query params (oh=, _nc_ohc=, ...) rotate
    per-request. Lets two URLs be compared for "is this the same photo"
    without false negatives from token rotation, see _resolve_missing's
    self-avatar cross-check, which needs exactly that comparison to catch
    the scraper's own photo leaking onto a candidate."""
    m = _ASSET_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _all_picture_asset_ids(blobs: list[Any]) -> set[str]:
    """Every photo asset id findable anywhere in `blobs`, under any known
    picture-carrying key, with NO id-scoping, used only to build the
    scraper's OWN self-photo fingerprint (see _self_avatar_assets), where
    the point is capturing every representation of "my own photo"
    regardless of which wrapper object it's nested under, not to attribute
    any of them to a particular entity (that's _extract_entity's job, and
    it stays strictly id-scoped)."""
    ids: set[str] = set()
    for blob in blobs:
        for d in iter_dicts(blob):
            for key in PAGE_CONTEXT_PICTURE_KEYS:
                pic = d.get(key)
                if isinstance(pic, dict):
                    aid = _photo_asset_id(pic.get("uri", ""))
                    if aid:
                        ids.add(aid)
    return ids


JS_DOM_AVATAR = """
() => {
    let best = -1, avatar = "";
    for (const im of document.querySelectorAll('svg image')) {
        const href = im.getAttribute('xlink:href') || im.getAttribute('href') || "";
        const w = im.getBoundingClientRect().width;
        if (href && w > best) { best = w; avatar = href; }
    }
    if (!avatar) {
        for (const img of document.querySelectorAll('img')) {
            const src = img.getAttribute('src') || "";
            const w = img.getBoundingClientRect().width;
            if (src && w > best && src.includes('fbcdn')) { best = w; avatar = src; }
        }
    }
    return avatar;
}
"""


class Discovery:
    """Runs keyword sweeps on an already-started browser session.

    LINKED TO: `discovery_path` in backend/platforms/registry.py names
    this class (loaded dynamically, by import path string, not a direct
    import -- see that module's own docstring on why), and
    backend/services/discovery_service.py is the actual caller: it
    constructs one Discovery per sweep batch and drives `run()` (or
    `sweep()` directly for a single keyword/tab, e.g. a re-sweep).
    """

    def __init__(self, args, ctx):
        """WHAT: binds this Discovery to one already-started browser
        context. HOW: `args` is a ScanOptions/DiscoveryOptions-shaped
        object (backend/platforms/scan_options.py) carrying every pacing
        knob (timeout, settle, concurrency, caps); `ctx` is the
        Playwright BrowserContext `stealth/browser.py::Session.start()`
        already produced -- this class never starts or owns a session
        itself, only drives pages inside one handed to it."""
        self.a = args
        self.ctx = ctx

    async def _self_avatar_assets(self) -> set[str]:
        """Every stable asset id (see _photo_asset_id) findable for the
        scraping account's OWN current avatar, fetched once per
        _resolve_missing batch, not per candidate. Defense-in-depth
        alongside the on_target_profile gate: confirmed via a real
        incident that the scraper's own photo can leak onto a candidate's
        Hit (a privacy-locked profile's own id-scoped payload substituting
        viewer-context UI data where the real, access-restricted photo
        would be). The gate closes the specific mechanism that caused
        that; this closes the door on ANY mechanism producing the same
        symptom, any avatar this function would attach is checked
        against this set before being trusted, regardless of which
        extraction path found it.

        Deliberately a SET gathered from multiple sources (the DOM's
        biggest rendered image AND every picture-key value in /me's own
        embedded JSON, un-scoped by id since the whole point here is
        capturing every representation of "my own photo"), not a single
        value from one heuristic: confirmed live that the biggest-DOM-
        image probe alone can land on a different crop/context (e.g. a
        cover photo) than whatever specific field a leak actually reuses
        (e.g. profilePicLarge), letting a real leak slip past a
        single-value comparison. Best-effort throughout: if this probe
        fails, it simply doesn't add protection this run, it does not
        block resolution.
        """
        try:
            page = await self.ctx.new_page()
            try:
                await page.goto(ME, wait_until="domcontentloaded", timeout=self.a.timeout * 1000)
                try:
                    await page.wait_for_function(JS_PROFILE_READY, timeout=RESOLVE_SETTLE_SEC * 1000)
                except Exception:
                    pass
                ids: set[str] = set()
                dom_avatar = await page.evaluate(JS_DOM_AVATAR)
                if dom_avatar:
                    aid = _photo_asset_id(dom_avatar)
                    if aid:
                        ids.add(aid)
                blobs: list[Any] = []
                for t in await page.evaluate(JS_EMBEDDED):
                    if isinstance(t, str) and t.strip().startswith("{"):
                        try:
                            blobs.append(json.loads(t))
                        except (json.JSONDecodeError, ValueError):
                            pass
                ids |= _all_picture_asset_ids(blobs)
                return ids
            finally:
                await page.close()
        except Exception:
            return set()

    async def _resolve_missing(self, ids: list[str], kind: str) -> dict[str, Hit]:
        """Best-effort name/avatar backfill for ids that reached `sweep()`'s
        reconciliation step with no edge to read a name/photo from, one
        visit to the profile's own page per id, reading whichever embedded
        payload on THAT page mentions the id's own name/profile_picture
        (see _extract_entity for the id-scoping that keeps this from ever
        attaching the wrong profile's identity to a candidate).

        Deliberately not the full multi-fallback cascade
        analysis_engine.py's read_name()/read_pic() use (DOM header,
        og:title, gql_strs, ...), importing from analysis_engine here
        would be circular (it already imports FROM this module), and this
        only needs to be good enough to stop a discovery card reading as a
        bare numeric id when the profile plainly has a name and photo. A
        profile this can't resolve (deleted, checkpointed, genuinely
        nameless) still gets its blank-name Hit from the caller, nothing
        here demotes a candidate, it only enriches one.
        """
        out: dict[str, Hit] = {}
        sem = asyncio.Semaphore(RESOLVE_CONCURRENCY)
        self_avatar_assets = await self._self_avatar_assets()

        async def one(eid: str) -> None:
            """Backfills one entity that search listed without enough
            detail to score, by visiting it directly. Holds a concurrency
            slot for the duration of the visit."""
            async with sem:
                page = await self.ctx.new_page()
                blobs: list[Any] = []

                async def on_response(resp):
                    """Collects this entity's own GraphQL payloads."""
                    try:
                        if "/api/graphql" not in resp.url:
                            return
                        text = await resp.text()
                    except Exception:
                        return
                    for line in text.splitlines():
                        line = line.strip()
                        if line.startswith("{"):
                            try:
                                blobs.append(json.loads(line))
                            except (json.JSONDecodeError, ValueError):
                                pass

                page.on("response", lambda r: asyncio.create_task(on_response(r)))
                blocked = False
                fallback_name = ""
                # confirmed False unless the identity gate below proves
                # otherwise, gates the page-title name fallback (avatar
                # recovery is off entirely; see PAGE_CONTEXT_PICTURE_KEYS)
                on_target_profile = False
                try:
                    await page.goto(
                        profile_url_for(eid, kind), wait_until="domcontentloaded",
                        timeout=self.a.timeout * 1000,
                    )
                    # a login wall / checkpoint / "content isn't available"
                    # page is never a source of THIS profile's real identity
                    #, skip extraction entirely rather than risk reading
                    # Facebook's own chrome text as if it were the profile.
                    # Checked FIRST, before the settle wait below: a blocked
                    # page will never satisfy JS_PROFILE_READY, so waiting
                    # the full ceiling on one would burn the whole batch's
                    # time budget on pages that can't produce data anyway.
                    try:
                        body_text = await page.inner_text("body")
                    except Exception:
                        body_text = ""
                    if RE_LOGIN.search(body_text) or RE_CHECKPOINT.search(body_text) or RE_GONE.search(body_text):
                        blocked = True
                    else:
                        # Wait for the profile's own data to actually be
                        # there, rather than sleeping a fixed guess and
                        # hoping (see RESOLVE_SETTLE_SEC). A timeout here is
                        # NOT an error: it just means this profile never
                        # fully rendered in the time allowed, so extraction
                        # proceeds with whatever did arrive, same
                        # best-effort, blank-beats-wrong degradation as
                        # everywhere else in this function.
                        try:
                            await page.wait_for_function(
                                JS_PROFILE_READY, timeout=RESOLVE_SETTLE_SEC * 1000,
                            )
                        except Exception:
                            pass
                        for t in await page.evaluate(JS_EMBEDDED):
                            if isinstance(t, str) and t.strip().startswith("{"):
                                try:
                                    blobs.append(json.loads(t))
                                except (json.JSONDecodeError, ValueError):
                                    pass

                        # CRITICAL: verify the page we actually landed on IS
                        # eid's profile before trusting either fallback below.
                        # `page.goto` can silently end up somewhere else:
                        # an invalid/dead id, a soft redirect that doesn't
                        # trip RE_GONE's "content isn't available" text,
                        # and land on, among other things, the SCRAPING
                        # ACCOUNT'S OWN logged-in home feed. Facebook's own
                        # nav chrome puts that account's avatar and name on
                        # every single page, so an untargeted "grab the
                        # biggest image" / "read the page title" fallback
                        # would confidently misattribute the scraper's own
                        # identity to a random candidate, a real profile
                        # picture and name attached to the WRONG entity,
                        # which is worse than a blank card: it reads as a
                        # confirmed, real impersonator when it's neither.
                        # Confirmed via this incident: an analyst's own
                        # Facebook account photo/name showed up on a
                        # discovery card. Both fallbacks are now gated on
                        # this page provably being eid's own profile, via
                        # its canonical link / og:url, the same identity
                        # signal _extract_entity's payload-id scoping
                        # relies on, just read from the DOM instead of a
                        # GraphQL blob. No confirmation, no fallback data:
                        # blank beats wrong, every time, no exceptions.
                        try:
                            page_url = await page.evaluate(
                                "() => (document.querySelector('link[rel=\"canonical\"]')?.href) "
                                "|| (document.querySelector('meta[property=\"og:url\"]')?.content) || ''"
                            )
                        except Exception:
                            page_url = ""
                        # Two ways to prove this page is eid's own, both
                        # id-scoped, neither guessing from unscoped DOM:
                        #   1. the canonical itself resolves to eid (a
                        #      personal profile, /profile.php?id=<eid>)
                        #   2. the canonical matches the link eid's OWN
                        #      payload record publishes for itself, the
                        #      Pages case, where the canonical is a vanity
                        #      slug that never mentions the numeric id
                        #      (see _entity_url_path).
                        on_target_profile = bool(page_url) and (
                            profile_id(page_url) == eid
                            or (
                                urlparse(normalize_url(page_url)).path.rstrip("/").lower()
                                == _entity_url_path(blobs, eid)
                                != ""
                            )
                        )

                        if on_target_profile:
                            # Name only, see PAGE_CONTEXT_PICTURE_KEYS's
                            # docstring for why there is deliberately no
                            # DOM-avatar fallback here any more: the exact
                            # same viewer-substitution that makes the JSON
                            # picture keys unsafe applies identically to
                            # whatever image is actually rendered on
                            # screen, confirmed live, being on the
                            # correct, identity-confirmed page does not
                            # make the DOM avatar trustworthy for a
                            # privacy-restricted profile.
                            try:
                                page_title = await page.title()
                                if page_title and page_title.strip() and page_title.lower() not in GENERIC_NAMES:
                                    cleaned_title = re.sub(r"\s*\|\s*Facebook\s*$", "", page_title).strip()
                                    cleaned_title = re.sub(r"^\(\d+\)\s*", "", cleaned_title).strip()
                                    if cleaned_title.lower() not in GENERIC_NAMES:
                                        fallback_name = cleaned_title
                            except Exception:
                                pass
                except Exception:
                    pass
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

                name, avatar, has_custom = ("", "", False) if blocked else _extract_entity(blobs, eid)
                if not name and fallback_name:
                    name = fallback_name

                # Defense-in-depth, kept even though avatar recovery is
                # currently off by default (TRUST_PAGE_CONTEXT_AVATAR):
                # if a future reliable positive signal re-enables it, this
                # still needs to be here. If it's the scraper's own
                # current photo, it is never eid's, discard it. See
                # _self_avatar_assets's docstring
                # for the confirmed incident this closes the door on.
                if avatar and self_avatar_assets and _photo_asset_id(avatar) in self_avatar_assets:
                    avatar, has_custom = "", False

                out[eid] = Hit(
                    entity_id=eid, name=name, url=profile_url_for(eid, kind),
                    avatar=avatar, has_custom_pic=has_custom, entity_type=kind,
                )

        try:
            await asyncio.wait_for(
                asyncio.gather(*(one(eid) for eid in ids)), timeout=RESOLVE_TIME_BUDGET_SEC,
            )
        except asyncio.TimeoutError:
            # whatever finished before the deadline is already in `out`:
            # each `one(eid)` writes its own entry the moment it completes,
            # not at the end of the batch, so a timeout here only drops the
            # candidates still in flight, not the ones already resolved
            pass
        return out

    async def sweep(self, keyword: str, tab: str, on_progress=None) -> Sweep:
        """One keyword, one tab (people/pages/groups), start to finish --
        the whole engine's core loop, and the method every other function
        in this file ultimately exists to serve.

        WHAT IT RETURNS: a `Sweep` (see the dataclass above) carrying every
        Hit found, which of the three completeness signals stopped it
        (exhausted / end-of-serp / stalled / a cap / an error -- see the
        module docstring's COMPLETENESS section), and the extraction
        chain's own record of what succeeded (network:graphql-search or
        dom:results-page, see `run_strategies`).

        HOW, roughly in order:
          1. Navigate to the tab's search URL (TABS) and wait for the
             first GraphQL search response (network) or embedded JSON
             (first-page render) to arrive.
          2. Scroll-paginate, absorbing each response's edges into `by_id`
             (`absorb()`, a closure) until one of the three completeness
             signals fires or a cap/timeout does.
          3. Reconcile against what Facebook's own cursor says it rendered
             (`rendered_ids`) and processed (`processed_ids`) to find any
             id parsed as an edge miss, then back-fill those via one cheap
             profile-page visit each (`_resolve_missing`).
          4. Run the extraction chain (GraphQL first, DOM fallback second,
             see `run_strategies` in shared/extraction.py) to pick the
             final hit list and record which one actually produced it.

        LINKED TO: called by `run()` below (one call per keyword x tab
        pair) and directly by discovery_service.py for a single re-sweep.
        Every helper above this method in the file -- `iter_results`,
        `page_state`, `parse_lines`/`parse_embedded`, `dom_search_hits`,
        `_tab_cap`, `_notify`, `_extract_entity`/`_resolve_missing` -- is a
        piece this method assembles; none of them is meant to be called on
        its own outside a test.
        """
        out = Sweep(keyword=keyword, tab=tab)
        started = time.time()
        page = await self.ctx.new_page()

        by_id: dict[str, Hit] = {}
        rendered_ids: set[str] = set()
        processed_ids: set[str] = set()
        state: Optional[PageState] = None
        arrived = asyncio.Event()
        kind = kind_for_tab(tab)

        cap = _tab_cap(self.a)

        def absorb(blob) -> None:
            """Folds one search payload into the running result set,
            enforcing the cap as it goes. See the note below for why the
            cap is applied HERE and not only between scrolls."""
            nonlocal state
            for hit in iter_results(blob):
                # capped here, not only in the while-loop's own check below:
                # one response can carry a whole page's worth of edges at
                # once, so checking only between scrolls let by_id overshoot
                # the cap by up to a page's width before the loop noticed, and
                # those extras were already incrementally saved to Mongo by
                # the time it did. Applies to every tab now, not just People.
                # A configured Pages/Groups cap used to only get the coarse
                # between-scrolls check below, so it could overshoot by a
                # whole response's worth of edges plus untrimmed backfill.
                if cap and len(by_id) >= cap:
                    break
                if hit.entity_id not in by_id:
                    hit.keyword, hit.tab, hit.entity_type = keyword, tab, kind
                    by_id[hit.entity_id] = hit
            if st := page_state(blob):
                state = st
                rendered_ids.update(st.ids_shown)
                processed_ids.update(st.ids_processed)

        async def on_response(resp):
            """Absorbs the search payloads this sweep fires. Filtered by
            REQUEST body, not just URL: every Facebook XHR shares the
            /api/graphql path, so the post data is the only thing that
            tells a search response apart from the dozens of unrelated
            queries a page makes."""
            try:
                if "/api/graphql" not in resp.url:
                    return
                if not is_search_response(resp.request.post_data or ""):
                    return
                text = await resp.text()
            except Exception:
                return
            for blob in parse_lines(text):
                absorb(blob)
            out.pages += 1
            arrived.set()

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        try:
            url = TABS[tab].format(q=quote(keyword))
            await page.goto(
                url, wait_until="domcontentloaded", timeout=self.a.timeout * 1000
            )
            # the first page of results is in the document, not over XHR
            try:
                await page.wait_for_function(
                    "() => document.body.innerText.length > 400",
                    timeout=self.a.settle * 1000,
                )
            except Exception:
                pass

            # Wait for the first GraphQL search response to arrive before
            # reading embedded data. Facebook's search results come via XHR
            # (not embedded in the initial HTML), and on slower connections or
            # heavier payloads, the response can arrive several seconds after
            # domcontentloaded fires. Without this wait, by_id is empty when
            # run_strategies evaluates, producing a false "0 results" report.
            if not by_id:
                try:
                    await asyncio.wait_for(arrived.wait(), timeout=self.a.settle)
                except asyncio.TimeoutError:
                    pass

            for blob in parse_embedded(await page.evaluate(JS_EMBEDDED)):
                absorb(blob)

            # Insertion order in by_id is discovery order, so slicing from
            # `notified` on each call yields exactly the hits new since the
            # last notification, callers (jobs.py) use this to persist and
            # show results while a single long sweep is still running,
            # instead of everything landing at once when it finally ends.
            notified = 0
            if on_progress and by_id:
                notified = len(by_id)
                try:
                    await _notify(on_progress, len(by_id), 0, list(by_id.values()))
                except Exception:
                    pass

            stalls = 0
            while True:
                if cap and len(by_id) >= cap:
                    out.stopped = "cap:results"
                    break
                if self.a.max_pages and out.pages >= self.a.max_pages:
                    out.stopped = "cap:pages"
                    break
                if self.a.max_seconds and time.time() - started >= self.a.max_seconds:
                    out.stopped = "cap:seconds"
                    break
                if state and not state.has_next:
                    out.stopped = "end-of-serp" if state.end_of_serp else "exhausted"
                    out.complete = True
                    break

                before = len(by_id)
                arrived.clear()
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception as e:
                    out.stopped = "error"
                    out.error = f"scroll failed: {e}"
                    break
                # wait for the next page of results rather than a fixed sleep
                try:
                    await asyncio.wait_for(arrived.wait(), timeout=self.a.page_wait)
                except asyncio.TimeoutError:
                    pass

                if len(by_id) > before:
                    stalls = 0
                    if on_progress:
                        new_hits = list(by_id.values())[notified:]
                        notified = len(by_id)
                        try:
                            await _notify(on_progress, len(by_id), out.pages, new_hits)
                        except Exception:
                            pass
                    # people search runs for a long time; show it is progressing
                    if out.pages % self.a.progress_every == 0:
                        print(
                            f"    [{tab:<6}] {keyword!r}: {len(by_id)} so far, "
                            f"page {out.pages}, {time.time()-started:.0f}s",
                            file=sys.stderr,
                        )
                else:
                    stalls += 1
                    if stalls >= self.a.patience:
                        out.stopped = "stalled"
                        break
                    await page.wait_for_timeout(600)

            # anything Facebook rendered but we never parsed as an edge: a
            # layout change would show up here rather than as silent data loss
            missing = rendered_ids - by_id.keys()

            # Ids the search BACKEND matched but Facebook deliberately
            # never rendered (`processed_unicorn_ids`). Counted as
            # telemetry, NEVER turned into result rows: these are
            # Facebook's internal bookkeeping for entities it filtered out
            # before display (deactivated, privacy-restricted, blocked to
            # this viewer, region-limited, deduped), so a human searching
            # by hand never sees them and there is usually no viewable
            # profile behind the id at all. Discovery's contract is
            # fidelity to what a real user sees; `result_ids_shown` is
            # Facebook's own statement of that. Kept as a count
            # (out.unshown, in summary()) so the gap stays observable if
            # Facebook's filtering behaviour shifts.
            # See docs/adr/0009-render-fidelity-in-discovery-results.md.
            unshown = processed_ids - by_id.keys()

            # one cheap profile-page visit per reconciled id to recover a
            # name/photo, see _resolve_missing's docstring for why this
            # doesn't reuse analysis_engine.py's full extraction cascade.
            # Best-effort: an id this can't resolve still gets tracked with
            # a blank name below, exactly as it always has.
            resolved: dict[str, Hit] = {}
            unresolved_in_by_id = {
                eid for eid, h in by_id.items()
                if not h.name or h.name.strip() == eid or h.name.strip().isdigit() or not h.avatar or not h.has_custom_pic
            }
            # deliberately NOT `| unshown`, see the comment above: those
            # ids mostly have no viewable profile behind them, so visiting
            # them spent the resolve budget (and real page loads under the
            # live session) on data that cannot exist, while starving the
            # ids that DO resolve.
            to_resolve = sorted(missing | unresolved_in_by_id)
            if to_resolve:
                try:
                    resolved = await self._resolve_missing(to_resolve, kind)
                except Exception:
                    resolved = {}

            for eid in sorted(unresolved_in_by_id):
                if r := resolved.get(eid):
                    h = by_id[eid]
                    if not h.name or h.name.strip() == eid or h.name.strip().isdigit():
                        if r.name:
                            h.name = r.name
                    if not h.avatar or not h.has_custom_pic:
                        if r.avatar and r.has_custom_pic:
                            h.avatar = r.avatar
                            h.has_custom_pic = r.has_custom_pic

            for eid in sorted(missing):
                r = resolved.get(eid)
                by_id[eid] = Hit(
                    entity_id=eid,
                    # blank when unresolved, not a "fb:{id}" placeholder
                    # string, the UI's own display_name -> username ->
                    # entity_id -> "Unnamed Profile" fallback already
                    # renders this sensibly, and nothing downstream keys
                    # off the old "fb:" prefix anymore
                    name=r.name if r else "",
                    url=profile_url_for(eid, kind),
                    avatar=r.avatar if r else "",
                    has_custom_pic=r.has_custom_pic if r else False,
                    entity_type=kind,
                    keyword=keyword,
                    tab=tab,
                    source="id-backfill",
                )
            out.backfilled = len(missing)

            # `unshown` is counted, never materialized as rows, see the
            # long comment where it's computed for the measured reasoning.
            out.unshown = len(unshown)
            # A blank name on a genuinely-rendered result is KEPT, not
            # dropped: privacy-restricted profiles often return no name,
            # and discarding them is the "people not appearing" bug. The
            # UI's entity_id/"Unnamed Profile" fallback renders them.
            #
            # `by_id` is ALREADY in Facebook's own top-to-bottom order --
            # pagination is sequential, edges are absorbed in render order,
            # and a duplicate id on a later page is dropped rather than
            # overwriting its earlier position -- and dict insertion order
            # preserves that. So sort ONLY by confirmed-result vs
            # best-effort backfill (Facebook never said where the latter
            # would have ranked), and let the stable sort keep every other
            # tie where it landed. Deliberately NOT by `hit.rank`: rank
            # resets per response, so using it interleaves pages.
            # See docs/adr/0009-render-fidelity-in-discovery-results.md.
            # GraphQL payload first, rendered page second. The DOM pass only
            # runs when the payload produced nothing at all, so a healthy
            # sweep never pays for it, and a doc-id rotation degrades to
            # "fewer fields per hit" instead of to "zero results, reported
            # as success".
            chain = await run_strategies(
                f"facebook/search[{keyword!r}/{tab}]",
                [
                    ("network:graphql-search", lambda: sorted(
                        by_id.values(), key=lambda h: h.source != "graphql",
                    )),
                    ("dom:results-page", lambda: dom_search_hits(page, keyword, tab)),
                ],
            )
            out.hits = [hit_to_row(h) for h in (chain.value or [])]
            out.extraction = chain
            if chain.degraded:
                out.source = "dom"
            if cap and len(out.hits) > cap:
                # the loop's own cap check (above) stops fetching at the limit, but
                # reconciliation can still add a page's worth of ids Facebook
                # already told us about (rendered/processed) before the cap
                # fired, trim back to the real limit here too, keeping the
                # graphql-confirmed hits (sorted first) over backfilled ones.
                # Applies to every tab, not just People, see absorb()'s
                # comment for why Pages/Groups need this exact-N guarantee too.
                out.hits = out.hits[:cap]
            out.reported_total = state.total_results if state else None
        except Exception as e:
            out.stopped, out.error = "error", f"{type(e).__name__}: {e}"
        finally:
            try:
                await page.close()
            except Exception:
                pass
            out.seconds = time.time() - started
        return out

    async def run(self, keywords: list[str], tabs: list[str]) -> list[Sweep]:
        """Every keyword on every tab. Keywords are independent, so they overlap."""
        jobs = [(k, t) for k in keywords for t in tabs]
        sem = asyncio.Semaphore(max(1, self.a.concurrency))

        async def one(i: int, keyword: str, tab: str):
            """One (keyword, tab) sweep, holding a concurrency slot and
            starting staggered so several tabs do not hit Facebook in the
            same instant."""
            async with sem:
                await asyncio.sleep(i % max(1, self.a.concurrency) * 1.0)
                s = await self.sweep(keyword, tab)
                print(
                    f"  [{tab:<6}] {keyword!r}: {s.summary()} ({s.seconds:.1f}s)",
                    file=sys.stderr,
                )
                return i, s

        pairs = await asyncio.gather(*(one(i, k, t) for i, (k, t) in enumerate(jobs)))
        return [s for _, s in sorted(pairs, key=lambda p: p[0])]

