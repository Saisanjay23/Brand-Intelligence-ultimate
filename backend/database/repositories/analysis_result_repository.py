"""Analysis results, kept for 24 hours and then deleted by MongoDB itself.

    analysis_results      one document per analysed profile
    analysis_screenshots  its evidence PNG, one document, same expiry

WHAT CHANGED, AND WHY IT IS WORTH SAYING OUT LOUD. Analysis output used to
be memory-only on purpose (see shared/analysis_store.py, and the raise in
profile_repository.save()). That made every result vanish on restart, on the
job's TTL, or on eviction -- an analyst who closed the tab lost the batch and
had to re-scrape it, which costs real page loads under a live session. These
two collections replace that with a bounded durable store: results survive a
reload, and stop existing entirely 24 hours later.

THIS IS NOT THE `profiles` COLLECTION, deliberately. Discovery owns that one,
field-scoped, with an analyst's triage decision on it that no sweep may
overwrite. Analysis output is a different thing with a different lifetime --
it is a reading, not a record -- and mixing a 24-hour cache into the durable
document would put a TTL next to data that must never expire. Separate
collections keep the two lifetimes from ever being confused for one another.

HOW THE 24 HOURS IS ACTUALLY ENFORCED -- two mechanisms, because one is not
enough:

  * A TTL INDEX on `expires_at` (expireAfterSeconds=0, the "expire at this
    exact clock time" form) is what DELETES the document. This is Mongo's
    own background job, so it keeps working when this process is down, and
    it cannot be skipped by a code path that forgot to call a sweeper.

  * EVERY READ ALSO FILTERS `expires_at > now`. The TTL monitor only runs
    about once a minute, so for up to ~60 seconds after expiry a document is
    still physically present. Relying on the index alone would hand an
    analyst a result the feature promised was gone. The filter closes that
    window: expired is invisible immediately, and the index reclaims the
    space shortly after.

SCREENSHOTS LIVE IN THEIR OWN COLLECTION, keyed by the same id, with their
own TTL index on the same instant. Two reasons. Inline bytes would make
every list query drag megabytes of PNG it never renders. And GridFS -- which
this codebase uses for durable evidence (evidence_repository.py) -- has no
TTL story that is safe: expiring `fs.files` leaves the chunks behind as
orphans, so it needs a sweeper, and a sweeper is exactly the thing that
stops running. A plain document with a `Binary` field is deleted whole, by
the same mechanism, with nothing left over.

RE-ANALYSING A URL REPLACES ITS RESULT rather than adding a second copy: the
id is derived from (platform, url), so the store holds the most recent
reading of each profile and the 24 hours runs from that reading. Same rule
the in-memory store used, kept so the change of storage does not quietly
change what an analyst sees.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson.binary import Binary

from backend.database.connection import db
from backend.shared.logging import get_logger

log = get_logger("repositories.analysis_result")

RESULTS = "analysis_results"
SCREENSHOTS = "analysis_screenshots"

# How long a result stays readable. The whole feature, in one number.
RETENTION_HOURS = 24

# Mongo refuses a document over 16MB. A full-page capture is normally
# hundreds of KB, but a very tall profile page can run large, and a result
# silently failing to save because its screenshot was oversized would lose
# the SCORED ROW too -- the part that matters most. So the row is written
# first and independently, and only the image is dropped when it will not
# fit (logged, never silent). Well under the limit so BSON overhead and the
# other fields can never push a passing document over it.
MAX_SCREENSHOT_BYTES = 12 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


def result_id(platform: str, url: str) -> str:
    """One stable id per analysed profile.

    Derived from (platform, url) rather than being random, which is what
    makes re-analysing a profile REPLACE its previous reading instead of
    leaving two rows that disagree. It is also what lets a live job's item
    and its saved counterpart be recognised as the same row by the UI, so
    the table can show a running job over the top of the saved set without
    listing anything twice.

    Hashed rather than concatenated: URLs are long and unbounded, and this
    value is used as `_id`, which is indexed.
    """
    raw = f"{(platform or '').strip()}\x00{(url or '').strip()}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def _live() -> dict:
    """The "not expired yet" filter every read applies -- see the module
    docstring on why the TTL index alone is not enough."""
    return {"expires_at": {"$gt": _now()}}


def _clean(doc: dict) -> dict:
    """A stored document as the API returns it: `_id` surfaced as
    `result_id`, timestamps as ISO strings, internals dropped."""
    out = dict(doc)
    out["result_id"] = str(out.pop("_id", ""))
    for f in ("analysed_at_dt", "expires_at"):
        v = out.pop(f, None)
        if f == "expires_at" and isinstance(v, datetime):
            # Naive-but-UTC out of Mongo (motor is not tz_aware); stamp it so
            # a browser does not read the unmarked string as local time.
            out["expires_at"] = (v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v).isoformat()
    return out


# ------------------------------------------------------------------- write

async def save(
    item: dict, *, job_id: str = "", org_id: str = "",
    screenshot: Optional[bytes] = None,
) -> str:
    """Persist one analysed profile for `RETENTION_HOURS`. Returns its id.

    `item` is an `AnalysisItem.to_dict()` -- the exact shape the frontend
    already renders, stored verbatim so a saved row and a live one are the
    same thing to every consumer and no mapping layer can drift between
    them.

    THE ROW AND THE IMAGE ARE TWO WRITES, and the row goes first. If the
    screenshot write fails (oversized, a storage hiccup), the result is
    still saved and still readable -- it just has no thumbnail. The other
    order would let an image problem cost an analyst the reading itself.
    """
    url = str(item.get("url") or "").strip()
    platform = str(item.get("platform") or "").strip()
    if not url:
        raise ValueError("analysis result has no url")

    rid = result_id(platform, url)
    now = _now()
    expires_at = now + timedelta(hours=RETENTION_HOURS)

    doc = {
        **item,
        "_id": rid,
        "job_id": job_id,
        "org_id": org_id,
        # A real datetime alongside the ISO string the item already carries:
        # sorting and range queries need one, and the API contract needs the
        # other. Derived here rather than trusted from the item, so a row
        # that never recorded `analysed_at` still sorts sensibly.
        "analysed_at_dt": now,
        "expires_at": expires_at,
        "has_screenshot": False,
    }

    await db()[RESULTS].replace_one({"_id": rid}, doc, upsert=True)

    if screenshot:
        if len(screenshot) > MAX_SCREENSHOT_BYTES:
            log.warning(
                f"analysis screenshot for {url} is {len(screenshot) / 1024 / 1024:.1f} MB, "
                f"over the {MAX_SCREENSHOT_BYTES / 1024 / 1024:.0f} MB cap -- "
                "the result was saved without it"
            )
        else:
            try:
                await db()[SCREENSHOTS].replace_one(
                    {"_id": rid},
                    {"_id": rid, "png": Binary(screenshot), "expires_at": expires_at},
                    upsert=True,
                )
                await db()[RESULTS].update_one({"_id": rid}, {"$set": {"has_screenshot": True}})
            except Exception as e:
                # Never fatal: the reading is saved, only its evidence is not.
                log.warning(f"could not store analysis screenshot for {url}: {type(e).__name__}: {e}")

    return rid


# -------------------------------------------------------------------- read

async def find(
    *, org_id: str = "", platform: str = "", job_id: str = "",
    limit: int = 500, offset: int = 0,
) -> tuple[list[dict], int]:
    """Saved results, newest first, plus the total matching count.

    Newest first because this list is a work queue read from the top: the
    batch an analyst just ran is the one they are looking at, and yesterday
    evening's is context underneath it.
    """
    q: dict[str, Any] = _live()
    if org_id:
        q["org_id"] = org_id
    if platform:
        q["platform"] = platform
    if job_id:
        q["job_id"] = job_id

    coll = db()[RESULTS]
    total = await coll.count_documents(q)
    cur = coll.find(q).sort("analysed_at_dt", -1).skip(max(0, offset)).limit(max(1, min(limit, 1000)))
    return [_clean(d) async for d in cur], total


async def get_screenshot(rid: str) -> Optional[bytes]:
    """The evidence PNG for one saved result, or None.

    Expiry-checked like every other read: a screenshot must not outlive the
    result it belongs to, even inside the TTL monitor's sweep window.
    """
    if not rid:
        return None
    doc = await db()[SCREENSHOTS].find_one({"_id": rid, **_live()})
    if not doc:
        return None
    png = doc.get("png")
    return bytes(png) if png else None


async def stats() -> dict:
    """What is being held right now -- for /stats, and for answering "did
    the retention actually delete anything"."""
    coll = db()[RESULTS]
    live = await coll.count_documents(_live())
    # Everything the TTL monitor has not swept yet. A persistently non-zero
    # gap means the TTL index is missing or disabled, which is exactly the
    # failure this feature would otherwise hide.
    total = await coll.count_documents({})
    return {
        "live": live,
        "awaiting_ttl_sweep": max(0, total - live),
        "retention_hours": RETENTION_HOURS,
    }


