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
from backend.shared.tasks import spawn
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

# No cached report and no lock any more. Both existed to manage a sweep that
# opened browsers; the report is now four database reads, so building it per
# request is cheaper than the bookkeeping needed to avoid doing so -- and a
# cache cannot go stale if there isn't one.


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
                        spawn(
                            email_service.send_session_expiring_alert(
                                platform=platform_id,
                                identifier=identifier,
                                hours_remaining=remaining_hours,
                            )
                        )

    return warnings


# How many pooled sessions one liveness pass will probe at once. Small on
# purpose: these are real accounts on real platforms, and the value here is
# in noticing a death early, not in finishing the sweep quickly.
_PROBE_CONCURRENCY = 4


async def probe_pool_liveness(platform_ids: Optional[list[str]] = None) -> dict[str, Any]:
    """A ZERO-BROWSER liveness pass over the pooled cookie sessions.

    WHAT THIS ADDS THAT `check_token_expiries` DOES NOT. That function reads
    stored `expires` timestamps, which catches a session whose cookies are
    about to run out on schedule. It cannot catch the other half: a session
    the platform killed early -- a password change, a security checkpoint, a
    rotation on their side -- whose cookies still have a month of nominal
    life left. That one sits in the pool looking perfectly healthy until a
    sweep tries to use it, which is the failure this service exists to get
    in front of.

    WHY THIS DOES NOT BREAK `build_canary_report`'S RULE. That function's
    docstring is emphatic that nothing in this module logs in, and the
    reason it gives is exact: it used to call `check_all_once()` off a GET
    the Alerts panel fires on mount, so merely opening a tab started
    authenticated page loads across six platforms. Nothing here does that.
    This opens no browser and holds no session; it sends one ordinary HTTPS
    request per account through shared/fast_http.py, on the two platforms
    whose logged-out state is a server redirect, and it is NOT wired into
    `build_canary_report` -- it is a function a scheduler or an operator
    calls deliberately.

    WHAT IT DOES WITH A "DEAD" ANSWER: escalates, never decides. A dead
    verdict over plain HTTP is indistinguishable from a bot wall or a rate
    limit (see fast_http.SessionVerdict), so this hands the account to
    `sessions.manager.check_item`, the authoritative browser check, which
    records the verdict and raises the incident if it agrees. A live answer
    is left alone entirely -- this pass is an early-warning trigger, not a
    health ledger, and it must not be able to reset another path's
    consecutive-failure ladder.
    """
    from backend.sessions import manager as sessions_engine
    from backend.shared import fast_http

    probed: list[dict[str, Any]] = []
    escalated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    wanted = platform_ids or [p for p in CRITICAL_COOKIES if fast_http.probe_for(p)]
    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def one(platform_id: str, item: dict) -> None:
        session_id = item.get("id") or ""
        identifier = item.get("identifier") or session_id
        async with sem:
            try:
                verdict = await fast_http.check_session_alive(
                    platform_id, item.get("cookies") or [])
            except Exception as e:                    # noqa: BLE001 - never fatal
                skipped.append({"platform": platform_id, "identifier": identifier,
                                "reason": f"{type(e).__name__}: {e}"})
                return

            entry = {"platform": platform_id, "session_id": session_id,
                     "identifier": identifier, "alive": verdict.alive,
                     "reason": verdict.reason}
            probed.append(entry)

            if verdict.alive is not False:
                # Live, or unprovable. Either way there is nothing to do:
                # the 30-minute monitor owns the health record.
                return

            log.warning(
                f"canary: {platform_id} session '{identifier}' failed its HTTP "
                f"liveness probe ({verdict.reason}) -- escalating to the browser check")
            try:
                result = await sessions_engine.check_item(platform_id, session_id)
            except Exception as e:                    # noqa: BLE001 - never fatal
                # In use by a running job (ConflictError), deleted between
                # the list and the check (NotFoundError), or the browser
                # itself failed. None of those are this function's to
                # resolve, and none of them are a verdict.
                skipped.append({"platform": platform_id, "identifier": identifier,
                                "reason": f"escalation did not run: {type(e).__name__}: {e}"})
                return
            escalated.append({"platform": platform_id, "identifier": identifier,
                              "confirmed_dead": not result.get("ok", True),
                              "detail": result.get("detail", "")})

    jobs = []
    for platform_id in wanted:
        if not fast_http.probe_for(platform_id):
            skipped.append({"platform": platform_id,
                            "reason": "no HTTP-provable session signal on this platform"})
            continue
        try:
            items = await sessions_db.list_pool(platform_id)
        except Exception as e:                        # noqa: BLE001
            log.warning(f"canary: could not list pool for {platform_id}: {e}")
            skipped.append({"platform": platform_id, "reason": f"pool unreadable: {e}"})
            continue
        for item in items:
            if (item.get("status") or "ready") in ("expired", "checkpointed", "unreadable"):
                continue        # already dead, handled by failure monitoring
            if not item.get("cookies"):
                continue        # nothing to probe with
            if sessions_engine._session_in_use(platform_id, item.get("id") or ""):
                # The same rule the browser sweep follows: the account being
                # scraped this second is off limits. One extra request is far
                # lighter than a second browser, but it is still traffic on an
                # account that is already busy, and it could only escalate to a
                # check `check_item` would refuse anyway.
                continue
            jobs.append(one(platform_id, item))

    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "probed": probed,
        "escalated": escalated,
        "skipped": skipped,
    }


