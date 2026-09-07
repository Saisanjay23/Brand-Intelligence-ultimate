"""Profile pictures, stored as bytes in Mongo (GridFS), addressed by content.

WHY THE BYTES ARE KEPT AT ALL. A discovered profile's picture used to be a
signed CDN URL and nothing else. Those URLs EXPIRE -- fbcdn signs them with
`oh=` and carries the deadline in `oe=`, and the window is hours, not days
(profile_repository.archive_stale_rejected's own docstring says as much).
So a discovery card looked right the moment it was found and decayed into
an initial-letter circle by the next morning, for every profile, with
nothing broken and nothing logged. Re-fetching is not an option either: the
signature is the only thing that would authorise the refetch, and it is the
part that died.

Fetching the bytes ONCE, at discovery time, and serving them from our own
origin is what makes a card's picture permanent. It also removes three
other problems at a stroke: CORP (which cost Instagram every avatar it had
-- see api/media.py), hotlink and referer fragility, and a round trip to
Meta on every single card render.

ADDRESSED BY CONTENT, NOT BY PROFILE. The filename is the sha256 of the
bytes. The same impersonator found under five keywords, or the same stock
photo reused across forty fake profiles, is stored exactly once -- and
re-running a sweep over profiles already cached writes nothing at all,
because the digest is already there. It also makes the stored object
immutable, which is what lets the serving route mark it
`Cache-Control: immutable` and never revalidate.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorGridFSBucket

from backend.database.connection import db
from backend.shared.logging import get_logger

log = get_logger("repositories.avatar")

BUCKET = "avatars"

_bucket: Optional[AsyncIOMotorGridFSBucket] = None


def _bucket_for() -> AsyncIOMotorGridFSBucket:
    global _bucket
    if _bucket is None:
        _bucket = AsyncIOMotorGridFSBucket(db(), bucket_name=BUCKET)
    return _bucket


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def exists(sha: str) -> bool:
    if not sha:
        return False
    bucket = _bucket_for()
    async for _ in bucket.find({"filename": sha}).limit(1):
        return True
    return False


async def store(data: bytes, content_type: str = "image/jpeg") -> str:
    """Store `data` and return its sha256. Storing the same bytes twice is
    a no-op rather than a second copy -- GridFS does not dedupe or overwrite
    by filename on its own (every upload gets a fresh id and revisions of one
    filename happily coexist), so the existence check IS the dedupe."""
    sha = digest(data)
    if await exists(sha):
        return sha
    try:
        await _bucket_for().upload_from_stream(
            sha, data, metadata={"content_type": content_type},
        )
    except Exception as e:
        # A racing writer that stored the same digest first is a success,
        # not a failure -- the bytes are there, which is all the caller
        # wanted. Anything else is worth surfacing.
        if not await exists(sha):
            log.warning(f"avatar store failed for {sha[:12]}: {type(e).__name__}: {e}")
            raise
    return sha


async def read(sha: str) -> Optional[tuple[bytes, str]]:
    """(bytes, content_type) for `sha`, or None. A bare key lookup, not a
    path join: there is no root to escape and an unrecognised digest is
    simply a miss."""
    if not sha:
        return None
    bucket = _bucket_for()
    try:
        stream = await bucket.open_download_stream_by_name(sha)
    except Exception:
        return None
    try:
        data = await stream.read()
    finally:
        stream.close()
    meta = getattr(stream, "metadata", None) or {}
    return data, meta.get("content_type") or "image/jpeg"


async def delete(sha: str) -> None:
    """Removes every revision at this digest. Note that an avatar can be
    referenced by MORE THAN ONE profile (that is the point of addressing by
    content), so this is not safe to call from a single profile's deletion
    -- only from a sweep that has already established nothing references it."""
    if not sha:
        return
    bucket = _bucket_for()
    async for old in bucket.find({"filename": sha}):
        await bucket.delete(old._id)


async def stats() -> dict:
    """Count and total bytes held. For an operator deciding whether the
    store needs pruning; nothing reads this to make a decision itself."""
    files = db()[f"{BUCKET}.files"]
    cur = files.aggregate([
        {"$group": {"_id": None, "count": {"$sum": 1}, "bytes": {"$sum": "$length"}}},
    ])
    async for row in cur:
        return {"count": row.get("count", 0), "bytes": row.get("bytes", 0)}
    return {"count": 0, "bytes": 0}


async def ensure_indexes() -> None:
    """GridFS creates its own `{filename, uploadDate}` index, which is what
    `exists`/`read` look up by. Nothing extra is needed; this exists so the
    startup path can treat every repository the same way."""
    return None
