"""Asynchronous email notification service for Brand Intelligence alerts.

Sends alerts for session failures, upcoming token expirations, and critical incidents
via SMTP using non-blocking asyncio worker threads.
"""

from __future__ import annotations

import asyncio
import smtplib
from html import escape as _esc
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
from typing import Optional

from backend.database.repositories import alert_settings_repository as alert_settings_db
from backend.shared.logging import get_logger

log = get_logger("services.email")


def _format_html_template(
    title: str,
    badge_text: str,
    badge_color: str,
    headline: str,
    details_table: list[tuple[str, str]],
    recommendation: str,
    footer_note: str = "Brand Intelligence Automated Security System",
) -> str:
    """Renders a sleek, responsive HTML email matching enterprise security alerts.

    Every value is ESCAPED except `recommendation`, which carries this
    module's own <strong> markup -- callers escape what they put inside it.
    Account names, exception text and incident messages come from outside
    this process, and one stray `<` or `&` used to break the whole layout.
    """
    esc = _esc
    title, badge_text, badge_color, headline, footer_note = (
        esc(title), esc(badge_text), esc(badge_color), esc(headline), esc(footer_note))
    details_table = [(esc(str(label)), esc(str(val))) for label, val in details_table]
    rows_html = "".join(
        f"""<tr>
            <td style="padding: 10px 14px; border-bottom: 1px solid #242B35; color: #8A99AD; font-size: 13px; font-weight: 500; width: 30%;">{label}</td>
            <td style="padding: 10px 14px; border-bottom: 1px solid #242B35; color: #F0F4F8; font-size: 13px; font-family: monospace;">{val}</td>
        </tr>"""
        for label, val in details_table
    )

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
</head>
<body style="margin: 0; padding: 24px; background-color: #0A0D12; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #F0F4F8;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width: 600px; margin: 0 auto; background-color: #12171F; border: 1px solid #202732; border-radius: 12px; overflow: hidden; box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45);">
        <!-- Header -->
        <tr>
            <td style="padding: 24px 28px; background: linear-gradient(180deg, #18202C 0%, #12171F 100%); border-bottom: 1px solid #202732;">
                <table width="100%" cellspacing="0" cellpadding="0">
                    <tr>
                        <td>
                            <span style="display: inline-block; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px; padding: 4px 10px; border-radius: 20px; background-color: {badge_color}22; color: {badge_color}; border: 1px solid {badge_color}66;">
                                {badge_text}
                            </span>
                            <h1 style="margin: 12px 0 0 0; font-size: 20px; font-weight: 700; color: #FFFFFF; letter-spacing: -0.3px;">
                                {headline}
                            </h1>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>

        <!-- Body Content -->
        <tr>
            <td style="padding: 24px 28px;">
                <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse: collapse; background-color: #0E1218; border: 1px solid #202732; border-radius: 8px; margin-bottom: 20px; overflow: hidden;">
                    {rows_html}
                </table>

                <!-- Action / Recommendation Box -->
                <div style="background-color: rgba(136, 56, 221, 0.08); border-left: 3px solid #8838DD; padding: 14px 18px; border-radius: 4px; margin-top: 18px;">
                    <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; color: #9A50E9; letter-spacing: 0.5px; margin-bottom: 4px;">
                        Recommended Operator Action
                    </div>
                    <div style="font-size: 13px; line-height: 1.5; color: #D0D8E2;">
                        {recommendation}
                    </div>
                </div>
            </td>
        </tr>

        <!-- Footer -->
        <tr>
            <td style="padding: 16px 28px; background-color: #0A0D12; border-top: 1px solid #1E2530; text-align: center;">
                <p style="margin: 0; font-size: 12px; color: #586576;">
                    {footer_note} • Generated at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
                </p>
            </td>
        </tr>
    </table>