async def build_canary_report() -> dict[str, Any]:
    """The session-pool health overview -- BUILT ENTIRELY FROM STORED STATE.

    NOTHING HERE LOGS IN. That is the whole design, and it is not a
    compromise: every live check this used to perform is already being done,
    continuously, by paths that own it properly.

        rotation / use counts   `get_healthy_session`, as each job takes a
                                session out of the pool
        rotated cookies         `sync_cookies` -> `refresh_cookies`, on every
                                session stop and at four explicit points in
                                the two runners
        dead / OK marking       `mark_session_failed` / `mark_session_ok`,
                                from eight call sites across the runners --
                                on EVERY job success and failure, immediately
        idle sessions           `sessions/manager.py::_monitor_loop`, every
                                30 minutes, jittered

    So a session a job has touched is current to the second, and an idle one
    is current to within half an hour. This function reads that and presents
    it; it has nothing to add by logging in again.

    WHY IT USED TO, AND WHY THAT WAS WRONG. It called `check_all_once()` --
    the exact function the 30-minute monitor calls -- so every invocation
    duplicated the monitor's work with a second set of authenticated page
    loads on real accounts. Worse, it hung off a GET that the Alerts panel
    calls on mount, so merely OPENING that tab started logins across six
    platforms, and two tabs started two sets concurrently. It also defeated
    the monitor's jitter, which exists precisely so probes do not land on a
    predictable schedule.

    An operator who wants a live check on demand has better tools already:
    per-platform and per-session "check now" on the Sessions page, both of
    which refuse while a job holds that session -- a guard this never had.
    """
    from backend.platforms import registry
    from backend.sessions import manager as sessions_engine

    expiry_warnings = await check_token_expiries()

    # What the monitor last recorded, per platform -- a database read, not a
    # login. This is the same verdict `check_all_once` would produce, already
    # computed and stored by whichever path last touched each session.
    try:
        check_results = await sessions_db.cached_health()
    except Exception as e:
        log.error(f"canary: could not read stored session health: {e}")
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

    # WHEN THE UNDERLYING DATA WAS ACTUALLY GATHERED -- the newest stored
    # check, not "now". Stamping the read time would make the page claim a
    # verification happened the instant it was opened, which is the same lie
    # this function was rewritten to stop telling. Null means nothing has
    # checked yet (a process that has not reached its first monitor sweep).
    checked = [v.get("checked_at") for v in check_results.values() if v.get("checked_at")]
    newest = max(checked) if checked else None
    if newest is not None and newest.tzinfo is None:
        # Motor hands datetimes back naive-but-UTC; stamp it so the browser
        # does not read the ISO string as local time.
        newest = newest.replace(tzinfo=timezone.utc)

    return {
        "last_run": newest.isoformat() if newest else None,
        "overall_healthy": overall_healthy,
        "platforms": platform_summaries,
        "warnings": warnings_list,
        "errors": errors,
    }
