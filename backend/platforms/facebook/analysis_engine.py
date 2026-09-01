"""Facebook analysis engine: validation, metadata analysis, and impersonation
signal extraction, profile URL -> a scored Row.

Session/login-checking and URL/identity normalization live in
discovery_engine.py (imported below) since discovery produces them first;
this file owns everything specific to reading and scoring a validated visit:
field readers, payload scoping (Harvest), and the browser-drive loop
(Scraper).
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from backend.shared.models.row import Row
from backend.platforms.scan_options import captures_screenshot
from backend.shared.text import (MONTHS, epoch_to_dt, find_ints,
                               is_place, iter_dicts, iter_kv, name_score,
                               parse_count)
from backend.platforms.facebook.discovery_engine import (RE_CHECKPOINT,
                                                          RE_DEFAULT_PIC,
                                                          RE_GONE, RE_LOGIN,
                                                          FacebookSession,
                                                          hd_picture_url,
                                                          normalize_url,
                                                          profile_id, tab_url)

# Field-reading constants
# Keys and patterns used to read fields off a profile.
#
# The K_* tuples are GraphQL key names; the RE_* patterns read rendered text.
# Both drift when Facebook ships changes, when a field goes blank across
# every profile, suspect these first.

MAX_FOLLOWERS = 5_000_000_000

# Set on a profile Facebook publishes no audience count for at all. Matched
# by shared/completeness.py to tell that apart from a failed read; keep the
# two in step (completeness.py::_NO_AUDIENCE_MARKERS).
NO_AUDIENCE_NOTE = "profile publishes no audience count"

K_FOLLOWERS = (
    "follower_count",
    "followers_count",
    "fan_count",
    "subscriber_count",
    "follower_count_int",
)
# "created_time" deliberately excluded. Confirmed live, in the raw payload:
# it belongs to COMMENT objects specifically:
# `"comment":{"created_time":...}` under an `XFBCommentTimestampBadge`
# typename, not to posts. `creation_time` (kept) is confirmed genuinely
# post-scoped in the same capture: it appears alongside the post's own
# `post_id` field. See read_last_post()/_post_stamps() below for the fuller
# fix, a key name match alone (even "creation_time") isn't proof of a
# real post; _post_stamps() additionally requires the post_id sibling.
K_POST_TIME = ("publish_time", "creation_time", "publish_time_ts")
K_LOCATION = (
    "current_city_name",
    "single_line_address",
    "full_address",
    "street_address",
    "city_name",
    "hometown_name",
)
# "short_name" is deliberately absent: it is the first name only, so it scores
# under NAME_THRESHOLD, and it is the key that leaks other entities' names
K_NAME = ("profile_name", "page_name", "name_for_display")
K_PIC = ("profile_picture", "profile_pic_url", "uri", "photo_image")

RE_FOLLOWERS = re.compile(
    r"([\d][\d.,\s]{0,15}[KMB]?)\s*(?:followers|people follow this)", re.I
)
# one header counter chip, e.g. "154M followers" / "53 friends" / "1.2K likes"
RE_CHIP = re.compile(
    r"^([\d][\d.,\s]{0,15}[KMB]?)\s*"
    r"(followers?|following|friends?|likes?|people follow this)\b",
    re.I,
)
# Anchored to the start of a line because these are About-tab FIELD labels,
# not prose. Unanchored, "From" matched mid-sentence marketing copy, a
# live Page produced "From classrooms to cement plants, from learning
# concepts to witnessing them" as a candidate location. `is_place` rejected
# it, so nothing wrong was ever stored, but relying on the validator alone
# to catch a matcher this loose is one plausible-looking city name away from
# publishing a fabricated location on a client-facing incident.
RE_LIVES_IN = re.compile(r"(?:^|\n)\s*Lives in\s+([^\n·|]{2,70})", re.M)
RE_FROM = re.compile(r"(?:^|\n)\s*From\s+([^\n·|]{2,70})", re.M)
RE_NO_POSTS = re.compile(
    r"(No posts yet|hasn't (?:added|shared|posted)|nothing to show|"
    r"No posts available)",
    re.I,
)

GENERIC_NAMES = {"facebook", "notifications"}


# Harvest
# Everything collected from one profile visit, and how to read it back.
#
# `scoped()` is the important part: it narrows the collected payloads to the
# entity that IS this profile, which is what keeps other people's names,
# follower counts and post timestamps out of the record.
#
# Embedded payloads are kept as raw text and parsed on demand. A profile page
# ships ~180 of them and only a handful mention the profile at all, so scoping
# substring-filters first and parses second, the same answer for a fraction
# of the work.


class Harvest:
    """WHAT: everything one profile visit collected, and the lookups that
    read fields back out of it.

    HOW: Facebook publishes the same value in several places and none of
    them reliably -- an intercepted /api/graphql response, an embedded
    `script[type=application/json]` blob, the rendered DOM. All three are
    accumulated here as a profile is visited, and the readers below query
    them in a deliberate order of trust:

      ent_*    -- scoped to dicts that ARE this profile (id == pid). The
                  only readings that cannot belong to somebody else.
      gql_*    -- unscoped across every payload. A fallback, and the
                  reason `ents` scoping exists: a Facebook page carries
                  suggested pages, sponsored blocks and commenters, each
                  with their own name and follower count.
      dom/html -- last resort, whatever actually rendered.

    LINKED TO: filled by Scraper.process()'s response listener; read by
    the field extractors, which record WHICH tier answered so
    shared/completeness.py can tell a real reading from a lucky guess."""

    def __init__(self):
        """Empty collectors. Everything is filled during one profile visit
        by process()'s response listener and its tab reads."""
        self.gql: list[Any] = []  # parsed XHR /api/graphql lines
        self.raw: list[str] = []  # unparsed script[type=application/json]
        self.html: dict[str, str] = {}
        self.text: dict[str, str] = {}
        self.ents: list[dict] = []  # dicts that ARE this profile (id == pid)
        self.dom: dict[str, Any] = {}  # header fields read straight off the page
        self._scopes: dict[str, "Harvest"] = {}

    # ---------- collection ----------

    def add_gql(self, body: str) -> None:
        """WHAT: absorbs one /api/graphql response body. HOW: Facebook
        streams these as JSON LINES, not one document, so each line is
        parsed independently and an unparseable one is skipped rather than
        discarding the whole response. LINKED TO: called from the
        page-response listener in Scraper.process()."""
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    self.gql.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    def add_embedded(self, texts) -> None:
        """WHAT: absorbs the page's embedded JSON script blocks. HOW: kept
        as RAW strings -- most are large and never queried, so parsing is
        deferred to mentioning(), which substring-filters first. Cached
        scope views are dropped because new payloads may change what
        counts as this profile's own entity."""
        self.raw.extend(t for t in texts or [] if t)
        self._scopes.clear()  # new payloads invalidate cached views

    # ---------- lookup ----------

    def gql_raw(self) -> str:
        """Every parsed XHR payload as one JSON string, for the regex
        scans that are cheaper than another tree walk. "" when the
        payloads contain something json cannot re-serialise."""
        try:
            return json.dumps(self.gql)
        except (TypeError, ValueError):
            return ""

    def mentioning(self, needle: str) -> Iterator[Any]:
        """Parsed payloads whose text contains `needle`, embedded, then XHR.

        The needle is the bare id, not `"id":"<id>"`. Matching the keyed form
        would depend on the payload having no space after the colon, which is
        true of Facebook's compact JSON today and would silently return
        nothing the day it stops being true.

        Case-INSENSITIVE on purpose: the only caller who passes a mixed-case
        needle is `entity_id_for`'s vanity slug (a bare numeric id has no
        case to get wrong), and there is no guarantee the caller's own
        casing matches whatever case the payload happens to render that
        vanity in. Comparing case-sensitively made this method return
        NOTHING for any capitalized vanity URL -- confirmed live against
        facebook.com/AdaniOnline, where the payload's own "AdaniOnline"
        never matched a lowercased "adanionline" needle, silently forcing
        every such profile down to the much weaker DOM id-resolution
        fallback. Safe for the numeric-id caller too, since lowercasing a
        string of digits is a no-op.
        """
        needle = needle.lower()
        for t in self.raw:
            if needle in t.lower():
                try:
                    yield json.loads(t)
                except (json.JSONDecodeError, ValueError):
                    continue
        for blob in self.gql:
            try:
                if needle in json.dumps(blob).lower():
                    yield blob
            except (TypeError, ValueError):
                continue

    def scoped(self, pid: str) -> "Harvest":
        """A view of the payloads narrowed to this profile.

        `ents` holds the dicts whose own id is the profile id, the GraphQL
        objects that ARE this profile. Reading a field off one of those is
        unambiguous. The wider page carries the notification flyout, friend
        suggestions and sponsored payloads, all with their own name, follower
        and timestamp keys; anything read from the unfiltered pile belongs to
        whoever happened to load first.

        An unverifiable id scopes to nothing, on purpose: blank beats wrong.
        """
        if pid in self._scopes:
            return self._scopes[pid]

        v = Harvest()
        v.html, v.text, v.dom = self.html, self.text, self.dom
        if pid and pid.isdigit():
            for blob in self.mentioning(pid):
                for d in iter_dicts(blob):
                    if d.get("id") == pid and len(d) > 1:
                        v.ents.append(d)
            for blob in self.gql:
                try:
                    if pid in json.dumps(blob):
                        v.gql.append(blob)
                except (TypeError, ValueError):
                    continue
        self._scopes[pid] = v
        return v

    # ---------- entity readers ----------

    def ent_scalar(self, key: str) -> str:
        """A field read directly off the profile entity, no nesting, no guessing."""
        for d in self.ents:
            v = d.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def ent_path(self, *paths: str) -> str:
        """WHAT: first non-empty string at any of these dotted paths,
        within this profile's OWN entity dicts. HOW: paths are tried in
        order, so callers pass them most-specific first; a missing
        intermediate key is a miss, never an exception. LINKED TO: the
        scoped counterpart of gql_strs() -- see the class docstring for
        why scoping matters on Facebook."""
        for d in self.ents:
            for p in paths:
                cur: Any = d
                for part in p.split("."):
                    cur = cur.get(part) if isinstance(cur, dict) else None
                    if cur is None:
                        break
                if isinstance(cur, str) and cur.strip():
                    return cur.strip()
        return ""

    def ent_social(self) -> list[str]:
        """The header chips: '70 followers', '8 following', '328 friends'.

        Facebook ships these already rendered, so this is the only GraphQL form
        of the follower count, there is no integer field anywhere in the
        entity. Two shapes occur: content[].text.text, and content[].text as a
        bare string under header_top_row.profile_user.
        """
        out: list[str] = []
        for _k, v in self._entity_kv({"profile_social_context"}):
            if not isinstance(v, dict):
                continue
            for item in v.get("content") or []:
                t = item.get("text") if isinstance(item, dict) else None
                if isinstance(t, dict):
                    t = t.get("text")
                if isinstance(t, str) and t.strip() and t.strip() not in out:
                    out.append(t.strip())
        return out

    def ent_ints(self, keys) -> list[int]:
        """Every integer stored under any of `keys` within this profile's
        own entities. Booleans are excluded deliberately: `True` is an int
        in Python and would otherwise be harvested as the number 1."""
        out = []
        for _k, v in self._entity_kv(set(keys)):
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out.append(int(v))
            elif isinstance(v, str) and v.isdigit():
                out.append(int(v))
        return out

    def ent_strs(self, keys) -> list[str]:
        """Every non-blank string stored under any of `keys` within this
        profile's own entities, in discovery order."""
        return [
            v.strip()
            for _k, v in self._entity_kv(set(keys))
            if isinstance(v, str) and v.strip()
        ]

    def _entity_kv(self, want: set[str]) -> Iterator[tuple[str, Any]]:
        """Every (key, value) under `want`, walked across this profile's
        own entity dicts only. The shared engine behind ent_ints/ent_strs/
        ent_social."""
        for d in self.ents:
            for k, v in iter_kv(d):
                if k in want:
                    yield k, v

    # ---------- unscoped fallbacks ----------

    def gql_ints(self, keys) -> list[int]:
        """Every integer under `keys` across ALL payloads, this profile's
        or not. Unscoped, so a value from here may belong to a suggested
        page or a commenter -- callers use it only after the ent_* tier
        has come up empty, and record that they did."""
        want, out = set(keys), []
        for blob in self.gql:
            for k, v in iter_kv(blob):
                if k not in want or isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    out.append(int(v))
                elif isinstance(v, str) and v.isdigit():
                    out.append(int(v))
        return out

    def gql_strs(self, keys) -> list[str]:
        """Every non-blank string under `keys` across ALL payloads. The
        unscoped fallback to ent_strs(), with the same caveat as
        gql_ints()."""
        want, out = set(keys), []
        for blob in self.gql:
            for k, v in iter_kv(blob):
                if k in want and isinstance(v, str) and v.strip():
                    out.append(v.strip())
        return out

    def all_html(self) -> str:
        """Every visited tab's raw page HTML, concatenated -- used where a
        regex needs to scan markup (entity-type sniffing via `__typename`
        in Scraper.process())."""
        return "\n".join(self.html.values())

    def all_text(self) -> str:
        """Every visited tab's rendered inner-text, concatenated -- used
        where a regex needs to scan visible copy (read_location()'s
        "Lives in"/"From" tier, RE_NO_POSTS in read_last_post())."""
        return "\n".join(self.text.values())


