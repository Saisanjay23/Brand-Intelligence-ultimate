"""Which keyword has actually been searched, on which platform, for which
client -- kept in the database rather than in a job's memory.

THE GUARANTEE THIS EXISTS TO MAKE ENFORCEABLE. Every keyword an analyst
saved under a client must be searched on every platform in scope. That is
not a nice-to-have in brand-impersonation monitoring: an unsearched
permutation is an impersonator nobody looked for, and the client is told the
sweep finished. The one unacceptable outcome is a keyword that was never
searched being indistinguishable from a keyword that was searched and found
nothing.

WHY THE JOB COULD NOT PROVIDE IT. `DiscoveryJob` lives in an in-memory
`JobStore` that evicts, and the scheduler that runs ten clients one after
another is plain JavaScript in a browser tab that a refresh ends. Between
them they held the only record of what had been swept, and they held it as
AGGREGATES -- `keywords_done` vs `keywords_total`, a note, a count. So every
way a sweep can end early lost the one detail that matters:

  * the pool ran out of usable sessions mid-platform, so `run.queue` still
    held keywords when `_MAX_CLAIM_ROUNDS` was spent -- the platform
    reported "partial" and the queue was discarded, naming nothing;
  * a keyword that took down `_MAX_KEYWORD_ATTEMPTS` sessions was dropped;
  * a platform with no usable session was skipped whole, taking every one
    of that client's keywords with it;
  * a tab sweep that raised inside the concurrent gather was logged and
    forgotten, uncounted;
  * Telegram's FloodWait hard-stopped a platform with keywords unreached.

Each is a legitimate thing to do. None of them is a legitimate thing to do
SILENTLY, and none of them survived the job's own lifetime, so nothing could
answer "what does this client still owe" the next morning, let alone act on
it.

THE SHAPE. One document per CELL -- (client, platform, tab, keyword type,
search term) -- which is exactly the granularity a sweep works at, so a
cell's state is written by the code that actually knows it and never
inferred. `_id` is derived from those five fields, so every write is an
idempotent upsert and re-running a client neither duplicates rows nor needs
a transaction.

Two clocks are kept per cell, and the difference between them is the whole
point: `attempted_at` moves on every attempt whatever the result, while
`covered_at` moves only when a sweep actually put the term into the
platform's search box and got an answer. A cell whose `attempted_at` is
recent and whose `covered_at` is null or old is precisely the work still
owing.

WHAT IT BUYS BEYOND THE PROOF. Because a cell knows its own state, the next
run can sweep only what is owed instead of repeating a whole client to
recover two keywords. On a 15-keyword client that is the difference between
a three-minute gap-closing pass and a forty-minute re-sweep of work already
done -- and the sessions not spent are the ones this codebase is most
careful with.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from pymongo import UpdateOne

from backend.database.connection import db
from backend.shared import resilience
from backend.shared.logging import get_logger

log = get_logger("repositories.coverage")

COVERAGE = "discovery_coverage"

# Mongo caps a bulk_write; a client with many keywords across six platforms
# and three tabs runs into the hundreds of cells, not the tens of thousands,
# but chunking keeps one enormous client from ever being the thing that
# discovers the limit.
_CHUNK = 500

# A CELL IS OWED UNTIL A SWEEP HAS ACTUALLY PUT IT INTO A SEARCH BOX.
#
# "" is a cell planned but never attempted -- the state every cell is born
# in, written up front by `plan()` so that the intent is durable BEFORE any
# sweeping starts. That ordering is deliberate: a job that dies between
# being accepted and doing anything still leaves behind a complete statement
# of what it was supposed to do.
#
# `missed` is a cell a sweep gave up on before reaching it; `broken` is one
# it reached and could not complete. Both need re-running. `truncated` does
# NOT: the term was searched and results came back, just not all of them --
# the keyword-coverage guarantee is about whether a keyword was looked for,
# and it was. `satisfied` is the clean case.
NEVER = ""
MISSED = "missed"
_OWED_OUTCOMES = (NEVER, MISSED, resilience.BROKEN)

# What `covered_at` is allowed to move on. Kept as its own tuple rather than
# "not owed" so that adding an outcome to resilience.py cannot silently
# start counting as coverage.
_COVERING_OUTCOMES = (resilience.SATISFIED, resilience.TRUNCATED)


def _now() -> datetime:
    # Naive-but-UTC, matching every other repository here -- see
    # database/connection.py's module docstring on why.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cell_id(group_id: str, platform: str, tab: str, kw_type: str, search: str) -> str:
    """The `_id` for one cell.

    Lower-cased on the two free-text parts so "Gautam Adani" and "gautam
    adani" are ONE cell: they are one search as far as any platform is
    concerned, and letting them be two rows would mean a client could show
    a keyword as owed forever because a re-save changed its capitalisation.
    The display casing is kept in `search`, which the analyst's own list
    still reads back from.
    """
    return "|".join((
        group_id, platform, tab, kw_type, (search or "").strip().lower(),
    ))


def _cell_doc(group_id: str, platform: str, tab: str, plan: Any) -> dict:
    """Identity for one cell, from a `KeywordPlan`-shaped object."""
    search = getattr(plan, "search", "") or ""
    return {
        "group_id": group_id,
        "platform": platform,
        "tab": tab,
        "kw_type": getattr(plan, "kw_type", "") or "",
        "search": search.strip(),
        # The bucket its hits file under. Carried so the gaps report can say
        # "Gautam Adani (via gautam.adani.hq)" rather than showing the
        # analyst a permutation they may not recognise on its own.
        "parent": getattr(plan, "parent", "") or search.strip(),
    }


async def plan(
    group_id: str, job_id: str, platform: str, tabs: Iterable[str], plans: Iterable[Any],
) -> int:
    """Declare every cell this job intends to sweep on one platform.

    Called BEFORE the sweeping starts, which is what makes an abandoned job
    legible: the cells exist, in state `""`, and anything that never gets
    attempted stays that way rather than never having existed. Returns how
    many cells were declared.

    `$setOnInsert` guards the history: a cell that a previous run already
    covered must not have its outcome reset to "never attempted" just
    because a new job listed it again. Only `last_planned_at`/`last_job_id`
    move on a re-plan.
    """
    tabs = list(tabs)
    ops: list[UpdateOne] = []
    now = _now()
    for p in plans:
        for tab in tabs:
            cell = _cell_doc(group_id, platform, tab, p)
            ops.append(UpdateOne(
                {"_id": cell_id(group_id, platform, tab, cell["kw_type"], cell["search"])},
                {
                    "$set": {**cell, "last_planned_at": now, "last_job_id": job_id},
                    "$setOnInsert": {
                        "outcome": NEVER,
                        "stop": "",
                        "attempts": 0,
                        "found": 0,
                        "new": 0,
                        "attempted_at": None,
                        "covered_at": None,
                        "first_planned_at": now,
                    },
                },
                upsert=True,
            ))
    return await _write(ops, f"plan {platform}")


async def record(
    group_id: str, job_id: str, platform: str, tab: str, kw_type: str, search: str,
    outcome: str, stop: str = "", found: int = 0, new: int = 0,
) -> None:
    """One sweep's verdict on one cell.

    `covered_at` moves only for an outcome that means the term genuinely
    reached the platform's search box (see `_COVERING_OUTCOMES`), which is
    what separates "we tried" from "it is done" -- `attempted_at` already
    records the former, and a cell where the two disagree is exactly the
    work still owing.
    """
    now = _now()
    fields: dict[str, Any] = {
        "outcome": outcome,
        "stop": stop,
        "found": int(found or 0),
        "new": int(new or 0),
        "attempted_at": now,
        "last_job_id": job_id,
    }
    if outcome in _COVERING_OUTCOMES:
        fields["covered_at"] = now
    await _write(
        [UpdateOne(
            {"_id": cell_id(group_id, platform, tab, kw_type, search)},
            {"$set": fields, "$inc": {"attempts": 1},
             # A cell recorded without ever having been planned (a test, a
             # hand-built job, a plan written by an older build) is still a
             # real observation and must not be dropped -- so the write
             # creates it, carrying the identity it would have had.
             "$setOnInsert": {
                 "group_id": group_id, "platform": platform, "tab": tab,
                 "kw_type": kw_type, "search": (search or "").strip(),
                 "parent": (search or "").strip(),
                 "first_planned_at": now,
             }},
            upsert=True,
        )],
        f"record {platform}/{tab}",
    )


async def miss(
    group_id: str, job_id: str, platform: str, tabs: Iterable[str],
    plans: Iterable[Any], reason: str,
) -> int:
    """Mark cells this job gave up on WITHOUT attempting, and say why.

    The counterpart to `plan`, and the reason the whole ledger earns its
    place: every early exit in `_sweep_platform` -- a spent session pool, a
    hard stop, a skipped platform, a keyword that killed every account --
    ends here, naming the exact cells it abandoned instead of leaving a
    count and a shrug.

    NEVER OVERWRITES A COVERED CELL'S HISTORY. `covered_at` is left alone,
    so a keyword swept cleanly last night and unreachable this morning
    reports as owed-again-since with its last good sweep still on record,
    rather than looking like it was never searched at all.
    """
    tabs = list(tabs)
    reason = " ".join(str(reason or "").split())[:200]
    ops: list[UpdateOne] = []
    now = _now()
    for p in plans:
        for tab in tabs:
            cell = _cell_doc(group_id, platform, tab, p)
            ops.append(UpdateOne(
                {"_id": cell_id(group_id, platform, tab, cell["kw_type"], cell["search"])},
                {
                    "$set": {
                        **cell,
                        "outcome": MISSED,
                        "stop": reason,
                        "attempted_at": now,
                        "last_job_id": job_id,
                    },
                    "$setOnInsert": {
                        "attempts": 0, "found": 0, "new": 0,
                        "covered_at": None, "first_planned_at": now,
                    },
                },
                upsert=True,
            ))
    return await _write(ops, f"miss {platform}")


async def owed(
    group_id: str, platforms: Optional[Iterable[str]] = None,
) -> list[dict]:
    """Every cell this client still owes, oldest-planned first.

    The one question the whole module exists to answer, asked the same way
    by the gaps API, by a gap-closing sweep, and by anyone auditing a
    client's coverage.
    """
    query: dict[str, Any] = {
        "group_id": group_id,
        "outcome": {"$in": list(_OWED_OUTCOMES)},
    }
    if platforms is not None:
        wanted = list(platforms)
        if not wanted:
            return []
        query["platform"] = {"$in": wanted}
    try:
        cursor = db()[COVERAGE].find(query).sort([("platform", 1), ("search", 1), ("tab", 1)])
        return [_public(doc) async for doc in cursor]
    except Exception as e:
        # A read failing must not be reported as "nothing is owed" -- that
        # is the exact false clean bill of health this module exists to
        # make impossible. Callers get the exception.
        log.error(f"coverage read failed for {group_id}: {type(e).__name__}: {e}")
        raise


async def summary(group_id: str) -> dict:
    """Counts by outcome for one client, for a badge or a health check."""
    try:
        cursor = db()[COVERAGE].aggregate([
            {"$match": {"group_id": group_id}},
            {"$group": {"_id": {"platform": "$platform", "outcome": "$outcome"},
                        "n": {"$sum": 1}}},
        ])
        by_platform: dict[str, dict[str, int]] = {}
        totals: dict[str, int] = {}
        async for row in cursor:
            pid = row["_id"]["platform"]
            outcome = row["_id"]["outcome"] or NEVER
            by_platform.setdefault(pid, {})[outcome] = row["n"]
            totals[outcome] = totals.get(outcome, 0) + row["n"]
        owed_total = sum(totals.get(o, 0) for o in _OWED_OUTCOMES)
        return {
            "group_id": group_id,
            "cells": sum(totals.values()),
            "owed": owed_total,
            "by_outcome": totals,
            "by_platform": by_platform,
        }
    except Exception as e:
        log.error(f"coverage summary failed for {group_id}: {type(e).__name__}: {e}")
        raise


async def delete_for_client(group_id: str) -> int:
    """Drop a client's ledger, for when the client itself is deleted."""
    try:
        res = await db()[COVERAGE].delete_many({"group_id": group_id})
        return int(res.deleted_count or 0)
    except Exception as e:
        log.error(f"coverage delete failed for {group_id}: {type(e).__name__}: {e}")
        return 0


