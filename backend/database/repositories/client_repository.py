"""Client persistence, the `clients` collection, one document per
caller-supplied `client_id` (your SaaS's own customer/org id, passed
straight through and used as-is, never regenerated).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from backend.shared.errors import ConflictError, NotFoundError, ValidationError
from backend.database.connection import db
from backend.shared import keywords as _keywords

CLIENTS = "clients"


def _utc(v):
    """Motor hands every datetime back naive-but-UTC-VALUED; stamp UTC
    explicitly on the way out so a JSON client doesn't read the unmarked
    ISO string as local time (same fix as profile_repository's
    `_stamp_utc_for_api`), otherwise the Scheduler tab's exact last-run
    timestamp would be off by the browser's own UTC offset."""
    return v.replace(tzinfo=timezone.utc) if isinstance(v, datetime) and v.tzinfo is None else v


def _to_out(doc: dict) -> dict:
    return {
        "client_id": doc["_id"],
        "name": doc.get("name", ""),
        "domain": doc.get("domain", ""),
        # two deliberately separate curated lists, not one merged bag:
        # individual names (people to protect) and domain/brand keyword
        # variants are different kinds of search terms an analyst tunes
        # independently. Combined at search time, never merged in storage.
        "name_keywords": doc.get("name_keywords", []),
        "domain_keywords": doc.get("domain_keywords", []),
        # The parent/child structure behind those two flat lists. A document
        # saved before this existed has no `keyword_groups` at all, so this
        # synthesises one childless parent per existing keyword -- which
        # searches itself, i.e. the exact pre-groups behaviour, with nothing
        # to migrate. See shared/keywords.py::groups_for_client.
        "keyword_groups": _keywords.groups_for_client(doc),
        # per-platform discovery cap, keyed by platform id, scoped
        # separately to individual-keyword vs domain-keyword sweeps, a
        # platform absent from either map (or mapped to 0) means "scrape
        # everything" for that keyword type. See services/discovery_service.py,
        # which reads these per platform when a sweep starts.
        #
        # Falls back to the pre-split `platform_limits` field for a document
        # saved before this existed, applied to BOTH keyword types (the
        # closest match to what a single combined cap used to mean)
        # never silently uncapped just because the client record predates
        # this field. The next save through client_service.upsert writes
        # real individual/domain values and drops the legacy field for good.
        "platform_limits_individual": doc.get("platform_limits_individual") or doc.get("platform_limits") or {},
        "platform_limits_domain": doc.get("platform_limits_domain") or doc.get("platform_limits") or {},
        # platform id -> {tab -> {"individual"/"domain": cap}}, for
        # platforms with more than one discovery tab, currently only
        # Facebook (people/pages/groups). A document saved before this was
        # split by keyword type may still carry the legacy flat {tab: cap}
        # shape, discovery_service.py's cap lookup understands both.
        "platform_tab_limits": doc.get("platform_tab_limits", {}),
        # the round-robin engine's rotation order and the Scheduler tab's
        # list order are both just this field, ascending. Set once at
        # creation (epoch-ms of insert, so a brand-new client always sorts
        # after every existing one) and never touched again by `upsert`,
        # only `reorder` changes it after that, an analyst dragging the
        # Scheduler tab's list is the only thing that should move a client.
        "order": doc.get("order", 0),
        "cron": doc.get("cron"),
        "created_at": _utc(doc.get("created_at")),
        # last_run_at/status/note/duration_s/run_count were written by a
        # server-side round-robin engine that no longer exists (client-side
        # sequencing replaced it, see SchedulerPanel.tsx's module docstring)
        # -- nothing writes them anymore, so they stay whatever they were
        # on a client that predates the rewrite, and absent on any newer one.
        "last_run_at": _utc(doc.get("last_run_at")),
        "last_run_status": doc.get("last_run_status"),
        "last_run_note": doc.get("last_run_note", ""),
        "last_run_duration_s": doc.get("last_run_duration_s"),
        "run_count": doc.get("run_count", 0),
        # Same story: `set_scheduler_enabled` below can still flip this, but
        # nothing reads it back to decide whether to run a client -- the
        # round-robin rotation it used to gate is gone, and the client-side
        # scheduler doesn't check it either.
        "scheduler_enabled": doc.get("scheduler_enabled", True),
        # WHAT THE SCHEDULER SHOULD RUN FOR THIS CLIENT, remembered between
        # runs so an analyst sets it once rather than on every queueing.
        #
        # Empty list means EVERY ready platform, matching what omitting
        # `platforms` already means to POST /discovery/jobs -- one canonical
        # spelling of "all", so a client that has never been configured and
        # one deliberately set to all behave identically.
        "scheduler_platforms": doc.get("scheduler_platforms") or [],
        # "" (all) | "individual" | "domain". Same convention as the Clients
        # page's own keyword-scope chips.
        "scheduler_keyword_scope": doc.get("scheduler_keyword_scope") or "",
        "scheduler_facebook_tabs": doc.get("scheduler_facebook_tabs") or [],
        "scheduler_budget_minutes": doc.get("scheduler_budget_minutes") or 0,
    }


