"""Incident persistence, the `incidents` collection, TTL-bounded. Kept in
Mongo rather than memory: unlike the health tracker's rolling window, an
incident is exactly the kind of thing a person comes back the next day to
ask "why did last night's run fail", losing it on every restart would
defeat the point. The TTL still bounds it automatically.
"""

from __future__ import annotations

from datetime import datetime

from backend.database.connection import db

INCIDENTS = "incidents"
RETENTION_DAYS = 14


async def record(doc: dict) -> None:
    try:
        await db()[INCIDENTS].insert_one(doc)
    except Exception:
        pass  # the incident log itself must never be why a job fails


async def since(ts: datetime) -> list[dict]:
    return await db()[INCIDENTS].find({"ts": {"$gte": ts}}).to_list(length=1000)


async def recent(limit: int = 50, severity: str = "", platform: str = "") -> list[dict]:
    """Newest incidents first, for the Live Activity panel.

    Filterable because the two questions an operator actually asks are
    different: "what is broken right now" (severity=critical) and "what has
    this platform been doing" (platform=...).
    """
    q: dict = {}
    if severity:
        q["severity"] = severity
    if platform:
        q["platform"] = platform
    cursor = db()[INCIDENTS].find(q).sort("ts", -1).limit(max(1, min(limit, 500)))
    return await cursor.to_list(length=None)


async def counts_by_severity() -> dict:
    """{severity: n} across everything retained, for the panel's header."""
    out: dict = {}
    async for d in db()[INCIDENTS].aggregate([
        {"$group": {"_id": "$severity", "n": {"$sum": 1}}},
    ]):
        out[str(d["_id"] or "unknown")] = d["n"]
    return out


async def delete_for_client(client_id: str) -> int:
    """Part of the client-deletion cascade, only ever removes incidents
    scoped to this one client; session-check incidents (scope is the
    literal "-- all clients --") are cross-client and untouched."""
    res = await db()[INCIDENTS].delete_many({"scope": client_id})
    return res.deleted_count


async def delete_incident(incident_id: str) -> bool:
    """Delete / dismiss a specific incident by ObjectId or string id."""
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId(incident_id)
        res = await db()[INCIDENTS].delete_one({"_id": oid})
        return res.deleted_count > 0
    except (InvalidId, TypeError):
        res = await db()[INCIDENTS].delete_one({"_id": incident_id})
        return res.deleted_count > 0


async def clear_all() -> int:
    """Clear all stored incidents."""
    res = await db()[INCIDENTS].delete_many({})
    return res.deleted_count


async def ensure_indexes() -> None:
    await db()[INCIDENTS].create_index("ts", expireAfterSeconds=RETENTION_DAYS * 86400, name="ttl_ts")

