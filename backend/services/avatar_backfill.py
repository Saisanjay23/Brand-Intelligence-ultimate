"""Recover profile pictures the sweeps found but never kept a copy of.

THE PROBLEM, MEASURED. 10,413 stored profiles have a picture URL and no
stored bytes. Their cards show an initial-letter circle while the profile
itself plainly has a photo -- the analyst opens it, sees the picture, and
reasonably concludes the tool is broken.

It is not that the picture was never there. Facebook and Instagram SIGN
their CDN URLs and expire them within hours; a stored Instagram URL fetched
twelve days later answers `403 URL signature expired`. The copy that was
supposed to outlive the signature is the one missing, because
`avatar_cache` gave every batch a flat 180-second budget regardless of size
and said nothing when a fetch failed. A 3,301-profile Facebook sweep cached
1,514. That cap now scales with the batch and every failure is counted and
named (see avatar_cache.batch_timeout_for), so this should not recur -- but
it has already happened, and those pictures need fetching again.

TWO TIERS, AND THE DIFFERENCE MATTERS A LOT.

    `from_stored_urls`  costs nothing but bandwidth. YouTube, Twitter and
                        Telegram do not sign their avatar URLs -- yt3.ggpht,
                        pbs.twimg and Telegram's inline data URIs all still
                        resolve from what is already in the database. No
                        session, no login, no scraping, no detection
                        surface. This covers 4,963 of the 10,413.

    `via_revisit`       costs real account time. Facebook and Instagram
                        URLs are dead, so the only way to get a live one is
                        to ask the platform again as a logged-in user --
                        one visit per profile, through the pooled session,
                        paced like any other scrape. This is the remaining
                        5,450 and it is hours of work, which is why it is a
                        separate call an operator chooses to make rather
                        than something that runs on its own.

RESUMABLE BY CONSTRUCTION. The work queue is "profiles with no
`avatar_sha`", recomputed on every call, and each recovered picture removes
itself from it the moment it lands. So an interrupted run loses nothing and
a re-run picks up exactly where it stopped -- no checkpoint to keep, and no
way for a crash to leave the job half-marked.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from backend.database.connection import db
from backend.services import avatar_cache
from backend.shared.imagefetch import url_is_live
from backend.shared.logging import get_logger

log = get_logger("services.avatar_backfill")

PROFILES = "profiles"

# WHICH PLATFORM IT IS TURNED OUT TO BE THE WRONG QUESTION.
#
# The first version of this split platforms into "URL still works" and "URL
# expired", because a sample of stored Facebook and Instagram rows all
# failed to re-fetch. That sample was twelve days old, and the conclusion
# drawn from it -- that Meta URLs are unrecoverable -- was wrong and
# expensive: it led to a per-profile re-visit design that rate-limited an
# Instagram account after 200 calls and recovered nothing.
#
# Meta stamps each URL's own expiry into it (`oe=`, see
# imagefetch.signed_expiry), and the real window measured across the stored
# data is 110-307 HOURS. Four and a half to thirteen days, not "hours" as
# this codebase's own comment claimed. So the question is never "what
# platform is this" but "is THIS URL still valid", asked per row -- and
# right now that makes 687 Facebook pictures free to recover that the
# platform-based rule had written off as needing a login.
#
# Kept only as the list of platforms a re-visit is even possible for.
REVISIT_PLATFORMS = ("facebook", "instagram")

# How many profiles one call pulls from Mongo at a time. Bounded so a
# 10,000-row backfill never materialises as one list.
_PAGE = 200

# SIZED TO THE ENGINE'S OWN BUDGET, not picked round. Facebook's
# `_resolve_missing` gives ITSELF 180 seconds (RESOLVE_TIME_BUDGET_SEC) at
# two concurrent visits, so a page of 200 would be truncated every single
# round -- the leftovers do come back on the next page, because the queue is
# recomputed from "still has no avatar_sha", but every truncated round pays
# for visits it then throws away. A page that fits the budget wastes
# nothing. Instagram is one cheap API call per profile with its own pacing,
# so it takes the default.
_REVISIT_PAGE = {"facebook": 80}

# HOW MANY SEPARATE 4xx ANSWERS WRITE A URL OFF. An unsigned YouTube or
# Twitter URL has no expiry stamp, so `url_is_live` can never rule it out --
# and when the account changes its picture the old URL answers 404 for ever.
# With nothing to stop it, the hourly retry fetched the same 18 dead
# pictures four times an hour for days. Three strikes across three hourly
# passes is long enough that a brief CDN block cannot write off a good
# picture, and short enough that the waste ends the same afternoon.
GONE_AFTER_STRIKES = 3


def _is_gone(doc: dict, url: str) -> bool:
    """Has the CDN refused THIS exact URL often enough to stop asking?"""
    return (bool(url) and doc.get("avatar_gone_url") == url
            and int(doc.get("avatar_gone_strikes") or 0) >= GONE_AFTER_STRIKES)


def _blank_query(client_id: Optional[str], platform: Optional[str] = None,
                 with_url: bool = True) -> dict:
    """Profiles on one platform that have no stored picture bytes.

    `avatar_sha` absent and `avatar_sha` empty are BOTH blanks and the
    difference is not meaningful -- an aggregation that checked only for
    empty reported this whole problem as zero rows, which is how it stayed
    invisible.
    """
    q: dict[str, Any] = {
        "$or": [
            {"avatar_sha": {"$exists": False}},
            {"avatar_sha": {"$in": ["", None]}},
        ],
    }
    if platform:
        q["platform"] = platform
    if client_id:
        q["client_id"] = client_id
    if with_url:
        q["profile_image_url"] = {"$nin": ["", None]}
    return q


async def count_blank(client_id: Optional[str] = None) -> dict[str, dict[str, int]]:
    """What is missing, per platform, split by whether it can still be had.

    `live` is what this process can recover on its own, right now, with
    nothing but an HTTP GET. `expired` is the number whose URL has provably
    passed its `oe=` stamp -- nobody can fetch those from that URL again, so
    they need re-discovering rather than retrying, and reporting them as
    merely "failed" would send someone to retry them for ever. `gone` is
    the same fact learned the other way: the CDN itself refused the URL
    GONE_AFTER_STRIKES times.
    """
    out: dict[str, dict[str, int]] = {}
    cursor = db()[PROFILES].find(
        _blank_query(client_id, with_url=False),
        {"platform": 1, "profile_image_url": 1,
         "avatar_gone_url": 1, "avatar_gone_strikes": 1},
    )
    now = time.time()
    async for doc in cursor:
        pid = doc.get("platform", "") or "unknown"
        row = out.setdefault(pid, {"live": 0, "expired": 0, "gone": 0,
                                   "no_url": 0, "total": 0})
        row["total"] += 1
        url = (doc.get("profile_image_url") or "").strip()
        if not url:
            row["no_url"] += 1
        elif _is_gone(doc, url):
            row["gone"] += 1
        elif url_is_live(url, now):
            row["live"] += 1
        else:
            row["expired"] += 1
    return out


async def from_stored_urls(
    client_id: Optional[str] = None,
    platforms: Optional[list[str]] = None,
    limit: int = 0,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> dict[str, Any]:
    """Re-download every picture whose stored URL is still valid. No session.

    ASKED PER ROW, NOT PER PLATFORM. A Facebook URL found four days ago is
    as fetchable as a Twitter one; a Facebook URL found three weeks ago is
    not. Only the URL knows, and it says so in its own `oe=` stamp -- so
    this filters on that and never on which platform a row came from. That
    one change makes 687 Facebook pictures free to recover which the
    platform-based rule had written off as needing a login.

    A row whose URL has expired is SKIPPED rather than attempted: the fetch
    could only ever return 403, and a log full of expected 403s is how a
    real failure stops being visible.
    """
    started = time.time()
    result: dict[str, Any] = {
        "tier": "stored-url", "platforms": {},
        "recovered": 0, "failed": 0, "skipped_expired": 0, "skipped_gone": 0,
    }

    # WALKED BY `_id`, NOT RE-QUERIED FROM THE TOP.
    #
    # The first version paged with a bare `.limit()` and no cursor, which
    # re-read the same first page every round. Recovered rows drop out of
    # the query, but EXPIRED ones never do -- so once the first page filled
    # up with expired rows, the scan could never reach the live rows sitting
    # behind them. It recovered 28 of 687 and reported itself finished.
    #
    # An `_id` watermark fixes it properly: every row in the owed set is
    # visited exactly once, whatever its state, and the walk ends when the
    # collection does rather than when one page happens to look unhelpful.
    after: Any = None
    while True:
        query = _blank_query(client_id, with_url=True)
        if platforms:
            query["platform"] = {"$in": list(platforms)}
        if after is not None:
            query["_id"] = {"$gt": after}
        cursor = db()[PROFILES].find(
            query,
            {"url": 1, "entity_id": 1, "profile_image_url": 1,
             "client_id": 1, "platform": 1,
             "avatar_gone_url": 1, "avatar_gone_strikes": 1},
        ).sort("_id", 1).limit(_PAGE)
        batch = [d async for d in cursor]
        if not batch:
            break
        after = batch[-1]["_id"]

        now = time.time()
        # (client, platform) -> items, because `cache_for_profiles` is scoped
        # to one of each and a backfill spans every client.
        buckets: dict[tuple[str, str], list[dict]] = {}
        skipped = skipped_gone = 0
        for doc in batch:
            url = (doc.get("profile_image_url") or "").strip()
            cid = doc.get("client_id") or ""
            pid = doc.get("platform") or ""
            if not cid or not pid:
                continue
            if not url_is_live(url, now):
                skipped += 1
                continue
            if _is_gone(doc, url):
                skipped_gone += 1
                continue
            buckets.setdefault((cid, pid), []).append({
                "url": doc.get("url", ""),
                "entity_id": doc.get("entity_id", ""),
                "profile_image_url": url,
            })
        result["skipped_expired"] += skipped
        result["skipped_gone"] += skipped_gone

        attempted = 0
        for (cid, pid), items in buckets.items():
            # Straight through the ordinary caching path, so a backfilled
            # picture is fingerprinted, logo-matched and stored exactly like
            # one caught during a sweep. A second implementation here would
            # be a second thing to keep correct.
            landed = await avatar_cache.cache_for_profiles(cid, pid, items)
            stats = result["platforms"].setdefault(pid, {"recovered": 0, "failed": 0})
            stats["recovered"] += landed
            stats["failed"] += max(0, len(items) - landed)
            result["recovered"] += landed
            result["failed"] += max(0, len(items) - landed)
            attempted += len(items)

        if on_progress is not None:
            on_progress(dict(result))
        if limit and result["recovered"] + result["failed"] >= limit:
            break
        # NOT `if attempted == 0: break`. A page made entirely of expired
        # rows is ordinary -- they are interleaved with live ones all
        # through the collection -- and stopping on one is exactly the bug
        # the `_id` watermark above replaced. The walk ends when the
        # collection does, and only then.

    result["seconds"] = round(time.time() - started, 1)
    if result["recovered"] or result["failed"]:
        log.info(
            f"avatar backfill: recovered {result['recovered']}, "
            f"failed {result['failed']}, "
            f"skipped {result['skipped_expired']} expired "
            f"and {result['skipped_gone']} gone "
            f"in {result['seconds']}s")
    return result


# ------------------------------------------------------------- the monitor

# EVERY HOUR, BECAUSE THE WINDOW IS DAYS. A signed URL lives 110-307 hours,
# so an hourly retry gets between 110 and 300 attempts before a picture
# becomes unrecoverable. No realistic outage -- a CDN wobble, a restart, a
# batch cut short, a night with the machine asleep -- survives that. This is
# what turns "the sweep tries once and best-effort is the contract" into
# "the picture is kept, full stop".
PICTURE_RETRY_INTERVAL_S = 3600.0

_monitor_task: Optional[Any] = None


async def _retry_loop() -> None:
    while True:
        try:
            res = await from_stored_urls()
            if res["recovered"]:
                log.info(
                    f"picture retry: recovered {res['recovered']} profile picture(s) "
                    f"a sweep had not managed to keep")
            if res["skipped_expired"]:
                # Named rather than silent: these are the ones no retry can
                # ever fix, and the only route back is re-discovery.
                log.warning(
                    f"picture retry: {res['skipped_expired']} profile(s) have a picture "
                    f"URL that has expired -- re-sweep their client to pick the picture "
                    f"up again; retrying cannot recover them")
            if res["skipped_gone"]:
                log.warning(
                    f"picture retry: {res['skipped_gone']} profile(s) have a picture "
                    f"URL the CDN has refused {GONE_AFTER_STRIKES} times (usually the "
                    f"account changed its picture) -- no longer retried; re-sweep "
                    f"their client to pick the new one up")
        except Exception as e:                       # noqa: BLE001 - never fatal
            log.error(f"picture retry sweep failed: {type(e).__name__}: {e}")
        await asyncio.sleep(PICTURE_RETRY_INTERVAL_S)


def start_retry_monitor() -> None:
    """Keep retrying every picture the sweeps did not manage to keep.

    Started from main.py alongside the session and evidence monitors, and
    idempotent the same way they are -- calling it twice leaves one loop.
    """
    global _monitor_task
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_retry_loop())
        log.info(
            f"profile-picture retry monitor started -- every "
            f"{PICTURE_RETRY_INTERVAL_S / 60:.0f}m, for as long as each URL stays valid")


def stop_retry_monitor() -> None:
    global _monitor_task
    if _monitor_task is not None:
        _monitor_task.cancel()
        _monitor_task = None


async def via_revisit(
    client_id: Optional[str] = None,
    platforms: Optional[list[str]] = None,
    limit: int = 0,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> dict[str, Any]:
    """Tier two: ask the platform again, as a logged-in user.

    THE EXPENSIVE ONE, and deliberately explicit about it. Facebook and
    Instagram signed URLs are dead, so each profile needs a live lookup:
    Instagram answers one authenticated API call per username, Facebook
    needs an actual page visit. On several thousand profiles that is hours
    of paced scraping on a real account, which is a decision an operator
    makes rather than something that should ever start by itself.

    Paced by the platform's own discovery settings, and it claims a pooled
    session the same way a sweep does -- so it queues behind a running sweep
    instead of competing with it for the same login.
    """
    from backend.sessions import manager as sessions_engine

    wanted = [p for p in (platforms or REVISIT_PLATFORMS) if p in REVISIT_PLATFORMS]
    started = time.time()
    result: dict[str, Any] = {"tier": "revisit", "platforms": {}, "recovered": 0, "failed": 0}

    for platform in wanted:
        pending = await db()[PROFILES].count_documents(_blank_query(client_id, platform))
        if not pending:
            continue
        log.info(
            f"avatar backfill [{platform}]: {pending} profile(s) need a live re-visit "
            f"-- this uses a pooled session and is paced like a sweep")
        try:
            recovered, failed, seen = await _revisit_platform(
                platform, client_id, limit, on_progress, sessions_engine)
        except Exception as e:                       # noqa: BLE001 - never fatal
            log.error(f"avatar backfill [{platform}] failed: {type(e).__name__}: {e}")
            result["platforms"][platform] = {"error": f"{type(e).__name__}: {e}"}
            continue
        result["platforms"][platform] = {"recovered": recovered, "failed": failed, "seen": seen}
        result["recovered"] += recovered
        result["failed"] += failed

    result["seconds"] = round(time.time() - started, 1)
    return result


async def _revisit_platform(
    platform: str, client_id: Optional[str], limit: int,
    on_progress: Optional[Callable[[dict], None]], sessions_engine: Any,
) -> tuple[int, int, int]:
    """One platform's re-visit pass, holding one session for its duration."""
    from backend.platforms.scan_options import DiscoveryOptions

    plat_obj, session_item = await sessions_engine.session_for_job(platform)
    session_id = str(session_item.get("id") or "")
    session = None
    recovered = failed = seen = 0
    try:
        session_cls = plat_obj.session_cls()
        options = DiscoveryOptions()
        session = session_cls(options, session_item.get("cookies") or [],
                              session_id=session_id)
        await session.start()
        refresh = _refresher_for(platform, plat_obj, session, options)

        while True:
            cursor = db()[PROFILES].find(
                _blank_query(client_id, platform),
                {"url": 1, "entity_id": 1, "username": 1, "client_id": 1},
            ).limit(_REVISIT_PAGE.get(platform, _PAGE))
            batch = [d async for d in cursor]
            if not batch:
                break

            fresh = await refresh(batch)
            by_client: dict[str, list[dict]] = {}
            for doc, image_url in fresh:
                if not image_url or not doc.get("client_id"):
                    failed += 1
                    continue
                by_client.setdefault(doc["client_id"], []).append({
                    "url": doc.get("url", ""),
                    "entity_id": doc.get("entity_id", ""),
                    "profile_image_url": image_url,
                })
            for cid, items in by_client.items():
                landed = await avatar_cache.cache_for_profiles(cid, platform, items)
                recovered += landed
                failed += max(0, len(items) - landed)

            seen += len(batch)
            if on_progress is not None:
                on_progress({"platform": platform, "seen": seen,
                             "recovered": recovered, "failed": failed})
            log.info(
                f"avatar backfill [{platform}]: {recovered} recovered / {seen} visited")
            if limit and seen >= limit:
                break
            if len(batch) < _REVISIT_PAGE.get(platform, _PAGE):
                break
            # A GUARD AGAINST SPINNING, not a pacing delay. If a page of
            # profiles yields nothing at all, the query will return the same
            # page forever -- so stop rather than loop on work that cannot
            # be done.
            if not any(u for _d, u in fresh):
                log.warning(
                    f"avatar backfill [{platform}]: a full page of {len(batch)} profile(s) "
                    f"returned no picture -- stopping rather than re-reading the same rows")
                break
    finally:
        if session is not None:
            try:
                await session.stop()
            except Exception:                        # noqa: BLE001
                pass
        sessions_engine.release_claim(platform, session_id)
    return recovered, failed, seen