def _config_fields(
    name: str, domain: str,
    name_keywords: Optional[list[str]], domain_keywords: Optional[list[str]],
    platform_limits_individual: Optional[dict[str, int]],
    platform_limits_domain: Optional[dict[str, int]],
    platform_tab_limits: Optional[dict[str, dict[str, object]]],
    cron: Optional[str],
    keyword_groups: Optional[dict],
) -> dict:
    """The stored shape of one client's own configuration.

    Shared by `create` and `upsert` so the two can never normalise a
    client's keywords differently -- the whole point of keeping this in one
    place is that a client created through one path and edited through the
    other ends up with identical structure.

    Nothing here reads or merges any OTHER client's document: every value
    comes from this call's arguments alone, keyed under this client's own
    `_id` by the callers below.
    """
    # `keyword_groups` is authoritative when supplied: the flat parent
    # lists are DERIVED from it rather than trusted from the request, so
    # the two physically cannot drift apart no matter what a caller sends.
    # Without groups (an older caller, or a client genuinely using only
    # flat keywords) the flat lists are taken as given and groups are
    # synthesised from them -- one childless parent each, which searches
    # itself. See shared/keywords.py.
    groups = _keywords.normalize_groups(keyword_groups)
    if any(groups[t] for t in _keywords.KEYWORD_TYPES):
        flat = _keywords.flat_keywords(groups)
        name_kw = flat["name_keywords"]
        domain_kw = flat["domain_keywords"]
    else:
        name_kw = name_keywords or []
        domain_kw = domain_keywords or []
        groups = _keywords.groups_from_flat(name_kw, domain_kw)
    return {
        "name": name, "domain": domain,
        "name_keywords": name_kw, "domain_keywords": domain_kw,
        "keyword_groups": groups,
        "platform_limits_individual": platform_limits_individual or {},
        "platform_limits_domain": platform_limits_domain or {},
        "platform_tab_limits": platform_tab_limits or {},
        "cron": cron,
    }


async def create(
    client_id: str, name: str, domain: str = "",
    name_keywords: Optional[list[str]] = None, domain_keywords: Optional[list[str]] = None,
    platform_limits_individual: Optional[dict[str, int]] = None,
    platform_limits_domain: Optional[dict[str, int]] = None,
    platform_tab_limits: Optional[dict[str, dict[str, object]]] = None,
    cron: Optional[str] = None,
    keyword_groups: Optional[dict] = None,
) -> dict:
    """Create a NEW client, refusing to touch an existing one.

    WHY THIS EXISTS SEPARATELY FROM `upsert`: `upsert` keys on `_id`, so
    saving a brand-new client under an org id that is already taken did not
    fail -- it silently overwrote that client's keywords, caps and cron with
    the new one's. Two different customers entered under one org id became
    one document, and the first one's configuration was simply gone, with
    the UI reporting a successful save.

    `insert_one` against the unique `_id` index is what makes this safe
    under concurrency: two simultaneous creates of the same id cannot both
    win, because the second gets DuplicateKeyError from Mongo itself rather
    than from a check-then-write that another request can slip between.
    """
    from pymongo.errors import DuplicateKeyError

    now = datetime.now(timezone.utc)
    doc = {
        "_id": client_id,
        "created_at": now,
        # epoch-ms at insert time: monotonically increasing, so a new client
        # always sorts at the END of the rotation/list order by default.
        "order": int(now.timestamp() * 1000),
        **_config_fields(
            name, domain, name_keywords, domain_keywords,
            platform_limits_individual, platform_limits_domain,
            platform_tab_limits, cron, keyword_groups,
        ),
    }
    try:
        await db()[CLIENTS].insert_one(doc)
    except DuplicateKeyError:
        raise ConflictError(
            f"client id {client_id!r} is already taken -- pick a different org id, "
            "or open that client and edit it instead"
        )
    return _to_out(doc)


