"""Sweep reports: what a client's discovery found, and what an analyst did
with it, as an email.

THE THREE NUMBERS, and why they are these three:

    New     validated within the last 24h   -- decisions just made
    Delta   validated before that           -- the settled backlog
    Total   New + Delta                     -- everything validated

They are the same split the Validated tab already shows, read through the
same `validated_at` clause (database/repositories/profile_repository.py), so
a report and the screen an analyst is looking at can never disagree. If the
boundary moves, it moves in one place for both.

WHY VALIDATION COUNTS CAN LOOK EMPTY RIGHT AFTER A SWEEP. Validating is a
human action that happens AFTER a sweep, so a report fired the moment one
finishes is really saying "here is what this sweep found, plus what you
triaged since the last report". That is the honest framing, and the email
says so rather than presenting a zero as if the sweep produced nothing.

READ-ONLY. This module computes and formats; it never writes to a profile,
never changes a status, and cannot affect discovery or analysis.
"""

from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone
from typing import Any, Optional

from backend.database.repositories import client_repository as clients_db
from backend.database.repositories import profile_repository as profiles_db
from backend.services import email_service
from backend.shared.logging import get_logger

log = get_logger("services.report")

# How many example profiles to list in a report. Enough to be useful at a
# glance; a report that inlines 300 rows is one nobody opens.
SAMPLE_LIMIT = 10


async def _count(client_id: str, **extra: Any) -> int:
    _, total, _ = await profiles_db.find(
        client_id, phase=profiles_db.PHASE_DISCOVERY, include_held=True,
        limit=1, **extra,
    )
    return total


async def build_client_report(client_id: str) -> dict:
    """The numbers and sample rows for one client. Pure reads."""
    try:
        client = await clients_db.get(client_id)
        name = client.get("name") or client_id
    except Exception:
        # A report for a client that has since been deleted is still worth
        # producing -- its profiles are what the analyst cares about.
        name = client_id

    new_validated = await _count(client_id, status="approved", validated_age="new")
    old_validated = await _count(client_id, status="approved", validated_age="old")
    pending = await _count(client_id, status="pending")
    new_pending = await _count(client_id, status="pending", age="new")
    logo_hits = await _count(client_id, logo_matched=True)

    rows, _, _ = await profiles_db.find(
        client_id, phase=profiles_db.PHASE_DISCOVERY, include_held=True,
        status="approved", validated_age="new", limit=SAMPLE_LIMIT,
    )

    return {
        "client_id": client_id,
        "name": name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "validated": {
            "new": new_validated,
            "delta": old_validated,
            "total": new_validated + old_validated,
        },
        "pending": {"new": new_pending, "total": pending},
        "logo_matches": logo_hits,
        "samples": [
            {
                "name": r.get("display_name") or r.get("username") or "?",
                "platform": r.get("platform", ""),
                "url": r.get("url", ""),
                "name_score": r.get("name_score"),
                "logo_similarity": r.get("logo_similarity"),
                "logo_tier": r.get("logo_match_tier", ""),
            }
            for r in rows
        ],
    }


async def build_combined_report() -> dict:
    """One row per client. Clients are read in sequence rather than
    concurrently: a digest is not latency-sensitive, and a burst of parallel
    aggregations against Mongo while a sweep is writing to it is a cost with
    no upside."""
    clients = await clients_db.list_all()
    reports = []
    for c in clients:
        try:
            reports.append(await build_client_report(c["client_id"]))
        except Exception as e:                       # noqa: BLE001 - never fatal
            log.warning(f"report for {c['client_id']!r} failed: {type(e).__name__}: {e}")
    totals = {
        "new": sum(r["validated"]["new"] for r in reports),
        "delta": sum(r["validated"]["delta"] for r in reports),
        "total": sum(r["validated"]["total"] for r in reports),
        "logo_matches": sum(r["logo_matches"] for r in reports),
        "pending": sum(r["pending"]["total"] for r in reports),
    }
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "clients": reports, "totals": totals}


# ------------------------------------------------------------------- HTML

_E = html.escape