def _public(doc: dict) -> dict:
    """One cell, shaped for an API response."""
    def iso(v: Any) -> Optional[str]:
        return v.replace(tzinfo=timezone.utc).isoformat() if isinstance(v, datetime) else None

    return {
        "platform": doc.get("platform", ""),
        "tab": doc.get("tab", ""),
        "kw_type": doc.get("kw_type", ""),
        "search": doc.get("search", ""),
        "parent": doc.get("parent", "") or doc.get("search", ""),
        "outcome": doc.get("outcome", NEVER) or NEVER,
        "reason": doc.get("stop", "") or "",
        "attempts": int(doc.get("attempts", 0) or 0),
        "attempted_at": iso(doc.get("attempted_at")),
        # Null here with a non-null `attempted_at` is the signature of a
        # keyword that has been tried and never once actually searched.
        "covered_at": iso(doc.get("covered_at")),
    }


async def _write(ops: list[UpdateOne], what: str) -> int:
    """Every write in this module, with one policy: bookkeeping must never
    take a sweep down.

    A Mongo blip mid-sweep has to cost the LEDGER, not the sweeping -- the
    profiles being found are the product, and losing them to a failed
    audit-trail write would be a strictly worse bug than the one this
    module fixes. Logged at error level because a ledger that is quietly
    not being written is the one failure mode that would make every
    coverage report a lie.
    """
    if not ops:
        return 0
    written = 0
    try:
        coll = db()[COVERAGE]
        for i in range(0, len(ops), _CHUNK):
            res = await coll.bulk_write(ops[i:i + _CHUNK], ordered=False)
            written += int((res.upserted_count or 0) + (res.modified_count or 0))
    except Exception as e:
        log.error(
            f"coverage {what}: {type(e).__name__}: {e} -- the sweep continues, "
            f"but {len(ops)} cell(s) are now unrecorded and this client's "
            f"coverage report will under-state what was swept")
        return 0
    return written


async def ensure_indexes() -> None:
    """Best-effort, same policy as every other repository here: an index
    that cannot be built must not stop the engine from starting.

    No unique index is needed -- `_id` IS the uniqueness constraint, by
    construction -- so the only index that carries load is the one behind
    `owed()`, which is the query a gaps report and a gap-closing sweep both
    run.
    """
    coll = db()[COVERAGE]
    for keys, name in (
        ([("group_id", 1), ("outcome", 1), ("platform", 1)], "coverage_owed"),
        ([("group_id", 1), ("platform", 1), ("search", 1)], "coverage_lookup"),
    ):
        try:
            await coll.create_index(keys, name=name)
        except Exception as e:
            log.error(f"could not build index {name!r}: {type(e).__name__}: {e}")
