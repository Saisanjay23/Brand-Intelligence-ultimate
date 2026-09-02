"""Profile persistence, the `profiles` collection, one document per
`(client_id, platform, url)`. Every platform's discovery/analysis results
land in the same collection, distinguished by a `platform` field, so
"every profile for this client" is one query, not a fan-out across
per-platform databases (see docs/adr/0004, the reasoning for a single
shared document per profile is unchanged from the original design, just
the physical storage got simpler).

Writes are field-scoped on purpose: discovery must never blank the analysis
fields of a profile it rediscovers, and an analyst's approve/reject must
never be undone by the next sweep. This is the single most important
invariant in the whole engine, it's what makes a daily re-sweep safe to
run unattended.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.config.settings import settings
from backend.shared.errors import ConflictError, NotFoundError, ValidationError
from backend.shared.logging import get_logger
from backend.shared.models.scoring import MEDIUM_MATCH_THRESHOLD, NAME_THRESHOLD
from backend.database.connection import db

log = get_logger("repositories.profile_repository")

PROFILES = "profiles"

PHASE_DISCOVERY = "discovery"
PHASE_ANALYSIS = "analysis"

# fields discovery is allowed to write; everything else is analysis territory
#
# followers/friends/location/bio/created_at were added alongside the
# discovery/analysis engine rebuild ("One Pass or Two" research): Twitter's
# SearchTimeline response and Telegram's plain SearchRequest result already
# carry these fields, parsed and then discarded before this change (see
# twitter/discovery_engine.py::user_to_row and
# telegram/discovery_engine.py::entity_to_row) -- without them here, save()'s
# field-scoped `$set` (owned = DISCOVERY_FIELDS for a discovery-phase write,
# below) would silently drop values discovery now legitimately has. They
# remain in ANALYSIS_FIELDS too: the same document field either phase can
# populate, whichever ran most recently and actually had a value.
DISCOVERY_FIELDS = (
    "entity_id", "username", "display_name", "entity_type",
    "discovery_source", "profile_image_url", "has_logo", "verified", "name_score",
    "name_exact_run", "followers", "friends", "location", "bio", "created_at",
)

# NOTE: `entity_id` is deliberately NOT here. It is the dedup key, and
# discovery is the only phase that observes it from an authoritative source
# (the platform's own search edge, always the canonical numeric/stable id).
# Analysis derives it from the URL it was handed and only upgrades a vanity
# slug to a numeric id when the profile page happens to expose one, when
# it doesn't, `row.profile_id` stays the slug. Letting that be written back
# would overwrite a correct numeric id with a slug, after which the next
# sweep's numeric-id hit no longer matches this document, tries to insert,
# and collides on the unique url index. Identity upgrades go through
# `promote_entity_id()` below, which only ever fills a blank or replaces a
# non-numeric id with a numeric one, never the reverse.
ANALYSIS_FIELDS = (
    "display_name", "entity_type", "target", "official_feed",
    "followers", "followers_exact", "friends", "location", "profile_image_url",
    "has_logo", "verified", "is_active", "has_name_match", "name_score", "name_exact_run",
    "last_post_date", "risk_score", "priority", "comments", "analysis_status", "sources",
    # The account's own join date and bio text. Both are read by the
    # engines on every visit and both used to die at this whitelist -- see
    # the note in services/analysis_service.py where they are mapped.
    # save() drops "" on its own, so a platform that genuinely cannot see
    # one (Facebook and Instagram do not expose a join date) simply never
    # writes the key, rather than storing a blank over something real.
    "created_at", "bio",
    # "yes" | "no" -- whether the profile was confirmed to HAVE posts at all.
    # Every engine has always computed this (it is what shared/completeness.py
    # uses to avoid re-queueing an account that genuinely never posted) and
    # every engine's value was then thrown away here, because this tuple is
    # the whitelist save() writes through. Without it stored, a blank
    # last_post_date is identical in the database whether the account has no
    # posts or its timeline failed to load -- the single distinction the
    # analysis pipeline most needs to be able to make. save() drops ""
    # (unknown) on its own, so only a real yes/no is ever persisted.
    "posts_seen",
    # Per-field verdicts from shared/completeness.py::field_report -- which
    # fields were read, which are genuinely absent at the profile, which this
    # platform never collects, and which were actually MISSED. Stored so the
    # answer to "why is this cell blank" survives the run that determined it.
    "field_status",
    # the GridFS key this profile's evidence screenshot is stored under
    # (see database/repositories/evidence_repository.py), not a filesystem
    # path, so the stored value stays valid across a redeploy with no volume
    # to remount. Served through `GET /profiles/{id}/screenshot`; see
    # api/profile_routes.py.
    "screenshot", "screenshot_at",
    # False when the analysis engine reached this profile but came away
    # without a field the platform does publish (see
    # shared/completeness.py). Distinct from `analysis_status`, which
    # only says whether the page was reached at all: a row can be OK and
    # still be missing its last-post date or its evidence screenshot,
    # and that used to make it permanently ineligible for another look.
    "analysis_complete",
)

# An analysis outcome that means "we did not actually get to look at this
# profile", as opposed to a real reading. A row in one of these states must
# stay in the analysis queue rather than being treated as done, see
# `urls_for(exclude_analysed=True)`. ERROR/CHECKPOINT/LOGIN_REQUIRED are all
# transient environment failures (timeout, dead proxy, session challenged
# mid-run), not verdicts about the profile.
#
# PARTIAL joined this list after a live check: 11 Instagram PARTIAL rows
# were found stuck at analysis_attempts=0, literally never eligible for a
# second try, because only a RETRYABLE status ever increments that counter.
# All 11 carried the same "profile payload not seen" note, a timing/render
# issue on that visit, not a verdict about the profile, re-running one of
# them live (outside this codebase, as a direct check) succeeded cleanly on
# the very next attempt. PARTIAL was also missing from publish()'s guard, so
# a row with no name/followers/date/location was eligible to be published as
# a client-facing incident with silently incomplete data. Both are now fixed
# by the one change: PARTIAL rows re-enter the analysis queue (bounded by
# MAX_ANALYSIS_ATTEMPTS, same as the other three) and cannot be published
# until they clear it or exhaust their retries and surface via
# stuck_analysis() instead.
RETRYABLE_ANALYSIS_STATUSES = ("ERROR", "CHECKPOINT", "LOGIN_REQUIRED", "PARTIAL")

# An analysis outcome that means the profile WAS read successfully. Anything
# else -- a retryable failure above, or the terminal GONE -- is a row whose
# blank columns are a symptom, which is exactly what the Data Quality
# filter's "Incomplete / broken" option is for (see _incomplete_clause).
# "" covers a row that reached the analysis phase without an outcome being
# recorded at all.
OK_ANALYSIS_STATUSES = ("OK", "")


def _blank_field(field: str) -> dict:
    """Mongo's "this field was never read". `None` already matches a missing
    key as well as an explicit null, `$exists` is spelled out anyway so the
    intent survives someone reading the query in isolation.

    Numeric 0 is deliberately NOT blank -- a brand-new account really can
    have 0 followers, and calling that unread would flag a correct reading
    as broken. Mirrors shared/completeness.py::_blank, which is the same
    rule applied to a live Row rather than a stored document.
    """
    return {"$or": [{field: {"$exists": False}}, {field: None}, {field: ""}]}


def _incomplete_clause() -> dict:
    """"This analysed row did not come back with usable data", as a query
    over what is actually STORED on a profile document.

    Deliberately NOT `{"analysis_complete": False}`: that field only exists
    on rows analysed after shared/completeness.py was introduced (614 of 934
    analysis rows in the live database predate it and carry no such field at
    all), so filtering on it would silently miss most of the broken rows an
    analyst is looking for. Evaluating the underlying fields directly is what
    makes this work on legacy rows too.

    Each clause mirrors one of shared/completeness.py::missing_fields' rules,
    restricted to the ones a stored document can answer honestly:

      * no display name -- the headline symptom (the table falls back to
        showing the raw URL, which is what "missing profile name" looks
        like on screen)
      * no audience number at all: BOTH `followers` and `friends` unread.
        Either one alone is a complete reading -- a Facebook Page publishes
        followers, a personal profile publishes friends (see
        facebook/analysis_engine.py::followers_from_friends) -- so requiring
        both to be blank is what keeps ~30 correctly-read personal profiles
        from being reported broken. Groups are exempt entirely: they publish
        a member count under neither field.
      * no evidence screenshot -- the takedown deliverable itself
      * an analysis outcome that is not a clean read (a retryable failure,
        or GONE)

      * a last-post date missing from a profile CONFIRMED to have posts.
        `posts_seen` is the engine's own verdict, now persisted (see
        ANALYSIS_FIELDS): "yes" means the timeline was read and does carry
        posts, so failing to date them is a real miss. "no" means the
        account genuinely never posted -- absent on purpose, not a gap.
        Absent/"" means the engine could not tell (a legacy row saved before
        this was stored, or a drifted parser) and is deliberately NOT
        flagged: guessing would sweep in every legitimately postless and
        private account, which is exactly the "missing means we failed to
        read it, never this account has none" mistake shared/completeness.py
        warns against. Those rows surface through LastPostExtractionDrift
        instead, which is the signal actually designed for them.
    """
    return {"$or": [
        _blank_field("display_name"),
        {"$and": [
            _blank_field("followers"),
            _blank_field("friends"),
            {"entity_type": {"$ne": "group"}},
        ]},
        _blank_field("screenshot"),
        {"analysis_status": {"$nin": list(OK_ANALYSIS_STATUSES)}},
        # Authoritative when present: the engine's own stored verdict, so
        # this filter and shared/completeness.py::field_report can never
        # disagree about the same row.
        {"field_status.last_post_date": "MISSED"},
        # Legacy rows carry no field_status (they predate it). For those,
        # fall back to the one unambiguous signal: posts confirmed present
        # but undated. Scoped to `field_status` being absent so a current
        # row is judged only by the verdict above.
        {"$and": [
            {"field_status": {"$exists": False}},
            _blank_field("last_post_date"),
            {"posts_seen": "yes"},
        ]},
    ]}

# How many times a single profile may fail analysis before it stops being
# retried. Without a cap, a genuinely dead URL (deleted account that still
# resolves, a permanent redirect loop) is re-attempted on every catch-up
# sweep forever, spending real page loads under a live session on nothing.
MAX_ANALYSIS_ATTEMPTS = 4

# what an analyst may correct by hand, a whitelist, so a stray PATCH cannot
# rewrite scraped evidence unlabelled
EDITABLE = {
    "has_logo", "is_active", "has_name_match", "risk_score", "priority",
    "comments", "target", "official_feed", "status",
    "display_name", "followers", "location", "last_post_date",
    # an analyst's own visual confirmation that the profile is lifting the
    # brand's logo/photo and/or username, distinct from has_logo (which
    # only says a custom photo exists, not that it matches anything).
    # Outrank the scraper's own has_logo/has_name_match in either direction
    # (see SCORING_FIELDS below and shared/models/scoring.py::resolve_match).
    # Only ever written from the ANALYSIS view now: discovery is Validate or
    # Reject, and validating means both matches hold by default, so these
    # stay unset until an analyst actually corrects one.
    "logo_match", "username_match",
    # an analyst's hand-edits to the computed published-incident preview
    # (see services/incident_publisher.py), flat dotted-path keys, merged
    # into whatever's already stored rather than replacing it wholesale
    # (see the special-casing in patch() below), so editing one field never
    # clobbers another already-saved override.
    "incident_overrides",
}



# each of these carries a `sources.<key>` provenance tag under a DIFFERENT
# key than the document field, a manual edit must relabel the matching key
PROVENANCE_KEYS = {
    "display_name": "name", "followers": "followers", "location": "location",
    "has_logo": "logo", "last_post_date": "last_post",
}

# a manual edit to any of these changes what the risk score/priority ought
# to be, so `patch` must recompute both, not just store the raw value.
# logo_match/username_match included since they now feed the score exactly
# like has_logo/has_name_match do (see compute_risk_score/compute_priority
# below), an analyst reversing a match in the analysis view must
# retrigger the same recompute a fresh scrape would.
#
# `status` is in here because validating a profile is itself a scoring
# event now: an approved profile counts as logo- and username-matched by
# default (scoring.resolve_match), so the moment its status changes its
# score has to be recomputed against that default, otherwise a profile
# would sit at its pre-validation score until some other field happened to
# be edited.
SCORING_FIELDS = {"has_logo", "has_name_match", "location", "last_post_date",
                  "logo_match", "username_match", "status"}


def _stamp_utc_for_api(doc: dict) -> dict:
    """Mongo hands every datetime back naive-but-UTC-VALUED (motor isn't
    tz_aware); stamp UTC explicitly on the way out so a JSON client doesn't
    read the unmarked ISO string as local time."""
    for f in ("first_seen", "last_seen", "changed_at", "publish_hold_until",
              "rejected_at", "screenshot_at", "analysed_at"):
        v = doc.get(f)
        if isinstance(v, datetime) and v.tzinfo is None:
            doc[f] = v.replace(tzinfo=timezone.utc)
    return doc


def _oid(doc_id: str) -> ObjectId:
    try:
        return ObjectId(doc_id)
    except (InvalidId, TypeError):
        raise NotFoundError(f"profile {doc_id!r} not found")


def _is_identity_upgrade(current: str, incoming: str) -> bool:
    """May `incoming` replace `current` as this profile's `entity_id`?

    Only in the two directions that strictly sharpen identity:
      - nothing -> something
      - a vanity slug -> the platform's canonical numeric id / YouTube channel id

    Never numeric -> slug, and never one numeric id -> a different one (that
    is two distinct profiles colliding on a URL, which is a genuine
    integrity problem and must not be papered over by silently rewriting
    the key).
    """
    current, incoming = (current or "").strip(), (incoming or "").strip()
    if not incoming or current == incoming:
        return False
    if not current:
        return True
    if incoming.startswith("UC") and len(incoming) >= 20 and not current.startswith("UC"):
        return True
    return incoming.isdigit() and not current.isdigit()


async def save(
    client_id: str, platform: str, phase: str, fields: dict,
    *, url: str, entity_id: str = "", keyword: str = "", initial_status: str = "pending",
    retry_pending: bool = False,
) -> bool:
    """Upsert one profile. Returns True when newly seen.

    Deduplication is by identity, not URL string, the platform's own id
    wins when available, the URL is the fallback for entities whose id
    could not be resolved. Every URL a profile has been seen at is kept in
    `urls`. Only the fields the calling phase owns (DISCOVERY_FIELDS /
    ANALYSIS_FIELDS) are ever written, so a discovery sweep re-finding an
    already-scored profile never blanks its score, and neither phase ever
    touches the analyst's `status`.

    `initial_status` only ever applies going INTO the doc's very first
    insert, or on top of a still-undecided "pending" one, never over an
    existing "approved"/"rejected" decision (see profile_service.py's
    add_manual_urls, the one caller that passes anything but the default:
    a hand-typed URL is itself the analyst's approval, so it should reach
    analysis without an extra click, but that must never silently
    override a decision an earlier sweep's card already got).

    `retry_pending` says "this job is going to visit this URL again", and
    exists solely to stop `analysis_attempts` being spent twice for the
    same thing. Analysis re-reads an incomplete profile up to
    `_COMPLETENESS_PASSES` more times WITHIN one job (see
    services/analysis_service.py), and every one of those passes saves.
    Counting each of them against a budget that was meant to bound whole
    SWEEPS burned it three times per job: live, every incomplete row on
    every platform sat at 6+ attempts (3 passes x 2 jobs) against a cap
    of 4, so `urls_for` had permanently excluded all of them and no later
    sweep could ever pick them up again. The in-job passes are already
    bounded by `_COMPLETENESS_PASSES`; this counter bounds the job, so
    only the job's final word on a URL spends from it.
    """
    # ANALYSIS RESULTS ARE MEMORY-ONLY -- enforced here, at the one write
    # boundary, rather than left as a convention every future caller has to
    # remember. Analysis output belongs in shared/analysis_store.py; only
    # discovery persists (see that module's docstring for the full split
    # and what memory-only costs). Raising is deliberate: a silent no-op
    # would look exactly like a successful save to the caller, and the
    # result would be gone with nothing to explain where.
    if phase == PHASE_ANALYSIS:
        raise ValidationError(
            "analysis results are memory-only and must not be persisted -- "
            "use backend/shared/analysis_store.py::analysis_store.put(); "
            "profile_repository.save() accepts discovery writes only"
        )

    coll = db()[PROFILES]
    eid = (entity_id or "").strip()

    match: dict[str, Any] = {"client_id": client_id, "platform": platform}
    keys: list[dict] = []
    if eid:
        keys.append({"entity_id": eid})
    keys.append({"url": url})
    keys.append({"urls": url})
    canonical_yt = f"https://www.youtube.com/channel/{eid}" if platform == "youtube" and eid and eid.startswith("UC") else ""
    if canonical_yt:
        keys.append({"url": canonical_yt})
        keys.append({"urls": canonical_yt})
    existing = await coll.find_one(
        {**match, "$or": keys},
        {"_id": 1, "url": 1, "status": 1, "entity_id": 1, "analysis_attempts": 1}
    )

    owned = ANALYSIS_FIELDS if phase == PHASE_ANALYSIS else DISCOVERY_FIELDS
    now = datetime.now(timezone.utc)
    add_urls = {"$each": [url, canonical_yt]} if canonical_yt else url
    update: dict[str, Any] = {
        "$set": {k: v for k, v in fields.items() if k in owned and v not in (None, "", {})},
        "$currentDate": {"last_seen": True},
        "$addToSet": {"urls": add_urls},
    }
    # phase only ever advances, a sweep that rediscovers an
    # already-scored profile must not demote it back to "discovery"
    if phase == PHASE_ANALYSIS:
        update["$set"]["phase"] = PHASE_ANALYSIS
        update["$set"]["analysed_at"] = now
        status_now = str(fields.get("analysis_status") or "")
        failed = status_now in RETRYABLE_ANALYSIS_STATUSES
        if failed:
            # A failed attempt is bookkeeping, not a finding: it must never
            # start a publish hold or present itself as a publishable
            # result. It only bumps the attempt counter that eventually
            # stops a permanently-dead URL from being retried forever --
            # and only when this visit is the job's LAST word on the URL,
            # see `retry_pending`.
            if not retry_pending:
                update["$inc"] = {"analysis_attempts": 1}
            update["$set"]["published"] = False
        else:
            # A real reading resets the failure counter (this profile is
            # reachable again) and starts its publish hold: held back from
            # the client-facing default view for `publish_hold_minutes`, so
            # an analyst who approved a false positive has a window to
            # revert before anything downstream sees it. See ADR 0007.
            # The hold EXPIRES on its own; `published` is only ever set
            # early, by an explicit Publish.
            update["$set"]["published"] = False
            update["$set"]["publish_hold_until"] = now + timedelta(minutes=settings.publish_hold_minutes)
            # A reading that came back short of a field the platform
            # publishes is a real finding (it holds and publishes like
            # any other) but it is NOT the end of the story, so it
            # spends an attempt rather than resetting the counter to
            # zero. Without this an incomplete row that stays incomplete
            # would be re-queued by urls_for on every catch-up sweep
            # forever, since each pass would clear its own budget.
            if fields.get("analysis_complete") is False:
                if not retry_pending:
                    update["$inc"] = {"analysis_attempts": 1}
            else:
                update["$set"]["analysis_attempts"] = 0
    if keyword:
        update["$addToSet"]["keywords"] = keyword

    if existing:
        # Identity only ever sharpens: fill a blank, or upgrade a vanity
        # slug to the platform's canonical numeric id. Never the reverse.
        # See the note on ANALYSIS_FIELDS for what overwriting a good id
        # with a slug does to the next sweep's dedup.
        if eid and _is_identity_upgrade(existing.get("entity_id") or "", eid):
            update["$set"]["entity_id"] = eid
        if initial_status != "pending" and existing.get("status") == "pending":
            update["$set"]["status"] = initial_status
        await coll.update_one({"_id": existing["_id"]}, update)
        return False

    update["$setOnInsert"] = {
        "client_id": client_id, "platform": platform, "url": url,
        "entity_id": eid, "status": initial_status,
        "first_seen": datetime.now(timezone.utc),
    }
    if phase != PHASE_ANALYSIS:
        update["$setOnInsert"]["phase"] = phase
    try:
        res = await coll.update_one({**match, "url": url}, update, upsert=True)
        return res.upserted_id is not None
    except DuplicateKeyError:
        # Another writer inserted this same profile between our read above
        # and this write. Re-resolve which document actually won the race
        # and apply the update to THAT one.
        #
        # The old version retried `update_one({**match, "url": url})`
        # which only works when the collision was on the url index. When it
        # was the ENTITY index (the winning document has the same
        # entity_id under a different url shape, routine on Facebook,
        # where the same profile is reachable as both /vanity and
        # profile.php?id=N), that filter matched zero documents and the
        # scraped result was silently dropped while still being counted as
        # saved by save_many. Re-running the identity lookup finds the
        # winner whichever index fired.
        winner = await coll.find_one({**match, "$or": keys}, {"_id": 1})
        if winner is None:
            log.warning(f"{platform}/{client_id}: {url} lost an insert race but no surviving document matched")
            return False
        update.pop("$setOnInsert", None)
        await coll.update_one({"_id": winner["_id"]}, update)
        return False


async def save_many(
    client_id: str, platform: str, phase: str, items: list[dict],
) -> tuple[int, int]:
    """Each item is `{**fields, "url":..., "entity_id":..., "keyword":...}`.
    -> (saved, newly seen). One bad row never sinks the batch.

    `retry_pending` may ride along on an item like the other three control
    keys: popped here so it reaches `save` as an argument and never lands
    in the document itself.
    """
    saved = new = 0
    for item in items:
        item = dict(item)
        url = item.pop("url")
        entity_id = item.pop("entity_id", "")
        keyword = item.pop("keyword", "")
        retry_pending = bool(item.pop("retry_pending", False))
        try:
            if await save(client_id, platform, phase, item, url=url, entity_id=entity_id,
                          keyword=keyword, retry_pending=retry_pending):
                new += 1
            saved += 1
        except Exception as e:
            log.warning(f"save failed for {url}: {type(e).__name__}: {e}")
    return saved, new


async def get_by_ids(client_id: str, ids: list[str]) -> list[dict]:
    """Raw profile docs for a hand-picked set of ids, scoped to one client so
    a stray/foreign id can never leak another client's data into a bulk
    operation. An unparseable id is skipped rather than failing the whole
    batch, same "one bad row never sinks it" spirit as save_many."""
    oids = []
    for i in ids:
        try:
            oids.append(_oid(i))
        except NotFoundError:
            continue
    if not oids:
        return []
    coll = db()[PROFILES]
    return [_stamp_utc_for_api(d) async for d in coll.find({"client_id": client_id, "_id": {"$in": oids}})]


async def get_by_urls(client_id: str, platform: str, urls: list[str]) -> list[dict]:
    """Raw profile docs matching a list of URLs for a specific client and platform."""
    if not urls:
        return []
    coll = db()[PROFILES]
    return [_stamp_utc_for_api(d) async for d in coll.find({
        "client_id": client_id,
        "platform": platform,
        "$or": [{"url": {"$in": urls}}, {"urls": {"$in": urls}}],
    })]


def _build_query(
    client_id: str, *, platform: Optional[str] = None, status: Optional[str] = None,
    phase: Optional[str] = None, include_held: bool = False, keyword: Optional[str] = None,
    entity_type: Optional[str] = None, priority: Optional[str] = None,
    match_level: Optional[str] = None, keyword_match_type: Optional[str] = None,
    search: Optional[str] = None, client_keywords: Optional[dict] = None,
    published: Optional[bool] = None, data_quality: Optional[str] = None,
) -> dict[str, Any]:
    """The filter `find()` queries with, factored out so `delete_matching()`
    (the "Delete Platform Data" button) can delete EXACTLY the set of
    profiles a given phase/status/published combination currently displays,
    no more and no less, by building the identical query rather than
    re-deriving similar-looking logic that could quietly drift out of sync
    with what the analyst is actually looking at.

    `published` is the analyst's Published/Unpublished tab (analysis phase
    only), a separate concern from `include_held`, which only decides
    whether an unpublished row is visible AT ALL. A document saved before
    ADR 0007 existed has neither `published` nor `publish_hold_until` at
    all; `profile_service._to_full` already defaults a missing `published`
    to `True` for API responses, so `published=True` here matches that same
    default (`$ne: False`, not `== True`) rather than re-hiding a legacy row
    the response layer would otherwise call published.

    `keyword` matches exactly one entry of the profile's `keywords` array
    (a scalar-vs-array Mongo query already does "array contains" for free),
    an analyst picks one of the client's actual searched keywords from a
    list, not a free-text substring.

    Clauses accumulate in `$and` rather than as top-level keys: several of
    them (phase, publish hold, keyword-category, free-text search) are each
    an `$or`, and assigning `q["$or"]` more than once silently keeps only
    the last one.
    """
    clauses: list[dict] = []
    q: dict[str, Any] = {"client_id": client_id}
    if platform:
        q["platform"] = platform
    if status:
        q["status"] = status
    if keyword:
        # Into `clauses`, NOT `q["keywords"]` -- the keyword-category filter
        # below writes the same key, and a second assignment would silently
        # drop this one (see the note on `$or` in this docstring; the same
        # trap, a different key). That combination is reachable straight
        # from the UI, which has both a keyword dropdown and an
        # Individual/Domain dropdown: picking a keyword AND a category
        # returned MORE rows than the keyword alone, because the keyword
        # filter had been thrown away.
        clauses.append({"keywords": keyword})
    if entity_type:
        q["entity_type"] = entity_type
    if priority:
        q["priority"] = priority
    if published is not None:
        q["published"] = False if published is False else {"$ne": False}
    if match_level:
        # Match level thresholds mirror the card badge and NAME_THRESHOLD / MEDIUM_MATCH_THRESHOLD:
        # - High:   name_score >= 80 (NAME_THRESHOLD)
        # - Medium: 50 <= name_score < 80
        # - Low:    name_score < 50
        if match_level == "high":
            q["name_score"] = {"$gte": NAME_THRESHOLD}
        elif match_level == "medium":
            q["name_score"] = {"$gte": MEDIUM_MATCH_THRESHOLD, "$lt": NAME_THRESHOLD}
        else:
            q["name_score"] = {"$lt": MEDIUM_MATCH_THRESHOLD, "$exists": True, "$ne": None}
    if keyword_match_type and client_keywords is not None:
        # "was this found under one of the client's INDIVIDUAL-name keywords
        # or one of its DOMAIN/brand keywords", the same classification
        # services/incident_publisher.py uses to pick a category, done as a
        # set-membership query rather than a stored per-profile field.
        bucket = ("name_keywords" if keyword_match_type == "individual" else "domain_keywords")
        wanted = list(client_keywords.get(bucket) or [])
        # an empty configured list can never match anything, express that
        # as an impossible clause rather than letting it degrade to "all"
        clauses.append({"keywords": {"$in": wanted} if wanted else {"$in": [None]}})
    if data_quality:
        # The analyst-facing "Data Quality" filter. "complete" is the exact
        # complement of "incomplete" ($nor of the same clause), so the two
        # options always partition the view with nothing falling between
        # them -- deriving the negative independently would let a row escape
        # both the moment either definition drifted.
        incomplete = _incomplete_clause()
        clauses.append(incomplete if data_quality == "incomplete" else {"$nor": [incomplete]})
    if search and search.strip():
        rx = {"$regex": re.escape(search.strip()), "$options": "i"}
        clauses.append({"$or": [{"display_name": rx}, {"username": rx}, {"url": rx}]})
    if phase:
        if phase == PHASE_DISCOVERY:
            # A profile that was approved (auto-queuing analysis) and later
            # reversed to rejected keeps its phase at "analysis", phase
            # never reverts. Without "rejected" here too, such a profile
            # vanished from the Discovery view's Rejected tab entirely,
            # keyword/confidence filters included, the moment it was
            # reversed post-analysis.
            clauses.append({"$or": [{"phase": PHASE_DISCOVERY}, {"status": {"$in": ["approved", "rejected"]}}]})
        else:
            q["phase"] = phase
            if phase == "analysis" and status != "rejected":
                clauses.append({"status": {"$ne": "rejected"}})
            if not include_held:
                clauses.append({"$or": [
                    {"published": True},
                    # the hold has expired on its own. ADR 0007's actual
                    # behaviour, which nothing implemented before now
                    {"publish_hold_until": {"$lte": datetime.now(timezone.utc)}},
                    # a row analysed before this feature existed has neither
                    # field at all, treat it as already published rather
                    # than retroactively hiding it
                    {"published": {"$exists": False}, "publish_hold_until": {"$exists": False}},
                ]})
    if clauses:
        q["$and"] = clauses
    return q


async def find(
    client_id: str, *, platform: Optional[str] = None, status: Optional[str] = None,
    phase: Optional[str] = None, limit: int = 100, offset: int = 0,
    include_held: bool = False, keyword: Optional[str] = None,
    entity_type: Optional[str] = None, priority: Optional[str] = None,
    match_level: Optional[str] = None, keyword_match_type: Optional[str] = None,
    search: Optional[str] = None, client_keywords: Optional[dict] = None,
    published: Optional[bool] = None, data_quality: Optional[str] = None,
) -> tuple[list[dict], int, dict]:
    """`include_held=False` (the default, used by any caller that doesn't
    explicitly ask otherwise, i.e. the SaaS backend's normal poll) hides a
    freshly analysed row until its publish hold clears, see ADR 0007. The
    analyst-facing frontend always passes `include_held=True` so analysts
    see held rows immediately, flagged with a countdown.

    EVERY filter here is applied server-side, before `limit`/`offset`.
    `priority`, `match_level`, `keyword_match_type` and `search` used to be
    applied in the browser over whatever page happened to be loaded, while
    `total` and the pager still came from the unfiltered server query, so
    filtering "High priority" across 500 rows showed only the High rows
    within page 1 and still claimed 500 results. A filter that doesn't
    survive pagination isn't a filter.
    """
    q = _build_query(
        client_id, platform=platform, status=status, phase=phase, include_held=include_held,
        keyword=keyword, entity_type=entity_type, priority=priority, match_level=match_level,
        keyword_match_type=keyword_match_type, search=search, client_keywords=client_keywords,
        published=published, data_quality=data_quality,
    )

    coll = db()[PROFILES]
    total = await coll.count_documents(q)
    if phase == PHASE_DISCOVERY and status == "rejected":
        # Rejected is the one status view that reads newest-decision-first
        # on purpose, an analyst reviewing what they've dismissed wants
        # the profile they JUST rejected at the top, not buried under
        # everything rejected before it. rejected_at (set only by an actual
        # reject decision in patch(), never by a routine re-discovery
        # sweep) is what makes "most recent" mean the reject, not a re-scan.
        sort_field, sort_dir = "rejected_at", -1
    elif phase == PHASE_DISCOVERY:
        # Every other discovery view (pending, approved, unfiltered) sorts
        # oldest-first (_id is a MongoDB ObjectId, whose leading bytes are
        # an insertion timestamp, ascending _id is the same order
        # documents were saved in, i.e. the order each platform actually
        # returned them, page by page). That makes page 1 of this listing
        # the first results a platform's own search returned and the last
        # listing page the last ones scraped, matching every platform's own
        # top-to-bottom order instead of "whichever profile was touched
        # most recently."
        sort_field, sort_dir = "_id", 1
    else:
        # analysis keeps the recency sort, newest finding first is what
        # an analyst reviewing scored results actually wants.
        sort_field, sort_dir = "last_seen", -1
    rows = []
    async for doc in coll.find(q).sort(sort_field, sort_dir).skip(offset).limit(limit):
        doc["id"] = str(doc.pop("_id"))
        rows.append(_stamp_utc_for_api(doc))

    plat_match = dict(q)
    plat_match.pop("platform", None)
    plat_counts = {}
    async for doc in coll.aggregate([{"$match": plat_match}, {"$group": {"_id": "$platform", "count": {"$sum": 1}}}]):
        if doc.get("_id"):
            plat_counts[str(doc["_id"])] = doc["count"]

    status_match = dict(q)
    status_match.pop("status", None)
    status_counts = {}
    async for doc in coll.aggregate([{"$match": status_match}, {"$group": {"_id": "$status", "count": {"$sum": 1}}}]):
        if doc.get("_id"):
            status_counts[str(doc["_id"])] = doc["count"]

    keyword_match = dict(q)
    keyword_match.pop("keywords", None)
    keyword_counts: dict[str, int] = {}
    async for doc in coll.aggregate([
        {"$match": keyword_match}, {"$unwind": "$keywords"},
        {"$group": {"_id": "$keywords", "count": {"$sum": 1}}},
    ]):
        if doc.get("_id"):
            keyword_counts[str(doc["_id"])] = doc["count"]

    counts = {"platforms": plat_counts, "statuses": status_counts, "keywords": keyword_counts}
    return rows, total, counts


async def urls_for(
    client_id: str, platform: str, status: Optional[str] = None,
    *, exclude_analysed: bool = False, with_keywords: bool = False,
) -> "list[str] | list[tuple[str, list[str]]]":
    """URLs an analysis run should visit.

    `with_keywords=True` returns `(url, keywords)` pairs instead of bare
    URLs -- `keywords` is the client keyword(s) this profile was actually
    discovered under (see `save()`'s own `keyword` param, `$addToSet`'d
    into this same field). analysis_service.py needs this to score each
    profile against ITS OWN matched keyword; every caller used to pass an
    empty target into every scrape instead (see that module's own comment
    on the bug this exists to fix).

    `exclude_analysed` means "skip what we have already READ", not "skip what
    we have already attempted". A profile whose last attempt ended in
    ERROR/CHECKPOINT/LOGIN_REQUIRED was never actually looked at, the
    session was challenged, the proxy died, the page timed out, so it
    stays in the queue.

    Before this distinction existed, any transient failure wrote
    `phase=analysis` and the profile was excluded from every future run:
    an approved impersonation candidate could silently drop out of the
    pipeline for good on one network blip, with nothing anywhere reporting
    that it had. `analysis_attempts` bounds the retry so a genuinely dead
    URL still stops eventually (MAX_ANALYSIS_ATTEMPTS) rather than
    consuming real page loads on every catch-up sweep forever.
    """
    q: dict[str, Any] = {"client_id": client_id, "platform": platform}
    if status:
        q["status"] = status
    if exclude_analysed:
        q["$or"] = [
            {"phase": {"$ne": PHASE_ANALYSIS}},
            {
                "analysis_status": {"$in": list(RETRYABLE_ANALYSIS_STATUSES)},
                "analysis_attempts": {"$lt": MAX_ANALYSIS_ATTEMPTS},
            },
            # NOTE: the old clause that re-queued profiles with
            # analysis_complete=False was removed by design. Profiles with
            # missing fields are now only re-analysed when an analyst
            # manually triggers a re-run, not automatically on every
            # catch-up sweep.
        ]
        # An analyst's manual "stop retrying" (see set_retry_state below)
        # overrides every clause above, including the un-throttled first
        # one. Without this AND, a profile an analyst had deliberately
        # given up on (a dead account, a confirmed false positive still
        # sitting at phase=discovery) would be swept right back in on the
        # very next catch-up tick -- the retry queue UI's Stop button would
        # have looked like it worked and done nothing.
        q["retry_disabled"] = {"$ne": True}
    if not with_keywords:
        return [d["url"] async for d in db()[PROFILES].find(q, {"url": 1, "_id": 0}) if d.get("url")]
    return [
        (d["url"], [str(k) for k in (d.get("keywords") or [])])
        async for d in db()[PROFILES].find(q, {"url": 1, "keywords": 1, "_id": 0})
        if d.get("url")
    ]


async def stuck_analysis(client_id: str, platform: Optional[str] = None) -> list[dict]:
    """Profiles that will NEVER be retried automatically -- either they
    exhausted MAX_ANALYSIS_ATTEMPTS, or an analyst manually stopped them
    (see set_retry_state). These are exactly the ones an analyst must be
    told about, "approved but we could never read it" is a coverage gap,
    not a result, and it is invisible unless something surfaces it."""
    q: dict[str, Any] = {
        "client_id": client_id, "status": "approved",
        "$or": [
            {
                "analysis_attempts": {"$gte": MAX_ANALYSIS_ATTEMPTS},
                # Either kind of exhaustion is a coverage gap worth an
                # analyst's attention: never read at all (a retryable
                # status), or read but permanently missing a field the
                # platform publishes. Nested inside this $or element (not a
                # second top-level "$or" key, which Python/Mongo would just
                # overwrite) so it stays scoped to "AND attempts >= max".
                "$or": [
                    {"analysis_status": {"$in": list(RETRYABLE_ANALYSIS_STATUSES)}},
                    {"analysis_complete": False},
                ],
            },
            {"retry_disabled": True},
        ],
    }
    if platform:
        q["platform"] = platform
    out = []
    async for d in db()[PROFILES].find(q, {"url": 1, "platform": 1, "display_name": 1,
                                            "analysis_status": 1, "analysis_attempts": 1,
                                            "comments": 1, "retry_disabled": 1}):
        d["id"] = str(d.pop("_id"))
        # "manually stopped" is the more honest reason when that's why this
        # row is here, an analyst reading "PARTIAL" or "" as the reason for
        # a row THEY stopped would reasonably think the scraper is still
        # the problem.
        if d.pop("retry_disabled", False):
            d["reason"] = "manually stopped"
        out.append(d)
    return out


# Every state `field_report()` (shared/completeness.py) can hand back for
# one field. Only "MISSED" means real, actionable data loss; the retry
# queue view filters a row's own field_status down to just these so an
# analyst sees "last post date: MISSED" and not four other fields that were
# never expected to have a value in the first place.
_ACTIONABLE_FIELD_VERDICT = "MISSED"


async def retry_queue_profiles(
    client_id: str, platform: Optional[str] = None, *, limit: int = 500,
) -> list[dict]:
    """Every approved profile analysis has not FINISHED with, whether or not
    it will still be retried automatically -- the full picture behind
    `urls_for`'s exclude_analysed union and `stuck_analysis`'s "gave up"
    subset, combined into one list a monitoring UI can render as a single
    queue instead of an analyst having to reconcile two different partial
    views by hand.

    Every returned document carries enough of its own state (analysis_
    attempts, analysis_status, analysis_complete, retry_disabled,
    field_status) for the caller to classify it as "eligible" (will be
    retried automatically), "exhausted" (hit MAX_ANALYSIS_ATTEMPTS), or
    "stopped" (an analyst turned it off) without a second query --
    services/profile_service.py::retry_queue does exactly that
    classification, kept there rather than here because "what a row's
    state MEANS" is a presentation decision, not a storage one.

    `limit` bounds an unbounded query on a large client: this is a live
    monitoring view meant to be read by a person, not an export, and a
    person does not review 5,000 rows in one screen either way.
    """
    q: dict[str, Any] = {
        "client_id": client_id, "status": "approved", "phase": PHASE_ANALYSIS,
        "$or": [
            {
                "analysis_status": {"$in": list(RETRYABLE_ANALYSIS_STATUSES)},
            },
            {"analysis_complete": False},
            {"retry_disabled": True},
        ],
    }
    if platform:
        q["platform"] = platform
    out = []
    async for d in (
        db()[PROFILES]
        .find(q)
        .sort("analysed_at", -1)
        .limit(max(1, limit))
    ):
        d["id"] = str(d.pop("_id"))
        out.append(d)
    return out


async def set_retry_state(profile_id: str, *, disabled: bool, reset_attempts: bool = False) -> Optional[dict]:
    """The retry queue UI's Stop / Resume action. A profile's own analysis
    fields (ANALYSIS_FIELDS) are untouched -- this only ever writes
    `retry_disabled` (+ optionally `analysis_attempts` for Resume), so
    stopping or resuming a profile can never be mistaken for, or interfere
    with, an actual re-read of it.

    `reset_attempts=True` is what makes Resume actually resume something
    that had already hit MAX_ANALYSIS_ATTEMPTS -- clearing `retry_disabled`
    alone would leave `urls_for`'s attempts<MAX condition still failing, so
    the profile would silently stay excluded and Resume would look like it
    did nothing.
    """
    fields: dict[str, Any] = {"retry_disabled": disabled}
    if reset_attempts:
        fields["analysis_attempts"] = 0
    res = await db()[PROFILES].find_one_and_update(
        {"_id": _oid(profile_id)}, {"$set": fields}, return_document=ReturnDocument.AFTER,
    )
    if res is None:
        return None
    res["id"] = str(res.pop("_id"))
    return res


async def get_by_id(doc_id: str) -> Optional[dict]:
    doc = await db()[PROFILES].find_one({"_id": _oid(doc_id)})
    if doc is None:
        return None
    doc["id"] = str(doc.pop("_id"))
    return _stamp_utc_for_api(doc)


def compute_risk_score(
    has_logo: bool, has_name_match: bool, location, last_post_date,
    logo_match: Optional[bool] = None, username_match: Optional[bool] = None,
    validated: bool = False,
) -> int:
    """The same rubric used during a live scrape (`shared/models/scoring.py`'s
    `Row.risk`), applied to a document's already-derived fields, so a hand
    correction and a fresh scrape can never silently disagree about how the
    same facts turn into a score. `logo_match`/`username_match`/`validated`
    resolve against the scraped signals through `scoring.resolve_match`."""
    from backend.shared.models.scoring import compute_score

    return compute_score(
        has_logo=bool(has_logo), has_name_match=bool(has_name_match),
        has_location=bool((location or "").strip()), last_post_iso=last_post_date or "",
        logo_match=logo_match, username_match=username_match, validated=validated,
    )


def compute_priority(
    has_logo: bool, risk_score: int,
    logo_match: Optional[bool] = None, validated: bool = False,
) -> str:
    """High/Medium/Low off the same resolved signals the score uses, an
    undone logo match has to be able to drop the priority too, or the two
    would disagree about the same profile.

    Deliberately still High on a logo alone, even where `compute_score`
    returns its floor of 2 for want of a name match: priority is defined as
    photo-driven "regardless of score" (see shared/models/row.py::Row
    .priority, which this must stay identical to).

    Takes the ALREADY-COMPUTED `risk_score` for the non-logo branch,
    deliberately -- not `has_name_match` re-derived on its own, which is
    what this function used to take. `Row.priority` (the fresh-scrape
    version this one must match) falls back to `score >= 5` when there's
    no logo match, and that score already folds in activity/location, not
    just "was the name a match." The old signature skipped that gate
    entirely: approving a dormant, name-matched-only profile with no logo
    stamped it High even though a fresh scrape of the identical facts,
    lacking the activity signal, would score it below 5 and call it Low --
    the two disagreeing about the same profile is exactly what this
    function exists to prevent. Callers already compute `risk_score` via
    `compute_risk_score` right before calling this; pass that value straight
    through rather than recomputing from a narrower set of signals.
    """
    from backend.shared.models.scoring import resolve_match

    if resolve_match(has_logo, logo_match, validated):
        return "High"
    return "High" if risk_score >= 5 else "Low"


async def patch(doc_id: str, fields: dict) -> dict:
    """Applies an analyst's whitelisted edit, recomputes score/priority
    when a scoring-relevant field changed, and relabels provenance so a
    manual correction never masquerades as scraped evidence."""
    safe = {k: v for k, v in fields.items() if k in EDITABLE}
    if "followers" in safe:
        try:
            safe["followers"] = None if safe["followers"] in (None, "") else int(safe["followers"])
        except (TypeError, ValueError):
            raise ValidationError("followers must be a number")
    overrides = safe.pop("incident_overrides", None)
    if overrides:
        if not isinstance(overrides, dict):
            raise ValidationError("incident_overrides must be an object of {field: value}")
        # dotted-path expansion, not a bare $set of the whole sub-document,
        # editing one field (e.g. "title") must never wipe out overrides a
        # previous edit already saved for a different field.
        for path, value in overrides.items():
            safe[f"incident_overrides.{path}"] = value
    if not safe:
        raise ValidationError("nothing updatable in that payload")

    oid = _oid(doc_id)
    if SCORING_FIELDS & safe.keys():
        doc = await db()[PROFILES].find_one({"_id": oid})
        if doc is None:
            raise NotFoundError(f"profile {doc_id!r} not found")
        merged = {**doc, **safe}
        validated = merged.get("status") == "approved"
        safe["risk_score"] = compute_risk_score(
            merged.get("has_logo", False), merged.get("has_name_match", False),
            merged.get("location"), merged.get("last_post_date"),
            merged.get("logo_match"), merged.get("username_match"), validated,
        )
        safe["priority"] = compute_priority(
            merged.get("has_logo", False), safe["risk_score"],
            merged.get("logo_match"), validated,
        )

    for field_name, source_key in PROVENANCE_KEYS.items():
        if field_name in safe:
            safe[f"sources.{source_key}"] = "manual"
    safe["last_seen"] = datetime.now(timezone.utc)
    if safe.get("status") == "rejected":
        # a dedicated timestamp for exactly the moment an analyst rejected
        # this profile, last_seen is no good for that ordering since a
        # routine re-discovery sweep bumps it on ANY already-seen profile,
        # rejected or not, with no analyst action involved. find()'s
        # rejected-list sort depends on this being untouched by anything
        # except an actual reject decision.
        safe["rejected_at"] = datetime.now(timezone.utc)

    write: dict[str, Any] = {"$set": safe}
    if "status" in safe:
        # a fresh decision resolves whatever reconsideration flagged this
        # profile, the "changed since rejection" label must not outlive it
        write["$unset"] = {"changes": "", "changed_at": ""}

    res = await db()[PROFILES].update_one({"_id": oid}, write)
    if res.matched_count == 0:
        raise NotFoundError(f"profile {doc_id!r} not found")
    updated = await get_by_id(doc_id)
    return updated or {}


async def publish(doc_id: str) -> dict:
    """An analyst confirming a held analysis result early, before its hold
    naturally clears, see ADR 0007. A no-op find()-visibility-wise for a
    row that was never held (already published, or not yet analysed).

    Guarded against publishing anything that isn't an approved, analysed
    finding: a profile still in `discovery` phase has no scored analysis to
    publish, and a `rejected` profile is an analyst's explicit call that this
    isn't a genuine impersonation, an incident must never be raised for it,
    even if it was rejected after already clearing analysis. Both are 409s,
    not silent no-ops, so a stale "Publish" click surfaces instead of quietly
    doing nothing (or, before this guard, publishing anyway).
    """
    oid = _oid(doc_id)
    doc = await db()[PROFILES].find_one({"_id": oid}, {"phase": 1, "status": 1, "analysis_status": 1})
    if doc is None:
        raise NotFoundError(f"profile {doc_id!r} not found")
    if doc.get("phase") != PHASE_ANALYSIS:
        raise ConflictError(f"profile {doc_id!r} has not been analysed yet")
    if doc.get("status") == "rejected":
        raise ConflictError(f"profile {doc_id!r} was rejected and cannot be published")
    if doc.get("analysis_status") in RETRYABLE_ANALYSIS_STATUSES:
        # ERROR/CHECKPOINT/LOGIN_REQUIRED: the analysis run never actually
        # read this profile. PARTIAL: it read SOME of the profile but not
        # enough to trust, either way this is queued for another attempt,
        # not a finding to publish yet.
        raise ConflictError(
            f"profile {doc_id!r} last analysis ended in {doc['analysis_status']} -- "
            "not a complete result yet, so there is nothing to publish "
            "(it will be retried automatically)"
        )
    res = await db()[PROFILES].update_one(
        {"_id": oid},
        # clearing the hold alongside `published` keeps the two consistent:
        # a row can otherwise read as published AND still-holding, which the
        # UI renders as a countdown on something already sent downstream
        {"$set": {"published": True}, "$unset": {"publish_hold_until": ""}},
    )
    if res.matched_count == 0:
        raise NotFoundError(f"profile {doc_id!r} not found")
    updated = await get_by_id(doc_id)
    return updated or {}


async def list_unpublished_ids(
    client_id: str, platform: Optional[str] = None, since: Optional[datetime] = None,
) -> list[str]:
    """Every analysis-phase profile for this client not yet flagged
    `published`, what a "Publish All" action iterates over, regardless
    of whether each row's own publish hold has already cleared. Excludes
    rejected profiles, see publish()'s guard for why those must never
    be published. `since`, when set, additionally restricts to profiles
    analysed on/after that time (the Publish filter's Recent/2-Days/Week
    scopes, see services/profile_service.py::publish_all_profiles)."""
    q: dict[str, Any] = {
        "client_id": client_id, "phase": PHASE_ANALYSIS,
        "published": {"$ne": True}, "status": {"$ne": "rejected"},
        # a failed attempt carries no reading, see publish()'s guard
        "analysis_status": {"$nin": list(RETRYABLE_ANALYSIS_STATUSES)},
    }
    if platform:
        q["platform"] = platform
    if since:
        # `analysed_at` only started being written at a certain point (see
        # save()'s PHASE_ANALYSIS branch); rows analysed before that have no
        # such field. Matching on it alone would silently drop every one of
        # those from a date-scoped publish, the analyst asks for "last
        # week" and quietly gets less than they asked for, with no error.
        # `last_seen` is written on every save (a $currentDate, so it is
        # present on 100% of rows) and is never later than the analysis that
        # produced the row, so it is the correct fallback.
        q["$or"] = [
            {"analysed_at": {"$gte": since}},
            {"analysed_at": {"$exists": False}, "last_seen": {"$gte": since}},
        ]
    return [str(d["_id"]) async for d in db()[PROFILES].find(q, {"_id": 1})]


async def stats(client_id: str, platform: Optional[str] = None) -> dict:
    import asyncio

    coll = db()[PROFILES]
    base: dict[str, Any] = {"client_id": client_id}
    if platform:
        base["platform"] = platform
    keys = (
        ("total", base),
        ("pending", {**base, "status": "pending"}),
        ("approved", {**base, "status": "approved"}),
        ("rejected", {**base, "status": "rejected"}),
        ("high", {**base, "priority": "High"}),
        ("medium", {**base, "priority": "Medium"}),
        ("low", {**base, "priority": "Low"}),
        ("analysed", {**base, "phase": PHASE_ANALYSIS}),
        # Coverage, not volume. `awaiting_analysis` is approved work the
        # engine still owes; `analysis_failed` is approved work it gave up
        # on. Both were previously invisible, an analyst had no way to
        # tell "nothing left to do" from "the queue quietly stopped
        # draining", which is exactly the question that decides whether a
        # client report is complete.
        ("awaiting_analysis", {
            **base, "status": "approved",
            "$or": [
                {"phase": {"$ne": PHASE_ANALYSIS}},
                {"analysis_status": {"$in": list(RETRYABLE_ANALYSIS_STATUSES)},
                 "analysis_attempts": {"$lt": MAX_ANALYSIS_ATTEMPTS}},
            ],
        }),
        ("analysis_failed", {
            **base, "status": "approved",
            "analysis_status": {"$in": list(RETRYABLE_ANALYSIS_STATUSES)},
            "analysis_attempts": {"$gte": MAX_ANALYSIS_ATTEMPTS},
        }),
        ("held", {
            **base, "phase": PHASE_ANALYSIS, "published": {"$ne": True},
            "publish_hold_until": {"$gt": datetime.now(timezone.utc)},
        }),
        ("with_evidence", {**base, "screenshot": {"$exists": True, "$ne": ""}}),
    )
    counts = await asyncio.gather(*(coll.count_documents(f) for _, f in keys))
    return dict(zip((k for k, _ in keys), counts))


async def delete_for_client(client_id: str) -> int:
    res = await db()[PROFILES].delete_many({"client_id": client_id})
    return res.deleted_count


async def delete_matching(
    client_id: str, platform: str, *, phase: Optional[str] = None,
    status: Optional[str] = None, published: Optional[bool] = None,
) -> dict:
    """The "Delete Platform Data" button (see
    profile_service.delete_for_client_platform), scoped to EXACTLY the
    phase/status/published combination the analyst currently has selected
    -- e.g. Discovery + Pending deletes only pending discovery-phase rows,
    leaving Validated/Rejected and every Analysis-phase row untouched. Uses
    the identical `_build_query` `find()` queries with, so "delete what I'm
    looking at" can never quietly drift from what IS actually displayed.

    `phase`/`status`/`published` all None (the pre-existing "Delete
    Platform Data" behavior, still used by client_service.delete's own
    cascade) means every row for this client+platform, unscoped, same as
    before this function had filters at all.

    Returns the deleted urls and screenshot keys too, not just a count:
    evidence screenshots and published incidents are cascaded from THIS
    exact set (see profile_service.delete_for_client_platform), never a
    separate blanket per-platform query, so deleting only a Pending sliver
    can never take an unrelated already-published finding's screenshot
    with it.
    """
    q = _build_query(client_id, platform=platform, phase=phase, status=status, published=published)
    coll = db()[PROFILES]
    urls: list[str] = []
    screenshot_keys: list[str] = []
    async for doc in coll.find(q, {"url": 1, "screenshot": 1}):
        if doc.get("url"):
            urls.append(doc["url"])
        if doc.get("screenshot"):
            screenshot_keys.append(doc["screenshot"])
    res = await coll.delete_many(q)
    return {"deleted": res.deleted_count, "urls": urls, "screenshot_keys": screenshot_keys}


async def delete_by_ids(ids: list[str]) -> int:
    """An analyst's explicit, individually-chosen hard delete, the Live
    Activity tab's DB browser. Unlike `cleanup_stale_pending`'s age/status
    gate, this trusts the caller's own selection completely: a malformed id
    is simply skipped (never a 404 for the whole batch) rather than failing
    an otherwise-valid bulk delete over one bad entry."""
    oids = []
    for doc_id in ids:
        try:
            oids.append(ObjectId(doc_id))
        except (InvalidId, TypeError):
            continue
    if not oids:
        return 0
    res = await db()[PROFILES].delete_many({"_id": {"$in": oids}})
    return res.deleted_count


async def cleanup_stale_pending(days: int = 60) -> int:
    """Deletes discovery-phase profiles that have sat in `pending`, never
    approved, never rejected, for `days` without a rediscovery bumping
    `last_seen`. Safe to hard-delete: a pending profile has no analyst
    decision recorded, so there is nothing for a future rediscovery to lose
    by starting over. Scoped to `phase=discovery` on purpose, a
    pending ANALYSIS-phase profile is either a fresh unpublished finding
    (still actively in the publish-hold review window) or one that just
    bounced back from `rejected` via the reconsideration path (see
    `save()`'s RECONSIDER_FIELDS) and carries a `changes` diff the analyst
    hasn't seen yet; neither should ever be silently deleted.

    Deliberately NOT wired into an automatic cron, call this from an
    operator-triggered endpoint or your own external scheduler so a
    misconfigured `days` value can't silently run unattended.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    res = await db()[PROFILES].delete_many({
        "status": "pending", "phase": PHASE_DISCOVERY, "last_seen": {"$lt": cutoff},
    })
    return res.deleted_count