# Readers
# Turning a Harvest into a Row: one function per report field.
#
# Every reader follows the same order, the profile's own GraphQL entity
# first, then the rendered header, then progressively looser fallbacks, and
# records which one answered via `row.mark()`. That provenance is what makes
# a filled cell auditable and a blank one meaningful.
#
# These are pure functions of (row, harvest): no browser, no network. That is
# what makes them testable against a saved payload.


def read_name(row: Row, h: Harvest) -> None:
    """WHAT: the profile's display name, into `row.profile_name`, plus
    the resulting name_score against `row.target`. HOW: tries five
    sources in trust order (graphql entity -> DOM header -> DOM post-author
    label -> og:title -> <title> tag -> loose unscoped graphql), taking
    the first non-generic hit; see the inline comments below for why each
    one is or isn't trusted. LINKED TO: called from read_profile() below,
    which Scraper.process() calls once the visit has landed."""
    # the entity's own "name" is the full display name; "short_name" is the
    # first name only and would fall under NAME_THRESHOLD, so never use it.
    # og:title is absent on logged-in renders and <title> is "(2) Facebook".
    # the third flag says whether a generic name is believable from that
    # source: an entity really can be called "Facebook", but a <title>
    # reading "Facebook" is just the browser tab.
    cands = [
        ("graphql", h.ent_scalar("name"), True),
        ("dom-header", h.dom.get("name"), True),
        ("dom-post-label", h.dom.get("postAuthor"), True),
    ]
    main = h.html.get("main", "")
    for tag, m in (
        (
            "og:title",
            re.search(
                r'property=["\']og:title["\'][^>]+content=["\']' r'([^"\']+)', main
            ),
        ),
        ("title-tag", re.search(r"<title>([^<|]{1,140})", main)),
    ):
        if m:
            cands.append(
                (
                    tag,
                    re.sub(
                        r"\s*\|\s*Facebook\s*$",
                        "",
                        re.sub(r"^\(\d+\)\s*", "", m.group(1)),
                    ).strip(),
                    False,
                )
            )
    cands += [("graphql-loose", v, True) for v in h.gql_strs(K_NAME)]
    for tag, c, trusted in cands:
        c = (c or "").strip()
        if not c or (not trusted and c.lower() in GENERIC_NAMES):
            continue
        row.profile_name = c
        row.mark("name", tag)
        break
    row.name_score = name_score(row.profile_name, row.target)