async def upsert(
    client_id: str, name: str, domain: str = "",
    name_keywords: Optional[list[str]] = None, domain_keywords: Optional[list[str]] = None,
    platform_limits_individual: Optional[dict[str, int]] = None,
    platform_limits_domain: Optional[dict[str, int]] = None,
    platform_tab_limits: Optional[dict[str, dict[str, object]]] = None,
    cron: Optional[str] = None,
    keyword_groups: Optional[dict] = None,
) -> dict:
    """`cron` is optional, a client with keywords but no cron only ever
    gets swept when `POST /discovery` is called for it explicitly; setting
    cron additionally schedules an automatic recurring sweep (see
    sessions/manager.py / services/scheduler_service.py).

    Creates the client when it does not exist. For the "the analyst is
    adding a NEW client" case use `create` instead, which refuses to
    overwrite one that is already there -- see its docstring.
    """
    now = datetime.now(timezone.utc)
    await db()[CLIENTS].update_one(
        {"_id": client_id},
        {
            "$set": _config_fields(
                name, domain, name_keywords, domain_keywords,
                platform_limits_individual, platform_limits_domain,
                platform_tab_limits, cron, keyword_groups,
            ),
            # legacy pre-split field, if any, is superseded the moment this
            # client is saved through the current form, _to_out's own
            # fallback only ever needs to cover a document nobody has
            # resaved since the split, never one that just went through here.
            "$unset": {"platform_limits": ""},
            # epoch-ms at insert time: monotonically increasing, so a new
            # client is always created at the END of the rotation/list order
            # by default, never touched again by a later edit-and-resave
            # through this same upsert (see `reorder` for the only thing
            # that changes it afterward).
            "$setOnInsert": {"_id": client_id, "created_at": now, "order": int(now.timestamp() * 1000)},
        },
        upsert=True,
    )
    doc = await db()[CLIENTS].find_one({"_id": client_id})
    return _to_out(doc)


async def set_scheduler_prefs(
    client_id: str, *, platforms: Optional[list[str]] = None,
    keyword_scope: Optional[str] = None,
    facebook_tabs: Optional[list[str]] = None,
    budget_minutes: Optional[int] = None,
) -> dict:
    """What the Scheduler should sweep for this client, and with which
    keywords. Persisted so the choice survives the queue being cleared, the
    browser being closed, and the analyst moving to another machine.

    NOT PART OF `_config_fields`, and that is the whole point. `upsert`
    writes a fixed set of configuration keys; folding these in would mean
    saving a client from the Clients form -- which knows nothing about
    scheduler preferences and sends none -- silently reset them to empty.
    That is the same shape of bug as the client-overwrite this repository
    already had once, so this is a separate narrow `$set` that touches these
    fields and nothing else.

    `platforms=[]` means every ready platform, which is exactly what
    omitting `platforms` means to the discovery API. `keyword_scope=""`
    means both keyword types. `facebook_tabs=[]` means all FB tabs.
    """
    fields: dict[str, Any] = {}
    if platforms is not None:
        fields["scheduler_platforms"] = [
            p.strip().lower() for p in platforms if str(p).strip()
        ]
    if keyword_scope is not None:
        scope = (keyword_scope or "").strip().lower()
        if scope not in ("", "individual", "domain"):
            raise ValidationError(
                f"keyword_scope must be 'individual', 'domain' or empty, not {scope!r}")
        fields["scheduler_keyword_scope"] = scope
    if facebook_tabs is not None:
        fields["scheduler_facebook_tabs"] = [
            t.strip().lower() for t in facebook_tabs if str(t).strip()
        ]
    if budget_minutes is not None:
        try:
            fields["scheduler_budget_minutes"] = max(0, int(budget_minutes))
        except (ValueError, TypeError):
            fields["scheduler_budget_minutes"] = 0
    if not fields:
        return await get(client_id)

    res = await db()[CLIENTS].update_one({"_id": client_id}, {"$set": fields})
    if res.matched_count == 0:
        raise NotFoundError(f"client {client_id!r} not found")
    return await get(client_id)