</body>
</html>"""


def _send_smtp_sync(
    host: str,
    port: int,
    user: str,
    password: str,
    sender: str,
    recipients: list[str],
    msg: MIMEMultipart,
    use_ssl: bool = False,
) -> tuple[bool, str]:
    """Blocking SMTP dispatch helper for thread pool."""
    if not host:
        return False, "SMTP host is not configured."
    if not recipients:
        return False, "No recipient email addresses specified."

    try:
        if use_ssl or port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                if user and password:
                    server.login(user, password)
                server.sendmail(sender, recipients, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                # Attempt STARTTLS if supported and port suggests secure transport (e.g. 587)
                if port == 587 or server.has_extn("STARTTLS"):
                    try:
                        server.starttls()
                        server.ehlo()
                    except Exception as tls_err:
                        # NEVER FALL THROUGH TO A PLAINTEXT LOGIN. This used
                        # to log at debug and carry on, which sent the SMTP
                        # password unencrypted -- and after a failed
                        # handshake the connection is usually dead anyway,
                        # so the real error surfaced later as a confusing
                        # "connection unexpectedly closed".
                        if user and password:
                            err = (f"secure connection (STARTTLS) to {host}:{port} failed "
                                   f"({type(tls_err).__name__}: {tls_err}) -- not sending "
                                   f"the password unencrypted")
                            log.error(f"SMTP delivery failed to {recipients}: {err}")
                            return False, err
                        log.warning(f"STARTTLS to {host}:{port} failed, sending "
                                    f"unauthenticated: {tls_err}")
                if user and password:
                    server.login(user, password)
                server.sendmail(sender, recipients, msg.as_string())

        return True, "Email sent successfully."
    except Exception as e:
        err_msg = f"{type(e).__name__}: {e}"
        log.error(f"SMTP delivery failed to {recipients}: {err_msg}")
        return False, err_msg


async def send_email(
    subject: str,
    html_content: str,
    text_content: Optional[str] = None,
    to_emails: Optional[list[str]] = None,
) -> tuple[bool, str]:
    """Asynchronously dispatches an email to the configured recipients or explicitly passed list."""
    settings = await alert_settings_db.get_settings()
    recipients = to_emails if to_emails is not None else list(settings.get("alert_emails") or [])

    if not recipients:
        log.debug("no alert email recipients configured, skipping email dispatch")
        return False, "No recipients configured."

    host = settings.get("smtp_host") or ""
    if not host:
        log.warning("cannot send alert email: SMTP host is empty in settings")
        return False, "SMTP host is not configured."

    port = int(settings.get("smtp_port") or 1025)
    user = settings.get("smtp_user") or ""
    password = settings.get("smtp_pass") or ""
    sender = settings.get("alert_from") or "alerts@brand-intelligence.local"
    use_ssl = bool(settings.get("smtp_ssl", False))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    if text_content:
        msg.attach(MIMEText(text_content, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    ok, detail = await asyncio.to_thread(
        _send_smtp_sync, host, port, user, password, sender, recipients, msg, use_ssl
    )
    if not ok and _transient_smtp(detail):
        # ONE RETRY FOR A MOMENTARY FAILURE. A mail server that drops the
        # connection or times out once (seen live: "SMTPServerDisconnected:
        # Connection unexpectedly closed: The read operation timed out")
        # otherwise lost the alert for good -- and an alert is only ever
        # sent because something already went wrong. A wrong password or a
        # rejected recipient is not retried: that fails identically again.
        log.warning(f"SMTP send failed transiently ({detail}) -- retrying once")
        await asyncio.sleep(_SMTP_RETRY_DELAY_S)
        ok, detail = await asyncio.to_thread(
            _send_smtp_sync, host, port, user, password, sender, recipients, msg, use_ssl
        )
    return ok, detail


_SMTP_RETRY_DELAY_S = 5.0
_TRANSIENT_SMTP_TOKENS = (
    "smtpserverdisconnected", "timed out", "timeout", "connection reset",
    "connection refused", "smtpconnecterror", "temporarily", "try again",
    "connection unexpectedly closed",
)


def _transient_smtp(detail: str) -> bool:
    d = (detail or "").lower()
    return any(tok in d for tok in _TRANSIENT_SMTP_TOKENS)


# ------------------------------------------------------------ failure reports

_SEVERITY_COLOR = {"critical": "#FF3B30", "warning": "#FF9500"}
_MONO = "font-family:Consolas,Menlo,monospace;"


def _subject_list(subjects: list[str], cap: int) -> tuple[list[str], int]:
    shown = list(subjects[:cap])
    return shown, max(0, len(subjects) - len(shown))


def _card_row(label: str, value: str, mono: bool = False) -> str:
    if not value:
        return ""
    font = _MONO if mono else ""
    return (f'<tr><td style="padding:6px 12px;color:#8A99AD;font-size:12px;width:26%;'
            f'vertical-align:top;">{_esc(label)}</td><td style="padding:6px 12px;'
            f'color:#E6ECF2;font-size:12px;{font}word-break:break-word;">{_esc(value)}</td></tr>')


def _finding_card(n: int, f: dict, subject_cap: int) -> str:
    color = _SEVERITY_COLOR.get(f.get("severity"), "#FF9500")
    shown, more = _subject_list(list(f.get("subjects") or []), subject_cap)
    items = "".join(f'<li style="margin:2px 0;word-break:break-all;">{_esc(x)}</li>' for x in shown)
    if more:
        items += f'<li style="margin:2px 0;color:#8A99AD;">... and {more} more</li>'
    scope = f" -- {_esc(f['scope'])}" if f.get("scope") else ""
    rows = (_card_row("Error", f.get("error", ""), mono=True)
            + _card_row("Where (file:line)", f.get("where", ""), mono=True)
            + _card_row("Likely cause", f.get("cause", ""))
            + _card_row("How to fix", f.get("fix", "")))
    return (
        f'<div style="border:1px solid #202732;border-left:4px solid {color};border-radius:8px;'
        f'background:#0E1218;margin:0 0 16px 0;">'
        f'<div style="padding:12px 14px 6px 14px;">'
        f'<span style="font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;'
        f'color:{color};">{_esc(str(f.get("severity", "")))} &middot; '
        f'{_esc(str(f.get("platform_name", "")))}{scope}</span>'
        f'<div style="font-size:15px;font-weight:700;color:#FFFFFF;margin-top:4px;">'
        f'{n}. {_esc(str(f.get("title", "")))}</div></div>'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse;">{rows}</table>'
        f'<div style="padding:6px 12px 12px 12px;">'
        f'<div style="font-size:12px;color:#8A99AD;margin-bottom:4px;">'
        f'{_esc(str(f.get("subject_label", "Affected")))} ({int(f.get("count", 0))}):</div>'
        f'<ul style="margin:0;padding-left:18px;color:#D0D8E2;font-size:12px;{_MONO}">{items}</ul>'
        f'</div></div>')


def render_failure_report(
    *, title: str, badge_text: str, headline: str, intro: str,
    summary: list[tuple[str, str]], findings: list[dict], hidden_findings: int = 0,
    subject_cap: int = 15,
) -> str:
    """The failure-report email: a summary table, then one card per
    finding -- what happened, the error, WHERE in the code (file:line), the
    likely cause and the fix. Built for the engineer who has to act on it.

    Every value is escaped: keywords, URLs and exception text all come from
    outside this process."""
    summary_rows = "".join(
        f'<tr><td style="padding:8px 14px;border-bottom:1px solid #242B35;color:#8A99AD;'
        f'font-size:13px;width:32%;vertical-align:top;">{_esc(str(k))}</td>'
        f'<td style="padding:8px 14px;border-bottom:1px solid #242B35;color:#F0F4F8;'
        f'font-size:13px;">{_esc(str(v))}</td></tr>'
        for k, v in summary)
    cards = "".join(_finding_card(n, f, subject_cap) for n, f in enumerate(findings, 1))
    if hidden_findings:
        cards += (f'<p style="color:#8A99AD;font-size:12px;">... and {hidden_findings} more '
                  f'finding(s) not shown. See the job in the application.</p>')
    if not cards:
        cards = '<p style="color:#8A99AD;font-size:13px;">No individual findings.</p>'
    generated = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>{_esc(title)}</title></head>'
        '<body style="margin:0;padding:24px;background-color:#0A0D12;font-family:-apple-system,'
        "BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#F0F4F8;\">"
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="max-width:720px;margin:0 auto;background-color:#12171F;border:1px solid #202732;'
        'border-radius:12px;">'
        '<tr><td style="padding:24px 28px;border-bottom:1px solid #202732;">'
        '<span style="display:inline-block;font-size:11px;font-weight:700;text-transform:uppercase;'
        'letter-spacing:.8px;padding:4px 10px;border-radius:20px;background-color:#FF3B3022;'
        f'color:#FF3B30;border:1px solid #FF3B3066;">{_esc(badge_text)}</span>'
        '<h1 style="margin:12px 0 6px 0;font-size:20px;font-weight:700;color:#FFFFFF;">'
        f'{_esc(headline)}</h1>'
        f'<p style="margin:0;font-size:13px;line-height:1.5;color:#B8C4D2;">{_esc(intro)}</p>'
        '</td></tr>'
        '<tr><td style="padding:20px 28px 4px 28px;"><table width="100%" cellspacing="0" '
        'cellpadding="0" style="border-collapse:collapse;background-color:#0E1218;'
        f'border:1px solid #202732;border-radius:8px;">{summary_rows}</table></td></tr>'
        '<tr><td style="padding:20px 28px;"><div style="font-size:11px;font-weight:700;'
        'text-transform:uppercase;color:#9A50E9;letter-spacing:.5px;margin-bottom:10px;">'
        f'Findings -- most severe first</div>{cards}</td></tr>'
        '<tr><td style="padding:16px 28px;background-color:#0A0D12;border-top:1px solid #1E2530;'
        'text-align:center;"><p style="margin:0;font-size:12px;color:#586576;">'
        f'Brand Intelligence Automated Monitoring &bull; Generated at {generated}</p></td></tr>'
        '</table></body></html>')


def render_failure_report_text(
    *, headline: str, intro: str, summary: list[tuple[str, str]],
    findings: list[dict], hidden_findings: int = 0, subject_cap: int = 15,
) -> str:
    """The same report as plain text, for mail clients that do not render
    HTML."""
    lines = [headline, "=" * len(headline), intro, ""]
    lines += [f"{k}: {v}" for k, v in summary]
    lines.append("")
    for n, f in enumerate(findings, 1):
        scope = f" -- {f['scope']}" if f.get("scope") else ""
        lines.append(f"{n}. [{str(f.get('severity', '')).upper()}] "
                     f"{f.get('platform_name', '')}{scope}: {f.get('title', '')}")
        for label, key in (("Error", "error"), ("Where", "where"),
                           ("Likely cause", "cause"), ("How to fix", "fix")):
            if f.get(key):
                lines.append(f"   {label}: {f[key]}")
        shown, more = _subject_list(list(f.get("subjects") or []), subject_cap)
        lines.append(f"   {f.get('subject_label', 'Affected')} ({f.get('count', 0)}):")
        lines += [f"     - {x}" for x in shown]
        if more:
            lines.append(f"     ... and {more} more")
        lines.append("")
    if hidden_findings:
        lines.append(f"... and {hidden_findings} more finding(s) not shown.")
    return "\n".join(lines)


async def send_test_email(to_email: Optional[str] = None) -> tuple[bool, str]:
    """Sends a test alert email to verify SMTP configuration and deliverability."""
    target_recipients = [to_email] if to_email else None
    html = _format_html_template(
        title="Test Alert - Brand Intelligence",
        badge_text="SYSTEM TEST",
        badge_color="#8838DD",
        headline="Brand Intelligence Alert System Test",
        details_table=[
            ("Status", "Operational"),
            ("Trigger", "Operator Manual Test"),
            ("SMTP Transport", "Verified Active"),
        ],
        recommendation="No action needed. Your email alert notifications are correctly configured and ready to receive live incident warnings.",
        footer_note="Test message sent from Brand Intelligence Suite",
    )
    text = "Brand Intelligence Alert System Test: Your SMTP configuration is verified and operational."
    return await send_email("✅ [Brand Intelligence] Alert System Test", html, text, target_recipients)


async def send_session_failure_alert(
    platform: str, identifier: str, session_id: str, reason: str, detail: str = ""
) -> bool:
    """Dispatches an alert when a session transitions into a dead/checkpointed/expired state."""
    settings = await alert_settings_db.get_settings()
    if not settings.get("alert_on_session_dead", True):
        return False

    plat_display = platform.capitalize()
    subject = f"🚨 [CRITICAL] {plat_display} Session {reason.upper()}: {identifier}"
    html = _format_html_template(
        title=f"{plat_display} Session Failure",
        badge_text=f"SESSION {reason.upper()}",
        badge_color="#FF3B30",
        headline=f"{plat_display} Session Is Unavailable",
        details_table=[
            ("Platform", plat_display),
            ("Account Identifier", identifier),
            ("Session ID", session_id),
            ("Failure Reason", reason),
            ("Detail", detail or "The platform rejected authentication or enforced a checkpoint."),
            ("Operational Impact", "Scheduled discovery & analysis sweeps on this platform will fail or pause."),
        ],
        recommendation=f"Navigate to <strong>Admin → Sessions</strong> in the application, locate the <strong>{_esc(plat_display)}</strong> session pool, and paste freshly exported cookies to restore automated scraping.",
    )
    text = f"CRITICAL: {plat_display} session {identifier} is {reason}. Detail: {detail}. Please re-authenticate under Sessions."
    ok, _ = await send_email(subject, html, text)
    return ok


async def send_session_expiring_alert(
    platform: str, identifier: str, hours_remaining: float, detail: str = ""
) -> bool:
    """Dispatches a warning alert when session tokens are approaching expiration (< 24h)."""
    settings = await alert_settings_db.get_settings()
    if not settings.get("alert_on_session_expiring", True):
        return False

    plat_display = platform.capitalize()
    subject = f"⚠️ [WARNING] {plat_display} Session Expiring in {hours_remaining:.1f}h: {identifier}"
    html = _format_html_template(
        title=f"{plat_display} Session Expiring Soon",
        badge_text="EXPIRING SOON",
        badge_color="#FF9500",
        headline=f"{plat_display} Authentication Expiring Soon",
        details_table=[
            ("Platform", plat_display),
            ("Account Identifier", identifier),
            ("Time Remaining", f"~{hours_remaining:.1f} hours"),
            ("Status", "Approaching cookie expiration deadline"),
            ("Detail", detail or "Auth token (xs, sessionid, auth_token) expiry timestamp is within warning threshold."),
        ],
        recommendation=f"Re-export fresh cookies from your browser for <strong>{_esc(f'{plat_display} ({identifier})')}</strong> and paste them in <strong>Admin → Sessions</strong> before automated jobs encounter a login wall.",
    )
    text = f"WARNING: {plat_display} session {identifier} expires in ~{hours_remaining:.1f} hours. Please refresh cookies before sweeps fail."
    ok, _ = await send_email(subject, html, text)
    return ok


async def send_critical_incident_alert(incident: dict) -> bool:
    """Dispatches an alert for high/critical severity system or impersonation incidents."""
    settings = await alert_settings_db.get_settings()
    if not settings.get("alert_on_critical_incident", True):
        return False

    platform = str(incident.get("platform") or "system").capitalize()
    error_type = incident.get("error_type") or "Incident"
    severity = str(incident.get("severity") or "critical").upper()
    subject = f"⚠️ [{severity}] {platform} Incident: {error_type}"

    html = _format_html_template(
        title=f"{platform} Incident Alert",
        badge_text=severity,
        badge_color="#FF3B30" if severity == "CRITICAL" else "#FF9500",
        headline=f"{platform} {error_type}",
        details_table=[
            ("Platform", platform),
            ("Error Type", error_type),
            ("Severity", severity),
            ("Message", incident.get("message") or ""),
            ("Cause", incident.get("cause") or ""),
        ],
        recommendation=_esc(str(incident.get("fix") or "Review the Live Incidents dashboard in the Brand Intelligence UI.")),
    )
    ok, _ = await send_email(subject, html)
    return ok
