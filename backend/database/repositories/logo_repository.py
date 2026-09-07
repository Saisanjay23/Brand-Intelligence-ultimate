"""The client's OWN logos -- the reference side of logo matching.

WHAT THESE ARE. An analyst uploads the real brand mark while configuring a
client's keywords, and attaches it to the parent keyword it belongs to. A
profile discovered under that keyword then has its avatar compared against
these, and a match is what lifts an impersonator wearing the right logo but
a name that scores badly ("Reliance Care Support") to the top of triage.

WHY SCOPED TO A KEYWORD, NOT JUST A CLIENT. It narrows what each candidate
is compared against, which both cuts work and removes a whole class of false
positive: two unrelated brands under one client (a parent company with
several marks) can no longer match each other's profiles. A logo saved with
an empty `keyword` is client-wide on purpose -- one mark that applies to
every keyword the client has.

SEPARATE FROM THE AVATAR STORE, DELIBERATELY. Both hold images
content-addressed by sha256, but their lifecycles have nothing in common:
avatars are harvested by sweeps and can be pruned whenever nothing points at
them, whereas a reference logo is analyst-curated configuration that must
survive every sweep, every prune, and the deletion of every profile.

The fingerprints are computed ONCE here, at upload, and stored alongside.
Matching a sweep's avatars is then pure arithmetic over stored hex strings
-- no decoding at query time. See shared/imagehashing.py.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorGridFSBucket

from backend.database.connection import db
from backend.shared.errors import NotFoundError, ValidationError
from backend.shared.imagehashing import ImageFingerprint
from backend.shared.logging import get_logger

log = get_logger("repositories.logo")

LOGOS = "client_logos"
BUCKET = "logos"

_bucket: Optional[AsyncIOMotorGridFSBucket] = None


def _bucket_for() -> AsyncIOMotorGridFSBucket:
    global _bucket
    if _bucket is None:
        _bucket = AsyncIOMotorGridFSBucket(db(), bucket_name=BUCKET)
    return _bucket


def _oid(logo_id: str) -> ObjectId:
    try:
        return ObjectId(logo_id)
    except Exception:
        raise ValidationError(f"{logo_id!r} is not a valid logo id")


def _to_out(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "client_id": doc.get("client_id", ""),
        # "" means client-wide: compared against every keyword's profiles.
        "keyword": doc.get("keyword", ""),
        "kind": doc.get("kind", ""),
        "sha": doc.get("sha", ""),
        "phash": doc.get("phash", ""),
        "embedding": doc.get("embedding") or None,
        "dhash": doc.get("dhash", ""),
        "width": doc.get("width", 0),
        "height": doc.get("height", 0),
        "bytes": doc.get("bytes", 0),
        "filename": doc.get("filename", ""),
        "content_type": doc.get("content_type", "image/png"),
        "created_at": doc.get("created_at"),
    }


async def has_any(client_id: str) -> bool:
    """Does this client have ANY reference logo? Asked once per batch before
    a sweep spends CPU on embeddings -- a client with no references is every
    client until an analyst opts in, and must pay nothing for this feature."""
    if not client_id:
        return False
    return await db()[LOGOS].count_documents({"client_id": client_id}, limit=1) > 0


async def add(
    client_id: str, *, data: bytes, fingerprint: ImageFingerprint,
    keyword: str = "", kind: str = "", filename: str = "",
    content_type: str = "image/png", embedding: Optional[list] = None,
) -> dict:
    """Store one reference logo and its fingerprints.

    The BYTES are content-addressed, so uploading the same file twice for two
    keywords stores one blob and two records -- which is the common case, a
    single brand mark that applies to several keywords.

    The RECORD is not deduped: the same image under two keywords is two
    genuinely different pieces of configuration, and deleting one must not
    silently disarm the other.
    """
    if not client_id:
        raise ValidationError("client_id is required")
    if not data:
        raise ValidationError("no image data")

    sha = hashlib.sha256(data).hexdigest()
    bucket = _bucket_for()
    exists = False
    async for _ in bucket.find({"filename": sha}).limit(1):
        exists = True
    if not exists:
        await bucket.upload_from_stream(sha, data, metadata={"content_type": content_type})

    doc = {
        "client_id": client_id,
        "keyword": (keyword or "").strip(),
        "kind": (kind or "").strip(),
        "sha": sha,
        "phash": fingerprint.phash,
        "dhash": fingerprint.dhash,
        # Unit-length CLIP vector, for the tier that catches the same mark
        # re-presented. Absent when the model was unavailable at upload --
        # the hash tiers still work, so that is a degradation, not a failure.
        "embedding": embedding or None,
        "width": fingerprint.width,
        "height": fingerprint.height,
        "bytes": len(data),
        "filename": (filename or "").strip()[:200],
        "content_type": content_type,
        "created_at": datetime.now(timezone.utc),
    }
    res = await db()[LOGOS].insert_one(doc)
    doc["_id"] = res.inserted_id
    return _to_out(doc)


async def list_for_client(client_id: str) -> list[dict]:
    cur = db()[LOGOS].find({"client_id": client_id}).sort([("created_at", 1)])
    return [_to_out(d) async for d in cur]


async def list_for_keywords(client_id: str, keywords: list[str]) -> list[dict]:
    """Every reference a profile found under `keywords` should be compared
    against: those keywords' own logos, PLUS the client-wide ones.

    Matched case-insensitively -- a keyword is analyst-typed text, and
    "Reliance" configured on the Clients page against "reliance" carried on a
    discovery row must not be two different things.
    """
    if not client_id:
        return []
    wanted = {k.strip().lower() for k in (keywords or []) if k and k.strip()}
    cur = db()[LOGOS].find({"client_id": client_id})
    out: list[dict] = []
    async for d in cur:
        kw = (d.get("keyword") or "").strip().lower()
        if not kw or kw in wanted:
            out.append(_to_out(d))
    return out


async def get(logo_id: str) -> dict:
    doc = await db()[LOGOS].find_one({"_id": _oid(logo_id)})
    if doc is None:
        raise NotFoundError(f"logo {logo_id!r} not found")
    return _to_out(doc)


async def read_bytes(sha: str) -> Optional[tuple[bytes, str]]:
    """(bytes, content_type) for a stored logo, or None."""
    if not sha:
        return None
    try:
        stream = await _bucket_for().open_download_stream_by_name(sha)
    except Exception:
        return None
    try:
        data = await stream.read()
    finally:
        stream.close()
    meta = getattr(stream, "metadata", None) or {}
    return data, meta.get("content_type") or "image/png"


async def delete(logo_id: str) -> dict:
    """Remove one reference. The BLOB only goes when no record still points
    at that digest -- the same mark is routinely attached to several
    keywords, and deleting one of those must not blind the others."""
    doc = await db()[LOGOS].find_one_and_delete({"_id": _oid(logo_id)})
    if doc is None:
        raise NotFoundError(f"logo {logo_id!r} not found")
    sha = doc.get("sha", "")
    if sha and await db()[LOGOS].count_documents({"sha": sha}, limit=1) == 0:
        async for old in _bucket_for().find({"filename": sha}):
            await _bucket_for().delete(old._id)
    return _to_out(doc)


async def delete_for_client(client_id: str) -> int:
    """Part of deleting a client: its reference logos are its configuration
    and go with it. Blobs still referenced by another client's records stay."""
    shas = {d["sha"] async for d in db()[LOGOS].find({"client_id": client_id}, {"sha": 1})}
    res = await db()[LOGOS].delete_many({"client_id": client_id})
    for sha in shas:
        if sha and await db()[LOGOS].count_documents({"sha": sha}, limit=1) == 0:
            async for old in _bucket_for().find({"filename": sha}):
                await _bucket_for().delete(old._id)
    return res.deleted_count


async def ensure_indexes() -> None:
    coll: Any = db()[LOGOS]
    # Every read is "this client's logos", and matching adds a keyword scope.
    await coll.create_index([("client_id", 1), ("keyword", 1)])
    # `delete` asks whether any record still points at a digest.
    await coll.create_index([("sha", 1)])