def take_chip(row: Row, chip: str, source: str) -> None:
    """One header counter, '154M followers', '53 friends', '1.2K likes'.

    Pages publish followers (and older ones only likes), personal profiles
    usually publish friends instead, creator profiles publish both.
    """
    m = RE_CHIP.match(chip)
    if not m:
        return
    val, exact = parse_count(m.group(1))
    if val is None or not (0 <= val < MAX_FOLLOWERS):
        return
    kind = m.group(2).lower()
    if kind.startswith("friend"):
        if row.friends is None:
            row.friends = val
            row.mark("friends", source)
        return
    if kind.startswith("following"):
        return
    if row.followers is not None:
        return
    row.followers = val
    row.followers_exact = "yes" if exact else "no"
    row.mark("followers", source)
    if kind.startswith("like"):
        row.note(f"page publishes likes, not followers ({chip})")
    if not exact:
        row.note(f"followers rounded ({chip})")


def followers_from_friends(row: Row, chips: "list[tuple[str, str]]") -> None:
    """A personal profile's FRIEND count is its audience number, so it
    belongs in `followers` when the profile published no follower count of
    its own.

    Facebook publishes a follower count on Pages and on creator profiles,
    and a friend count instead on an ordinary personal profile. The report
    has exactly ONE audience column (`followers`) -- there is no friends
    column in the grid or in any export -- so a friend count left only in
    `row.friends` was read correctly and then thrown away: 123 of the 165
    personal profiles in a single 663-URL run had a perfectly good friend
    count stored and still showed a blank Followers cell.

    Runs LAST, after every genuine follower tier in read_counts, so a real
    follower count always wins on a creator profile that publishes both.
    The source is tagged `...-friends` and a note is added so the two stay
    distinguishable to anyone reading the row rather than the column.
    """
    for chip, src in chips:
        m = RE_CHIP.match(chip)
        if not m or not m.group(2).lower().startswith("friend"):
            continue
        val, exact = parse_count(m.group(1))
        if val is None or not (0 <= val < MAX_FOLLOWERS):
            continue
        row.followers = val
        row.followers_exact = "yes" if exact else "no"
        row.mark("followers", f"{src}-friends")
        row.note(f"profile publishes friends, not followers ({chip})")
        if not exact:
            row.note(f"followers rounded ({chip})")
        return


def read_counts(row: Row, h: Harvest) -> None:
    """The audience number, whichever of the three Facebook publishes."""
    chips = [(s, "graphql-social-context") for s in h.ent_social()]
    # the header line holds the same counters when the entity is unreadable
    chips += [
        (part.strip(), "dom-header")
        for part in re.split(r"[•·|]", str(h.dom.get("counter") or ""))
    ]
    for chip, src in chips:
        take_chip(row, chip, src)
    if row.followers is not None:
        return
    ents = [n for n in h.ent_ints(K_FOLLOWERS) if 0 <= n < MAX_FOLLOWERS]
    if ents:
        row.followers = max(ents)
        row.followers_exact = "yes"
        row.mark("followers", "graphql")
        return
    ints = [n for n in h.gql_ints(K_FOLLOWERS) if 0 <= n < MAX_FOLLOWERS]
    if ints:
        row.followers = max(ints)
        row.followers_exact = "yes"
        row.mark("followers", "graphql-loose")
        return
    if m := RE_FOLLOWERS.search(h.text.get("main", "")):
        val, exact = parse_count(m.group(1))
        if val is not None:
            row.followers = val
            row.followers_exact = "yes" if exact else "no"
            row.mark("followers", "page-text")
            if not exact:
                row.note(f"followers rounded ({m.group(1).strip()})")
            return
    followers_from_friends(row, chips)
    if row.followers is None and row.friends is None and h.ents:
        # Every tier above came up empty on a profile we DID successfully
        # read (`h.ents` is non-empty: the entity resolved, and the name,
        # avatar and post dates all came out of its payload). That
        # combination is not a parser failure -- it is Facebook declining
        # to publish an audience number for this profile at all.
        #
        # Confirmed live (2026-08-22) on the real stored rows that showed
        # this: a brand-new locked-down personal profile renders its name,
        # "Add friend", and its tab bar, and NO count anywhere -- not on the
        # timeline, not on /friends, not on /about, not on
        # /about_profile_transparency (those tabs return 155-299 characters
        # of text in total), and no rendered chip anywhere in the GraphQL
        # payload either. There is nothing further to fetch.
        #
        # Saying so matters because silence here is not free:
        # shared/completeness.py counts a blank audience number as a MISS,
        # which re-queues the profile on every sweep forever and reports it
        # to the analyst as real data loss. This note is what lets it be
        # read as the honest answer it is -- the same job
        # `posts_seen == "no"` already does for a profile with no posts.
        row.note(NO_AUDIENCE_NOTE)
        row.mark("followers", "not-published")


def _post_stamps(roots) -> list[int]:
    """WHAT: the post timestamps under `roots` -> a list of epoch ints.
    HOW: only dicts carrying a `post_id` sibling count, which is what
    scopes this to genuine posts. LINKED TO: read_last_post() picks the
    newest of these.

    K_POST_TIME values that belong to a genuine post object, not just
    any nested dict that happens to reuse the key name.

    This used to trust ANY dict carrying a K_POST_TIME key, on the
    assumption that being inside the profile's own entity subtree (`ents`)
    already meant "this profile's own post". That assumption was wrong,
    confirmed live: a comment on one of this profile's posts is ALSO
    nested inside that same entity subtree, and its own `created_time`
    field was being counted as if it were a post, letting a stranger's
    comment on an old post make a dormant page's last-post date look like
    today. (A different manifestation of the same root cause, trusting a
    key name with no structural check, was closed earlier by dropping
    "created_time" from K_POST_TIME entirely, once it was confirmed to
    always be a comment field. This closes the general case: even
    `creation_time`, confirmed genuinely post-scoped, still needs a real
    post to hang off of.)

    A genuine post's own dict was confirmed live to always carry a
    `post_id` sibling in the SAME dict, e.g.
    `{"post_id": "...", "creation_time": 1786353437, "attachments": [...]}`.
    Requiring that sibling is what actually scopes this to posts, not the
    entity-subtree membership that was doing that job before.
    """
    out: list[int] = []
    for root in roots:
        for d in iter_dicts(root):
            if "post_id" not in d:
                continue
            for key in K_POST_TIME:
                v = d.get(key)
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    out.append(int(v))
                elif isinstance(v, str) and v.isdigit():
                    out.append(int(v))
    return out


