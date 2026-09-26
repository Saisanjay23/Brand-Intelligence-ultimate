"""Scheduler API: when the queue runs, who is in it, and how it went.

    GET    /scheduler              the schedule, the queue, the live run
    PUT    /scheduler/schedule     set the date/time/repeat
    PUT    /scheduler/queue        set the clients and their order
    POST   /scheduler/preview      what a schedule WOULD do, without saving it
    POST   /scheduler/run          run the queue now
    POST   /scheduler/stop         stop the run in flight
    GET    /scheduler/runs         run history

THE SCHEDULE IS A WALL CLOCK PLUS A ZONE, never a UTC instant. An analyst
types "02:30" meaning where they are sitting, and that has to still mean
02:30 after the clocks change. See `backend/services/schedule_math.py` for
the whole of that reasoning -- it is the part of this feature most likely
to be quietly wrong.

THE QUEUE LIVES HERE, not in the browser. It used to be one tab's
localStorage, which meant the machine that set up the queue was the only
one that could run it, and closing that tab was enough to stop a sweep that
was supposed to happen at two in the morning.

RUNNING IS EXCLUSIVE. `POST /scheduler/run` during a run in flight is a
409, not a second run. One sweep at a time is the guarantee the whole
queue exists to provide: two at once means two clients competing for the
same platform sessions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field

from backend.config.settings import settings
from backend.database.repositories import client_repository as clients_db
from backend.database.repositories import schedule_repository as schedule_db
from backend.services import schedule_math
from backend.services.schedule_math import Schedule, ScheduleError
from backend.services.scheduler_service import scheduler_engine
from backend.shared import fast_http
from backend.shared.errors import ConflictError, ValidationError
from backend.shared.logging import get_logger

log = get_logger("api.scheduler")

router = APIRouter(prefix="/scheduler", tags=["scheduler"])

# How many upcoming firing times to show. Enough for an analyst to confirm
# at a glance that "weekly, Mon+Fri" really means what they think it means
# before they walk away for the night.
_PREVIEW_COUNT = 5


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


# ------------------------------------------------------------------ bodies

class ScheduleBody(BaseModel):
    """When the queue should run.

    `at` is a wall clock in `tz`. `on_date` applies to `once` only, and
    `weekdays` to `weekly` only; both are ignored (not rejected) for the
    other modes, so switching mode in the UI does not require clearing the
    fields the other mode used.
    """

    enabled: bool = Field(
        False,
        description="Off means nothing fires. The queue can still be run by hand.")
    mode: str = Field(
        "daily", description="'once' | 'daily' | 'weekly'.")
    at: str = Field(
        "02:00", description="Wall clock in `tz`, 24-hour, e.g. '02:30'.")
    on_date: str = Field(
        "", description="YYYY-MM-DD. Required for mode 'once', ignored otherwise.")
    weekdays: list[int] = Field(
        default_factory=list,
        description="Monday=0 ... Sunday=6. Required for mode 'weekly', "
                    "ignored otherwise.")
    tz: str = Field(
        default_factory=lambda: settings.default_timezone,
        description="IANA timezone name, e.g. 'Asia/Kolkata'. Defaults to India IST (Asia/Kolkata).")


class QueueBody(BaseModel):
    """The clients to sweep, in the order to sweep them.

    THE FULL LIST, every time. Order is the thing being stored, and an
    add/remove/move API over a shared ordered list is three ways to end up
    with an order nobody asked for.
    """

    client_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------- responses

class EntryOut(BaseModel):
    """One client's place in a run."""

    model_config = {"extra": "allow"}

    client_id: str
    name: str = ""
    status: str = "pending"
    job_id: str = ""
    message: str = ""
    found: int = 0
    new_profiles: int = 0
    platforms: dict[str, str] = Field(default_factory=dict)
    platform_details: dict[str, dict] = Field(default_factory=dict)
    owed: int = Field(
        -1,
        description="Searches this client still owes on the durable coverage "
                    "ledger after its sweep settled. -1 means NOT KNOWN (the "
                    "read failed, or it has not run) -- deliberately not 0, "
                    "which is the clean bill of health.")
    closing_gaps: bool = False
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class RunOut(BaseModel):
    model_config = {"extra": "allow"}

    run_id: str
    trigger: str = Field(
        "manual",
        description="'scheduled' (fired on time), 'catch_up' (fired late, "
                    "within the grace window), or 'manual' (somebody pressed Run).")
    status: str = Field(
        "done",
        description="running | done | cancelled | failed | interrupted | missed. "
                    "'interrupted' means the backend restarted mid-run; 'missed' "
                    "means a scheduled time went by while the backend was down "
                    "and was too late to honour.")
    message: str = ""
    entries: list[EntryOut] = Field(default_factory=list)
    current_id: str = ""
    stopping: bool = False
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    due_at: Optional[str] = None
    late_seconds: float = 0.0


class WallClockShiftOut(BaseModel):
    kind: str
    requested: str
    actual: str
    on: str
    note: str


