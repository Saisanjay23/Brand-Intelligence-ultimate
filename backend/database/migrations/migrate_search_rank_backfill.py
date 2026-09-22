"""One-off migration: give every existing profile its `search_rank`.

WHAT THIS IS FOR. The discovery grid shows profiles in the order the
platform's own search returned them. It used to get that ordering for free
from ascending `_id`: MongoDB's ObjectId starts with an insertion
timestamp, and while ONE account swept ONE keyword at a time, "the order we
saved them" and "the order the platform listed them" were the same
sequence.

Sharding the keyword list across several pooled accounts ended that. Three
workers now write concurrently, so ascending `_id` interleaves three
keywords -- keyword A's third hit lands between keyword B's first and
keyword C's second, and the grid stops reflecting what any platform
actually returned. Sweeps therefore now RECORD each profile's position as
`search_rank` (see profile_repository.save), which no amount of concurrency
can scramble.

This fills that field in for the rows written before it existed.

WHY `_id` IS THE RIGHT SOURCE HERE, having just been called unreliable.
Every one of these rows was written BEFORE sharding was switched on, by a
single worker sweeping one keyword at a time. For that data insertion order
IS platform order, exactly as the old sort assumed -- so reading position
back out of `_id`, GROUPED BY KEYWORD, recovers the true ranking rather than
inventing one. It is the same information the grid was already using; this
just writes it down before the guarantee behind it goes away.

MIN ACROSS KEYWORDS, matching how live sweeps write it. 2.3% of profiles
were found by more than one keyword. `save()` records the best position a
profile reached (`$min`), so a profile that came first for one keyword is
not demoted because an unrelated keyword later found it fortieth. This
computes a rank per (platform, keyword) and keeps the lowest, so backfilled
rows and freshly swept ones mean the same thing.

CONSERVATIVE. Only rows with no usable `search_rank` are touched, and only
when a rank can actually be derived. A profile with no keywords recorded is
left alone rather than given a made-up position -- an absent rank sorts
with its `_id` tie-break exactly as it always did, which is the honest
outcome for a row nobody can place.

Idempotent: re-running finds nothing left to do.

Usage:
    python -m backend.database.migrations.migrate_search_rank_backfill --dry-run
    python -m backend.database.migrations.migrate_search_rank_backfill
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

from backend.config.settings import settings

BATCH = 500


async def migrate(dry_run: bool = False) -> None:
    client = AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    coll = client[settings.mongo_db_name]["profiles"]

    total_docs = await coll.count_documents({})
    already = await coll.count_documents({"search_rank": {"$gte": 1}})
    print(f"profiles: {total_docs}   already ranked: {already}")

    # POSITION WITHIN ONE KEYWORD'S RESULTS, which is what a rank means.
    # Walking the collection in `_id` order once and bucketing as we go is
    # what makes that grouping possible without sorting per keyword: each
    # bucket sees its own rows in the order they were saved, which for this
    # pre-sharding data is the order the platform listed them.
    seen: dict[tuple[str, str, str], int] = defaultdict(int)
    best: dict = {}
    scanned = 0

    cursor = coll.find(
        {}, {"_id": 1, "client_id": 1, "platform": 1, "keywords": 1, "search_rank": 1}
    ).sort("_id", 1)
    async for doc in cursor:
        scanned += 1
        keywords = [k for k in (doc.get("keywords") or []) if k]
        if not keywords:
            continue
        client_id = str(doc.get("client_id") or "")
        platform = str(doc.get("platform") or "")
        ranks = []
        for kw in keywords:
            key = (client_id, platform, str(kw))
            seen[key] += 1
            ranks.append(seen[key])
        rank = min(ranks)

        current = doc.get("search_rank")
        if isinstance(current, int) and 1 <= current <= rank:
            continue        # already ranked at least as well
        best[doc["_id"]] = rank

    print(f"scanned {scanned} row(s); {len(best)} need a rank written")
    if best:
        sample = list(best.items())[:8]
        print("\nsample (id -> rank):")
        for _id, r in sample:
            print(f"  {_id}  ->  #{r}")

    spread = Counter(min(r, 50) for r in best.values())
    if spread:
        print("\nrank distribution (capped at 50):")
        for r in sorted(spread)[:10]:
            print(f"  #{r:<3} {spread[r]} row(s)")

    if dry_run:
        print(f"\nDRY RUN -- nothing written. {len(best)} row(s) would be updated.")
        client.close()
        return

    written = 0
    ops: list[UpdateOne] = []
    for _id, rank in best.items():
        ops.append(UpdateOne({"_id": _id}, {"$set": {"search_rank": rank}}))
        if len(ops) >= BATCH:
            res = await coll.bulk_write(ops, ordered=False)
            written += res.modified_count
            ops = []
    if ops:
        res = await coll.bulk_write(ops, ordered=False)
        written += res.modified_count

    print(f"\nupdated {written} row(s).")
    client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()
    asyncio.run(migrate(args.dry_run))


if __name__ == "__main__":
    main()
