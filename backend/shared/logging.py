"""Structured logging: JSON records to stdout and a JSONL audit file.

Audit trails on disk expire weekly (7 days retention by default) to keep disk
usage bounded and comply with operational data retention limits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from backend.config.settings import settings

_configured = False
_last_prune_time: float = 0.0
_log_retention_task: Optional[asyncio.Task] = None
_PRUNE_INTERVAL_SECONDS = 12 * 3600  # Prune check every 12 hours


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            entry[key] = value
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def prune_logs(days: int = 7, log_path: Optional[Path] = None) -> int:
    """Prunes log records and rotated files older than `days` (default 7 days / weekly).

    Returns total number of lines pruned from active JSONL files.
    """
    target_dir = log_path or settings.log_path
    if not target_dir.exists():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pruned_count = 0

    # 1. Prune brand_intel.jsonl records older than cutoff
    main_log = target_dir / "brand_intel.jsonl"
    if main_log.is_file():
        tmp_path = target_dir / f"brand_intel_{os.getpid()}_{int(time.time() * 1000)}.tmp"
        file_pruned = 0
        try:
            with main_log.open("r", encoding="utf-8", errors="replace") as fin, \
                 tmp_path.open("w", encoding="utf-8") as fout:
                for line in fin:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                        ts_str = entry.get("ts")
                        if ts_str:
                            ts_dt = datetime.fromisoformat(ts_str)
                            if ts_dt.tzinfo is None:
                                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                            if ts_dt < cutoff:
                                file_pruned += 1
                                continue
                    except Exception:
                        pass
                    fout.write(raw + "\n")

            if file_pruned > 0:
                # Windows safe replacement
                try:
                    tmp_path.replace(main_log)
                except Exception:
                    # Retry with temporary copy if file was briefly locked
                    time.sleep(0.05)
                    tmp_path.replace(main_log)
                pruned_count += file_pruned
            else:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    # 2. Delete any rotated / backup log files older than cutoff
    cutoff_ts = cutoff.timestamp()
    try:
        for file in target_dir.iterdir():
            if file.is_file() and file.name != "brand_intel.jsonl":
                if file.suffix in {".jsonl", ".log", ".tmp"} or ".jsonl." in file.name:
                    try:
                        if file.stat().st_mtime < cutoff_ts:
                            file.unlink(missing_ok=True)
                    except Exception:
                        pass
    except Exception:
        pass

    return pruned_count


class _JsonLinesFile(logging.Handler):
    """Best-effort audit trail on disk, logging must never take down a job.
    Prunes expired logs weekly (7 days).
    """

    def emit(self, record: logging.LogRecord) -> None:
        global _last_prune_time
        try:
            settings.log_path.mkdir(parents=True, exist_ok=True)
            line = self.format(record)
            with (settings.log_path / "brand_intel.jsonl").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write(line + "\n")

            now = time.time()
            if now - _last_prune_time > _PRUNE_INTERVAL_SECONDS:
                _last_prune_time = now
                retention = getattr(settings, "log_retention_days", 7)
                prune_logs(days=retention, log_path=settings.log_path)
        except Exception:
            pass


async def _log_retention_loop() -> None:
    while True:
        try:
            retention = getattr(settings, "log_retention_days", 7)
            prune_logs(days=retention, log_path=settings.log_path)
        except Exception:
            pass
        await asyncio.sleep(24 * 3600)


def start_log_retention_monitor() -> None:
    global _log_retention_task
    if _log_retention_task is None or _log_retention_task.done():
        try:
            loop = asyncio.get_running_loop()
            _log_retention_task = loop.create_task(_log_retention_loop())
        except RuntimeError:
            pass


def stop_log_retention_monitor() -> None:
    global _log_retention_task
    if _log_retention_task is not None:
        _log_retention_task.cancel()
        _log_retention_task = None


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger("bi")
    root.setLevel(logging.INFO)
    formatter = _JsonFormatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    file_handler = _JsonLinesFile()
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _configured = True

    try:
        retention = getattr(settings, "log_retention_days", 7)
        prune_logs(days=retention, log_path=settings.log_path)
    except Exception:
        pass


def get_logger(name: str) -> logging.Logger:
    if not _configured:
        configure_logging()
    return logging.getLogger(f"bi.{name}")


def log_extra(**fields) -> dict:
    """`log.info("saved profile", extra=log_extra(platform="facebook", n=3))`"""
    return {"extra_fields": fields}

