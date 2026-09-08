"""One-off migration: `session/<platform>.json` cookie pool files -> the
`sessions` collection in the current single `brand_intel` Mongo database.

Read-only against the files, nothing on disk is modified or deleted.
Telegram's `.session` blob is NOT touched: it stays a file by design
(Telethon owns that format; see `platforms/registry.py`'s `session_blob`
field).

Idempotent: skips any (platform, cookie names) combination that already
matches an existing pooled session's cookie names, rather than blindly
appending a duplicate on every run.

Never logs a cookie value, only counts and session identifiers. This
migrates live credentials; treat its output review the same way you would
a credentials file.

Usage:
    python -m backend.database.migrations.migrate_sessions_to_mongo --dry-run
    python -m backend.database.migrations.migrate_sessions_to_mongo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from backend.database.repositories import session_repository as sessions_db
from backend.sessions.cookies import normalize_cookies
from backend.platforms.registry import PLATFORMS
from backend.config.settings import settings


def _read_pool_file(path) -> list[dict]:
    """-> a list of {identifier, cookies} entries, handling both the
    legacy flat-array cookie export and the pool-file shape the app itself
    wrote (`{"version": 2, "sessions": [...]}`)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    obj = json.loads(text)
    if isinstance(obj, list):
        return [{"identifier": "Legacy Session", "cookies": obj}]
    if isinstance(obj, dict) and obj.get("version") == 2:
        return [
            {"identifier": s.get("identifier", s.get("id", "Session")), "cookies": s.get("cookies", [])}
            for s in obj.get("sessions", [])
        ]
    return []


async def migrate(dry_run: bool) -> None:
    for platform_id, p in PLATFORMS.items():
        if not p.uses_cookies:
            continue
        path = settings.session_blob_path / f"{platform_id}.json"
        if not path.exists() or path.stat().st_size == 0:
            print(f"{platform_id}: no cookie file at {path}, skipping")
            continue

        try:
            entries = _read_pool_file(path)
        except Exception as e:
            print(f"{platform_id}: could not parse {path}: {type(e).__name__}: {e}")
            continue
        if not entries:
            print(f"{platform_id}: {path} has no sessions, skipping")
            continue

        existing = await sessions_db.list_pool(platform_id)
        existing_name_sets = {frozenset(c["name"] for c in s["cookies"]) for s in existing}

        migrated = skipped_dupe = 0
        for entry in entries:
            cookies = entry["cookies"]
            if cookies and not all("domain" in c for c in cookies):
                cookies = normalize_cookies(cookies, p.cookie_domain)
            if not cookies:
                continue
            name_set = frozenset(c["name"] for c in cookies)
            if name_set in existing_name_sets:
                skipped_dupe += 1
                continue

            migrated += 1
            print(
                f"{platform_id}: {'[dry-run] would add' if dry_run else 'adding'} "
                f"session {entry['identifier']!r} ({len(cookies)} cookies)"
            )
            if not dry_run:
                await sessions_db.add_item(platform_id, cookies, entry["identifier"])

        print(f"{platform_id}: {migrated} session(s) migrated, {skipped_dupe} already present")

    print(
        "\nTelegram's MTProto session file (session/telegram.session) was NOT "
        "touched -- it stays a file both engines read from the same path; "
        "see platforms/registry.py's `session_blob` field."
    )
    print("\ndry run complete -- nothing written." if dry_run else "\nmigration complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = parser.parse_args()
    try:
        asyncio.run(migrate(args.dry_run))
    except KeyboardInterrupt:
        sys.exit(1)
