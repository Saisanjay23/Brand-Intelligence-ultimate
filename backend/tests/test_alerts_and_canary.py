"""Tests for Alert Settings, Email Service, Session Canary Watchdog, and Alerts API."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from backend.database.repositories import alert_settings_repository as alert_settings_db
from backend.database.repositories import incident_repository as incidents_db
from backend.services import email_service
from backend.services import session_canary_service


def test_email_html_formatting():
    html = email_service._format_html_template(
        title="Session Dead",
        badge_text="CRITICAL",
        badge_color="#FF3B30",
        headline="Facebook Session Checkpointed",
        details_table=[
            ("Platform", "Facebook"),
            ("Account", "analyst_1"),
            ("Status", "checkpointed"),
        ],
        recommendation="Paste new cookies in Sessions panel.",
    )
    assert "Facebook Session Checkpointed" in html
    assert "Paste new cookies in Sessions panel." in html
    assert "analyst_1" in html
    assert "#FF3B30" in html


@pytest.mark.asyncio
async def test_alert_settings_defaults_and_save():
    fake_store = {}

    mock_coll = MagicMock()

    async def fake_find_one(query):
        return fake_store.get(query.get("_id"))

    async def fake_update_one(query, update, upsert=False):
        doc = fake_store.setdefault(query["_id"], {"_id": query["_id"]})
        if "$set" in update:
            doc.update(update["$set"])
        return MagicMock(acknowledged=True)

    mock_coll.find_one = AsyncMock(side_effect=fake_find_one)
    mock_coll.update_one = AsyncMock(side_effect=fake_update_one)

    with patch("backend.database.repositories.alert_settings_repository.db") as mock_db:
        mock_db.return_value = {alert_settings_db.SETTINGS_COLLECTION: mock_coll}

        # Defaults
        defs = await alert_settings_db.get_settings()
        assert defs["alert_on_session_dead"] is True
        assert "alert_emails" in defs

        # Save
        test_emails = ["secops@cyfirma.com", "analyst@cyfirma.com"]
        saved = await alert_settings_db.save_settings({
            "alert_emails": test_emails,
            "smtp_host": "smtp.test.local",
            "smtp_port": 587,
        })
        assert saved["alert_emails"] == test_emails
        assert saved["smtp_host"] == "smtp.test.local"
        assert saved["smtp_port"] == 587


@pytest.mark.asyncio
async def test_email_send_without_host_fails_gracefully():
    with patch.object(alert_settings_db, "get_settings", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {
            "alert_emails": ["test@example.com"],
            "smtp_host": "",
        }
        ok, msg = await email_service.send_email("Subject", "<h1>Content</h1>")
        assert ok is False
        assert "not configured" in msg.lower()


@pytest.mark.asyncio
async def test_incident_record_delete_and_clear():
    fake_incidents = {}

    mock_coll = MagicMock()

    async def fake_insert_one(doc):
        from bson import ObjectId
        oid = ObjectId()
        doc_copy = dict(doc)
        doc_copy["_id"] = oid
        fake_incidents[str(oid)] = doc_copy
        return MagicMock(inserted_id=oid)

    async def fake_delete_one(query):
        key = str(query.get("_id"))
        if key in fake_incidents:
            del fake_incidents[key]
            return MagicMock(deleted_count=1)
        return MagicMock(deleted_count=0)

    async def fake_delete_many(query):
        count = len(fake_incidents)
        fake_incidents.clear()
        return MagicMock(deleted_count=count)

    mock_coll.insert_one = AsyncMock(side_effect=fake_insert_one)
    mock_coll.delete_one = AsyncMock(side_effect=fake_delete_one)
    mock_coll.delete_many = AsyncMock(side_effect=fake_delete_many)

    with patch("backend.database.repositories.incident_repository.db") as mock_db:
        mock_db.return_value = {incidents_db.INCIDENTS: mock_coll}

        doc = {
            "platform": "instagram",
            "kind": "session_canary",
            "job_id": "test-job",
            "error_type": "SessionExpiringSoon",
            "severity": "warning",
            "message": "Instagram cookies expire in 6 hours",
            "ts": datetime.now(timezone.utc),
        }
        await incidents_db.record(doc)
        assert len(fake_incidents) == 1
        oid_key = next(iter(fake_incidents.keys()))

        # Delete single incident
        deleted = await incidents_db.delete_incident(oid_key)
        assert deleted is True
        assert len(fake_incidents) == 0

        # Clear all
        cleared = await incidents_db.clear_all()
        assert cleared == 0


@pytest.mark.asyncio
async def test_canary_token_expiry_detection():
    now = datetime.now(timezone.utc).timestamp()
    soon_exp = now + 6 * 3600  # 6 hours from now

    fake_pool = [
        {
            "id": "test_fb_session",
            "identifier": "fb_bot_1",
            "status": "ready",
            "cookies": [
                {"name": "c_user", "value": "12345", "expires": soon_exp},
                {"name": "xs", "value": "secret", "expires": soon_exp},
            ],
        }
    ]

    with patch("backend.database.repositories.session_repository.list_pool", new_callable=AsyncMock) as mock_pool, \
         patch("backend.services.email_service.send_session_expiring_alert", new_callable=AsyncMock) as mock_alert, \
         patch("backend.database.repositories.incident_repository.record", new_callable=AsyncMock) as mock_rec:
        mock_pool.return_value = fake_pool
        mock_alert.return_value = True

        session_canary_service._recent_alerts.clear()

        warnings = await session_canary_service.check_token_expiries()
        fb_warns = [w for w in warnings if w["platform"] == "facebook"]
        assert len(fb_warns) == 1
        assert fb_warns[0]["identifier"] == "fb_bot_1"
        assert 5.0 <= fb_warns[0]["remaining_hours"] <= 6.5
        mock_alert.assert_called_once()
        mock_rec.assert_called_once()
