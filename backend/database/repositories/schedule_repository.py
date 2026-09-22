"""The Scheduler's durable memory: one schedule, one queue, and every run.

Two collections.

`scheduler` holds a SINGLE document (`_id: "default"`) -- the schedule an
analyst set, the ordered queue of clients it sweeps, and a pointer to the
run happening right now. One document because there is one Scheduler: the
whole guarantee it offers is that clients are swept ONE AT A TIME, and a
second concurrent schedule would be the first thing to break it.

`scheduler_runs` holds one document per run, written as the run happens
rather than at the end.

WHY THE RUN IS WRITTEN AS IT GOES, NOT AT THE END. A run takes minutes per
client and a queue of ten can take an hour. Writing only on completion
means a backend restarted at minute fifty leaves no record that anything
ever ran -- the analyst arrives to an empty history and a queue that looks
untouched, with no way to tell "it never started" from "it got nine of ten
clients done and then the machine rebooted". Those are completely different
situations and the tool has to be able to tell them apart. So every entry
transition is flushed, and a run interrupted by a restart is reconciled on
the way back up (see `reconcile_interrupted`) into a run that SAYS it was
interrupted.

A MISSED RUN IS ALSO A RUN. When a firing time goes by while the backend is
down and comes back too late to honour it, that gets a `missed` document
here with the reason. A schedule that did not fire and left nothing behind
is the exact failure this feature exists to be immune to -- "nothing in the
history" must never be the record of a missed sweep.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from backend.database.connection import db
from backend.shared.logging import get_logger

log = get_logger("repositories.schedule")

SCHEDULE = "scheduler"
RUNS = "scheduler_runs"
DOC_ID = "default"

# Run statuses. `interrupted` is the one that only ever appears after a
# restart caught a run mid-flight -- see `reconcile_interrupted`.
RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_CANCELLED = "cancelled"
RUN_FAILED = "failed"
RUN_INTERRUPTED = "interrupted"
RUN_MISSED = "missed"
RUN_TERMINAL = frozenset({RUN_DONE, RUN_CANCELLED, RUN_FAILED,
                          RUN_INTERRUPTED, RUN_MISSED})

# How many runs to keep when the history is trimmed. Generous: these are
# small documents and an analyst asking "did last Tuesday's sweep run?"
# is the entire point of keeping them.
_HISTORY_KEEP = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: Any) -> Optional[datetime]:
    """Mongo hands back naive datetimes (it stores UTC without the tzinfo).
    Everything above this layer does timezone-aware arithmetic, so a naive
    one read back would silently compare as if it were local time -- which
    on a machine at UTC+5:30 means a schedule firing five and a half hours
    off with nothing to show for it."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _default_doc() -> dict:
    return {
        "_id": DOC_ID,
        # The schedule, in `schedule_math.Schedule`'s own shape.
        "schedule": {
            "enabled": False,
            "mode": "daily",
            "at": "02:00",
            "on_date": "",
            "weekdays": [],
            "tz": "UTC",
        },
        # Ordered client ids. Order IS the sweep order.
        "queue": [],
        # client_id -> last known display name, so a queue entry for a
        # client that has since been deleted still has something to show
        # besides a bare id.
        "queue_names": {},
        # The run in flight, by id. "" when idle.
        "active_run_id": "",
        # Computed from `schedule` and rewritten on every change and after
        # every fire. Stored rather than derived on read so that a restart
        # can tell a firing time it MISSED from one that is still ahead.
        "next_run_at": None,
        "last_fired_at": None,
        "created_at": _now(),
        "updated_at": _now(),
    }


async def ensure_indexes() -> None:
    """Indexes for the history reads the panel does.

    The singleton document needs none -- it is fetched by `_id`.
    """
    await db()[RUNS].create_index([("started_at", -1)])
    await db()[RUNS].create_index([("status", 1), ("started_at", -1)])


# ------------------------------------------------------------ the singleton