def read_last_post(row: Row, h: Harvest) -> None:
    """WHAT: sets row.last_post_iso from whatever this visit collected.
    HOW: three tiers, precise-and-narrow first, proven-but-looser as the
    safety net -- so the column is never silently empty just because the
    precise method's data has not arrived over XHR yet. LINKED TO: called
    by Scraper.process(); _post_stamps() supplies the candidate timestamps
    and row.mark() records which tier answered.

    The tier that answered matters as much as the answer: a date from tier
    3 is looser evidence than one from tier 1, and shared/completeness.py
    reads that provenance rather than treating every filled cell alike.
    """
    # Three tiers, precise-and-narrow first, proven-but-looser as the safety
    # net, never silently empty just because the precise method's data
    # hasn't arrived yet over XHR.
    #
    # 1. Entity-scoped, post_id-gated: this profile's own subtree, only
    #    dicts confirmed (live) to be a real post (they carry a `post_id`
    #    sibling, notifications and comments do not, so this is what
    #    actually excludes them, not the entity-subtree membership alone).
    # 2. Unscoped, still post_id-gated: broader reach across every parsed
    #    payload (embedded script tags + XHR), same real-post proof
    #    required.
    # 3. The original un-gated text-regex scan, INCLUDING the rendered
    #    page's own HTML, kept, not removed, because it is the only
    #    source available before certain XHR responses (e.g. the timeline
    #    feed units query) have necessarily arrived, and losing it produced
    #    an empty result live where tiers 1-2 alone found nothing yet. Only
    #    reached when 1-2 come up empty, and "created_time" is still
    #    excluded from K_POST_TIME (the confirmed comment field), so the
    #    specific leak that motivated this rewrite stays closed even here.
    stamps, tag = _post_stamps(h.ents), "graphql"
    if not stamps:
        raw_parsed = []
        for t in h.raw:
            try:
                raw_parsed.append(json.loads(t))
            except (json.JSONDecodeError, ValueError):
                continue
        stamps = _post_stamps(h.gql) + _post_stamps(raw_parsed)
        tag = "graphql-unscoped"
    if not stamps:
        stamps = find_ints(h.gql_raw() + h.html.get("main", ""), K_POST_TIME)
        tag = "payload-regex-ungated"
    dts = [epoch_to_dt(t) for t in stamps]
    dts = [d for d in dts if d]
    if dts:
        row.last_post_iso = max(dts).date().isoformat()
        row.posts_seen = "yes"
        row.mark("last_post", tag)
    elif RE_NO_POSTS.search(h.all_text()):
        row.posts_seen = "no"
        row.mark("last_post", "no-posts-notice")


# DOM last-post fallback
# read_last_post above works from payload timestamps. When Facebook does not
# ship those for a given profile, the date is still right there on screen:
# every post's permalink carries the exact publish time in its aria-label,
# put there for screen readers. Captured live:
#
#     <a href=".../posts/pfbid032..." aria-label="Friday 7 August 2026 at 14:14">3d</a>
#
# That is a precise absolute timestamp, not the "3d" relative text a person
# sees, so it needs no arithmetic against "now" and cannot drift.
#
# `/reel/` is in the selector for a Page's own Reel posts, matched the same
# way as a regular post, confirmed live via a genuine case
# (`permalink_url: ".../reel/1711498136706603/"`, authored by the page
# itself per its `actors` field). It does NOT make this fallback see every
# Reel, though: confirmed live on that same profile, Facebook does not
# render Reels inline in the default chronological timeline at all, the
# only `/reel/` link on that view was the nav shortcut to the separate
# Reels tab (`href="/reel/?s=tab"`, aria-label "Reels", which fails
# parse_aria_date below harmlessly). Reaching an out-of-timeline Reel would
# need a second page visit to that tab, which this fallback does not do.
# That gap is acceptable here because it is the least-trusted of three
# tiers: read_last_post()'s payload-based extraction runs first and is
# confirmed to capture Reel timestamps correctly (it reads structured data,
# not the rendered page), this DOM path only ever fires when that has
# already returned nothing.
#
# The same selector doubles as the evidence screenshot's "a real post is on
# screen" anchor (see Scraper.screenshot). Defined once here rather than
# written out twice: it is the platform's own live-verified hook for a post
# permalink, and the two uses want exactly the same thing.
POST_LINK_SELECTOR = (
    'a[href*="/posts/"], a[href*="story_fbid"], a[href*="/videos/"], '
    'a[href*="/reel/"], a[href*="permalink"]'
)

JS_POST_TIMES = """
() => {
  const out = [];
  const sel = 'a[href*="/posts/"], a[href*="story_fbid"], a[href*="/videos/"], a[href*="/reel/"], a[href*="permalink"]';
  for (const a of document.querySelectorAll(sel)) {
    const al = a.getAttribute('aria-label') || a.getAttribute('title') || '';
    if (al) out.push(al);
    const inner = a.querySelector('[aria-label]');
    if (inner) {
      const t = inner.getAttribute('aria-label') || '';
      if (t) out.push(t);
    }
  }
  return out.slice(0, 60);
}
"""

_MONTHS = {m.lower(): i for i, m in enumerate(MONTHS, start=1)}
# "Friday 7 August 2026 at 14:14" / "7 August 2026" / "August 7, 2026"
RE_ARIA_DMY = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\b")
RE_ARIA_MDY = re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})\b")


def parse_aria_date(label: str) -> Optional[str]:
    """An aria-label timestamp -> 'YYYY-MM-DD', or None if it isn't one."""
    for rx, order in ((RE_ARIA_DMY, "dmy"), (RE_ARIA_MDY, "mdy")):
        if m := rx.search(label or ""):
            day, mon, year = (m.group(1), m.group(2), m.group(3)) if order == "dmy" \
                else (m.group(2), m.group(1), m.group(3))
            month = _MONTHS.get(mon.lower()[:3]) or _MONTHS.get(mon.lower())
            if not month:
                # try full-name match (MONTHS holds full names)
                month = next((i for i, name in enumerate(MONTHS, 1)
                              if name.lower().startswith(mon.lower()[:3])), None)
            if not month:
                continue
            try:
                dt = datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
            except ValueError:
                continue
            # a post cannot be in the future; a stamp before Facebook existed
            # is not a post date either
            now = datetime.now(timezone.utc)
            if 2004 <= dt.year and dt <= now:
                return dt.date().isoformat()
    return None


async def dom_last_post(page) -> str:
    """Newest post date read off post-permalink aria-labels. '' when the
    page shows no dated post."""
    try:
        labels = await page.evaluate(JS_POST_TIMES)
    except Exception:
        return ""
    dates = [d for d in (parse_aria_date(x) for x in labels or []) if d]
    return max(dates) if dates else ""