# ------------------------------------------------------------------ delete

async def delete_many(ids: list[str]) -> int:
    """Delete exactly these results -- the "Delete Selected" action.

    Their screenshots go with them. Leaving those behind would keep
    megabytes alive with nothing referencing them until their own TTL fired,
    which is the orphan problem this design exists to avoid.
    """
    wanted = [i for i in (ids or []) if i]
    if not wanted:
        return 0
    res = await db()[RESULTS].delete_many({"_id": {"$in": wanted}})
    await db()[SCREENSHOTS].delete_many({"_id": {"$in": wanted}})
    return res.deleted_count


async def delete_all(*, org_id: str = "", platform: str = "") -> int:
    """Delete every saved result, or every one for a client/platform.

    Scoping is available but the UI's "Delete All" passes neither, on
    purpose: it means what it says. The confirmation in front of it is what
    stands between an analyst and that, not this function.
    """
    q: dict[str, Any] = {}
    if org_id:
        q["org_id"] = org_id
    if platform:
        q["platform"] = platform

    if not q:
        res = await db()[RESULTS].delete_many({})
        await db()[SCREENSHOTS].delete_many({})
        return res.deleted_count

    # Scoped: the screenshots collection carries no org/platform of its own
    # (it is keyed by result id and nothing else), so the ids have to be
    # resolved before the rows they belong to are gone.
    doomed = [d["_id"] async for d in db()[RESULTS].find(q, {"_id": 1})]
    if not doomed:
        return 0
    res = await db()[RESULTS].delete_many({"_id": {"$in": doomed}})
    await db()[SCREENSHOTS].delete_many({"_id": {"$in": doomed}})
    return res.deleted_count


# ------------------------------------------------------------------ indexes

async def ensure_indexes() -> None:
    """The TTL indexes ARE the feature -- without them nothing is ever
    deleted and this becomes an unbounded collection of screenshots.

    `expireAfterSeconds=0` against a date field is Mongo's "delete this
    document when that instant passes" form, as opposed to the more common
    "delete N seconds after this timestamp". It is used here because the
    expiry is a property of the result (set once, at save time) rather than
    a policy applied to a creation date -- so changing RETENTION_HOURS
    affects new results and leaves already-promised expiries alone.
    """
    await db()[RESULTS].create_index("expires_at", expireAfterSeconds=0, name="ttl_expires_at")
    await db()[SCREENSHOTS].create_index("expires_at", expireAfterSeconds=0, name="ttl_expires_at")
    # The listing's own sort, so "newest first" does not become a collection
    # scan once a busy day has filled this up.
    await db()[RESULTS].create_index([("analysed_at_dt", -1)], name="analysed_desc")
    await db()[RESULTS].create_index([("org_id", 1), ("analysed_at_dt", -1)], name="org_analysed")
