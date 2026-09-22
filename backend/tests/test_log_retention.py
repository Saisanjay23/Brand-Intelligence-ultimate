"""Tests for weekly log expiration across application audit logs and incident logs."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.config.settings import settings
from backend.database.repositories import incident_repository as incidents_db
from backend.shared.logging import prune_logs


def test_settings_retention_defaults():
    """Weekly expiration defaults to 7 days across settings and incident repository."""
    assert settings.log_retention_days == 7
    assert settings.incident_retention_days == 7
    assert incidents_db.RETENTION_DAYS == 7


def test_prune_logs_removes_records_older_than_7_days(tmp_path: Path):
    """Log lines older than 7 days are stripped, while newer entries are preserved."""
    log_file = tmp_path / "brand_intel.jsonl"
    now = datetime.now(timezone.utc)

    entries = []
    # 5 entries older than 7 days (e.g. 10 to 14 days ago)
    for i in range(10, 15):
        past_ts = (now - timedelta(days=i)).isoformat()
        entries.append(json.dumps({"ts": past_ts, "msg": f"old log {i}"}))

    # 5 entries within the last 7 days (e.g. 1 to 5 days ago)
    for i in range(1, 6):
        recent_ts = (now - timedelta(days=i)).isoformat()
        entries.append(json.dumps({"ts": recent_ts, "msg": f"recent log {i}"}))

    # 1 entry without ts or malformed
    entries.append(json.dumps({"msg": "no timestamp entry"}))

    log_file.write_text("\n".join(entries) + "\n", encoding="utf-8")

    pruned = prune_logs(days=7, log_path=tmp_path)
    assert pruned == 5

    remaining_lines = [line.strip() for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(remaining_lines) == 6

    # Verify all remaining entries with timestamp are >= 7 days cutoff
    cutoff = now - timedelta(days=7)
    for line in remaining_lines:
        data = json.loads(line)
        if "ts" in data:
            dt = datetime.fromisoformat(data["ts"])
            assert dt >= cutoff


def test_prune_logs_deletes_old_backup_files(tmp_path: Path):
    """Any old rotated/backup log files older than 7 days are pruned."""
    old_file = tmp_path / "brand_intel_backup_old.jsonl"
    recent_file = tmp_path / "brand_intel_backup_new.jsonl"

    old_file.write_text("old", encoding="utf-8")
    recent_file.write_text("new", encoding="utf-8")

    ten_days_ago = time.time() - (10 * 86400)
    one_day_ago = time.time() - (1 * 86400)

    os.utime(old_file, (ten_days_ago, ten_days_ago))
    os.utime(recent_file, (one_day_ago, one_day_ago))

    prune_logs(days=7, log_path=tmp_path)

    assert not old_file.exists()
    assert recent_file.exists()


def test_prune_logs_handles_missing_or_empty_path(tmp_path: Path):
    """Missing or empty log paths do not raise exceptions."""
    non_existent = tmp_path / "does_not_exist"
    assert prune_logs(days=7, log_path=non_existent) == 0

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert prune_logs(days=7, log_path=empty_dir) == 0


@pytest.mark.asyncio
async def test_incident_purge_expired():
    """Incidents older than retention days are purged from MongoDB."""
    mock_coll = AsyncMock()
    mock_coll.delete_many.return_value.deleted_count = 12

    with patch("backend.database.repositories.incident_repository.db") as mock_db:
        mock_db.return_value.__getitem__.return_value = mock_coll
        deleted = await incidents_db.purge_expired(days=7)
        assert deleted == 12
        assert mock_coll.delete_many.called
        call_arg = mock_coll.delete_many.call_args[0][0]
        assert "ts" in call_arg
        assert "$lt" in call_arg["ts"]


@pytest.mark.asyncio
async def test_incident_ensure_indexes_recreates_ttl_if_mismatched():
    """ensure_indexes drops and recreates ttl_ts if expireAfterSeconds was modified."""
    mock_coll = AsyncMock()
    # Simulate old 14-day index (14 * 86400 = 1209600)
    mock_coll.index_information.return_value = {
        "ttl_ts": {"key": [("ts", 1)], "expireAfterSeconds": 14 * 86400}
    }

    with patch("backend.database.repositories.incident_repository.db") as mock_db:
        mock_db.return_value.__getitem__.return_value = mock_coll
        await incidents_db.ensure_indexes()
        # Should drop old index and recreate with 7 days (604800s)
        mock_coll.drop_index.assert_called_with("ttl_ts")
        mock_coll.create_index.assert_called_with("ts", expireAfterSeconds=7 * 86400, name="ttl_ts")
