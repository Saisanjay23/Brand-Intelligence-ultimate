"""One-off migration: put the real handle in `profiles.username`.

WHAT WAS WRONG. `Row` carried only `profile_id` (the platform's internal
id), and discovery's `row_to_fields` mapped that straight into the
`username` column. So every row ever written stored the id as the handle --
an Instagram profile whose handle is `defnce.app` was saved, and rendered
in the UI, as `@50840430092`. It also reached the CSV/clipboard export and
the `username` half of the profile search.

The engines now capture the handle properly (see `Row.username` and
shared/text.py::handle_from_url). This backfills the rows written before
that, recovering the handle from each row's own `url` -- the same place
`handle_from_url` reads it for Facebook and YouTube at write time, and a
field this migration never modifies.

CONSERVATIVE BY DESIGN. A row is only rewritten when its URL actually
yields a handle and that handle differs from what is stored. Rows whose URL
carries no handle (`facebook.com/profile.php?id=N`, `t.me/c/<id>`,
`youtube.com/channel/UC...`) are left exactly as they are -- the id is the
correct fallback there, and blanking it would lose information. `username`
is not part of either unique index (those are client_id+platform+url and
client_id+platform+entity_id), so nothing here can affect deduplication.

Idempotent: re-running it finds nothing left to change.

Usage:
    python -m backend.database.migrations.migrate_username_backfill --dry-run
    python -m backend.database.migrations.migrate_username_backfill
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

from backend.config.settings import settings
from backend.shared.text import handle_from_url

BATCH = 500


async def migrate(dry_run: bool) -> None:
    client = AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    coll = client[settings.mongo_db_name]["profiles"]

    scanned = 0
    per_platform: Counter[str] = Counter()
    unchanged: Counter[str] = Counter()
    samples: list[tuple[str, str, str]] = []
    pending: list[UpdateOne] = []
    written = 0

    async for doc in coll.find({}, {"url": 1, "username": 1, "platform": 1}):
        scanned += 1
        platform = doc.get("platform") or "?"
        handle = handle_from_url(doc.get("url") or "")
        current = doc.get("username") or ""
        if not handle or handle == current:
            unchanged[platform] += 1
            continue
        per_platform[platform] += 1
        if len(samples) < 12:
            samples.append((platform, current, handle))
        pending.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"username": handle}}))
        if not dry_run and len(pending) >= BATCH:
            await coll.bulk_write(pending, ordered=False)
            written += len(pending)
            pending = []

    if pending and not dry_run:
        await coll.bulk_write(pending, ordered=False)
        written += len(pending)

    total = sum(per_platform.values())
    print(f"scanned {scanned} profile(s)\n")
    print(f"{'platform':12} {'to fix':>8} {'already correct / no handle':>30}")
    print("-" * 52)
    for plat in sorted(set(per_platform) | set(unchanged)):
        print(f"{plat:12} {per_platform[plat]:>8} {unchanged[plat]:>30}")
    print("-" * 52)
    print(f"{'TOTAL':12} {total:>8} {sum(unchanged.values()):>30}\n")

    if samples:
        print("sample rewrites (stored -> recovered):")
        for plat, current, handle in samples:
            print(f"  {plat:10} {current!r:24} -> {handle!r}")
        print()

    if dry_run:
        print(f"DRY RUN -- nothing written. {total} row(s) would be updated.")
    else:
        print(f"updated {written} row(s).")

    client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()
    asyncio.run(migrate(args.dry_run))


if __name__ == "__main__":
    main()
