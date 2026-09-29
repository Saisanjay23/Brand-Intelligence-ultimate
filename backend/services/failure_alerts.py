"""Failure reports: what broke, on which platform, where in the code, and how
to fix it -- mailed at the end of a discovery job, an analysis job, or a
Scheduler run.

WHAT THIS ADDS TO WHAT ALREADY MAILS. Session failures and the platform-wide
parser-drift canary (engine_health_service) already send email, but both are
about the POOL or the PLATFORM over a day of evidence. Nothing told anyone
that tonight's sweep of one client lost eleven keywords on X to a stall, or
that twenty analysed URLs came back without a last-post date. Those are the
failures an analyst acts on the next morning, and they were only visible to
someone who opened the job and read every row.

ONE EMAIL PER JOB (or per Scheduler run), NEVER PER FAILURE. Findings are
grouped by (platform, what happened, where in the code), so forty keywords
that all stalled at the same line are one finding listing forty keywords --
which is also how an engineer wants to read it: one cause, one fix.

WHEN IT SENDS
  discovery -- at least one CRITICAL finding: a platform not searched, or a
               sweep that BROKE. A sweep that ran out of its time budget, or
               an extraction that fell back to its backup path, rides along
               as a warning but never triggers a mail on its own.
  analysis  -- any finding at all, because every one of them is data the
               analyst asked for and did not get: a URL that could not be
               read, or one that came back missing a field the platform does
               publish (shared/completeness.py decides which).
  scheduler -- a run that failed or was missed, or any client in it that
               failed, was skipped, still owes searches, or had findings.

NOTHING HERE MAY BREAK A JOB. Every entry point catches everything and logs
it: a failure report that fails must cost the report, never the sweep.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from backend.database.repositories import alert_settings_repository as alert_settings_db
from backend.shared import diagnostics, resilience
from backend.shared.logging import get_logger

log = get_logger("services.failure_alerts")

CRITICAL = "critical"
WARNING = "warning"

# Listing caps, so a thousand-URL job produces a readable email rather than
# one an SMTP server refuses. The counts are always exact; only the lists
# are shortened, and the email says by how much.
_MAX_FINDINGS = 30
_MAX_SUBJECTS = 15


def _display(platform: str) -> str:
    try:
        from backend.platforms import registry
        return registry.display_name(platform)
    except Exception:                                   # noqa: BLE001
        return platform.capitalize()


def _finding(*, severity: str, platform: str, title: str, subject_label: str,
             subjects: list[str], error: str, where: str, cause: str, fix: str,
             scope: str = "") -> dict[str, Any]:
    return {
        "severity": severity, "platform": platform, "platform_name": _display(platform),
        "title": title, "subject_label": subject_label, "count": len(subjects),
        "subjects": subjects, "error": error, "where": where,
        "cause": cause, "fix": fix, "scope": scope,
    }


def _ordered(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda f: (f["severity"] != CRITICAL, -f["count"]))


# ------------------------------------------------------------- discovery

def discovery_findings(job: Any) -> list[dict[str, Any]]:
    """Everything that went wrong in one discovery job, grouped.

    Read entirely from the job's own record -- `platforms` for what never
    ran, `history` for every sweep that did -- so it describes exactly what
    the job reports, and costs no database read.
    """
    out: list[dict[str, Any]] = []
    history = list(getattr(job, "history", []) or [])
    searches = [getattr(p, "search", str(p)) for p in (getattr(job, "keyword_plan", []) or [])]

    # A PLATFORM THAT NEVER RAN, OR FELL OVER BEFORE ITS SWEEPS. Its
    # keywords are all missed, and the sweep history is empty for it, so
    # this is the only place it can surface.
    for pid, prog in (getattr(job, "platforms", {}) or {}).items():
        swept_here = any(h.platform == pid for h in history)
        if prog.status == "skipped":
            out.append(_finding(
                severity=CRITICAL, platform=pid,
                title="Platform was not searched at all",
                subject_label="Keywords not searched", subjects=searches,
                error=prog.note or "skipped",
                where=diagnostics.locate("backend/discovery/runner.py",
                                         r"async def platform_readiness\("),
                cause=("No usable account/API key was available for this platform when "
                       "the job started, so every keyword was skipped on it. They are "
                       "recorded as still owed."),
                fix=("Open Admin -> Sessions and restore this platform's pool (fresh "
                     "cookies, or the API key for YouTube/Telegram). The next scheduled "
                     "run's gap-closing lap will search the owed keywords.")))
        elif prog.status == "failed" and not swept_here:
            out.append(_finding(
                severity=CRITICAL, platform=pid,
                title="Platform failed before any keyword was searched",
                subject_label="Keywords not searched", subjects=searches,
                error=prog.note or "failed",
                where=diagnostics.locate("backend/discovery/runner.py",
                                         r"async def _claim_sessions\("),
                cause=("Every account for this platform was unusable (expired, "
                       "checkpointed, cooling off or busy) or failed its login check."),
                fix=("Check Admin -> Sessions for this platform. The error above is the "
                     "pool's own message; add or refresh accounts, then re-run.")))

    # SWEEPS THAT DID NOT GO CLEANLY, grouped by platform + stop + location.
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for h in history:
        outcome = getattr(h, "outcome", resilience.SATISFIED)
        stopped = (getattr(h, "stopped", "") or "").strip()
        if outcome == resilience.SATISFIED or stopped == "cancelled":
            continue
        where = getattr(h, "where", "") or diagnostics.stop_site(h.platform, stopped)
        groups.setdefault((h.platform, stopped, where, outcome), []).append(h)

    for (pid, stopped, where, outcome), sweeps in groups.items():
        advice = diagnostics.stop_advice(stopped)
        severity = CRITICAL if outcome == resilience.BROKEN else WARNING
        if advice.severity == WARNING:
            severity = WARNING
        error = next((s.error for s in sweeps if getattr(s, "error", "")), "")
        out.append(_finding(
            severity=severity, platform=pid, title=advice.title,
            subject_label="Keywords affected",
            subjects=[f"{s.keyword} [{s.tab}]" for s in sweeps],
            error=error or f"stop code: {stopped or 'none'}",
            where=where, cause=advice.cause, fix=advice.fix))

    # STILL WORKING, ON THE BACKUP PATH -- the early warning before a
    # parser breaks outright. Never a trigger on its own.
    fallback: "OrderedDict[str, list]" = OrderedDict()
    for h in history:
        if (getattr(h, "source", "") or "") == "dom" and h.hits_found:
            fallback.setdefault(h.platform, []).append(h)
    for pid, sweeps in fallback.items():
        out.append(_finding(
            severity=WARNING, platform=pid,
            title="Results came from the backup (page-scraping) parser",
            subject_label="Keywords affected",
            subjects=[f"{s.keyword} [{s.tab}]" for s in sweeps],
            error="primary network-payload parser returned nothing; DOM fallback used",
            where=diagnostics.locate(diagnostics.discovery_file(pid), r"run_strategies\("),
            cause=("The platform's own search payload stopped matching our parser, so "
                   "results were read from the rendered page instead. The backup path "
                   "returns fewer fields and is one layout change from failing too."),
            fix=("Search the backend log for 'recovered on fallback' on this platform: "
                 "it names the failed strategy with its file and line. Capture a live "
                 "search response and update the primary parser.")))
    return _ordered(out)


# -------------------------------------------------------------- analysis

def analysis_findings(job: Any) -> list[dict[str, Any]]:
    """Every URL that failed or came back incomplete, grouped by platform,
    what went wrong, and where."""
    out: list[dict[str, Any]] = []
    failed: "OrderedDict[tuple, list]" = OrderedDict()
    missing: "OrderedDict[tuple, list]" = OrderedDict()

    for it in getattr(job, "items", []) or []:
        row_status = (getattr(it, "row_status", "") or "").upper()
        if row_status == "GONE":
            continue            # a finding about the profile, not a failure
        if it.status == "error":
            status = row_status if row_status in ("CHECKPOINT", "LOGIN_REQUIRED") else "ERROR"
            where = getattr(it, "where", "") or diagnostics.locate(
                diagnostics.analysis_file(it.platform), r"    async def process\(")
            failed.setdefault((it.platform, status, where), []).append(it)
        elif it.status == "done":
            if row_status == "PARTIAL":
                failed.setdefault((it.platform, "PARTIAL", diagnostics.locate(
                    diagnostics.analysis_file(it.platform), r"    async def process\(")),
                    []).append(it)
            for f in getattr(it, "missed", []) or []:
                missing.setdefault((it.platform, f), []).append(it)

    for (pid, status, where), items in failed.items():
        advice = diagnostics.status_advice(status)
        out.append(_finding(
            severity=advice.severity if advice else CRITICAL, platform=pid,
            title=advice.title if advice else f"{status} while reading profiles",
            subject_label="URLs affected", subjects=[i.url for i in items],
            error=next((i.error or i.comments for i in items if (i.error or i.comments)), status),
            where=where, cause=advice.cause if advice else "",
            fix=advice.fix if advice else ""))

    for (pid, f), items in missing.items():
        advice = diagnostics.field_advice(pid, f)
        out.append(_finding(
            severity=advice.severity, platform=pid, title=advice.title,
            subject_label="URLs missing this field", subjects=[i.url for i in items],
            error=f"'{f}' blank after a successful visit",
            where=diagnostics.field_site(pid, f), cause=advice.cause, fix=advice.fix))
    return _ordered(out)


# --------------------------------------------------------------- sending

async def _enabled(flag: str) -> bool:
    try:
        return bool((await alert_settings_db.get_settings()).get(flag, True))
    except Exception:                                   # noqa: BLE001
        return True


def _trim(findings: list[dict]) -> tuple[list[dict], int]:
    shown = findings[:_MAX_FINDINGS]
    return shown, max(0, len(findings) - len(shown))


async def _client_name(client_id: str) -> str:
    if not client_id:
        return ""
    try:
        from backend.database.repositories import client_repository as clients_db
        client = await clients_db.try_get(client_id)
        return (client or {}).get("name") or client_id
    except Exception:                                   # noqa: BLE001
        return client_id


async def _send(subject: str, headline: str, badge: str, summary: list[tuple[str, str]],
                findings: list[dict], intro: str) -> bool:
    from backend.services import email_service
    shown, hidden = _trim(findings)
    html = email_service.render_failure_report(
        title=subject, badge_text=badge, headline=headline, intro=intro,
        summary=summary, findings=shown, hidden_findings=hidden,
        subject_cap=_MAX_SUBJECTS)
    text = email_service.render_failure_report_text(
        headline=headline, intro=intro, summary=summary, findings=shown,
        hidden_findings=hidden, subject_cap=_MAX_SUBJECTS)
    ok, detail = await email_service.send_email(subject, html, text)
    if not ok:
        log.warning(f"failure report not delivered ({detail}): {subject}")
    return ok


def _counts(findings: Iterable[dict]) -> tuple[int, int]:
    crit = sum(1 for f in findings if f["severity"] == CRITICAL)
    warn = sum(1 for f in findings if f["severity"] == WARNING)
    return crit, warn


async def notify_discovery(job: Any) -> bool:
    """Mail this discovery job's failures, if it had any critical ones."""
    try:
        if not await _enabled("alert_on_discovery_failure"):
            return False
        findings = discovery_findings(job)
        crit, warn = _counts(findings)
        if not crit:
            return False
        client = await _client_name(getattr(job, "group_id", ""))
        broken_platforms = sorted({f["platform_name"] for f in findings
                                   if f["severity"] == CRITICAL})
        summary = [
            ("Client", client or "-"),
            ("Job ID", getattr(job, "id", "")),
            ("Job status", f"{getattr(job, 'status', '')} -- {getattr(job, 'message', '')}"),
            ("Platforms with failures", ", ".join(broken_platforms)),
            ("Profiles found", f"{getattr(job, 'found', 0)} ({getattr(job, 'new', 0)} new)"),
            ("Problems", f"{crit} critical, {warn} warning"),
        ]
        return await _send(
            subject=f"[Brand Intelligence] Discovery failures -- {client or job.id}: "
                    f"{', '.join(broken_platforms)}",
            headline="Discovery sweep finished with failures",
            badge="DISCOVERY FAILURE", summary=summary, findings=findings,
            intro=("Some searches in this discovery job did not complete. Every "
                   "keyword listed below is recorded as still owed and will be "
                   "retried by the next scheduled run's gap-closing lap once the "
                   "cause is fixed."))
    except Exception as e:                              # noqa: BLE001
        log.error(f"discovery failure alert crashed: {type(e).__name__}: {e}")
        return False