def _cards(new: int, delta: int, total: int) -> str:
    def card(label, value, colour):
        return (f'<td align="center" style="padding:14px 10px;background:#141A22;'
                f'border:1px solid #242B35;border-radius:8px;">'
                f'<div style="color:{colour};font-size:26px;font-weight:800;'
                f'font-family:Arial,sans-serif;">{value}</div>'
                f'<div style="color:#8A99AD;font-size:11px;letter-spacing:1px;'
                f'text-transform:uppercase;margin-top:4px;">{label}</div></td>')
    return ('<table role="presentation" width="100%" cellspacing="8" cellpadding="0">'
            f'<tr>{card("New", new, "#36B5A0")}{card("Delta", delta, "#8838DD")}'
            f'{card("Total", total, "#F0F4F8")}</tr></table>')


def _sample_table(samples: list[dict]) -> str:
    if not samples:
        return ('<p style="color:#8A99AD;font-size:13px;">'
                'No profiles were validated in the last 24 hours. Validation is a '
                'manual step, so this is normal when a sweep has just finished and '
                'the results have not been triaged yet.</p>')
    head = ("".join(f'<th align="left" style="padding:8px 10px;color:#8A99AD;'
                    f'font-size:11px;text-transform:uppercase;border-bottom:1px solid '
                    f'#242B35;">{h}</th>' for h in ("Profile", "Platform", "Name", "Logo")))
    body = ""
    for s in samples:
        logo = (f'{s["logo_similarity"]}% {_E(s["logo_tier"])}'
                if s.get("logo_similarity") is not None else "&mdash;")
        body += (
            f'<tr>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #1B2129;">'
            f'<a href="{_E(s["url"])}" style="color:#00E5FF;text-decoration:none;'
            f'font-size:13px;">{_E(s["name"])[:48]}</a></td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #1B2129;color:#F0F4F8;'
            f'font-size:12px;">{_E(s["platform"])}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #1B2129;color:#F0F4F8;'
            f'font-size:12px;">{s["name_score"] if s.get("name_score") is not None else "&mdash;"}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #1B2129;color:#FDB71B;'
            f'font-size:12px;">{logo}</td></tr>')
    return (f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            f'style="margin-top:6px;"><tr>{head}</tr>{body}</table>')


def _shell(title: str, subtitle: str, inner: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_E(title)}</title></head>
<body style="margin:0;padding:24px;background:#0B0F14;font-family:Arial,Helvetica,sans-serif;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"
       style="max-width:680px;margin:0 auto;background:#0F141A;border:1px solid #242B35;
              border-radius:12px;overflow:hidden;">
  <tr><td style="padding:20px 24px;border-bottom:1px solid #242B35;">
    <div style="color:#F0F4F8;font-size:18px;font-weight:700;">{_E(title)}</div>
    <div style="color:#8A99AD;font-size:12px;margin-top:4px;">{_E(subtitle)}</div>
  </td></tr>
  <tr><td style="padding:20px 24px;">{inner}</td></tr>
  <tr><td style="padding:14px 24px;border-top:1px solid #242B35;color:#5A6675;font-size:11px;">
    Brand Intelligence Suite &middot; automated report
  </td></tr>