class SchedulerStateOut(BaseModel):
    schedule: ScheduleBody
    queue: list[str] = Field(default_factory=list)
    queue_names: dict[str, str] = Field(default_factory=dict)
    running: bool = False
    next_run_at: Optional[str] = Field(
        None,
        description="When this schedule next fires, UTC. NULL means it never "
                    "does -- it is off, or it is a one-time schedule whose "
                    "moment has passed. Show that as 'not scheduled', never as "
                    "'not yet'.")
    upcoming: list[str] = Field(
        default_factory=list,
        description="The next few firing times, so a schedule can be confirmed "
                    "before it is relied on.")
    last_fired_at: Optional[str] = None
    wall_clock_shift: Optional[WallClockShiftOut] = Field(
        None,
        description="Set when the next run's local time does not exist, or "
                    "exists twice, because of a daylight-saving change.")
    catch_up_grace_minutes: int = 0
    run: Optional[RunOut] = None


class RunList(BaseModel):
    items: list[RunOut]


class RunStarted(BaseModel):
    run_id: str
    message: str


class StopResult(BaseModel):
    stopping: bool
    message: str


class PreviewOut(BaseModel):
    upcoming: list[str]
    wall_clock_shift: Optional[WallClockShiftOut] = None
    note: str = ""


# ------------------------------------------------------------- serialising

def _entry_out(raw: dict) -> dict:
    return {**raw,
            "started_at": _iso(raw.get("started_at")),
            "finished_at": _iso(raw.get("finished_at"))}


def _run_out(raw: Optional[dict]) -> Optional[dict]:
    if not raw:
        return None
    return {
        **raw,
        "entries": [_entry_out(e) for e in (raw.get("entries") or [])],
        "started_at": _iso(raw.get("started_at")),
        "finished_at": _iso(raw.get("finished_at")),
        "due_at": _iso(raw.get("due_at")),
    }


def _state_out(snap: dict, schedule: Schedule) -> dict:
    upcoming = schedule_math.next_runs(
        schedule, datetime.now(timezone.utc), _PREVIEW_COUNT)
    return {
        **snap,
        "next_run_at": _iso(snap.get("next_run_at")),
        "last_fired_at": _iso(snap.get("last_fired_at")),
        "upcoming": [_iso(u) for u in upcoming],
        "run": _run_out(snap.get("run")),
    }


class DetectedTimezoneOut(BaseModel):
    timezone: str = Field(description="Detected or fallback IANA timezone name")
    ip: str = Field("", description="Public IP or VPN egress IP")
    city: str = Field("", description="City of egress IP")
    country: str = Field("", description="Country of egress IP")
    source: str = Field("default_fallback", description="'vpn_ip_egress' | 'default_fallback'")


@router.get("/detect-timezone", response_model=DetectedTimezoneOut,
            summary="Smartly detect timezone from VPN or public IP egress")
async def detect_timezone() -> dict:
    """Inspects the machine's egress IP (e.g. through active VPN or direct connection)
    to determine the geographic timezone, falling back to India IST (Asia/Kolkata).
    """
    for endpoint in ("https://ipinfo.io/json", "https://ipapi.co/json/"):
        try:
            res = await fast_http.fetch(endpoint, timeout=3.0)
            if res.status == 200 and res.body:
                import json
                data = json.loads(res.body.decode("utf-8"))
                tz = (data.get("timezone") or "").strip()
                if tz:
                    try:
                        from zoneinfo import ZoneInfo
                        ZoneInfo(tz)
                        return {
                            "timezone": tz,
                            "ip": str(data.get("ip") or ""),
                            "city": str(data.get("city") or ""),
                            "country": str(data.get("country_name") or data.get("country") or ""),
                            "source": "vpn_ip_egress",
                        }
                    except Exception:
                        pass
        except Exception:
            continue

    return {
        "timezone": getattr(settings, "default_timezone", "Asia/Kolkata"),
        "ip": "",
        "city": "",
        "country": "",
        "source": "default_fallback",
    }


# ------------------------------------------------------------------ routes

@router.get("", response_model=SchedulerStateOut,
            summary="The schedule, the queue and the run in flight")
async def get_state() -> dict:
    """One read for the whole Scheduler page.

    Safe to poll: it reads one small document plus, while a run is going,
    the engine's own in-memory copy of it.
    """
    snap = await scheduler_engine.snapshot()
    return _state_out(snap, Schedule.from_dict(snap["schedule"]))


@router.put("/schedule", response_model=SchedulerStateOut,
            summary="Set when the queue runs")