async def notify_analysis(job: Any) -> bool:
    """Mail this analysis job's failed and incomplete URLs, if any."""
    try:
        if not await _enabled("alert_on_analysis_failure"):
            return False
        findings = analysis_findings(job)
        if not findings:
            return False
        crit, warn = _counts(findings)
        items = list(getattr(job, "items", []) or [])
        failed_urls = sum(1 for i in items if i.status == "error"
                          and (getattr(i, "row_status", "") or "").upper() != "GONE")
        incomplete = sum(1 for i in items if i.status == "done" and getattr(i, "missed", None))
        client = await _client_name(getattr(job, "org_id", ""))
        platforms = sorted({f["platform_name"] for f in findings})
        summary = [
            ("Client", client or "Quick analysis (no client)"),
            ("Job ID", getattr(job, "id", "")),
            ("Job status", f"{getattr(job, 'status', '')} -- {getattr(job, 'message', '')}"),
            ("URLs analysed", str(getattr(job, "total", len(items)))),
            ("URLs that failed", str(failed_urls)),
            ("URLs with missing data", str(incomplete)),
            ("Platforms affected", ", ".join(platforms)),
            ("Problems", f"{crit} critical, {warn} warning"),
        ]
        return await _send(
            subject=f"[Brand Intelligence] Analysis issues -- {failed_urls} failed, "
                    f"{incomplete} incomplete ({', '.join(platforms)})",
            headline="Profile analysis finished with failures or missing data",
            badge="ANALYSIS ISSUE", summary=summary, findings=findings,
            intro=("The URLs below either could not be read or were read without a "
                   "field the platform publishes. Re-analysing them after the fix "
                   "fills the gaps without losing anything already read."))
    except Exception as e:                              # noqa: BLE001
        log.error(f"analysis failure alert crashed: {type(e).__name__}: {e}")
        return False