</table></body></html>"""


def render_client_html(rep: dict) -> str:
    v = rep["validated"]
    inner = (
        '<div style="color:#8A99AD;font-size:12px;text-transform:uppercase;'
        'letter-spacing:1px;margin-bottom:8px;">Validated profiles</div>'
        + _cards(v["new"], v["delta"], v["total"])
        + f'<div style="color:#8A99AD;font-size:13px;margin:16px 0 6px;">'
          f'Awaiting triage: <strong style="color:#F0F4F8;">{rep["pending"]["total"]}</strong> '
          f'({rep["pending"]["new"]} found in the last 24h) &nbsp;&middot;&nbsp; '
          f'Logo matches: <strong style="color:#FDB71B;">{rep["logo_matches"]}</strong></div>'
        + '<div style="color:#8A99AD;font-size:12px;text-transform:uppercase;'
          'letter-spacing:1px;margin:18px 0 4px;">Newly validated</div>'
        + _sample_table(rep["samples"])
    )
    return _shell(f'{rep["name"]} — sweep report', f'Client {rep["client_id"]}', inner)


def render_combined_html(rep: dict) -> str:
    t = rep["totals"]
    rows = ""
    for r in sorted(rep["clients"], key=lambda x: -x["validated"]["new"]):
        v = r["validated"]
        rows += (
            f'<tr>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #1B2129;color:#F0F4F8;'
            f'font-size:13px;">{_E(r["name"])[:36]}</td>'
            f'<td align="right" style="padding:8px 10px;border-bottom:1px solid #1B2129;'
            f'color:#36B5A0;font-size:13px;font-weight:700;">{v["new"]}</td>'
            f'<td align="right" style="padding:8px 10px;border-bottom:1px solid #1B2129;'
            f'color:#8838DD;font-size:13px;">{v["delta"]}</td>'
            f'<td align="right" style="padding:8px 10px;border-bottom:1px solid #1B2129;'
            f'color:#F0F4F8;font-size:13px;">{v["total"]}</td>'
            f'<td align="right" style="padding:8px 10px;border-bottom:1px solid #1B2129;'
            f'color:#FDB71B;font-size:13px;">{r["logo_matches"]}</td>'
            f'<td align="right" style="padding:8px 10px;border-bottom:1px solid #1B2129;'
            f'color:#8A99AD;font-size:13px;">{r["pending"]["total"]}</td></tr>')
    head = "".join(
        f'<th align="{a}" style="padding:8px 10px;color:#8A99AD;font-size:11px;'
        f'text-transform:uppercase;border-bottom:1px solid #242B35;">{h}</th>'
        for h, a in (("Client", "left"), ("New", "right"), ("Delta", "right"),
                     ("Total", "right"), ("Logo", "right"), ("Pending", "right")))
    inner = (
        _cards(t["new"], t["delta"], t["total"])
        + f'<div style="color:#8A99AD;font-size:13px;margin:16px 0 6px;">'
          f'Across <strong style="color:#F0F4F8;">{len(rep["clients"])}</strong> clients '
          f'&nbsp;&middot;&nbsp; {t["logo_matches"]} logo matches '
          f'&nbsp;&middot;&nbsp; {t["pending"]} awaiting triage</div>'
        + f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
          f'<tr>{head}</tr>{rows}</table>'
    )
    return _shell("All clients — combined report",
                  f'{len(rep["clients"])} clients', inner)


def render_text(rep: dict) -> str:
    """Plain-text alternative. Not decoration: a mail client that refuses
    HTML shows this, and a report nobody can read is a report nobody acts on."""
    v = rep["validated"]
    return (f'{rep["name"]} - sweep report\n'
            f'  Validated: new {v["new"]}, delta {v["delta"]}, total {v["total"]}\n'
            f'  Awaiting triage: {rep["pending"]["total"]}\n'
            f'  Logo matches: {rep["logo_matches"]}\n')


# ------------------------------------------------------------------ sending

async def send_client_report(client_id: str, to_emails: Optional[list[str]] = None) -> tuple[bool, str]:
    rep = await build_client_report(client_id)
    return await email_service.send_email(
        f'[Brand Intelligence] {rep["name"]} — {rep["validated"]["new"]} newly validated',
        render_client_html(rep), render_text(rep), to_emails,
    )


async def send_combined_report(to_emails: Optional[list[str]] = None) -> tuple[bool, str]:
    rep = await build_combined_report()
    t = rep["totals"]
    return await email_service.send_email(
        f'[Brand Intelligence] All clients — {t["new"]} newly validated',
        render_combined_html(rep),
        f'All clients: new {t["new"]}, delta {t["delta"]}, total {t["total"]}\n',
        to_emails,
    )


def spawn_client_report(client_id: str) -> Optional[asyncio.Task]:
    """Fire a report behind a finished sweep. Returned, not discarded, so it
    cannot be garbage collected mid-flight -- the same reasoning as
    avatar_cache.spawn.

    Never raises into the caller: a job that found profiles must not be
    reported as failed because an SMTP server was unreachable.
    """
    if not client_id:
        return None

    async def run() -> None:
        try:
            ok, detail = await send_client_report(client_id)
            if ok:
                log.info(f"sweep report sent for client {client_id!r}")
            else:
                # "No recipients configured" is the normal state until an
                # operator opts in, so this is info, not a warning.
                log.info(f"sweep report not sent for {client_id!r}: {detail}")
        except Exception as e:                       # noqa: BLE001 - never fatal
            log.warning(f"sweep report failed for {client_id!r}: {type(e).__name__}: {e}")

    return asyncio.create_task(run())
