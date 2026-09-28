"""Where each client's "New" tab begins: the start of its latest run.

WHY NOT A CLOCK. "New" used to mean "first seen in the last 24 hours". With a
weekly schedule that emptied itself a day after every run: an analyst who
looked on day two found nothing in New and that week's finds mixed into
thousands of Old profiles. Now New holds what the LATEST run found, for as
long as it is the latest run -- a week, a month, whatever the schedule is --
and the moment the next run finds something, last run's finds move to Old.

WHY "FINDS SOMETHING", NOT "STARTS". A run that fails at once (a logged-out
account) or finds nothing new must not empty New: last week's unreviewed
profiles would drop into Old with nothing to replace them. So the boundary
moves when a run saves its first NEW profile, and it moves to that run's
START -- every profile the run finds after that lands on the New side.

`$max`, so a slower, older job finishing late can never drag the boundary
back and resurrect the previous run's profiles as New.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.database.connection import db
from backend.shared.logging import get_logger

log = get_logger("repositories.new_window")

COLLECTION = "discovery_new_window"
PROFILES = "profiles"


def _utc(v: datetime) -> datetime:
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


async def advance(group_id: str, since: datetime) -> None:
    """A run for `group_id` that started at `since` has found a new profile."""
    if not group_id:
        return
    await db()[COLLECTION].update_one(
        {"_id": group_id},
        {"$max": {"since": _utc(since)}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


# Two finds further apart than this belong to different runs. A scheduled
# run's gap-closing pass comes round within hours of its first pass; the next
# weekly run is days away. Used only to seed clients whose runs predate this.
_SAME_RUN_GAP = timedelta(hours=12)


async def _from_history(group_id: str) -> Optional[datetime]:
    """For a client whose runs predate this module: when its latest batch of
    finds began, read off the profiles themselves -- walking back from the
    newest first_seen until a gap longer than _SAME_RUN_GAP. (Sweep telemetry
    was tried first and is not fit for this: its per-sweep `new` counts miss
    streamed saves, so runs that found hundreds of profiles record 0.)"""
    cursor = db()[PROFILES].find(
        {"client_id": group_id, "first_seen": {"$type": "date"}},
        {"first_seen": 1, "_id": 0},
    ).sort("first_seen", -1)
    earliest: Optional[datetime] = None
    async for d in cursor:
        t = _utc(d["first_seen"])
        if earliest is not None and earliest - t > _SAME_RUN_GAP:
            break
        earliest = t
    return earliest


async def since(group_id: str) -> Optional[datetime]:
    """The instant this client's New tab begins, or None when it has never
    had a run that found anything (the caller then keeps the 24h rule)."""
    if not group_id:
        return None
    try:
        doc = await db()[COLLECTION].find_one({"_id": group_id}, {"since": 1})
        if doc and isinstance(doc.get("since"), datetime):
            return _utc(doc["since"])
        backfilled = await _from_history(group_id)
        if backfilled is not None:
            await advance(group_id, backfilled)
        return backfilled
    except Exception as e:                              # noqa: BLE001
        # Never break the grid over this: the 24h rule still answers.
        log.warning(f"new-window lookup failed for {group_id!r}: {type(e).__name__}: {e}")
        return None
