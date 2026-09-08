"""Session pool persistence, pooled login cookies (`sessions`) and cached
liveness results (`session_health`, `session_item_health`), replacing the
pre-rebuild `session/<platform>.json` files. `_id` = `"<platform>:<id>"` so
lookups/upserts never need a separate compound-unique index.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from pymongo import ReturnDocument

from backend.database.connection import db

SESSIONS = "sessions"
SESSION_HEALTH = "session_health"
SESSION_ITEM_HEALTH = "session_item_health"


def _doc_id(platform: str, session_id: str) -> str:
    return f"{platform}:{session_id}"


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


async def _forget_health(platform: str, session_id: str) -> None:
    """Drop the cached liveness verdicts that the write we just made has
    invalidated, kept here, next to the writes themselves, so a new call
    path physically cannot forget it the way it could if each caller had to
    remember.

    Both rows describe credentials that no longer exist: the item row is
    what the last live check concluded about the OLD cookies/key, and the
    platform row is a pool-wide summary ("all 4 pooled sessions are
    unavailable") computed from a pool that has since changed. The platform
    row is the one that bit us, manager.status() reads it to override a
    live-computed `ready` with `checkpointed`, so a stale one made a
    freshly re-pasted session read as fixed in the pool list and
    simultaneously unusable in the header count and the platform rail until
    the next 30-minute sweep.

    Clearing the item row has a second benefit: manager._pick_batch orders
    the sweep by last-checked date and treats "never checked" as oldest, so
    a just-fixed session is verified FIRST on the next sweep rather than
    last. It also un-suppresses its next SessionInvalid alert, which
    _record_item_result would otherwise skip because the stale row already
    said this item was failing.
    """
    await db()[SESSION_ITEM_HEALTH].delete_one({"platform": platform, "session_id": session_id})
    await db()[SESSION_HEALTH].delete_one({"platform": platform})


def _to_item(doc: dict) -> dict:
    return {
        "platform": doc["platform"], "id": doc["session_id"],
        "identifier": doc.get("identifier", doc["session_id"]),
        "status": doc.get("status", "ready"),
        "cookies": doc.get("cookies", []),
        "rate_limited_until": float(doc.get("rate_limited_until") or 0),
        "last_used": float(doc.get("last_used") or 0),
        # Epoch seconds a REAL JOB last proved this session healthy
        # (mark_session_ok). Distinct from `last_used`, which is bumped when
        # a session is merely handed out, before anything has confirmed it
        # works.
        #
        # THIS WAS MISSING FROM THIS MAPPING, and it silently disabled a
        # stealth mitigation. `sessions/manager.py::_pick_batch` skips a
        # probe for a session a job proved healthy within PROVEN_FRESH_S --
        # but it reads that field off THIS dict, and with the key absent it
        # resolved to 0.0, making every session look infinitely overdue and
        # therefore always eligible. The write side was correct all along
        # (every pooled session carries a current `last_ok` in Mongo); the
        # value simply never reached the reader.
        #
        # What that cost is the exact thing PROVEN_FRESH_S was added to
        # prevent: with one pooled account per platform, the most-overdue
        # session is the same account every sweep, so it was loading its own
        # authenticated settings page 48 times a day on a metronome-regular
        # 30-minute cadence -- volume and periodicity both being things
        # these platforms score against an account.
        "last_ok": float(doc.get("last_ok") or 0),
        # how many times this session has been handed to a job, see
        # increment_use_count(), called only from sessions/manager.py's
        # get_healthy_session() at the moment a session is actually picked
        # for real use (not on every health check).
        "use_count": int(doc.get("use_count") or 0),
        # consecutive failures since this session last demonstrably worked
        # drives the graduated quarantine ladder in sessions/manager.py.
        # Reset to 0 by mark_session_ok, never by simply handing it out.
        "consecutive_failures": int(doc.get("consecutive_failures") or 0),
        "quarantine_minutes": int(doc.get("quarantine_minutes") or 0),
        # epoch seconds this item first went dead (expired/checkpointed/
        # unreadable), 0 while healthy or rate-limited, the auto-purge
        # sweep's grace-period clock (see sessions/manager.py).
        "dead_since": float(doc.get("dead_since") or 0),
        # epoch seconds this item's CREDENTIALS last changed (cookies/key/
        # MTProto blob pasted or rewritten), deliberately not bumped by
        # routine bookkeeping writes like last_used/status/use_count, so it
        # answers exactly one question: is a cached health result older than
        # the credentials it was measuring? See sessions/manager.py::status.
        "credentials_updated_at": float(doc.get("credentials_updated_at") or 0),
        # Why this entry last stopped working, in words, kept ON the entry
        # rather than only in the logs, an auto-login that fails at 2am
        # otherwise leaves a row that just reads "checkpointed" with the
        # actual reason (wrong password? network timeout? 2FA prompt?) buried
        # in a log file nobody is reading. Cleared whenever credentials are
        # rewritten, since it described the previous ones.
        "last_error": doc.get("last_error", ""),
        "api_key": doc.get("api_key", ""),
        "api_id": doc.get("api_id", ""),
        "api_hash": doc.get("api_hash", ""),
        "phone": doc.get("phone", ""),
        "session_blob": doc.get("session_blob"),
        "username": doc.get("username", ""),
        "password": doc.get("password", ""),
        "two_factor_secret": doc.get("two_factor_secret", ""),
    }


async def list_pool(platform: str) -> list[dict]:
    return [_to_item(d) async for d in db()[SESSIONS].find({"platform": platform})]


async def count_pool(platform: str) -> int:
    return await db()[SESSIONS].count_documents({"platform": platform})


async def get_item(platform: str, session_id: str) -> Optional[dict]:
    doc = await db()[SESSIONS].find_one({"_id": _doc_id(platform, session_id)})
    return _to_item(doc) if doc else None


# A NEW pooled entry joins the rotation at the BACK, not the front.
#
# `manager._pick_least_recently_used` sorts ascending on `last_used`, so a
# freshly-added account seeded with 0.0 was the lowest possible value and
# therefore the very first session handed out -- ahead of every established
# account, for its first job. That is backwards: a minutes-old account is
# the one platforms scrutinise hardest, and with no result caps configured
# a single discovery turn can be hundreds of authenticated page loads.
#
# Stamping "now" instead means a new entry sorts LAST, so it only gets work
# once every already-trusted account has had its turn. Same pool, same
# rotation, no ramp logic -- it just stops actively preferring the most
# fragile account in the pool.
_NEW_SESSION_LAST_USED = _now  # stamped at insert; see the note above


async def add_item(platform: str, cookies: list[dict], identifier: str) -> dict:
    if await count_pool(platform) >= 20:
        raise ValueError(f"Session pool capacity limit (20) reached for {platform}. Please update an expired session or delete one.")
    session_id = uuid.uuid4().hex[:8]
    doc = {
        "_id": _doc_id(platform, session_id), "platform": platform, "session_id": session_id,
        "identifier": identifier, "status": "ready", "cookies": cookies,
        "rate_limited_until": 0.0, "last_used": _NEW_SESSION_LAST_USED(),
        "username": "", "password": "", "two_factor_secret": "",
        "credentials_updated_at": _now(),
    }
    await db()[SESSIONS].insert_one(doc)
    await _forget_health(platform, session_id)
    return _to_item(doc)


async def save_api_key_session(platform: str, key: str, identifier: str) -> dict:
    if await count_pool(platform) >= 20:
        raise ValueError(f"Session pool capacity limit (20) reached for {platform}. Please update an expired key or delete one.")
    session_id = uuid.uuid4().hex[:8]
    doc = {
        "_id": _doc_id(platform, session_id), "platform": platform, "session_id": session_id,
        "identifier": identifier, "status": "ready", "api_key": key, "cookies": [],
        "rate_limited_until": 0.0, "last_used": _NEW_SESSION_LAST_USED(),
        "credentials_updated_at": _now(),
    }
    await db()[SESSIONS].update_one({"_id": _doc_id(platform, session_id)}, {"$set": doc}, upsert=True)
    await _forget_health(platform, session_id)
    return _to_item(doc)


async def save_mtproto_session(platform: str, identifier: str, api_id: int, api_hash: str, phone: str, session_blob: Optional[bytes]) -> dict:
    if await count_pool(platform) >= 20:
        raise ValueError(f"Session pool capacity limit (20) reached for {platform}. Please update an expired account or delete one.")
    session_id = uuid.uuid4().hex[:8]
    doc = {
        "_id": _doc_id(platform, session_id), "platform": platform, "session_id": session_id,
        "identifier": identifier, "status": "ready", "api_id": api_id, "api_hash": api_hash,
        "phone": phone, "session_blob": session_blob, "cookies": [],
        "rate_limited_until": 0.0, "last_used": _NEW_SESSION_LAST_USED(),
        "credentials_updated_at": _now(),
    }
    await db()[SESSIONS].update_one({"_id": _doc_id(platform, session_id)}, {"$set": doc}, upsert=True)
    await _forget_health(platform, session_id)
    return _to_item(doc)


async def update_session_credentials(platform: str, session_id: str, **credentials) -> Optional[dict]:
    # Rewriting an entry's credentials is a clean slate: whatever quarantine
    # ladder / dead-since clock it had accrued belonged to the OLD
    # credentials, not these.
    fields = {
        "status": "ready", "rate_limited_until": 0.0, "consecutive_failures": 0, "dead_since": 0.0,
        # these ARE new credentials, stamping this is what tells
        # sessions/manager.py::status to stop trusting any health result
        # recorded against the old ones (see _to_item).
        "credentials_updated_at": _now(),
        # whatever went wrong last time was the old credentials' problem
        "last_error": "",
    }
    for k in ("cookies", "api_key", "identifier", "api_id", "api_hash", "phone", "session_blob", "username", "password", "two_factor_secret"):
        if k in credentials and credentials[k] is not None:
            fields[k] = credentials[k]
    res = await db()[SESSIONS].update_one({"_id": _doc_id(platform, session_id)}, {"$set": fields})
    if res.matched_count == 0:
        return None
    await _forget_health(platform, session_id)
    return await get_item(platform, session_id)


async def update_item(platform: str, session_id: str, **fields) -> bool:
    res = await db()[SESSIONS].update_one({"_id": _doc_id(platform, session_id)}, {"$set": fields})
    return res.matched_count > 0


async def increment_use_count(platform: str, session_id: str) -> int:
    """Atomic +1, returning the new total, a $set of a value read back
    earlier would race two concurrent round-robin slots picking the same
    just-freed session and silently drop one of the two increments."""
    doc = await db()[SESSIONS].find_one_and_update(
        {"_id": _doc_id(platform, session_id)},
        {"$inc": {"use_count": 1}},
        return_document=ReturnDocument.AFTER,
    )
    return int(doc.get("use_count") or 0) if doc else 0


async def delete_item(platform: str, session_id: str) -> bool:
    res = await db()[SESSIONS].delete_one({"_id": _doc_id(platform, session_id)})
    await _forget_health(platform, session_id)
    return res.deleted_count > 0


async def delete_pool(platform: str) -> int:
    res = await db()[SESSIONS].delete_many({"platform": platform})
    await db()[SESSION_ITEM_HEALTH].delete_many({"platform": platform})
    await db()[SESSION_HEALTH].delete_one({"platform": platform})
    return res.deleted_count


# ---------- health cache ----------

async def item_last_checked(platform: str, session_id: str) -> Optional[datetime]:
    doc = await db()[SESSION_ITEM_HEALTH].find_one({"platform": platform, "session_id": session_id})
    return doc.get("checked_at") if doc else None


async def record_item_health(platform: str, session_id: str, identifier: str, ok: bool, detail: str) -> Optional[dict]:
    """Returns the PREVIOUS doc (or None if first check) so the caller can
    decide whether this is a fresh failure worth an incident."""
    coll = db()[SESSION_ITEM_HEALTH]
    previous = await coll.find_one({"platform": platform, "session_id": session_id})
    await coll.update_one(
        {"platform": platform, "session_id": session_id},
        {"$set": {
            "platform": platform, "session_id": session_id, "identifier": identifier,
            "ok": ok, "detail": detail, "checked_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    return previous


async def record_platform_health(platform: str, ok: bool, detail: str) -> None:
    await db()[SESSION_HEALTH].update_one(
        {"platform": platform},
        {"$set": {"platform": platform, "ok": ok, "detail": detail, "checked_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def cached_item_health(platform: str) -> dict[str, dict]:
    """The last live-check verdict for each session of one platform, keyed
    by session id, the per-session counterpart of `cached_health`.

    Recorded all along by the background monitor; this is what lets the
    Sessions panel say WHY a row is red ("session invalid or checkpointed")
    instead of showing an unexplained red dot that could equally mean
    logged out, challenged, or merely rate-limited, three problems with
    three different fixes.
    """
    out: dict[str, dict] = {}
    async for d in db()[SESSION_ITEM_HEALTH].find({"platform": platform}):
        out[d["session_id"]] = {
            "ok": d.get("ok", True), "detail": d.get("detail", ""), "checked_at": d.get("checked_at"),
        }
    return out


async def cached_health() -> dict[str, dict]:
    out: dict[str, dict] = {}
    async for d in db()[SESSION_HEALTH].find({}):
        out[d["platform"]] = {"ok": d.get("ok", True), "detail": d.get("detail", ""), "checked_at": d.get("checked_at")}
    return out


async def ensure_indexes() -> None:
    await db()[SESSIONS].create_index([("platform", 1), ("session_id", 1)], unique=True, name="uniq_platform_session")
