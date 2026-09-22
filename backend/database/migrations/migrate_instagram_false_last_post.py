"""One-off migration: retract Instagram last-post dates that were read off
somebody else's post, and send those rows back to be re-read.

WHAT WAS WRONG. Instagram's analysis engine could record the CURRENT DATE
as a profile's last-post date, including for accounts that had never posted
at all. Nothing was inventing a date -- it was reading a real timestamp off
content the profile does not own:

  * a profile visit also draws the VIEWER'S OWN home feed and
    recommendations, served on the same `api/graphql` path the engine
    watches, and the nested parts of those posts (carousel slides, clips
    metadata) carry a timestamp with no author beside them. The parser's
    "nobody is named, take it anyway" fallback took the newest of them,
    which is by construction from today;
  * the profile's own record carries timestamps that are not posts (a
    highlight cover, a tagged-media preview), and they were read even when
    Instagram had stated a media count of zero;
  * the page-reading fallback scanned the whole document, so a profile
    with no grid of its own was dated from Instagram's suggestion tiles.

All three are fixed in the engine (see instagram/analysis_engine.py's
module docstring). This is about the rows already written.

WHY IT MATTERS ENOUGH TO REWRITE STORED ROWS. A present-day date makes an
account ACTIVE, and activity feeds `risk_score` and `priority` -- so each
of these rows is sitting in an analyst's queue rated as a live
impersonation on the strength of a stranger's post. It also erased its own
contradiction: the buggy write set `posts_seen = "yes"` on the way past,
which is the one field that could afterwards have disputed the date.

WHAT THIS DOES, AND WHY IT IS NOT A DELETE. A suspect row has its date
CLEARED and is flagged `field_status.last_post_date = "MISSED"`, which is
the existing signal that puts a profile back in the analysis queue (see
profile_repository.py::_needs_analysis). So this does not assert "this
account never posted". It asserts "this date cannot be trusted, go and
read it again" -- and the corrected engine then writes the real answer, or
an honest blank.

That distinction is what makes the false-positive case safe. The
fingerprint below cannot separate "dated from a stranger's post today"
from "genuinely posted on the day it was scanned", because in the database
those look identical. An account that really did post that day gets its
date back on the next pass, at the cost of one re-read.

Risk and priority are recomputed for any ANALYSED row whose date changes,
through the same `compute_risk_score`/`compute_priority` the engines and
the analyst's own edits use -- so a stored score can never drift from the
field it was derived from.

WHICH ROWS. Instagram profiles carrying a last-post date, where either:

    the date equals the day the row was analysed -- the signature of the
    defect, since the stray timestamps all came from content published
    that same day; or

    `posts_seen` says "no" while a date is stored -- an outright
    self-contradiction, wrong whatever the date says.

Rows an ANALYST edited by hand are never touched: `sources.last_post`
reads "analyst" once somebody has corrected the field themselves, and
their answer outranks anything this script could work out.

Idempotent: re-running finds nothing left to change.

Usage:
    python -m backend.database.migrations.migrate_instagram_false_last_post --dry-run
    python -m backend.database.migrations.migrate_instagram_false_last_post
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

from backend.config.settings import settings
from backend.database.repositories.profile_repository import (compute_priority,
                                                              compute_risk_score)

BATCH = 500

# A hand-corrected field is the analyst's, not ours. `profile_repository
# .patch()` relabels provenance exactly so a manual correction can never be
# mistaken for scraped evidence -- this is the other half of that promise.
ANALYST_SOURCES = {"analyst", "manual"}


def _day(value: Any) -> str:
    """The YYYY-MM-DD a stored timestamp falls on, or "" if it is not one.

    `analysed_at` is a real datetime in Mongo but can come back as a string
    on documents written by older code paths, so both are accepted rather
    than letting one shape silently match nothing.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return ""