def read_location(row: Row, h: Harvest) -> None:
    """WHAT: the profile's stated city/hometown, into `row.location`. HOW:
    three tiers -- the entity's own scoped location fields (K_LOCATION)
    first, then a "Lives in"/"From" text-regex scan of the rendered About
    tab, then the unscoped graphql fallback -- each candidate validated by
    `shared/text.py::is_place` before being accepted, so marketing copy
    that happens to start with "From" (see RE_FROM's own comment for the
    live false-positive this guards) never becomes a fabricated location.
    LINKED TO: called from Scraper.process() after the About-tab visit,
    not from read_profile() -- location is not on the main timeline."""
    for v in h.ent_strs(K_LOCATION):
        if is_place(v):
            row.location = v.strip()
            return
    for rx in (RE_LIVES_IN, RE_FROM):
        if m := rx.search(h.all_text()):
            if is_place(m.group(1)):
                row.location = m.group(1).strip(" ,·|")
                return
    for v in h.gql_strs(K_LOCATION):
        if is_place(v):
            row.location = v.strip()
            return


def read_pic(row: Row, h: Harvest) -> None:
    """WHAT: the profile picture, into `row.profile_pic_url` +
    `row.has_custom_pic`. HOW: four tiers in trust order -- the entity's
    own scoped picture-uri paths, the DOM header avatar, the page's
    og:image meta tag, the unscoped graphql fallback -- upgraded to full
    resolution via hd_picture_url() and checked against RE_DEFAULT_PIC to
    tell a real upload from Facebook's own silhouette placeholder. LINKED
    TO: called from read_profile() below."""
    # the entity's own picture, else the header avatar, not whichever
    # fbcdn URL happened to appear first in the pile
    url = h.ent_path(
        "profile_picture.uri",
        "profile_picture_for_sticky_bar.uri",
        "delegate_page.profile_picture.uri",
    )
    tag = "graphql"
    if not url:
        url, tag = str(h.dom.get("avatar") or "").strip(), "dom-avatar"
    main = h.html.get("main", "")
    if not url:
        if m := re.search(
            r'property=["\']og:image["\'][^>]+content=["\']' r'([^"\']+)', main
        ):
            url, tag = m.group(1).replace("&amp;", "&"), "og:image"
    if not url:
        for v in h.gql_strs(K_PIC):
            if v.startswith("http") and "fbcdn" in v:
                url, tag = v, "graphql-loose"
                break
    if url:
        row.mark("logo", tag)
        # whatever the source, fbcdn signs the crop range up to `cstp`, not
        # the tiny `ctp` thumbnail actually requested, this recovers the
        # real uploaded photo instead of a 40-60px snippet thumbnail
        row.profile_pic_url = hd_picture_url(url)
        row.has_custom_pic = not bool(RE_DEFAULT_PIC.search(url))
    elif RE_DEFAULT_PIC.search(main):
        row.has_custom_pic = False


def read_verified(row: Row, h: Harvest) -> None:
    """WHAT: the real, platform-issued verification badge, into
    `row.verified`. HOW: reads the DOM header's own detection of
    `svg[title="Verified account"]` (see Scraper.JS_HEADER) -- there is
    no GraphQL field for this that has been found reliable, so DOM is the
    only tier. LINKED TO: called from read_profile() below."""
    # only ever set True on an actual detection, never write False, since
    # a scroll/settle timing miss on one visit must not erase a badge this
    # or an earlier visit already confirmed (see Row.verified's docstring).
    if h.dom.get("verified"):
        row.verified = True
        row.mark("verified", "dom-header")


def read_profile(row: Row, h: Harvest) -> None:
    """Everything available from the timeline visit."""
    read_name(row, h)
    read_counts(row, h)
    read_last_post(row, h)
    read_pic(row, h)
    read_verified(row, h)


# Scraper
# Drives a logged-in browser over Facebook profiles and reads their fields.
#
# This module owns the visit sequence. The browser session itself is
# discovery_engine.py's FacebookSession; turning what a visit collected into
# report fields is read_* above; holding and scoping the payloads is Harvest.


