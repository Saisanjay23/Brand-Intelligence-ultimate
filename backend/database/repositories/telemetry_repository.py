"""A rolling record of how sweeps have been GOING, per platform, over days.

WHAT THIS IS FOR, AND WHY NOTHING ELSE COVERS IT. Everything else in this
engine watches one sweep, one session or one keyword. Nothing watches the
ENGINE -- the thing that quietly stops working when a platform ships a
change, and whose failure mode is not an error but a clean, cheerful,
permanent zero.

The mechanism this restores was described in `shared/extraction.py`'s own
docstring and then lost: it names "services/discovery_service.py's
parser-drift canary" as the thing that "detects the symptom", and that file
was deleted with the old backend. `run_strategies` still reports the CAUSE
beautifully -- which strategy failed, at which file and line, with the
source text of the line to change -- but it reports it once, into a log
line, and then the moment is gone. Nobody was aggregating, so nobody could
see the shape that actually matters:

    Facebook rotates a GraphQL doc id. The payload branch recognises
    nothing. The DOM fallback carries the sweep, or returns nothing at all.
    Every sweep reports `no-results`, which is a real and common answer.
    The job reports done. The coverage ledger records the keyword as
    covered, because it WAS searched. Every dashboard is green and the
    client is told nobody is impersonating them, for as long as it takes a
    human to get suspicious.

THE DISCRIMINATOR. A keyword that genuinely matches nobody is specific to
one client and one keyword. A broken parser is specific to a PLATFORM and
hits every client and every keyword on it at once. So the signal is never
"this sweep found nothing" -- it is "this platform found nothing across
many distinct keywords belonging to many distinct clients, when last week
it did". That is why this collection stores one row per sweep with its
client and keyword attached, and why the aggregation below counts DISTINCT
keywords and clients rather than sweeps.

SELF-CALIBRATING, ON PURPOSE. There are no per-platform thresholds here and
none in the detector that reads it. A baseline window supplies what normal
looks like for each platform -- its usual extraction source, its usual
yield, its usual rate of empty searches -- and the recent window is judged
against that. Hard-coded expectations are themselves a thing that drifts:
they are written once against whatever the platform did that month, and
nobody revisits them until they start lying. A baseline cannot go stale
because it is recomputed every time it is asked for.

BOUNDED BY CONSTRUCTION. A TTL index expires rows at
`RETENTION_DAYS`, so this cannot grow without limit no matter how long the
engine runs -- the same policy `incidents` uses, for the same reason.
Volume is modest: one row per (keyword, tab) per platform per run, which
for ten clients of fifteen keywords across six platforms is a few thousand
rows a day and a few tens of thousands retained.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.database.connection import db
from backend.shared.logging import get_logger

log = get_logger("repositories.telemetry")

TELEMETRY = "sweep_telemetry"

# Long enough that a baseline is not destroyed by a quiet weekend, short
# enough that the collection stays small. Matches `incidents`.
RETENTION_DAYS = 14

# Written in one go at the end of a job rather than per sweep: a sweep takes
# seconds to minutes and this is bookkeeping, so batching costs nothing in
# freshness and saves one round trip per search for the life of the engine.
_CHUNK = 1000


def _now() -> datetime:
    # Naive-but-UTC, matching every other repository here -- see
    # database/connection.py's module docstring.
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def record_sweeps(group_id: str, job_id: str, sweeps: list[Any]) -> int:
    """One job's completed sweeps, appended to the rolling record.

    Takes `CompletedSweep`-shaped objects straight off `job.history`, so
    the runner needs to keep no second copy of anything. Never raises: a
    telemetry write failing must cost the observability and never the
    sweep, exactly as `coverage_repository._write` treats its own.
    """
    if not sweeps:
        return 0
    ts = _now()
    docs = []
    for s in sweeps:
        docs.append({
            "ts": ts,
            "group_id": group_id,
            "job_id": job_id,
            "platform": getattr(s, "platform", "") or "",
            "tab": getattr(s, "tab", "") or "",
            # The keyword is stored because the detector counts DISTINCT
            # keywords: a platform returning nothing for one term is
            # ordinary, and for forty terms across six clients is not.
            "keyword": getattr(s, "keyword", "") or "",
            "outcome": getattr(s, "outcome", "") or "",
            "stopped": getattr(s, "stopped", "") or "",
            # WHICH EXTRACTION PATH ACTUALLY PRODUCED THE RESULT. The
            # earliest warning there is: an engine that has silently fallen
            # back to scraping the rendered DOM because the platform's own
            # payload stopped parsing is still returning rows, and is one
            # layout change away from returning none.
            "source": getattr(s, "source", "") or "",
            # {attribute: [hits, misses]} for every key this engine
            # deliberately targets -- see shared/schema_probe.py. Stored
            # per sweep so the detector can compare one key's hit rate
            # against its own past and name it when it stops matching.
            "schema": dict(getattr(s, "schema", None) or {}),
            "hits": int(getattr(s, "hits_found", 0) or 0),
            "new": int(getattr(s, "hits_new", 0) or 0),
            "seconds": float(getattr(s, "duration_seconds", 0.0) or 0.0),
        })
    try:
        coll = db()[TELEMETRY]
        for i in range(0, len(docs), _CHUNK):
            await coll.insert_many(docs[i:i + _CHUNK], ordered=False)
    except Exception as e:
        log.error(
            f"sweep telemetry write failed: {type(e).__name__}: {e} -- the job is "
            f"unaffected, but {len(docs)} sweep(s) are missing from the record the "
            f"parser-drift detector reads")
        return 0
    return len(docs)


async def window(since: datetime, until: Optional[datetime] = None) -> dict[str, dict]:
    """Per-platform shape of the sweeps in one time window.

    TWO KINDS OF NUMBER, AND THEY MUST NOT BE MIXED.

    MAGNITUDE -- yield and empty-rate -- is measured PER SWEEP, because the
    two windows being compared are different lengths (a day against a week)
    and anything summed over a window is proportional to how long the window
    is. The first version of this divided total hits by DISTINCT (client,
    keyword) searches; over a seven-day baseline that counted seven days of
    hits against one week's worth of distinct keywords, inflating the
    baseline roughly sevenfold and making every healthy platform look like
    it had collapsed. Per-sweep means are invariant to window length, which
    is the only property that makes the comparison meaningful.

    SCOPE -- how many distinct searches and distinct clients -- is measured
    by DEDUPLICATED (client, keyword) pairs, because scope is what separates
    a broken platform from a quiet client, and forty sweeps of one keyword
    must not be able to masquerade as forty keywords.

    Returns, per platform:
        sweeps        individual (keyword, tab) searches run
        hits          total profiles found
        empty_sweeps  of those sweeps, how many found nothing
        searches      DISTINCT (client, keyword) pairs -- evidence, not size
        clients       DISTINCT clients swept -- the cross-client check
        sources       {extraction source: sweeps}; the modal entry is normal
    """
    match: dict[str, Any] = {"ts": {"$gte": since}}
    if until is not None:
        match["ts"]["$lt"] = until
    try:
        cursor = db()[TELEMETRY].aggregate([
            {"$match": match},
            {"$facet": {
                # Magnitude: one row per sweep, never collapsed.
                "totals": [
                    {"$group": {
                        "_id": "$platform",
                        "sweeps": {"$sum": 1},
                        "hits": {"$sum": "$hits"},
                        "empty_sweeps": {"$sum": {"$cond": [{"$lte": ["$hits", 0]}, 1, 0]}},
                        "clients": {"$addToSet": "$group_id"},
                    }},
                ],
                # Scope: collapsed to distinct (client, keyword) pairs, so a
                # platform with three Facebook tabs does not get three votes
                # per keyword.
                "scope": [
                    {"$group": {"_id": {"p": "$platform", "g": "$group_id", "k": "$keyword"}}},
                    {"$group": {"_id": "$_id.p", "searches": {"$sum": 1}}},
                ],
                "sources": [
                    {"$group": {"_id": {"p": "$platform", "s": "$source"}, "n": {"$sum": 1}}},
                ],
            }},
        ])
        facets = await cursor.to_list(length=1)
    except Exception as e:
        # A read failing must never be reported as "everything looks fine" --
        # that is a false clean bill of health, and the detector above this
        # is specifically built to make those impossible. Callers get the
        # exception and report unknown.
        log.error(f"sweep telemetry window failed: {type(e).__name__}: {e}")
        raise

    out: dict[str, dict] = {}
    if not facets:
        return out
    for row in facets[0].get("totals", []):
        out[row["_id"]] = {
            "sweeps": int(row.get("sweeps", 0)),
            "hits": int(row.get("hits", 0)),
            "empty_sweeps": int(row.get("empty_sweeps", 0)),
            "clients": len(row.get("clients") or []),
            "searches": 0,
            "sources": {},
        }
    for row in facets[0].get("scope", []):
        if row["_id"] in out:
            out[row["_id"]]["searches"] = int(row.get("searches", 0))
    for row in facets[0].get("sources", []):
        pid = row["_id"]["p"]
        if pid in out:
            out[pid]["sources"][row["_id"]["s"] or ""] = int(row.get("n", 0))
    return out


async def schema_window(since: datetime, until: Optional[datetime] = None) -> dict[str, dict]:
    """Per-platform, per-attribute hit/miss totals in one time window.

    `{platform: {attribute: {"hits": n, "misses": n}}}`. Kept separate from
    `window()` rather than folded into it because the two answer different
    questions and are read at different times -- and because unwinding a
    per-document map costs a pipeline stage that the health summary has no
    use for.

    Mongo cannot group by a dynamic key directly, so `$objectToArray`
    turns `{key: [hits, misses]}` into rows first. Sweeps that recorded no
    schema at all (an engine with no probe, or telemetry written before
    the probe existed) simply contribute nothing.
    """
    match: dict[str, Any] = {"ts": {"$gte": since}, "schema": {"$exists": True, "$ne": {}}}
    if until is not None:
        match["ts"]["$lt"] = until
    try:
        cursor = db()[TELEMETRY].aggregate([
            {"$match": match},
            {"$project": {"platform": 1, "kv": {"$objectToArray": "$schema"}}},
            {"$unwind": "$kv"},
            {"$group": {
                "_id": {"p": "$platform", "k": "$kv.k"},
                # The probe stores [hits, misses]; a malformed or missing
                # element reads as 0 rather than failing the whole pipeline.
                "hits": {"$sum": {"$ifNull": [{"$arrayElemAt": ["$kv.v", 0]}, 0]}},
                "misses": {"$sum": {"$ifNull": [{"$arrayElemAt": ["$kv.v", 1]}, 0]}},
            }},
        ])
        rows = await cursor.to_list(length=5000)
    except Exception as e:
        log.error(f"schema window failed: {type(e).__name__}: {e}")
        raise

    out: dict[str, dict] = {}
    for row in rows:
        pid = row["_id"]["p"]
        out.setdefault(pid, {})[row["_id"]["k"]] = {
            "hits": int(row.get("hits", 0)),
            "misses": int(row.get("misses", 0)),
        }
    return out


async def ensure_indexes() -> None:
    """Best-effort, same policy as every repository here: an index that
    cannot be built must not stop the engine from starting.

    The TTL index is the one that matters. Without it this collection
    grows for the life of the deployment, which is precisely the kind of
    slow failure this module exists to catch in others.
    """
    coll = db()[TELEMETRY]
    try:
        await coll.create_index(
            [("ts", 1)], name="telemetry_ttl",
            expireAfterSeconds=RETENTION_DAYS * 24 * 3600)
    except Exception as e:
        log.error(
            f"could not build the sweep-telemetry TTL index: {type(e).__name__}: {e} -- "
            f"the collection will NOT self-expire; drop and recreate the index, or "
            f"prune {TELEMETRY} manually")
    try:
        await coll.create_index(
            [("platform", 1), ("ts", -1)], name="telemetry_platform_window")
    except Exception as e:
        log.error(f"could not build index telemetry_platform_window: {type(e).__name__}: {e}")


async def purge_before(cutoff: datetime) -> int:
    """Manual prune, for a deployment whose TTL index never built."""
    try:
        res = await db()[TELEMETRY].delete_many({"ts": {"$lt": cutoff}})
        return int(res.deleted_count or 0)
    except Exception as e:
        log.error(f"sweep telemetry purge failed: {type(e).__name__}: {e}")
        return 0


def default_windows(hours: int = 24, baseline_days: int = 7) -> tuple[datetime, datetime, datetime]:
    """(baseline_start, recent_start, now) for the detector's two windows.

    The baseline deliberately EXCLUDES the recent window. Including it
    would let a drift that has been running for a day quietly become part
    of what "normal" means, which is how a self-calibrating detector
    calibrates itself into silence.
    """
    now = _now()
    recent_start = now - timedelta(hours=hours)
    baseline_start = recent_start - timedelta(days=baseline_days)
    return baseline_start, recent_start, now
