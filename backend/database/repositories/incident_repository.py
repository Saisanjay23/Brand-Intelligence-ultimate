"""Incident persistence, the `incidents` collection, TTL-bounded (weekly expiration).
Kept in Mongo rather than memory: unlike the health tracker's rolling window, an
incident is exactly the kind of thing a person comes back the next day to
ask "why did last night's run fail", losing it on every restart would
defeat the point. The TTL bounds it to weekly retention (7 days) automatically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.config.settings import settings
from backend.database.connection import db

INCIDENTS = "incidents"
RETENTION_DAYS = getattr(settings, "incident_retention_days", 7)


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


async def purge_expired(days: int = RETENTION_DAYS) -> int:
    """Prune incidents older than `days` (default 7 days / weekly)."""
    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        res = await db()[INCIDENTS].delete_many({"ts": {"$lt": cutoff}})
        return int(res.deleted_count or 0)
    except Exception:
        return 0


async def ensure_indexes() -> None:
    coll = db()[INCIDENTS]
    expected_ttl = RETENTION_DAYS * 86400
    try:
        info = await coll.index_information()
        if "ttl_ts" in info and info["ttl_ts"].get("expireAfterSeconds") != expected_ttl:
            await coll.drop_index("ttl_ts")
    except Exception:
        pass
    try:
        await coll.create_index("ts", expireAfterSeconds=expected_ttl, name="ttl_ts")
    except Exception:
        pass

