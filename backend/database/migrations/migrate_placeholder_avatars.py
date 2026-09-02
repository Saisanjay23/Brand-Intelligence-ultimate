"""One-off migration: correct `has_logo` on rows whose avatar is the
platform's own stock placeholder, and rescore them.

WHAT WAS WRONG. The engines decided "does this account use a real profile
picture" from URL patterns that had drifted out of date, so a platform's
own generic avatar was recorded as a real upload:

  * Facebook -- the grey silhouette and the illustrated default GROUP
    avatar, matched only by CDN path tag, which the silhouette does not
    carry.
  * Instagram -- the anonymous avatar, whose asset id Instagram rotated;
    the engine still checked only the old one.
  * YouTube -- generates a per-channel letter avatar served from the same
    host and URL shape as a real upload, which no URL rule can catch.

WHY IT MATTERS ENOUGH TO REWRITE STORED ROWS. `has_logo` is the heaviest
input to the risk rubric: a logo match alone outweighs location and
dormancy combined, and `Row.priority` returns High on it outright
regardless of score. Every one of these rows is therefore sitting in an
analyst's queue scored as though the account had taken the brand's picture.

`risk_score` and `priority` are recomputed for any ANALYSED row whose logo
verdict changes, through the same `compute_score` the engines use -- so the
stored score cannot drift from the corrected field. Discovery-phase rows
carry no score yet and only have `has_logo` corrected.

YouTube needs the image itself, so those rows are fetched (only the ones
whose URL carries the generated-avatar prefix -- a fraction of channels).
A fetch failure leaves the row untouched rather than guessing.

Idempotent: re-running finds nothing left to change.

Usage:
    python -m backend.database.migrations.migrate_placeholder_avatars --dry-run
    python -m backend.database.migrations.migrate_placeholder_avatars
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

import httpx
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

from backend.config.settings import settings
from backend.shared.avatars import (YOUTUBE_GENERATED_PREFIX,
                                    is_generated_avatar, looks_like_placeholder)
from backend.shared.models.scoring import compute_score

BATCH = 500
CONCURRENCY = 16


async def _is_placeholder(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                          platform: str, url: str) -> bool:
    """True only where the evidence settles it; False otherwise, so an
    unreachable image leaves the row exactly as it is."""
    if looks_like_placeholder(platform, url):
        return True
    if platform != "youtube" or YOUTUBE_GENERATED_PREFIX not in (url or ""):
        return False
    async with sem:
        try:
            r = await client.get(url)
            if r.status_code != 200:
                return False
            return is_generated_avatar("youtube", url, r.content) is True
        except Exception:
            return False


async def migrate(dry_run: bool) -> None:
    client = AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    coll = client[settings.mongo_db_name]["profiles"]

    docs = [
        d async for d in coll.find(
            {"profile_image_url": {"$nin": ["", None]}},
            {"profile_image_url": 1, "platform": 1, "has_logo": 1, "phase": 1,
             "has_name_match": 1, "location": 1, "last_post_date": 1,
             "risk_score": 1, "priority": 1},
        )
    ]
    print(f"scanning {len(docs)} profile(s) with an avatar ...")

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as http:
        verdicts = await asyncio.gather(*(
            _is_placeholder(http, sem, d.get("platform") or "", d["profile_image_url"])
            for d in docs
        ))

    fixed: Counter[str] = Counter()
    rescored = 0
    pending: list[UpdateOne] = []
    written = 0

    for doc, is_ph in zip(docs, verdicts):
        if not is_ph or doc.get("has_logo") is False:
            continue
        platform = doc.get("platform") or "?"
        fixed[platform] += 1
        fields: dict = {"has_logo": False}

        # An analysed row carries a score derived from has_logo, so it must
        # move with it -- otherwise the row contradicts itself.
        if doc.get("phase") == "analysis" and doc.get("risk_score") is not None:
            new_score = compute_score(
                has_logo=False,
                has_name_match=bool(doc.get("has_name_match")),
                has_location=bool((doc.get("location") or "").strip()),
                last_post_iso=doc.get("last_post_date") or "",
            )
            fields["risk_score"] = new_score
            fields["priority"] = "High" if new_score >= 5 else "Low"
            rescored += 1

        pending.append(UpdateOne({"_id": doc["_id"]}, {"$set": fields}))
        if not dry_run and len(pending) >= BATCH:
            await coll.bulk_write(pending, ordered=False)
            written += len(pending)
            pending = []

    if pending and not dry_run:
        await coll.bulk_write(pending, ordered=False)
        written += len(pending)

    total = sum(fixed.values())
    print(f"\n{'platform':12} {'stock avatars read as a real picture':>38}")
    print("-" * 52)
    for platform in sorted(fixed):
        print(f"{platform:12} {fixed[platform]:>38}")
    print("-" * 52)
    print(f"{'TOTAL':12} {total:>38}\n")
    print(f"analysed rows also rescored: {rescored}")

    if dry_run:
        print(f"\nDRY RUN -- nothing written. {total} row(s) would be corrected.")
    else:
        print(f"\nupdated {written} row(s).")

    client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()
    asyncio.run(migrate(args.dry_run))


if __name__ == "__main__":
    main()