async def get_state() -> dict:
    """The schedule, the queue and the active run pointer.

    Never raises for a missing document: a Scheduler that has never been
    configured reads back as the defaults, which is a real answer ("nothing
    is scheduled") rather than an error the panel has to special-case.
    """
    doc = await db()[SCHEDULE].find_one({"_id": DOC_ID})
    if not doc:
        return _default_doc()
    base = _default_doc()
    base.update(doc)
    base["schedule"] = {**base["schedule"], **(doc.get("schedule") or {})}
    base["next_run_at"] = _utc(base.get("next_run_at"))
    base["last_fired_at"] = _utc(base.get("last_fired_at"))
    return base


async def _patch(fields: dict) -> dict:
    fields = {**fields, "updated_at": _now()}
    await db()[SCHEDULE].update_one(
        {"_id": DOC_ID}, {"$set": fields, "$setOnInsert": {"created_at": _now()}},
        upsert=True)
    return await get_state()


async def save_schedule(schedule: dict, next_run_at: Optional[datetime]) -> dict:
    """Store the schedule and the instant it next fires.

    The two are written TOGETHER, always. `next_run_at` is derived from
    `schedule`, and a window where the stored pair disagree is a window
    where the tick loop fires on the old time or, worse, on a time that no
    longer means anything.
    """
    return await _patch({"schedule": schedule, "next_run_at": next_run_at})


async def save_next_run(next_run_at: Optional[datetime]) -> dict:
    return await _patch({"next_run_at": next_run_at})


async def mark_fired(fired_at: datetime, next_run_at: Optional[datetime]) -> dict:
    """Record that the schedule fired, and when it fires next.

    WRITTEN BEFORE THE RUN STARTS, not after it finishes. A crash one
    second into an hour-long run must not leave `next_run_at` still in the
    past, or the tick loop restarts the same sweep on every pass -- a loop
    that drives real logged-in accounts at full speed until someone
    notices.
    """
    return await _patch({"last_fired_at": fired_at, "next_run_at": next_run_at})


