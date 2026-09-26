"""Re-read profiles we already know about, through the one path that is
safe to read them from: the platform's own SEARCH.

WHY THIS EXISTS RATHER THAN A PROFILE VISIT. The obvious way to refresh a
known profile is to open it. For pictures that is forbidden here, and the
reason is documented at length in facebook/discovery_engine.py's
`TRUST_PAGE_CONTEXT_AVATAR`: for a privacy-restricted profile, Facebook's
client substitutes THE VIEWER'S OWN PHOTO into every picture field it
renders -- JSON and DOM alike -- because it genuinely cannot show a
non-friend the real one. Three separate defences were tried and each was
caught leaking the scraper's own face onto a candidate. So a resolve visit
recovers a name and never a picture, and that rule stays.

Search results are the exception the codebase already trusts: `iter_results`
reads a known field position in the search response, verified across 500+
real photos, and that is where every picture in this product has always come
from.

SO AIM SEARCH AT ONE PROFILE INSTEAD OF AT A KEYWORD. Measured live:
searching a blank profile's own display name returned that exact profile 4
times out of 4, each with a fresh picture.

AND THEN ONLY KEEP THAT ONE. The first version also saved the other 24
results, on the theory that they were free repairs. Measured over 15 real
searches: 424 profiles seen, 11 of them rows we held. The other 413 were
strangers, and saving them put 119 unrelated people into a
brand-monitoring client, each filed under a person's name as though it
were one of the analyst's keywords. They had to be deleted again.

So the rate is roughly one repair per search, and that is the honest cost:
a blank profile needs a search of its own. Discovery decides who belongs to
a client; a refresh only re-reads what discovery already decided.

WHAT IT UPDATES, AND WHAT IT REFUSES TO TOUCH. Everything goes through
`profile_repository.save_many` on the DISCOVERY phase, exactly as a sweep
does, which is what makes the guarantees hold rather than be re-implemented:

    updated    every field discovery owns -- picture URL, display name,
               username, verified, follower counts, entity type
    deduped    by platform id first, URL second; a profile already known is
               updated in place and never duplicated
    untouched  the analyst's `status`. Discovery writes only DISCOVERY_FIELDS
               and `status` is not one of them, so a validate/reject decision
               survives any number of refreshes
    untouched  analysis results, scores and the logo verdict, for the same
               reason

A profile whose picture genuinely IS the platform's blank silhouette gets
that silhouette stored like any other picture, deliberately: the card then
shows what the platform shows, rather than an initial-letter circle that
claims there is no picture at all.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from backend.database.connection import db
from backend.database.repositories import profile_repository as profiles_db
from backend.platforms.scan_options import DiscoveryOptions
from backend.services import avatar_cache
from backend.shared.logging import get_logger

log = get_logger("services.profile_refresh")

PROFILES = "profiles"

# Which stored field makes the best search term for each platform, in order
# of preference. Facebook's search is a people search over display names;
# Instagram's is a handle search, and a handle is exact where a display name
# is not.
SEARCH_TERM_FIELDS = {
    "facebook": ("display_name", "username"),
    "instagram": ("username", "display_name"),
    "twitter": ("username", "display_name"),
    "tiktok": ("username", "display_name"),
    "youtube": ("display_name",),
}

# The tab each platform's people-search lives on.
SEARCH_TAB = {
    "facebook": "people", "instagram": "people", "twitter": "people",
    "tiktok": "people", "youtube": "channels",
}

# Results to take per search. Wider than the one profile being looked for,
# because a name search does not always rank the exact person first and a
# few extra results cost nothing on a page that has already loaded -- but
# not wide, because only results we already hold are kept and the tail of a
# name search is strangers. Measured: ~1 of 28 results is a row we hold.
_PER_SEARCH = 30

# A search is a real page load on a live account. This is the same pacing a
# sweep uses between keywords, and the reason this runs in the background
# rather than blocking anything.
_GAP_SECONDS = 4.0


async def _blank_docs(client_id: Optional[str], platform: str, limit: int) -> list[dict]:
    q: dict[str, Any] = {
        "platform": platform,
        "$or": [{"avatar_sha": {"$exists": False}},
                {"avatar_sha": {"$in": ["", None]}}],
    }
    if client_id:
        q["client_id"] = client_id
    cur = db()[PROFILES].find(
        q, {"entity_id": 1, "url": 1, "username": 1, "display_name": 1,
            "client_id": 1, "keywords": 1},
    ).limit(limit)
    return [d async for d in cur]


def _terms_for(docs: list[dict], platform: str) -> list[tuple[str, str]]:
    """(search term, the client it belongs to), deduped, best term first.

    Deduped case-insensitively because blank profiles cluster around the
    same names -- that is how they came to be discovered together -- and
    running the identical search twice costs a page load to learn nothing.
    """
    fields = SEARCH_TERM_FIELDS.get(platform, ("display_name", "username"))
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for d in docs:
        cid = d.get("client_id") or ""
        if not cid:
            continue
        for f in fields:
            term = (d.get(f) or "").strip()
            if len(term) < 3:
                continue
            key = (cid, term.lower())
            if key in seen:
                break
            seen.add(key)
            out.append((term, cid))
            break
    return out


async def refresh_platform(
    platform: str,
    client_id: Optional[str] = None,
    max_searches: int = 40,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> dict[str, Any]:
    """Repair blank profiles on one platform by searching for them.

    Bounded by SEARCHES, not by profiles, because one search repairs many.
    `max_searches` is the real cost knob: it is how many live page loads
    this will make on the account, and nothing here can exceed it.
    """
    from backend.sessions import manager as sessions_engine

    started = time.time()
    out: dict[str, Any] = {
        "platform": platform, "searches": 0, "seen": 0,
        "saved": 0, "repaired": 0, "terms_exhausted": False,
    }

    # Ask for more blanks than searches, because the terms dedupe down hard
    # and because a search repairs neighbours that were never its term.
    docs = await _blank_docs(client_id, platform, max_searches * 25)
    if not docs:
        return out
    terms = _terms_for(docs, platform)[:max_searches]
    if not terms:
        log.warning(
            f"profile refresh [{platform}]: {len(docs)} blank profile(s) but none "
            f"carry a usable search term -- they have neither a display name nor a "
            f"username, so search cannot be aimed at them")
        return out

    # Which of our rows a result could repair, keyed the two ways a hit can
    # be recognised. Built once, so matching a result is a dict lookup
    # rather than a database round trip per hit.
    by_eid = {str(d.get("entity_id") or ""): d for d in docs if d.get("entity_id")}
    by_url = {str(d.get("url") or ""): d for d in docs if d.get("url")}

    plat_obj, session_item = await sessions_engine.session_for_job(platform)
    session_id = str(session_item.get("id") or "")
    session = None
    try:
        options = DiscoveryOptions()
        options.max_results = _PER_SEARCH
        options.max_seconds = 90
        session = plat_obj.session_cls()(
            options, session_item.get("cookies") or [], session_id=session_id)
        await session.start()
        discoverer = plat_obj.discoverer()(options, session.ctx)
        tab = SEARCH_TAB.get(platform, "people")

        for term, cid in terms:
            try:
                sweep = await discoverer.sweep(term, tab)
            except Exception as e:                   # noqa: BLE001
                log.warning(f"profile refresh [{platform}] {term!r}: "
                            f"{type(e).__name__}: {e}")
                continue
            out["searches"] += 1
            hits = [h for h in (sweep.hits or []) if h.url]
            out["seen"] += len(hits)

            # ONLY RESULTS WE ALREADY HOLD ARE SAVED. Everything else in
            # the result set is discarded, and that restraint is the whole
            # correctness of this module.
            #
            # The first version saved every hit, on the theory that the
            # other 24 results were free repairs. Measured: they are not.
            # Searching a blank profile's own name ("Taylor-Jayne Adrian")
            # returns 25 people with similar names, of whom roughly one is
            # a row we hold -- so saving them all added 119 strangers to a
            # brand-monitoring client, each filed under a person's name as
            # though it were one of the analyst's keywords. Discovery
            # decides who belongs to a client; a refresh only re-reads what
            # discovery already decided.
            rows: list[dict] = []
            for h in hits:
                known = (
                    by_eid.get(str(getattr(h, "profile_id", "")))
                    or by_url.get(str(h.url))
                )
                if known is None or (known.get("client_id") or "") != cid:
                    continue
                rows.append(profiles_db_fields(h, term))
            if not rows:
                if on_progress is not None:
                    on_progress(dict(out))
                await asyncio.sleep(_GAP_SECONDS)
                continue
            saved, new = await profiles_db.save_many(
                cid, platform, profiles_db.PHASE_DISCOVERY, rows)
            out["saved"] += saved
            out["repaired"] += len(rows)
            if new:
                # Cannot happen by construction -- every row here was read
                # out of the database a moment ago. If it ever does, the
                # matching is wrong and is inventing profiles, which is the
                # exact failure this rewrite removed.
                log.error(
                    f"profile refresh [{platform}] {term!r}: save reported {new} NEW "
                    f"profile(s) while refreshing known rows -- matching is wrong, "
                    f"investigate before running this again")
            # Fresh URLs, straight into the ordinary caching path so the
            # bytes are fingerprinted and logo-matched like any other.
            await avatar_cache.cache_for_profiles(cid, platform, rows)
            out["seen_known"] = out.get("seen_known", 0) + len(rows)

            if on_progress is not None:
                on_progress(dict(out))
            await asyncio.sleep(_GAP_SECONDS)
    finally:
        if session is not None:
            try:
                await session.stop()
            except Exception:                        # noqa: BLE001
                pass
        sessions_engine.release_claim(platform, session_id)

    out["seconds"] = round(time.time() - started, 1)
    log.info(
        f"profile refresh [{platform}]: {out['searches']} search(es) saw "
        f"{out['seen']} profile(s), {out['repaired']} of them were rows that had "
        f"no picture, in {out['seconds']}s")
    return out


def profiles_db_fields(row: Any, term: str) -> dict:
    """One discovery `Row` -> the field dict `save_many` expects.

    A deliberately thin local copy of `discovery/runner.py::row_to_fields`,
    minus the keyword-scoring half. Scoring belongs to a sweep that knows
    which investigation it is running; a refresh is re-reading a profile
    that has already been scored, and re-deriving a score from the search
    term would overwrite a real reading with a weaker one.
    """
    src = ",".join(sorted({v.split(":", 1)[-1] for v in row.src.values()})) or "search"
    return {
        "url": row.url,
        "entity_id": row.profile_id,
        # Left as the term searched. `save` only fills a blank keyword and
        # never rewrites one, so a profile already filed under a client's
        # real keyword keeps it.
        "keyword": term,
        "username": row.username,
        "display_name": row.profile_name,
        "entity_type": row.entity_type,
        "profile_image_url": row.profile_pic_url,
        "discovery_source": src,
        "verified": row.verified,
        "followers": row.followers,
        "friends": row.friends,
        "location": row.location,
        "bio": row.bio,
        "created_at": row.created_iso,
    }
