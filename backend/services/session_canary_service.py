"""Proactive Session Canary & Expiration Watchdog service.

Performs background canary health checks, detects approaching cookie expirations
(< 24 hours), records incidents, and triggers email notifications before sweeps fail.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from backend.database.repositories import alert_settings_repository as alert_settings_db
from backend.database.repositories import incident_repository as incidents_db
from backend.database.repositories import session_repository as sessions_db
from backend.services import email_service
from backend.shared.logging import get_logger

log = get_logger("services.session_canary")

# Key authentication cookies per platform whose expiry signals imminent session death
CRITICAL_COOKIES: dict[str, tuple[str, ...]] = {
    "facebook": ("xs", "c_user", "datr"),
    "instagram": ("sessionid", "ds_user_id"),
    "twitter": ("auth_token", "ct0"),
    "tiktok": ("sessionid", "sessionid_ss", "sid_tt"),
}

# Cache of recently alerted expirations (key -> epoch timestamp) to prevent spamming
_recent_alerts: dict[str, float] = {}
_ALERT_COOLDOWN_SEC = 12 * 3600  # Alert at most once every 12h per session for impending expiration

_last_canary_report: dict[str, Any] = {
    "last_run": None,
    "overall_healthy": True,
    "platforms": {},
    "warnings": [],
    "errors": [],
}


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


async def check_token_expiries() -> list[dict[str, Any]]:
    """Inspects all stored sessions across platforms for upcoming cookie expirations."""
    settings = await alert_settings_db.get_settings()
    threshold_hours = float(settings.get("session_expiry_warning_hours", 24))
    now = _now()
    warnings = []

    for platform_id, key_cookies in CRITICAL_COOKIES.items():
        try:
            items = await sessions_db.list_pool(platform_id)
        except Exception as e:
            log.warning(f"canary: could not list pool for {platform_id}: {e}")
            continue

        for item in items:
            session_id = item.get("id") or ""
            identifier = item.get("identifier") or session_id
            status = item.get("status") or "ready"
            if status in ("expired", "checkpointed", "unreadable"):
                continue  # already dead, handled by failure monitoring

            cookies = item.get("cookies") or []
            earliest_expiry: Optional[float] = None

            for c in cookies:
                if c.get("name") in key_cookies:
                    exp = c.get("expires")
                    if isinstance(exp, (int, float)) and exp > now:
                        earliest_expiry = exp if earliest_expiry is None else min(earliest_expiry, exp)

            if earliest_expiry is not None:
                remaining_hours = (earliest_expiry - now) / 3600.0
                if 0 < remaining_hours <= threshold_hours:
                    warn_info = {
                        "platform": platform_id,
                        "session_id": session_id,
                        "identifier": identifier,
                        "remaining_hours": round(remaining_hours, 1),
                    }
                    warnings.append(warn_info)

                    # Deduplicate notification so we don't spam on every monitor pass
                    dedup_key = f"{platform_id}:{session_id}:expiring"
                    last_alert = _recent_alerts.get(dedup_key, 0.0)
                    if (now - last_alert) >= _ALERT_COOLDOWN_SEC:
                        _recent_alerts[dedup_key] = now
                        log.warning(
                            f"canary: {platform_id} session '{identifier}' expires in ~{remaining_hours:.1f}h"
                        )
                        # Record incident
                        await incidents_db.record({
                            "platform": platform_id,
                            "kind": "session_canary",
                            "scope": "-- all clients --",
                            "job_id": "session-canary-expiring",
                            "error_type": "SessionExpiringSoon",
                            "severity": "warning",
                            "message": (
                                f"Authentication cookie for {platform_id.capitalize()} account '{identifier}' "
                                f"expires in approximately {remaining_hours:.1f} hours. "
                                "Re-export fresh cookies to prevent automated sweep disruption."
                            ),
                            "cause": "Platform session cookie lifetime expiration.",
                            "fix": f"Export fresh cookies from your browser for {identifier} and update under Sessions.",
                            "ts": datetime.now(timezone.utc),
                        })
                        # Dispatch email alert asynchronously
                        asyncio.create_task(
                            email_service.send_session_expiring_alert(
                                platform=platform_id,
                                identifier=identifier,
                                hours_remaining=remaining_hours,
                            )
                        )

    return warnings


async def run_canary_sweep() -> dict[str, Any]:
    """Runs a complete proactive session canary check across all platforms and returns a report."""
    global _last_canary_report
    from backend.platforms import registry
    from backend.sessions import manager as sessions_engine

    expiry_warnings = await check_token_expiries()

    # Run check_all_once across monitored platforms
    try:
        check_results = await sessions_engine.check_all_once()
    except Exception as e:
        log.error(f"canary sweep check_all_once failed: {e}")
        check_results = {}

    platform_summaries: dict[str, Any] = {}
    errors: list[str] = []
    warnings_list: list[str] = []

    for plat_id in registry.PLATFORMS:
        summary = await sessions_engine.pool_summary(plat_id)
        plat_check = check_results.get(plat_id, {})
        plat_warnings = [w for w in expiry_warnings if w["platform"] == plat_id]

        status = "healthy"
        if summary["total"] == 0:
            status = "unconfigured"
        elif summary["available"] == 0 and summary["total"] > 0:
            status = "error"
            errors.append(f"{plat_id.capitalize()}: All {summary['total']} pooled sessions are unavailable")
        elif plat_warnings:
            status = "warning"
            for pw in plat_warnings:
                warnings_list.append(
                    f"{plat_id.capitalize()} ({pw['identifier']}): Expires in ~{pw['remaining_hours']}h"
                )

        platform_summaries[plat_id] = {
            "platform": plat_id,
            "status": status,
            "total": summary["total"],
            "available": summary["available"],
            "dead": summary["dead"],
            "warnings": plat_warnings,
            "check_details": plat_check,
        }

    overall_healthy = len(errors) == 0

    report = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "overall_healthy": overall_healthy,
        "platforms": platform_summaries,
        "warnings": warnings_list,
        "errors": errors,
    }
    _last_canary_report = report
    return report


def get_latest_canary_report() -> dict[str, Any]:
    """Returns the cached latest canary status overview."""
    return _last_canary_report
