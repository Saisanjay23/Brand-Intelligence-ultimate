"""Evidence screenshots, stored in Mongo (GridFS) alongside everything else
this engine keeps, not as loose files under the project directory.

Screenshots are THE deliverable for a takedown request: the impersonating
account is routinely gone by the time anyone reads the incident, so the
capture is the only durable proof it ever existed (see `analysis_service.py`
docstrings). Living as bare files under a project-local `evidence/` folder
meant they weren't backed up with the rest of the data, didn't move with a
database migration/restore, and grew on a single server's disk with no
relation to how the rest of this engine's state is kept.

GridFS, not the `profiles` collection directly: a full-page capture can run
past Mongo's 16MB single-document limit, and embedding image bytes inline in
`profiles` would bloat every ordinary list/find query on that collection
(pagination, exports, dedup lookups) that has nothing to do with evidence.
GridFS keeps the binary data in its own `fs.chunks`/`fs.files` collections,
addressed by filename, and streams rather than loading the whole document.

Keyed by the exact same "relative path", style string
(`{client_id}/{platform_id}/{stem}.png`) profile documents already store in
their `screenshot` field, so no change was needed to what gets written to a
profile doc, or to the re-analysis-overwrites-its-own-capture behavior
only to where the bytes physically live.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorGridFSBucket

from backend.database.connection import db
from backend.shared.logging import get_logger

log = get_logger("repositories.evidence")

_bucket: Optional[AsyncIOMotorGridFSBucket] = None


def _bucket_for() -> AsyncIOMotorGridFSBucket:
    global _bucket
    if _bucket is None:
        _bucket = AsyncIOMotorGridFSBucket(db(), bucket_name="evidence")
    return _bucket


async def save(key: str, data: bytes, content_type: str = "image/png") -> None:
    """Store `data` under `key`, replacing any existing capture at that key.

    GridFS itself does not overwrite-by-filename (each upload gets its own
    id, and multiple revisions of one filename can coexist), deleting the
    old revision(s) first is what gives re-analysing a profile the same
    "overwrites its own previous capture" behavior the old file-based
    version had, rather than the store growing one entry per run forever.
    """
    bucket = _bucket_for()
    async for old in bucket.find({"filename": key}):
        await bucket.delete(old._id)
    await bucket.upload_from_stream(key, data, metadata={"content_type": content_type})


async def read(key: str) -> Optional[bytes]:
    """The bytes stored under `key`, or None if nothing is there.

    A bare key lookup, not a filesystem path join, there is no root to
    escape and no `../` to guard against; an unrecognised key is simply a
    miss, the GridFS analogue of the old code's containment check.
    """
    if not key:
        return None
    bucket = _bucket_for()
    try:
        stream = await bucket.open_download_stream_by_name(key)
    except Exception:
        return None
    try:
        return await stream.read()
    finally:
        stream.close()


async def delete(key: str) -> None:
    if not key:
        return
    bucket = _bucket_for()
    async for old in bucket.find({"filename": key}):
        await bucket.delete(old._id)


async def delete_for_client(client_id: str) -> int:
    """Part of the client-deletion cascade (see client_service.delete)
    without this, deleting a client's profiles left every screenshot they
    ever referenced behind in GridFS forever, since a screenshot's only
    link to a client is the `{client_id}/{platform}/{stem}.png` filename
    prefix, not a queryable `client_id` field a plain collection delete
    would ever reach."""
    if not client_id:
        return 0
    bucket = _bucket_for()
    n = 0
    async for f in bucket.find({"filename": {"$regex": f"^{re.escape(client_id)}/"}}):
        await bucket.delete(f._id)
        n += 1
    return n


async def delete_many(keys: list[str]) -> int:
    """Delete exactly these screenshots, the scoped "Delete Platform Data"
    cascade (see profile_service.delete_for_client_platform): when the
    delete was narrowed to e.g. Discovery + Pending, only the screenshots
    those specific deleted profiles owned should go with them, never every
    screenshot for the whole client+platform (that would also take an
    unrelated already-published finding's evidence)."""
    n = 0
    for key in {k for k in keys if k}:
        await delete(key)
        n += 1
    return n


async def delete_older_than(days: int = 7) -> int:
    """Delete evidence screenshots older than `days` days from GridFS.

    Deleting through `bucket.delete()` deletes the file record in
    `evidence.files` AND all its corresponding chunk documents in
    `evidence.chunks`.
    """
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    bucket = _bucket_for()
    n = 0
    try:
        async for f in bucket.find({"uploadDate": {"$lt": cutoff}}):
            try:
                await bucket.delete(f._id)
                n += 1
            except Exception as e:
                log.warning(f"failed to delete expired evidence file {f._id}: {e}")
    except Exception as e:
        log.error(f"error querying expired evidence files: {e}")
    if n > 0:
        log.info(f"retention: pruned {n} evidence screenshot(s) older than {days} days")
    return n