# fields stripped by archive_stale_rejected(), the heavy, purely-cosmetic
# or search-convenience ones. Never includes anything RECONSIDER_FIELDS
# reads (display_name, has_logo) or anything the dedup match in save() needs
# (client_id, platform, url, entity_id), an archived rejected profile
# stays exactly as reconsider-able as an unarchived one.
_ARCHIVE_STRIP_FIELDS = ("profile_image_url", "keywords", "comments", "sources", "urls")


async def archive_stale_rejected(days: int = 180) -> int:
    """Shrinks (does NOT delete) rejected profiles untouched for `days`,
    strips the signed CDN avatar URL (500-800 chars, and expired within
    hours of being scraped anyway), the keywords array, free-text comments,
    and provenance metadata, while keeping status/display_name/has_logo/url/
    entity_id fully intact. A rediscovery of an archived profile still goes
    through the exact same reconsideration check in save() as an
    unarchived one, this only reduces document size, it never changes
    triage behavior.

    Trade-off, by design: an archived rejected profile's avatar renders as
    a fallback initial-circle instead of its real photo if an analyst ever
    filters back to it (ProfileAvatar already handles a missing image
    gracefully, no error, no broken UI), and it drops out of the
    discovery keyword-filter dropdown for keywords that were only ever
    tracked in the now-stripped `keywords` array. Both are judged
    acceptable specifically because this only ever touches profiles a
    human already rejected AND then didn't revisit for `days`, opt-in,
    not run automatically, so you're accepting this trade-off deliberately
    each time you call it, not by default.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    res = await db()[PROFILES].update_many(
        {
            "status": "rejected", "last_seen": {"$lt": cutoff},
            # only bother touching a document that still has something to strip
            "$or": [{f: {"$exists": True, "$ne": None}} for f in _ARCHIVE_STRIP_FIELDS],
        },
        {"$unset": {f: "" for f in _ARCHIVE_STRIP_FIELDS}},
    )
    return res.modified_count


async def find_duplicate_identities(limit: int = 50) -> list[dict]:
    """Groups of documents that violate what the unique indexes are meant to
    guarantee. Reported by `GET /health/data-integrity` so a build-index
    failure has an answer attached to it instead of just a stack trace."""
    coll = db()[PROFILES]
    out: list[dict] = []
    for field, prefilter in (("url", {}), ("entity_id", {"entity_id": {"$nin": [None, ""]}})):
        pipeline: list[dict] = [
            {"$match": prefilter},
            {"$group": {
                "_id": {"client_id": "$client_id", "platform": "$platform", "value": f"${field}"},
                "n": {"$sum": 1}, "ids": {"$push": "$_id"},
            }},
            {"$match": {"n": {"$gt": 1}}},
            {"$limit": limit},
        ]
        async for d in coll.aggregate(pipeline):
            out.append({
                "on": field, **d["_id"],
                "count": d["n"], "ids": [str(i) for i in d["ids"]],
            })
    return out


async def ensure_indexes() -> None:
    """Best-effort. A unique index that cannot be built on the data already
    in the collection must NOT stop the process from starting.

    Previously this raised straight out of the FastAPI lifespan, so one
    pre-existing duplicate, exactly what the old `entity_id` overwrite bug
    could produce, meant the whole engine refused to boot, with a
    DuplicateKeyError as the only clue. The engine now starts, logs loudly,
    and points at `GET /health/data-integrity`, which names the offending
    documents. Degraded-but-running beats dead, and the non-unique indexes
    that carry the query load still get built either way.
    """
    coll = db()[PROFILES]
    unique_specs = (
        ([("client_id", 1), ("platform", 1), ("url", 1)], "uniq_client_platform_url", None),
        ([("client_id", 1), ("platform", 1), ("entity_id", 1)], "uniq_client_platform_entity",
         {"entity_id": {"$type": "string", "$gt": ""}}),
    )
    for keys, name, partial in unique_specs:
        try:
            kwargs: dict[str, Any] = {"unique": True, "name": name}
            if partial:
                kwargs["partialFilterExpression"] = partial
            await coll.create_index(keys, **kwargs)
        except Exception as e:
            log.error(
                f"could not build unique index {name!r}: {type(e).__name__}: {e} -- "
                "the collection already holds documents that violate it. "
                "The engine will run without this index (deduplication falls back to "
                "the read-then-write path in save(), which is racier but correct for a "
                "single writer). Call GET /health/data-integrity to list the offenders."
            )

    for keys, name in (
        ([("client_id", 1), ("platform", 1), ("urls", 1)], "client_platform_urls"),
        ([("client_id", 1), ("status", 1), ("last_seen", -1)], "client_status_seen"),
        ([("client_id", 1), ("priority", 1)], "client_priority"),
        ([("client_id", 1), ("keywords", 1)], "client_keywords"),
        # drives the analysis queue (urls_for) and the publish-hold sweep
        ([("client_id", 1), ("platform", 1), ("phase", 1), ("status", 1)], "client_platform_phase_status"),
        ([("phase", 1), ("published", 1), ("publish_hold_until", 1)], "publish_hold"),
    ):
        try:
            await coll.create_index(keys, name=name)
        except Exception as e:
            log.error(f"could not build index {name!r}: {type(e).__name__}: {e}")
