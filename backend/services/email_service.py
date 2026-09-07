"""Asynchronous email notification service for Brand Intelligence alerts.

Sends alerts for session failures, upcoming token expirations, and critical incidents
via SMTP using non-blocking asyncio worker threads.
"""

from __future__ import annotations

import asyncio
import smtplib
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
    """Renders a sleek, responsive HTML email matching enterprise security alerts."""
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
                        log.debug(f"STARTTLS negotiation skipped/failed: {tls_err}")
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

    return await asyncio.to_thread(
        _send_smtp_sync, host, port, user, password, sender, recipients, msg, use_ssl
    )


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
        recommendation=f"Navigate to <strong>Admin → Sessions</strong> in the application, locate the <strong>{plat_display}</strong> session pool, and paste freshly exported cookies to restore automated scraping.",
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
        recommendation=f"Re-export fresh cookies from your browser for <strong>{plat_display} ({identifier})</strong> and paste them in <strong>Admin → Sessions</strong> before automated jobs encounter a login wall.",
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
        recommendation=incident.get("fix") or "Review the Live Incidents dashboard in the Brand Intelligence UI.",
    )
    ok, _ = await send_email(subject, html)
    return ok