async def set_schedule(body: ScheduleBody) -> dict:
    """422 on a schedule that could never fire -- a weekly with no days
    selected, a bad timezone, a time that is not a time.

    REFUSED RATHER THAN STORED AND IGNORED. A schedule saved successfully
    is a promise that something will happen at that time; a saved schedule
    that silently never fires is worse than no scheduler at all, because
    the analyst has already stopped thinking about it.
    """
    schedule = Schedule(
        enabled=body.enabled, mode=body.mode, at=body.at,
        on_date=body.on_date, weekdays=tuple(sorted(set(body.weekdays))),
        tz=body.tz,
    )
    try:
        schedule.validate()
    except ScheduleError as e:
        raise ValidationError(str(e)) from None

    now = datetime.now(timezone.utc)
    next_at, shift = schedule_math.next_run_after(schedule, now)

    # Enabled, valid, and yet nothing ahead of it: the only way to get here
    # is a one-time schedule set in the past. Refused, because "saved" and
    # "will never run" must not be the same outcome.
    if schedule.enabled and next_at is None:
        raise ValidationError(
            f"{body.on_date} {body.at} is in the past in {body.tz} -- that run "
            "can never happen. Pick a future date and time, or use Run now.")

    await schedule_db.save_schedule(schedule.to_dict(), next_at)
    if shift:
        log.info(f"scheduler: {shift.describe()}")
    log.info(
        f"scheduler: schedule saved -- {'on' if schedule.enabled else 'OFF'}, "
        f"{schedule.mode} at {schedule.at} {schedule.tz}; next run "
        f"{_iso(next_at) or 'never'}")

    snap = await scheduler_engine.snapshot()
    return _state_out(snap, schedule)


@router.post("/preview", response_model=PreviewOut,
             summary="What a schedule would do, without saving it")
async def preview(body: ScheduleBody) -> dict:
    """For showing the next few firing times while an analyst is still
    typing, so "weekly, Mon + Fri, 09:00" can be confirmed as real dates
    before anybody relies on it overnight."""
    schedule = Schedule(
        enabled=True,       # previewing an off schedule would answer nothing
        mode=body.mode, at=body.at, on_date=body.on_date,
        weekdays=tuple(sorted(set(body.weekdays))), tz=body.tz,
    )
    try:
        schedule.validate()
    except ScheduleError as e:
        return {"upcoming": [], "wall_clock_shift": None, "note": str(e)}

    now = datetime.now(timezone.utc)
    upcoming = schedule_math.next_runs(schedule, now, _PREVIEW_COUNT)
    _next, shift = schedule_math.next_run_after(schedule, now)
    note = ""
    if not upcoming:
        note = ("that time is already in the past -- this schedule would "
                "never run")
    elif shift:
        note = shift.describe()
    return {
        "upcoming": [_iso(u) for u in upcoming],
        "wall_clock_shift": shift.to_dict() if shift else None,
        "note": note,
    }


@router.put("/queue", response_model=SchedulerStateOut,
            summary="Set the clients to sweep, in order")
async def set_queue(body: QueueBody) -> dict:
    """409 while a run is in flight.

    Changing the list under a running sweep would mean the run's own copy
    and the stored queue disagree about what is happening, and an analyst
    reading either one would be reading something untrue.
    """
    if scheduler_engine.busy:
        raise ConflictError(
            "a run is in progress -- stop it before changing the queue")

    # One place per client, first position wins. A client listed twice would
    # be swept twice in one run -- double the session time on the same
    # accounts for results the first pass already saved.
    client_ids = list(dict.fromkeys(c.strip() for c in body.client_ids if c and c.strip()))

    # Names are snapshotted so a queued client that is later deleted still
    # has something to show besides a bare id.
    names: dict[str, str] = {}
    for cid in client_ids:
        doc = await clients_db.try_get(cid)
        if doc:
            names[cid] = doc.get("name") or cid

    await schedule_db.set_queue(client_ids, names)
    snap = await scheduler_engine.snapshot()
    return _state_out(snap, Schedule.from_dict(snap["schedule"]))


@router.post("/run", response_model=RunStarted,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Run the queue now")
async def run_now() -> dict:
    """The same code path a scheduled fire takes, labelled `manual`.

    409 when a run is already going: the second press is declined rather
    than queued, because a queued second sweep of every client, starting
    whenever the first happens to end, is never what the press meant.
    """
    if scheduler_engine.busy:
        raise ConflictError("a run is already in progress")
    run_id = await scheduler_engine.fire(trigger="manual")
    if run_id is None:
        raise ConflictError("a run is already in progress")
    return {"run_id": run_id, "message": "the queue is running"}


@router.post("/stop", response_model=StopResult, summary="Stop the run")
async def stop_run() -> dict:
    """Cancels the sweep in flight and unwinds -- immediately, rather than
    after the current client finishes. Profiles already found stay found:
    discovery writes per completed sweep, not at the end."""
    stopping = await scheduler_engine.stop()
    return {
        "stopping": stopping,
        "message": ("stopping -- cancelling the sweep in flight"
                    if stopping else "nothing is running"),
    }


@router.get("/runs", response_model=RunList, summary="Run history")
async def list_runs(
    limit: int = Query(25, ge=1, le=200),
) -> dict:
    """Newest first.

    A `missed` row here is a scheduled time that went by while the backend
    was down and was too late to honour. It is recorded deliberately: a
    sweep that did not happen has to leave a trace, or it is
    indistinguishable from a schedule nobody ever set.
    """
    runs = await schedule_db.list_runs(limit)
    return {"items": [_run_out({**r, "run_id": r["_id"]}) for r in runs]}