async def reorder(client_ids: list[str]) -> None:
    """Persists an analyst's drag-to-reorder of the Scheduler tab's client
    list: `client_ids` is the FULL desired order, front to back. Each gets
    `order` set to its index, so the round-robin engine's rotation (built
    from `list_all()`, sorted by this same field) and the Scheduler tab's
    own listing both reflect the new order on their very next read, no
    restart needed. A client id this list omits (deleted mid-drag, e.g.)
    is silently skipped rather than erroring the whole batch."""
    from pymongo import UpdateOne

    ops = [
        UpdateOne({"_id": cid}, {"$set": {"order": i}})
        for i, cid in enumerate(client_ids)
    ]
    if ops:
        await db()[CLIENTS].bulk_write(ops, ordered=False)


async def add_keyword(client_id: str, keyword: str, kind: str) -> None:
    """Appends `keyword` as a new PARENT to a client's name_keywords
    (kind="name") or domain_keywords (kind="domain") without touching the
    rest of the document. Unlike `upsert` this never replaces the array
    wholesale, so it's safe to call from a flow (e.g. add_manual_urls) that
    only knows about the one new keyword, not the client's full configured
    set. `$addToSet` makes it idempotent: adding the same keyword twice is
    a no-op, not a duplicate entry.

    The new parent is added to `keyword_groups` too, with NO children --
    it searches itself, which is exactly right for a keyword an analyst
    just introduced by pasting a URL rather than by curating permutations
    for it. Skipping this would leave the flat list and the groups
    disagreeing, and `groups_for_client` treats non-empty groups as
    authoritative, so the new keyword would be stored but never actually
    swept. Read-modify-write rather than a `$addToSet` on a nested array
    because groups are objects keyed by `parent`, which `$addToSet` cannot
    dedupe on.
    """
    field = "name_keywords" if kind == "name" else "domain_keywords"
    kw_type = _keywords.INDIVIDUAL if kind == "name" else _keywords.DOMAIN

    doc = await db()[CLIENTS].find_one({"_id": client_id})
    if doc is None:
        return
    groups = _keywords.groups_for_client(doc)
    if not any(g["parent"].strip().lower() == keyword.strip().lower()
               for g in groups.get(kw_type, [])):
        groups.setdefault(kw_type, []).append({"parent": keyword, "children": []})

    await db()[CLIENTS].update_one(
        {"_id": client_id},
        {"$addToSet": {field: keyword}, "$set": {"keyword_groups": groups}},
    )


async def get(client_id: str) -> dict:
    doc = await db()[CLIENTS].find_one({"_id": client_id})
    if doc is None:
        raise NotFoundError(f"client {client_id!r} not found")
    return _to_out(doc)


async def try_get(client_id: str) -> Optional[dict]:
    """Like `get`, but returns None instead of raising, for internal
    engine code that needs to check existence without a 404 semantics."""
    doc = await db()[CLIENTS].find_one({"_id": client_id})
    return _to_out(doc) if doc else None


async def list_all() -> list[dict]:
    """Every client, used by the scheduler's cron sync and the analysis
    catch-up sweep, which operate across all of them, AND by the
    round-robin engine to build its rotation -- sorted by `order` (then
    `_id` as a stable tiebreaker for the handful of pre-existing documents
    that predate the field and share order=0), which is exactly what makes
    an analyst's Scheduler-tab reorder change the engine's actual rotation
    sequence, not just the tab's own display order."""
    return [_to_out(d) async for d in db()[CLIENTS].find({}).sort([("order", 1), ("_id", 1)])]


async def delete(client_id: str) -> dict:
    doc = await db()[CLIENTS].find_one_and_delete({"_id": client_id})
    if doc is None:
        raise NotFoundError(f"client {client_id!r} not found")
    return _to_out(doc)


async def ensure_indexes() -> None:
    pass  # _id is already the unique key; nothing extra to index here