def _refresher_for(platform: str, plat_obj: Any, session: Any, options: Any):
    """A `(docs) -> [(doc, fresh_image_url)]` for one platform.

    Each reuses the engine's own profile reader rather than re-implementing
    one: Facebook's `_resolve_missing` already visits a profile page and
    id-scopes what it reads (so it can never attach another account's
    picture), and Instagram's profile-info endpoint is one authenticated
    call. Writing new scraping here would be a second parser to keep
    correct against the same platforms.
    """
    if platform == "facebook":
        discoverer = plat_obj.discoverer()(options, session.ctx)

        async def _facebook(docs: list[dict]) -> list[tuple[dict, str]]:
            ids = [d.get("entity_id", "") for d in docs if d.get("entity_id")]
            if not ids:
                return [(d, "") for d in docs]
            hits = await discoverer._resolve_missing(ids, "profile")
            return [(d, getattr(hits.get(d.get("entity_id", "")), "avatar", "") or "")
                    for d in docs]

        return _facebook

    if platform == "instagram":
        from backend.platforms.instagram.discovery_engine import (
            MOBILE_UA, PROFILE_INFO_API, parse_lines, profile_from)
        from urllib.parse import quote

        async def _instagram(docs: list[dict]) -> list[tuple[dict, str]]:
            out: list[tuple[dict, str]] = []
            for d in docs:
                handle = (d.get("username") or "").strip()
                if not handle:
                    out.append((d, ""))
                    continue
                try:
                    res = await session.ctx.request.get(
                        PROFILE_INFO_API.format(u=quote(handle)),
                        headers={"User-Agent": MOBILE_UA,
                                 "x-ig-app-id": "936619743392459",
                                 "accept": "application/json"},
                        timeout=options.timeout * 1000,
                    )
                    text = await res.text() if res.status == 200 else ""
                except Exception:                    # noqa: BLE001
                    text = ""
                avatar = ""
                for blob in parse_lines(text):
                    if user := profile_from(blob, handle):
                        avatar = user.avatar or ""
                        break
                out.append((d, avatar))
                # Paced: this is an authenticated API call per profile on a
                # real account, and a few thousand of them back to back with
                # no gap is the one pattern worth not having.
                await asyncio.sleep(1.5)
            return out

        return _instagram

    raise ValueError(f"no avatar refresher for {platform!r}")