class Scraper:
    """One logged-in browser session, driven over a list of profiles.

    LINKED TO: `analysis_path` in backend/platforms/registry.py names this
    class (loaded dynamically, by import path string -- see that module's
    docstring for why not a direct import), and backend/services/
    analysis_service.py is the actual caller: one Scraper per analysis run,
    driven via `run()`/`run_parallel()` or `one()` for a single profile.
    """

    # callers normalise URLs without knowing which platform they are holding
    normalize_url = staticmethod(normalize_url)

    def __init__(
        self,
        args,
        cookies: list[dict],
        session_id: str = "",
        proxy: Optional[dict] = None,
    ):
        """WHAT: binds this Scraper to one FacebookSession (a fresh
        browser context) built from `cookies`. HOW: `args` is a
        ScanOptions-shaped object (backend/platforms/scan_options.py)
        carrying pacing knobs and the evidence GridFS prefix; images are
        allowed to load only when evidence capture is on, since a
        screenshot with every image blocked is useless as impersonation
        proof."""
        self.a = args
        self.evidence = args.evidence or None  # GridFS key prefix, not a path
        # evidence screenshots need images, so the session must not block them
        self.session = FacebookSession(
            args,
            cookies,
            load_images=captures_screenshot(args),
            session_id=session_id,
            proxy=proxy,
        )

    # ───────────────────────────── browser ────────────────────────────── #

    @property
    def ctx(self):
        """The live Playwright BrowserContext, once `start()` has run."""
        return self.session.ctx

    async def start(self):
        """Launches the browser context (delegates to FacebookSession/
        stealth/browser.py::Session.start())."""
        await self.session.start()

    async def stop(self):
        """Closes the browser context and its Playwright driver."""
        await self.session.stop()

    async def pause(self, mult=1.0):
        """Between-profile pacing (jittered, fatigue-aware) -- see
        stealth/human.py. `mult` scales the base delay for a slower or
        faster step than usual."""
        await self.session.pause(mult)

    async def check_session(self) -> bool:
        """Is this cookie set still logged in and unchallenged? Delegates
        to FacebookSession.check_session() above."""
        return await self.session.check_session()

    # ─────────────────────────── page scripts ─────────────────────────── #

    # Ready when the profile's own payload has landed: the social-context block
    # plus, once we know it, the entity id. Everything we extract is present at
    # that point, typically under 2s, so waiting a fixed 3.5s is dead time.
    JS_READY = """
    (needle) => {
      let ctx = false, id = !needle;
      for (const el of document.querySelectorAll('script[type="application/json"]')) {
        const t = el.textContent || "";
        if (!ctx && t.includes('profile_social_context')) ctx = true;
        if (!id && t.includes('"id":"' + needle + '"')) id = true;
        if (ctx && id) return true;
      }
      return false;
    }
    """

    # The server render ships far more GraphQL in <script type="application/json">
    # than the XHR traffic does, that is where the profile entity lives.
    JS_EMBEDDED = (
        "() => Array.from(document.querySelectorAll("
        "'script[type=\"application/json\"]')).map(s => s.textContent)"
        ".filter(t => t && t.length > 40)"
    )

    # Reads the profile header itself: the name is the line directly above the
    # counter chip, the avatar is the largest <image> on the page, and
    # "set=pb.<id>." photo links give us the owner's numeric id even when the
    # URL is a vanity slug.
    JS_HEADER = """
    () => {
      const lines = (document.body.innerText || "").split("\\n").map(s => s.trim());
      let name = "", followers = "", counter = "";
      for (let i = 0; i < lines.length; i++) {
        // the header counter line: "70 followers - 8 following" on creators,
        // "53 friends" on personal profiles, "154M followers" on pages
        const m = /^([\\d][\\d.,\\s]{0,15}[KMB]?)\\s*(followers?|friends?|likes)\\b/i.exec(lines[i]);
        if (!m) continue;
        counter = lines[i];
        const fm = /([\\d][\\d.,\\s]{0,15}[KMB]?)\\s*followers?\\b/i.exec(lines[i]);
        if (fm) followers = fm[1];
        for (let j = i - 1; j >= 0 && j >= i - 4; j--) {
          const c = lines[j];
          if (c && c.length < 80 && !/^[\\d,.]+$/.test(c) &&
              !/notification|^search$|^facebook$|^add friend$|^follow$/i.test(c)) {
            name = c; break;
          }
        }
        break;
      }
      let postAuthor = "";
      for (const e of document.querySelectorAll('[aria-label]')) {
        const l = e.getAttribute('aria-label') || "";
        if (/^Actions for this post by /i.test(l)) {
          postAuthor = l.replace(/^Actions for this post by /i, "").trim();
          break;
        }
      }
      let avatar = "", best = -1;
      for (const im of document.querySelectorAll('svg image')) {
        const href = im.getAttribute('xlink:href') || im.getAttribute('href') || "";
        const w = im.getBoundingClientRect().width;
        if (href && w > best) { best = w; avatar = href; }
      }
      const pbIds = Array.from(document.querySelectorAll('a[href*="set=pb."]'))
        .map(a => ((a.getAttribute('href') || "").match(/set=pb\\.(\\d+)\\./) || [])[1])
        .filter(Boolean);
      // the real, platform-issued badge -- confirmed live against
      // facebook.com/facebook: <svg role="img" title="Verified account">
      const verified = !!document.querySelector('svg[title="Verified account"]');
      return {name, followers, counter, postAuthor, avatar, pbIds, verified};
    }
    """

    # ──────────────────────────── collection ──────────────────────────── #

    async def visit(
        self, page, url, h: Harvest, tag, scrolls=0, needle: Optional[str] = None
    ) -> bool:
        """WHAT: navigates `page` to `url`, waits for data readiness, and
        stores the resulting HTML/text/embedded-JSON into `h` under `tag`
        (a Harvest visit-tab key, e.g. "main"/"about"). Returns whether
        the page produced any HTML at all. HOW: if `needle` (a numeric
        entity id) is given, polls JS_READY for that id's own payload to
        land before reading anything, capped at `self.a.settle` seconds;
        otherwise a flat short wait. Scrolls `scrolls` times afterward if
        asked (the main timeline visit only). LINKED TO: called from
        process() below for every tab a profile visit touches (main,
        about, about_profile_transparency)."""
        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=self.a.timeout * 1000
            )
        except Exception:
            return False
        try:
            if needle is not None:
                try:
                    await page.wait_for_function(
                        self.JS_READY, arg=needle, timeout=self.a.settle * 1000
                    )
                except Exception:
                    # gone, login-walled or an unusual layout, fall through and
                    # let the field readers and status checks report what they see
                    await page.wait_for_timeout(1500)
            else:
                await page.wait_for_timeout(1500)
            for _ in range(scrolls):
                await page.mouse.wheel(0, 2600)
                await page.wait_for_timeout(1200)
        except Exception:
            pass
        try:
            h.html[tag] = await page.content()
        except Exception:
            h.html[tag] = ""
        try:
            h.text[tag] = await page.inner_text("body")
        except Exception:
            h.text[tag] = re.sub(r"<[^>]+>", " ", h.html.get(tag, ""))
        try:
            h.add_embedded(await page.evaluate(self.JS_EMBEDDED))
        except Exception:
            pass
        return bool(h.html[tag])

    # How long read_dom will wait for the photo-album anchors that
    # owner_id() reads, and ONLY on a vanity URL, where they are the
    # fallback that resolves the numeric id. See the note in read_dom.
    _PB_ID_WAIT_MS = 4000

    async def read_dom(self, page, h: Harvest, scrolled: bool = False,
                       await_ids: bool = False) -> None:
        """WHAT: runs JS_HEADER against `page` and stores the result on
        `h.dom` (name, follower-chip text, avatar, verified badge, the
        pbIds used to resolve a vanity URL's numeric id -- see
        Scraper.owner_id). HOW: if the page has been scrolled, scrolls
        back to the top first, since the profile intro block can unmount
        off-screen. LINKED TO: called once per visit from process().

        `await_ids` waits (briefly, bounded) for the photo-album anchors
        before reading. It is set only for a VANITY url, where those
        anchors are what owner_id() resolves the numeric id from.

        Why it is needed: visit()'s readiness poll is given an EMPTY
        needle for a vanity url (there is no numeric id to wait for yet),
        and JS_READY treats an empty needle as "the id half is already
        satisfied" -- so it returns as soon as the social-context PAYLOAD
        lands, which happens well before the photo-album anchors render.
        read_dom then saw an empty pbIds list and resolve_id fell through
        to "id unresolved -- fields not scope-verified".

        Confirmed live 2026-08-23 on facebook.com/AdaniOnline: the real
        visit reported the id unresolved, while the same page given ~9s
        exposed six anchors, unanimously 100064457091354 (corroborated by
        `owning_profile_id` in the payloads). Unresolved is not cosmetic:
        every field then comes from the UNSCOPED gql_* readers instead of
        this profile's own entity, and the row files under the vanity slug
        rather than the numeric id, so the same profile reached by its two
        URL shapes becomes two rows."""
        try:
            if scrolled:
                # scrolling can unmount the intro block, go back up first
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(700)
            if await_ids:
                try:
                    await page.wait_for_selector(
                        'a[href*="set=pb."]', timeout=self._PB_ID_WAIT_MS)
                except Exception:
                    # No album anchors on this profile at all (a brand-new
                    # or photo-less account). resolve_id reports it
                    # honestly rather than this failing the visit.
                    pass
            h.dom = await page.evaluate(self.JS_HEADER) or {}
        except Exception:
            h.dom = {}

    async def screenshot(self, page, row: Row) -> None:
        """WHAT: captures the evidence PNG into GridFS. HOW: waits for
        visible content first so the capture is not a half-painted page,
        under a DETERMINISTIC key so re-analysing overwrites its own
        previous capture rather than accumulating one per run.
        Best-effort: a failed capture never fails the visit. LINKED TO:
        database/repositories/evidence_repository.py owns the store;
        row.screenshot holds that key, not a filesystem path."""
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
            # JS_READY (above) is a DATA-readiness check, it can pass while
            # the screen is still a bare loading splash, since it reads
            # embedded JSON, never the rendered page. See
            # Session.wait_for_visible_content for why this is separate.
            # A post permalink is Facebook's "the feed has painted" signal.
            # Without it this returned in 0.07s -- the character floor is
            # met by the page's own chrome, so it was never waiting for
            # anything, and the capture showed a header above an empty feed.
            await self.session.wait_for_visible_content(
                page, content_selector=POST_LINK_SELECTOR)
            data = await page.screenshot(full_page=False)
            
            if self.evidence:
                from backend.database.repositories import evidence_repository
                await evidence_repository.save(key, data)
                row.screenshot = key
                
            if getattr(self.a, 'ephemeral_screenshot', False):
                row.screenshot_bytes = data
        except Exception:
            pass

    # ───────────────────────────── identity ───────────────────────────── #

    @staticmethod
    def entity_id_for(h: Harvest, url: str) -> str:
        """Numeric id for a vanity URL, taken from the payloads.

        The entity that owns the page carries its own canonical url/vanity, so
        matching the slug against those gives the id without trusting the DOM.
        """
        slug = profile_id(url).lower()
        if not slug or slug.isdigit():
            return ""
        for blob in h.mentioning(slug):
            for d in iter_dicts(blob):
                i = d.get("id")
                if not (isinstance(i, str) and i.isdigit()):
                    continue
                for k in ("url", "profile_url"):
                    v = d.get(k)
                    if isinstance(v, str) and v.lower().rstrip("/").endswith(
                        "/" + slug
                    ):
                        return i
                for k in ("vanity", "userVanity", "username"):
                    v = d.get(k)
                    if isinstance(v, str) and v.lower() == slug:
                        return i
        return ""

    @staticmethod
    def owner_id(h: Harvest) -> str:
        """Most frequent id in the profile's own photo-album links."""
        ids = h.dom.get("pbIds") or []
        return max(set(ids), key=ids.count) if ids else ""

    def resolve_id(self, row: Row, h: Harvest, url: str) -> str:
        """Vanity URLs carry no numeric id, ask the payloads, then the DOM."""
        if row.profile_id.isdigit():
            return row.profile_id
        pid = self.entity_id_for(h, url) or self.owner_id(h)
        if not pid:
            row.note("id unresolved -- fields not scope-verified")
        else:
            row.mark("id", pid)
            # adopt the numeric id as this row's identity. Discovery stores the
            # numeric id, so leaving the vanity slug here would file the same
            # profile twice, once per URL shape.
            row.profile_id = pid
        return pid

    # A rendered "915 followers" / "12 friends" / "0 following" line. Present
    # on every live profile and Page; absent from a removed one, whose body
    # is nothing but the unavailable-placeholder and Facebook's own chrome.
    RE_AUDIENCE_LINE = re.compile(r"[\d][\d,.\s]*[KMB]?\s*(followers?|friends?|following)\b", re.I)

    @classmethod
    def has_profile_content(cls, h: Harvest, txt: str) -> bool:
        """Did this page carry the profile's OWN data, whatever else is on it?

        Facebook reuses ONE string -- "This content isn't available at the
        moment" -- for two completely different situations:

          * the profile/Page/group itself is removed, and
          * a single restricted or deleted POST sitting in a live profile's
            timeline (or a deleted comment under one).

        Confirmed live against all 8 profiles this engine had marked GONE:
        5 of them were fully alive (343k / 915 / 193 / 90 / 14 followers,
        real names, full profile chrome) and carried that placeholder purely
        because one item in their feed was restricted. The wording is
        byte-identical in both cases, so no amount of tightening RE_GONE can
        separate them -- the text genuinely does not know the difference.

        What does separate them, cleanly, is whether the profile's own
        audience number came back: on the 3 genuinely-removed pages the body
        was 257 characters of pure chrome with no count anywhere, in the
        payload or the DOM; on all 5 live ones a real count was readable.
        """
        if [n for n in h.ent_ints(K_FOLLOWERS) if 0 <= n < MAX_FOLLOWERS]:
            return True
        if [n for n in h.gql_ints(K_FOLLOWERS) if 0 <= n < MAX_FOLLOWERS]:
            return True
        return bool(cls.RE_AUDIENCE_LINE.search(txt))

    @classmethod
    def blocked_status(cls, row: Row, page_url: str, txt: str, h: Harvest) -> bool:
        """Session or availability problems that make field reading pointless."""
        if "/checkpoint" in page_url or RE_CHECKPOINT.search(txt):
            row.status, msg = "CHECKPOINT", "session checkpointed"
        elif "/login" in page_url or RE_LOGIN.search(txt):
            row.status, msg = "LOGIN_REQUIRED", "cookies rejected/expired"
        # The content gate is what stops a live profile being written off as
        # taken down because one post in its timeline is restricted -- see
        # has_profile_content. Without it this returned GONE for 5 of the 8
        # profiles it was applied to, discarding every field on a page that
        # had just handed over the name and follower count, and, because
        # GONE is terminal in shared/completeness.py, marking the row
        # analysis_complete so it was never retried either.
        elif RE_GONE.search(txt) and not cls.has_profile_content(h, txt):
            row.status = "GONE"
            msg = "removed or unavailable -- may already be taken down"
        else:
            return False
        row.note(msg)
        return True

    # ───────────────────────────── per URL ────────────────────────────── #

    async def process(
        self, raw_url: str, target: str, feed: str, known: Optional[dict] = None,
    ) -> Row:
        """One profile URL, start to finish -- the whole engine's core
        loop, the counterpart to discovery_engine.py's Discovery.sweep().

        Every field but one is still derived from this visit alone,
        regardless of `known` (whatever discovery already read for this
        URL, see analysis/runner.py's `seed_by_url`) -- they all come from
        the SAME main-timeline visit a status/screenshot/last-post read
        needs anyway, so skipping them would save nothing. `location` is
        the exception: it lives behind the two About-tab visits at step 6
        below, a real extra cost, so when discovery already has one those
        visits are skipped rather than re-confirming it.

        WHAT IT RETURNS: a scored `Row` (shared/models/row.py), status OK/
        PARTIAL/GONE/ERROR/CHECKPOINT/LOGIN_REQUIRED, every field this
        engine reads tagged with `row.mark()` so its provenance survives
        into the stored document (see shared/completeness.py::field_report,
        which reads those tags).

        HOW, roughly in order:
          1. Visit the main timeline (`visit()`), bail out to ERROR/
             CHECKPOINT/LOGIN_REQUIRED/GONE if the session or the profile
             itself blocks it (`blocked_status()`).
          2. Read the DOM header (`read_dom()`), resolve a vanity URL to
             its numeric id (`resolve_id()`), and scope the Harvest to
             that id (`h.scoped()`).
          3. Sniff entity_type (group/page/profile) from the URL shape and
             payload `__typename`.
          4. Run every field reader (`read_profile()` -> read_name/
             read_counts/read_last_post/read_pic/read_verified), then
             capture the evidence screenshot.
          5. Fall back to the DOM aria-label last-post date
             (`dom_last_post()`) if the payload carried none.
          6. Visit the About and About-transparency tabs for `read_location`,
             the one field the timeline itself rarely carries.

        LINKED TO: called by `one()` below (which turns an exception into
        an ERROR row instead of crashing the whole batch), which `run()`/
        `run_parallel()` call once per URL in a job.
        """
        url = normalize_url(raw_url)
        row = Row(url=url, target=target, original_feed=feed)
        row.profile_id = profile_id(url)
        if known and known.get("location"):
            row.location = known["location"]
            row.mark("location", "discovery")

        page = await self.ctx.new_page()
        h = Harvest()

        async def on_response(resp):
            """Feeds every /api/graphql body this visit fires into the
            Harvest. Silent on failure: a response that cannot be read is
            one tier of one field, never a reason to fail the visit."""
            try:
                if "/api/graphql" in resp.url and resp.request.resource_type in (
                    "xhr",
                    "fetch",
                ):
                    h.add_gql(await resp.text())
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        try:
            needle = row.profile_id if row.profile_id.isdigit() else ""
            if not await self.visit(
                page, url, h, "main", scrolls=self.a.scrolls, needle=needle
            ):
                row.status = "ERROR"
                row.note("main nav failed")
                return row

            txt = h.text.get("main", "")
            if self.blocked_status(row, page.url, txt, h):
                if row.status == "GONE":
                    read_name(row, h)
                return row

            # `needle` is empty exactly when the URL is a vanity one, which
            # is the only case where the numeric id has to come out of the
            # DOM -- see read_dom's `await_ids`.
            await self.read_dom(page, h, scrolled=self.a.scrolls > 0,
                                await_ids=not needle)
            pid = self.resolve_id(row, h, url)
            hs = h.scoped(pid)

            # the URL itself is the most reliable signal for a group visit:
            # /groups/<id>/ is unambiguous, unlike sniffing a payload
            # __typename that may not even be present (see the Page check
            # right below, which exists precisely because that sniff is a
            # fallback, not a sure thing)
            if "/groups/" in url or (hs.ent_scalar("__typename") or "").lower() == "group" or (
                not hs.ents and re.search(r'"__typename"\s*:\s*"Group"', h.all_html())
            ):
                row.entity_type = "group"
            elif (hs.ent_scalar("__typename") or "").lower() == "page" or (
                not hs.ents
                and re.search(
                    r'"__typename"\s*:\s*"Page"|Page transparency', h.all_html()
                )
            ):
                row.entity_type = "page"

            read_profile(row, hs)
            await self.screenshot(page, row)

            # Payload timestamps first (read_profile -> read_last_post,
            # above); the rendered page second. Only runs when the payload
            # carried no usable post time, so a normal visit costs nothing
            # extra, and it reads an exact absolute timestamp out of the
            # permalink's aria-label rather than doing arithmetic on the "3d"
            # a human sees.
            #
            # Deliberately BEFORE the About-tab visits below, not after:
            # confirmed live this used to be dead code in production, the
            # two About-tab page visits happen unconditionally, and by the
            # time this used to run at the end of process(), page.url had
            # already moved to ".../about", a page with no post permalinks
            # at all, so this fallback always found nothing and never
            # actually fell back to anything. Running it here, while the
            # page is still the timeline that was just visited, is what
            # makes it work.
            if not row.last_post_iso and row.posts_seen != "no":
                iso = await dom_last_post(page)
                if iso:
                    row.last_post_iso = iso
                    row.posts_seen = "yes"
                    row.mark("last_post", "dom-aria")

            # The main profile page rarely carries a location. Facebook Pages
            # put their city/country on the About tab instead, so this visit
            # happens unconditionally when location isn't already known:
            # accuracy on a field the report actually promises beats saving
            # one page load. When discovery already read one for this URL
            # (row.location pre-filled from `known`, above), it's treated as
            # already accurate and these two visits are skipped instead.
            # Join/creation date is deliberately NOT read here (or anywhere
            # in this engine, see ADR/product decision): Facebook does not
            # expose it to an ordinary session at all, not in the rendered
            # tab and not in any payload, so attempting it never succeeded
            # and wasn't worth the two extra page loads either way.
            if not row.location:
                await self.pause(0.4)
                for sk in ("about_profile_transparency", "about"):
                    await self.visit(page, tab_url(url, sk), h, sk)
                    await self.pause(0.3)

                read_location(row, h.scoped(pid))

            row.status = "OK" if row.profile_name else "PARTIAL"
            return row
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # ─────────────────────────── orchestration ────────────────────────── #

    async def one(self, u: str, tgt: str, feed: str, known: Optional[dict] = None) -> Row:
        """process() with a failed profile turned into a reportable row."""
        try:
            return await self.process(u, tgt, feed, known)
        except Exception as e:
            row = Row(url=normalize_url(u), target=tgt, original_feed=feed)
            row.profile_id = profile_id(row.url)
            row.status = "ERROR"
            row.note(f"{type(e).__name__}: {e}")
            return row

    @staticmethod
    def report(i: int, total: int, u: str, row: Row) -> None:
        """One progress line per profile, carrying the fields an operator
        needs to spot a silent extraction failure early: a column reading
        "-" on every line means that field has stopped being read."""
        from backend.shared.logging import get_logger as _gl
        _log = _gl("platforms.facebook.analysis")
        _log.info(
            f"[{i}/{total}] {u} | {row.status} name={row.profile_name[:22]} "
            f"followers={row.followers if row.followers is not None else '-'} "
            f"friends={row.friends if row.friends is not None else '-'} "
            f"active={row.active_yes or '-'} risk={row.risk} {row.priority}"
        )

    async def run(self, jobs: list[tuple[str, str, str]]) -> list[Row]:
        """WHAT: drives a whole batch of (url, target, feed) jobs. HOW:
        hands off to run_parallel() when concurrency allows it, otherwise
        walks them one at a time with pacing between profiles, aborting on
        the first CHECKPOINT unless `keep_going` was set -- a challenge
        means Facebook is already suspicious, and continuing is what turns
        it into a dead session. Rows gathered before the abort are still
        returned. LINKED TO: the standalone entry point; the API path
        drives one() through services/analysis_service.py."""
        from backend.shared.logging import get_logger as _gl
        _run_log = _gl("platforms.facebook.analysis")
        if getattr(self.a, "concurrency", 1) > 1:
            return await self.run_parallel(jobs)
        rows: list[Row] = []
        for i, (u, tgt, feed) in enumerate(jobs, 1):
            row = await self.one(u, tgt, feed)
            rows.append(row)
            self.report(i, len(jobs), u, row)
            if row.status == "CHECKPOINT" and not self.a.keep_going:
                _run_log.warning("CHECKPOINT -- aborting to avoid burning the session.")
                break
            if i < len(jobs):
                await self.pause()
        return rows

    async def run_parallel(self, jobs: list[tuple[str, str, str]]) -> list[Row]:
        """Several tabs at once, same session. Faster, and more conspicuous."""
        sem = asyncio.Semaphore(self.a.concurrency)
        done = 0

        async def worker(idx: int, job: tuple[str, str, str]) -> tuple[int, Row]:
            """One profile in its own tab, holding a concurrency slot.
            Starts staggered so several tabs do not hit Facebook in the
            same instant, and returns its index so the caller can restore
            the caller's original job order."""
            nonlocal done
            async with sem:
                await asyncio.sleep(idx % self.a.concurrency * 1.5)  # stagger
                row = await self.one(*job)
                done += 1
                self.report(done, len(jobs), job[0], row)
                await self.pause(0.5)
                return idx, row

        pairs = await asyncio.gather(*(worker(i, j) for i, j in enumerate(jobs)))
        return [r for _, r in sorted(pairs, key=lambda p: p[0])]
