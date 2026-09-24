"""One-off migration: re-judge every stored logo verdict from the picture
we already hold, and record how strong each verdict is.

WHAT WAS WRONG. Measured on this deployment (2026-09-24): 187 Facebook
profiles wear one of 48 generated letter avatars, the pixel check recognises
all 48, and 180 of those profiles were stored as "Logo: Yes". The avatar
cache HAD written "No" for them -- and the next sweep to re-find each
profile wrote the URL rule's "Yes" straight back over it (see
profile_repository._keep_stronger_logo, which now prevents that).

WHAT THIS DOES, per profile with a cached picture (`avatar_sha`):

  1. Recognises a platform's stock avatar by fingerprint -- no download,
     the fingerprint is already stored (shared/avatars.py
     DEFAULT_AVATAR_FINGERPRINTS).
  2. For Facebook and YouTube, reads the cached bytes once and runs the
     generated-letter-avatar check.
  3. Where either says "placeholder", writes has_logo=False with PIXELS
     strength (shared/logo_verdict.py), so no later URL-only save can undo
     it -- UNLESS an analyst set the verdict by hand (`sources.logo` =
     "manual"), which nothing automated overrides.
  4. Recomputes risk_score/priority on any row that carries them, through
     the same functions `profile_repository.patch` uses.

Never writes True: absence of a placeholder signal is not proof of a real
upload, and this only corrects in the direction the evidence supports.

Idempotent: re-running finds nothing left to change.

Usage:
    python -m backend.database.migrations.migrate_logo_evidence --dry-run
    python -m backend.database.migrations.migrate_logo_evidence
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from pymongo import UpdateOne

from backend.database.connection import db
from backend.database.repositories import avatar_repository as avatars_db
from backend.database.repositories.profile_repository import (compute_priority,
                                                              compute_risk_score)
from backend.shared import logo_verdict
from backend.shared.avatars import default_avatar_match, is_generated_avatar

BATCH = 500
PIXEL_PLATFORMS = ("facebook", "youtube")


async def _verdict(doc: dict, generated_cache: dict[str, bool]) -> str:
    """The placeholder source tag for this profile's picture, or ""."""
    platform = doc.get("platform") or ""
    if default_avatar_match(platform, doc.get("avatar_phash") or "", doc.get("avatar_dhash") or ""):
        return "default-avatar-hash"
    if platform not in PIXEL_PLATFORMS:
        return ""
    sha = doc.get("avatar_sha") or ""
    if sha not in generated_cache:
        got = None
        try:
            got = await avatars_db.read(sha)
        except Exception:
            got = None
        generated_cache[sha] = bool(got) and await asyncio.to_thread(
            is_generated_avatar, platform, doc.get("profile_image_url") or "", got[0]) is True
    return "generated-avatar" if generated_cache[sha] else ""


async def migrate(dry_run: bool) -> None:
    coll = db()["profiles"]
    fixed: Counter[str] = Counter()
    skipped_manual = 0
    rescored = 0
    pending: list[UpdateOne] = []
    written = 0
    generated_cache: dict[str, bool] = {}

    cursor = coll.find(
        {"avatar_sha": {"$nin": ["", None]}},
        {"platform": 1, "avatar_sha": 1, "avatar_phash": 1, "avatar_dhash": 1,
         "profile_image_url": 1, "has_logo": 1, "logo_strength": 1, "logo_source": 1,
         "sources": 1, "status": 1, "has_name_match": 1, "location": 1,
         "last_post_date": 1, "logo_match": 1, "username_match": 1, "risk_score": 1},
    )
    async for doc in cursor:
        source = await _verdict(doc, generated_cache)
        if not source:
            continue
        stored = logo_verdict.doc_evidence(doc)
        incoming = logo_verdict.LogoEvidence(False, logo_verdict.PIXELS, source)
        if stored.known and logo_verdict.stronger(stored, incoming) is stored:
            if stored.source == "manual" and stored.value is True:
                skipped_manual += 1
            if doc.get("logo_strength") == stored.strength:
                continue      # already recorded at this strength
        winner = logo_verdict.stronger(stored, incoming) if stored.known else incoming
        fields: dict = {
            "has_logo": winner.value, "logo_strength": winner.strength,
            "logo_source": winner.source,
        }
        if winner is incoming:
            fields["sources.logo"] = source
        if doc.get("has_logo") is winner.value and doc.get("logo_strength") == winner.strength:
            continue
        if doc.get("has_logo") is not False and winner.value is False:
            fixed[doc.get("platform") or "?"] += 1
        if doc.get("risk_score") is not None:
            validated = doc.get("status") == "approved"
            score = compute_risk_score(
                winner.value, doc.get("has_name_match", False), doc.get("location"),
                doc.get("last_post_date"), doc.get("logo_match"),
                doc.get("username_match"), validated)
            fields["risk_score"] = score
            fields["priority"] = compute_priority(
                winner.value, score, doc.get("logo_match"), validated)
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
    print(f"\n{'platform':12} {'placeholder read as a real picture':>36}")
    print("-" * 50)
    for platform in sorted(fixed):
        print(f"{platform:12} {fixed[platform]:>36}")
    print("-" * 50)
    print(f"{'TOTAL':12} {total:>36}\n")
    print(f"rows rescored: {rescored}; manual 'Yes' left alone: {skipped_manual}")
    print(f"rows whose evidence strength was recorded or corrected: "
          f"{written if not dry_run else len(pending)}")
    if dry_run:
        print("\nDRY RUN -- nothing written.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()
    asyncio.run(migrate(args.dry_run))


if __name__ == "__main__":
    main()
