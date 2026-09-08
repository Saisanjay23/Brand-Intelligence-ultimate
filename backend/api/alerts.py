"""REST API router for Alerts, Live Incidents, Email settings, and Session Canary.

Enables operators to manage alert email recipients, configure SMTP delivery,
test email dispatch, inspect live security/session incidents, and run proactive
session canary health checks.
"""

from __future__ import annotations

from typing import Any, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.database.repositories import alert_settings_repository as alert_settings_db
from backend.database.repositories import incident_repository as incidents_db
from backend.services import email_service
from backend.services import session_canary_service
from backend.shared.logging import get_logger

log = get_logger("api.alerts")

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertSettingsIn(BaseModel):
    alert_emails: list[str] = Field(default_factory=list)
    smtp_host: str = ""
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_pass: str = ""
    alert_from: str = "alerts@brand-intelligence.local"
    smtp_ssl: bool = False
    alert_on_session_dead: bool = True
    alert_on_session_expiring: bool = True
    alert_on_critical_incident: bool = True
    session_expiry_warning_hours: int = 24
    report_on_sweep_complete: bool = False
    report_emails: list[str] = Field(default_factory=list)


class TestEmailIn(BaseModel):
    to_email: Optional[str] = None


def _format_incident(doc: dict) -> dict:
    """Format MongoDB incident document with string ID and ISO timestamp."""
    out = dict(doc)
    out["id"] = str(out.pop("_id", ""))
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out



@router.get("/settings", summary="Get alert and notification settings")
async def get_alert_settings() -> dict[str, Any]:
    settings = await alert_settings_db.get_settings()
    # Mask password for safety in UI
    res = dict(settings)
    if res.get("smtp_pass"):
        res["smtp_pass"] = "••••••••"
    return res


@router.put("/settings", summary="Update alert and notification settings")
async def update_alert_settings(body: AlertSettingsIn) -> dict[str, Any]:
    """Updates only the fields the caller actually sent.

    `exclude_unset=True` is doing real work here. Every field on
    AlertSettingsIn has a default, so a plain `model_dump()` returns a
    COMPLETE settings object no matter how little the caller sent -- and
    save_settings `$set`s all of it. A request carrying just
    `{"report_on_sweep_complete": true}` therefore reset alert_emails to
    empty, smtp_host to "", the port to 1025 and every toggle to its default,
    silently and with a 200.

    The UI happens to avoid it by sending the whole object back, but that
    only moves the problem: two operators (or two tabs) both load the
    settings, one changes the recipients, the other flips a toggle, and the
    second save puts the stale recipients back. Writing only what was sent
    makes both edits survive, and makes a partial PUT mean what it says."""
    saved = await alert_settings_db.save_settings(body.model_dump(exclude_unset=True))
    res = dict(saved)
    if res.get("smtp_pass"):
        res["smtp_pass"] = "••••••••"
    return res


@router.post("/test", summary="Send a test verification email")
async def send_test_email(body: TestEmailIn) -> dict[str, Any]:
    ok, detail = await email_service.send_test_email(body.to_email)
    if not ok:
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True, "detail": detail}


@router.get("/incidents", summary="Get recent system and session incidents")
async def get_incidents(
    limit: int = Query(50, ge=1, le=200),
    severity: str = Query("", description="critical, warning, info"),
    platform: str = Query("", description="facebook, instagram, twitter, etc."),
) -> dict[str, Any]:
    raw_incidents = await incidents_db.recent(limit=limit, severity=severity, platform=platform)
    counts = await incidents_db.counts_by_severity()
    return {
        "incidents": [_format_incident(d) for d in raw_incidents],
        "counts": counts,
    }


@router.delete("/incidents/{incident_id}", summary="Dismiss a specific incident")
async def dismiss_incident(incident_id: str) -> dict[str, Any]:
    ok = await incidents_db.delete_incident(incident_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Incident not found")
    return {"ok": True}


@router.delete("/incidents", summary="Clear all stored incidents")
async def clear_all_incidents() -> dict[str, Any]:
    cleared = await incidents_db.clear_all()
    return {"ok": True, "cleared": cleared}


@router.get("/canary/status", summary="Session pool health overview")
async def get_canary_status() -> dict[str, Any]:
    """Built from stored session state on every call -- a few database reads,
    free to poll, and never a login.

    THERE IS NO LONGER A `POST /canary/run`. It ran `check_all_once()`, the
    same function the 30-minute session monitor already runs, so it only ever
    duplicated work that was happening anyway -- with a second set of
    authenticated page loads on real accounts, at an unjittered moment of
    someone's choosing. This endpoint used to trigger it too, which meant
    opening the Alerts tab logged into six platforms.

    Session state is kept current without any of that: every job updates the
    sessions it touches the moment it succeeds or fails, and the monitor
    covers the idle ones every 30 minutes. To force a check on one account,
    use the Sessions page -- `POST /sessions/{platform}/check` and
    `POST /sessions/{platform}/{session_id}/check` both refuse while a job
    holds that session, which the canary sweep never did.

    `last_run` is when the underlying checks actually happened, not when this
    was called."""
    return await session_canary_service.build_canary_report()
