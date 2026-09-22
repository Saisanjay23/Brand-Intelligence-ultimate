"""One-off migration: give stored YouTube channels their @handle URL.

WHAT WAS WRONG. Every YouTube profile was stored as
`https://www.youtube.com/channel/UCAOnNAx9wF8tkPtKH1a8VUw` -- correct, and
useless. An analyst cannot hold that against a brand name, recognise it on
a card, or tell two of them apart. The channel's public identity, the one
its own page displays and its share button copies, is
`https://www.youtube.com/@NewGautamAdani-q3o`.

The cause was not a choice. `search.list`, the one call a sweep makes,
returns a channel id and a snippet and no handle at all -- the handle lives
on `snippet.customUrl`, which only `channels.list` returns. Discovery now
spends one extra unit per search page to get it (see
platforms/youtube/discovery_engine.py). This is about the rows already
stored.

ANALYSIS EXPORTS ARE ALREADY CORRECT WITHOUT THIS. The export prints the
URL the analysis run resolved, and that run reads `customUrl` itself -- so
a stored `/channel/...` row already exports as the handle the next time it
is analysed. This migration is about what an analyst SEES before that: the
discovery cards, the profile list, and the link they click.

HOW THE HANDLE IS FOUND. `channels.list`, batched 50 ids per call for a
single quota unit, using each row's stored `entity_id`. A channel the API
will not answer for -- deleted, suspended, or simply past today's quota --
is left exactly as it is rather than guessed at, and counted in the report
so the number is visible instead of silently absorbed.

WHY REWRITING A URL IS SAFE HERE, since a URL is an identity and not a
label. `entity_id` is untouched and remains the UC id, which is what
`profile_repository` deduplicates on -- it matches entity_id first and
keeps every URL shape a profile has been seen under in a `urls` array. The
old URL is added to that array rather than dropped, so an inbound
reference to the id form still resolves to this document.

THE ONE CASE THIS REFUSES. `(client_id, platform, url)` is a UNIQUE index.
If a client already holds a separate document at the handle URL, writing
this one there would collide -- so it is skipped and reported by URL,
never merged. Merging two profile documents means reconciling an analyst's
triage decisions on both, which is a different job than backfilling a URL
and does not belong in a script whose whole promise is that it only
rewrites a link.

Idempotent: re-running finds nothing left to change.

Usage:
    python -m backend.database.migrations.migrate_youtube_handle_urls --dry-run
    python -m backend.database.migrations.migrate_youtube_handle_urls
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

from backend.config.settings import settings
from backend.platforms.youtube.discovery_engine import (QuotaExceeded,
                                                        YouTubeAPI,
                                                        channel_url)

BATCH = 500
# channels.list takes up to 50 ids per call and charges one unit for the
# call, not per id -- so this is the whole cost knob and it is already at
# its cheapest setting.
IDS_PER_CALL = 50


async def _handles(api: YouTubeAPI, ids: list[str]) -> dict[str, str]:
    """`{channel id: handle}` for every id the API answers for.

    Stops at the first quota refusal rather than hammering a key that has
    already said no: whatever resolved before that point is still returned
    and still written, and the remaining rows are left for the next run.
    """
    out: dict[str, str] = {}
    for i in range(0, len(ids), IDS_PER_CALL):
        chunk = ids[i:i + IDS_PER_CALL]
        try:
            items = await api.channels(chunk)
        except QuotaExceeded as e:
            print(f"\nquota exhausted after {len(out)} handle(s): {e}")
            print("re-run tomorrow -- this migration is idempotent and will "
                  "pick up where it stopped.")
            break
        except Exception as e:                       # noqa: BLE001
            print(f"\nlookup failed for a batch of {len(chunk)} "
                  f"({type(e).__name__}: {e}) -- those rows are left unchanged")
            continue
        for ch in items:
            cid = ch.get("id") or ""
            custom = str(((ch.get("snippet") or {}).get("customUrl")) or "").strip()
            if cid and custom:
                out[cid] = custom.lstrip("@").strip()
        print(f"  resolved {len(out)}/{len(ids)} ...", end="\r", flush=True)
    return out


async def migrate(dry_run: bool) -> None:
    client = AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    coll = client[settings.mongo_db_name]["profiles"]

    docs = [
        d async for d in coll.find(
            {"platform": "youtube", "url": {"$regex": r"/channel/UC"}},
            {"url": 1, "entity_id": 1, "username": 1, "client_id": 1, "urls": 1},
        )
    ]
    print(f"{len(docs)} YouTube profile(s) still stored under a /channel/ URL")
    if not docs:
        client.close()
        return

    ids = sorted({(d.get("entity_id") or "").strip() for d in docs} - {""})
    print(f"resolving {len(ids)} channel id(s), "
          f"{-(-len(ids) // IDS_PER_CALL)} API call(s) ...")

    api = YouTubeAPI()
    handle_by_id = await _handles(api, ids)
    print(f"  resolved {len(handle_by_id)}/{len(ids)} handles      ")

    outcome: Counter[str] = Counter()
    pending: list[UpdateOne] = []
    written = 0
    collisions: list[str] = []

    for doc in docs:
        cid = (doc.get("entity_id") or "").strip()
        handle = handle_by_id.get(cid, "")
        if not handle:
            # No handle, or the API would not answer. Either way there is
            # nothing truer to write than what is already there.
            outcome["no handle available -- left as is"] += 1
            continue

        new_url = channel_url(cid, handle)
        old_url = doc.get("url") or ""
        if new_url == old_url:
            outcome["already correct"] += 1
            continue

        clash = await coll.find_one(
            {"client_id": doc.get("client_id"), "platform": "youtube",
             "url": new_url, "_id": {"$ne": doc["_id"]}},
            {"_id": 1},
        )
        if clash is not None:
            # See the module docstring: the unique index would reject this,
            # and merging two profiles is not this script's job.
            outcome["skipped -- another document already holds that URL"] += 1
            if len(collisions) < 10:
                collisions.append(new_url)
            continue

        pending.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": {"url": new_url, "username": handle},
             # BOTH shapes kept. An inbound reference to the id URL still
             # has to resolve to this document -- `save()` looks the
             # profile up by `urls` as well as by `url`.
             "$addToSet": {"urls": {"$each": [old_url, new_url]}}},
        ))
        outcome["rewritten to the @handle URL"] += 1

        if not dry_run and len(pending) >= BATCH:
            await coll.bulk_write(pending, ordered=False)
            written += len(pending)
            pending = []

    if pending and not dry_run:
        await coll.bulk_write(pending, ordered=False)
        written += len(pending)

    print(f"\n{'outcome':52} {'rows':>7}")
    print("-" * 61)
    for key in sorted(outcome):
        print(f"{key:52} {outcome[key]:>7}")
    print("-" * 61)
    print(f"{'TOTAL':52} {sum(outcome.values()):>7}")

    if collisions:
        print("\nURLs already held by another document (not merged):")
        for url in collisions:
            print(f"  {url}")

    fixed = outcome["rewritten to the @handle URL"]
    if dry_run:
        print(f"\nDRY RUN -- nothing written. {fixed} row(s) would be rewritten.")
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
