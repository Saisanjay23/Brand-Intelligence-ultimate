"""Alert and notification settings persistence.

Stores email alert recipients, SMTP configuration, and notification triggers
in the `alert_settings` MongoDB collection, with seamless fallback to environment
variables in `backend.config.settings.Settings`.
"""

from __future__ import annotations

from typing import Any
from datetime import datetime, timezone

from backend.config.settings import settings
from backend.database.connection import db
from backend.shared.logging import get_logger

log = get_logger("repositories.alert_settings")

SETTINGS_COLLECTION = "alert_settings"
SETTINGS_DOC_ID = "global_alert_settings"


def _default_settings() -> dict[str, Any]:
    return {
        "_id": SETTINGS_DOC_ID,
        "alert_emails": list(settings.alert_emails or []),
        "smtp_host": settings.smtp_host or "",
        "smtp_port": settings.smtp_port or 1025,
        "smtp_user": settings.smtp_user or "",
        "smtp_pass": settings.smtp_pass or "",
        "alert_from": settings.alert_from or "alerts@brand-intelligence.local",
        "smtp_ssl": (settings.smtp_port == 465),
        "alert_on_session_dead": True,
        "alert_on_session_expiring": True,
        "alert_on_critical_incident": True,
        # OFF by default, deliberately. A sweep finishing is a routine event
        # that happens many times a day; turning this on without the operator
        # asking would turn a feature into a mail flood on the first sweep
        # after an upgrade.
        "report_on_sweep_complete": False,
        # Falls back to `alert_emails` when empty, so reports can either
        # share the alert recipients or go to a different list.
        "report_emails": [],
        "session_expiry_warning_hours": 24,
        "updated_at": datetime.now(timezone.utc),
    }


async def get_settings() -> dict[str, Any]:
    """Retrieve the global alert settings, or the environment defaults if not yet saved."""
    try:
        doc = await db()[SETTINGS_COLLECTION].find_one({"_id": SETTINGS_DOC_ID})
        if doc:
            doc.pop("_id", None)
            return doc
    except Exception as e:
        log.warning(f"failed to read alert settings from DB, falling back to env: {e}")

    defaults = _default_settings()
    defaults.pop("_id", None)
    return defaults


async def save_settings(fields: dict[str, Any]) -> dict[str, Any]:
    """Update global alert settings in MongoDB."""
    allowed = {
        "report_on_sweep_complete",
        "report_emails",
        "alert_emails",
        "smtp_host",
        "smtp_port",
        "smtp_user",
        "smtp_pass",
        "alert_from",
        "smtp_ssl",
        "alert_on_session_dead",
        "alert_on_session_expiring",
        "alert_on_critical_incident",
        "session_expiry_warning_hours",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    # Preserve existing password if caller passed masked placeholder or empty string when existing pass exists
    if updates.get("smtp_pass") in ("••••••••", ""):
        current = await get_settings()
        if current.get("smtp_pass"):
            updates["smtp_pass"] = current["smtp_pass"]

    updates["updated_at"] = datetime.now(timezone.utc)

    await db()[SETTINGS_COLLECTION].update_one(
        {"_id": SETTINGS_DOC_ID},
        {"$set": updates},
        upsert=True,
    )
    return await get_settings()