def _suspect(doc: dict) -> str:
    """Why this row's date cannot be trusted, or "" to leave it alone.

    Returns the REASON rather than a bool so the report can show the split:
    the two fingerprints are different failures and an operator should see
    how many of each they are about to correct.
    """
    last_post = (doc.get("last_post_date") or "").strip()
    if not last_post:
        return ""
    source = str((doc.get("sources") or {}).get("last_post") or "").lower()
    if source in ANALYST_SOURCES:
        return ""
    if (doc.get("posts_seen") or "") == "no":
        return "contradicts posts_seen=no"
    if last_post == _day(doc.get("analysed_at")):
        return "dated the day it was scanned"
    return ""


async def migrate(dry_run: bool) -> None:
    client = AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    coll = client[settings.mongo_db_name]["profiles"]

    docs = [
        d async for d in coll.find(
            {"platform": "instagram", "last_post_date": {"$nin": ["", None]}},
            {"last_post_date": 1, "posts_seen": 1, "analysed_at": 1, "sources": 1,
             "phase": 1, "has_logo": 1, "has_name_match": 1, "location": 1,
             "risk_score": 1, "priority": 1, "logo_match": 1, "username_match": 1,
             "status": 1, "url": 1},
        )
    ]
    print(f"scanning {len(docs)} Instagram profile(s) with a last-post date ...")

    reasons: Counter[str] = Counter()
    rescored = 0
    written = 0
    pending: list[UpdateOne] = []
    samples: list[tuple[str, str, str]] = []

    for doc in docs:
        reason = _suspect(doc)
        if not reason:
            continue
        reasons[reason] += 1
        if len(samples) < 10:
            samples.append((doc.get("url") or "?", doc.get("last_post_date") or "", reason))

        fields: dict[str, Any] = {
            "last_post_date": "",
            # The re-read signal, not a cosmetic label. This is what
            # profile_repository::_needs_analysis reads to put the profile
            # back in the queue, which is the whole point of the migration:
            # the date is retracted, not decided.
            "field_status.last_post_date": "MISSED",
            "sources.last_post": "retracted:unattributed",
        }

        # An analysed row's score was derived from this date, so it has to
        # move with it or the row contradicts itself. Unanalysed discovery
        # rows carry no score yet and only lose the date.
        if doc.get("phase") == "analysis" and doc.get("risk_score") is not None:
            # "approved" is the status an analyst's validation sets, and
            # `profile_repository.patch()` reads it exactly this way. The
            # two must not disagree about the same profile.
            validated = doc.get("status") == "approved"
            score = compute_risk_score(
                doc.get("has_logo", False), doc.get("has_name_match", False),
                doc.get("location"), "",
                doc.get("logo_match"), doc.get("username_match"), validated,
            )
            fields["risk_score"] = score
            fields["priority"] = compute_priority(
                doc.get("has_logo", False), score,
                doc.get("logo_match"), validated,
            )
            # With no date there is no evidence of a post in the window.
            # False, not None: shared/models/row.py::active_yes is explicit
            # that an undated row reads inactive rather than blank.
            fields["is_active"] = False
            rescored += 1

        pending.append(UpdateOne({"_id": doc["_id"]}, {"$set": fields}))
        if not dry_run and len(pending) >= BATCH:
            await coll.bulk_write(pending, ordered=False)
            written += len(pending)
            pending = []

    if pending and not dry_run:
        await coll.bulk_write(pending, ordered=False)
        written += len(pending)

    total = sum(reasons.values())
    print(f"\n{'why the date was retracted':38} {'rows':>8}")
    print("-" * 48)
    for reason in sorted(reasons):
        print(f"{reason:38} {reasons[reason]:>8}")
    print("-" * 48)
    print(f"{'TOTAL':38} {total:>8}\n")
    print(f"analysed rows also rescored: {rescored}")

    if samples:
        print("\nexamples:")
        for url, was, why in samples:
            print(f"  {was}  {why:32}  {url}")

    if dry_run:
        print(f"\nDRY RUN -- nothing written. {total} row(s) would be retracted "
              f"and re-queued for analysis.")
    else:
        print(f"\nupdated {written} row(s). They are now queued to have their "
              f"last-post date re-read.")

    client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()
    asyncio.run(migrate(args.dry_run))


if __name__ == "__main__":
    main()