def scheduler_needs_alert(status: str, entries: list[dict],
                          findings_by_client: dict[str, list[dict]]) -> bool:
    if status in ("failed",):
        return True
    for e in entries:
        if e.get("status") in ("failed", "skipped", "cancelled"):
            return True
        owed = e.get("owed", -1)
        if owed > 0 or (owed == -1 and e.get("status") in ("done", "failed")):
            return True
    return any(f["severity"] == CRITICAL
               for fs in findings_by_client.values() for f in fs)


async def notify_scheduler_run(
    *, run_id: str, trigger: str, status: str, message: str,
    entries: list[dict], findings_by_client: dict[str, list[dict]],
) -> bool:
    """ONE email for a whole Scheduler run, when anything in it needs a
    human: the run's own verdict, one line per client, then every client's
    discovery findings."""
    try:
        if not await _enabled("alert_on_scheduler_issue"):
            return False
        if status == "cancelled" or not scheduler_needs_alert(status, entries, findings_by_client):
            return False
        findings: list[dict] = []
        for e in entries:
            for f in findings_by_client.get(e.get("client_id", ""), []):
                findings.append({**f, "scope": e.get("name") or e.get("client_id", "")})
        findings = _ordered(findings)
        summary = [("Run ID", run_id), ("Trigger", trigger), ("Result", f"{status} -- {message}")]
        for e in entries:
            owed = e.get("owed", -1)
            owed_s = "unknown (ledger unreadable)" if owed == -1 else str(owed)
            summary.append((
                e.get("name") or e.get("client_id", ""),
                f"{e.get('status')} | found {e.get('found', 0)} ({e.get('new_profiles', 0)} new) "
                f"| searches still owed: {owed_s} | {e.get('message', '')}"))
        return await _send(
            subject=f"[Brand Intelligence] Scheduled run needs attention -- {status}: {message}",
            headline="Scheduled discovery run finished with problems",
            badge="SCHEDULER", summary=summary, findings=findings,
            intro=("One line per client below, then every failure found in their "
                   "sweeps with the file and line to look at and how to fix it."))
    except Exception as e:                              # noqa: BLE001
        log.error(f"scheduler alert crashed: {type(e).__name__}: {e}")
        return False


async def notify_scheduler_missed(*, due_at: Optional[datetime], reason: str) -> bool:
    """A scheduled run that did not happen at all."""
    try:
        if not await _enabled("alert_on_scheduler_issue"):
            return False
        due = due_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if due_at else "-"
        finding = _finding(
            severity=CRITICAL, platform="scheduler", title="Scheduled run did not happen",
            subject_label="Run", subjects=[f"due {due}"], error=reason,
            where=diagnostics.locate("backend/services/scheduler_service.py",
                                     r"async def _tick\("),
            cause=reason,
            fix=("If the backend was down, make sure it runs as a service that starts "
                 "with the machine. To sweep now, open the Scheduler page and press "
                 "Run queue."))
        return await _send(
            subject=f"[Brand Intelligence] Scheduled run missed (due {due})",
            headline="A scheduled discovery run was missed",
            badge="SCHEDULER", summary=[("Due at", due), ("Reason", reason)],
            findings=[finding],
            intro="The Scheduler did not run the sweep that was due. Nothing was searched.")
    except Exception as e:                              # noqa: BLE001
        log.error(f"scheduler missed-run alert crashed: {type(e).__name__}: {e}")
        return False