async def set_queue(client_ids: list[str], names: dict[str, str]) -> dict:
    """Replace the queue wholesale, in the given order.

    Wholesale rather than per-entry because ORDER is the thing being
    stored, and an add/remove/reorder API over a shared list is three ways
    to end up with an order nobody asked for.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for cid in client_ids:
        cid = (cid or "").strip()
        # A client queued twice would be swept twice in one run, competing
        # with itself for the same platform sessions.
        if not cid or cid in seen:
            continue
        seen.add(cid)
        ordered.append(cid)
    return await _patch({
        "queue": ordered,
        "queue_names": {k: v for k, v in (names or {}).items() if k in seen},
    })


async def set_active_run(run_id: str) -> dict:
    return await _patch({"active_run_id": run_id or ""})


# ------------------------------------------------------------------- runs

def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


async def create_run(
    *, run_id: str, trigger: str, entries: list[dict],
    due_at: Optional[datetime] = None, late_seconds: float = 0.0,
    status: str = RUN_RUNNING, message: str = "",
) -> dict:
    """Open a run document.

    `trigger` is `scheduled`, `catch_up` or `manual`, and it is kept for
    the life of the record: "did last night's sweep run, or did somebody
    press the button at nine this morning?" is a question the history has
    to be able to answer on its own.
    """
    doc = {
        "_id": run_id,
        "trigger": trigger,
        "due_at": due_at,
        "late_seconds": round(float(late_seconds), 1),
        "status": status,
        "message": message,
        "entries": entries,
        "current_id": "",
        "stopping": False,
        "started_at": _now(),
        "finished_at": _now() if status in RUN_TERMINAL else None,
    }
    await db()[RUNS].insert_one(doc)
    return doc


async def record_missed(
    *, due_at: datetime, late_seconds: float, reason: str,
) -> dict:
    """A firing time that went by unhonoured, written down.

    See the module docstring: this exists so that a sweep which did not
    happen is a VISIBLE record rather than an absence, because an absence
    is indistinguishable from a schedule nobody ever set.
    """
    return await create_run(
        run_id=new_run_id(), trigger="scheduled", entries=[],
        due_at=due_at, late_seconds=late_seconds,
        status=RUN_MISSED, message=reason)


async def get_run(run_id: str) -> Optional[dict]:
    if not run_id:
        return None
    doc = await db()[RUNS].find_one({"_id": run_id})
    if doc:
        doc["due_at"] = _utc(doc.get("due_at"))
        doc["started_at"] = _utc(doc.get("started_at"))
        doc["finished_at"] = _utc(doc.get("finished_at"))
    return doc


async def save_run(run_id: str, fields: dict) -> None:
    """Flush part of a run.

    Best effort BY DESIGN: a Mongo blip must not kill a sweep that is
    otherwise going fine. The run keeps its authoritative state in memory
    while it is alive (see `scheduler_service`); this is the copy that
    survives the process, and losing a tick of it costs history, not work.
    """
    try:
        await db()[RUNS].update_one({"_id": run_id}, {"$set": fields})
    except Exception as e:                                  # noqa: BLE001
        log.warning(f"scheduler: could not flush run {run_id}: {type(e).__name__}: {e}")


async def finish_run(run_id: str, *, status: str, message: str) -> None:
    await save_run(run_id, {
        "status": status, "message": message, "current_id": "",
        "stopping": False, "finished_at": _now(),
    })


async def list_runs(limit: int = 25) -> list[dict]:
    cursor = db()[RUNS].find().sort("started_at", -1).limit(max(1, min(limit, 200)))
    out = []
    async for doc in cursor:
        doc["due_at"] = _utc(doc.get("due_at"))
        doc["started_at"] = _utc(doc.get("started_at"))
        doc["finished_at"] = _utc(doc.get("finished_at"))
        out.append(doc)
    return out


async def reconcile_interrupted() -> list[str]:
    """Close out runs the process died in the middle of.

    Called once at startup. A run still marked `running` in the database
    when nothing is running cannot be resumed: the discovery jobs it was
    driving lived in the previous process's memory and went with it. What
    it CAN be is honest -- `interrupted`, with its per-client entries
    frozen as they were, so the history shows six clients done and four
    never reached rather than a run that appears to still be going.

    A `running` entry inside such a run is rewritten to `interrupted` for
    the same reason: it says what is true. The sweep that entry had in
    flight may well have been most of the way through and its profiles are
    already written (discovery saves per completed sweep), so this is not
    a claim that the work was lost -- only that this process stopped
    watching it.
    """
    stale = []
    async for doc in db()[RUNS].find({"status": RUN_RUNNING}):
        stale.append(doc)
    if not stale:
        return []

    ids = []
    for doc in stale:
        entries = doc.get("entries") or []
        for e in entries:
            if e.get("status") == "running":
                e["status"] = "interrupted"
                e["message"] = (
                    "the backend restarted while this client was being swept -- "
                    "anything already found was saved, but this run stopped "
                    "watching it")
                e["finished_at"] = _now()
        await db()[RUNS].update_one({"_id": doc["_id"]}, {"$set": {
            "status": RUN_INTERRUPTED,
            "message": "the backend restarted while this run was in progress",
            "entries": entries,
            "current_id": "",
            "stopping": False,
            "finished_at": _now(),
        }})
        ids.append(doc["_id"])

    await _patch({"active_run_id": ""})
    log.warning(
        f"scheduler: {len(ids)} run(s) were interrupted by a restart and have been "
        f"closed out as such: {', '.join(ids)}")
    return ids


async def trim_history(keep: int = _HISTORY_KEEP) -> int:
    """Drop all but the most recent `keep` runs. Never touches a run that
    is still open."""
    try:
        cursor = db()[RUNS].find({"status": {"$in": list(RUN_TERMINAL)}},
                                 {"_id": 1}).sort("started_at", -1).skip(keep)
        doomed = [d["_id"] async for d in cursor]
        if not doomed:
            return 0
        res = await db()[RUNS].delete_many({"_id": {"$in": doomed}})
        return res.deleted_count
    except Exception as e:                                  # noqa: BLE001
        log.warning(f"scheduler: history trim failed: {type(e).__name__}: {e}")
        return 0
