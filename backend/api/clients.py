"""Clients API: the org records discovery and analysis are scoped to.

    GET    /clients                 every client, in list order
    POST   /clients                 create a new one (409 if the id is taken)
    GET    /clients/{client_id}     read one
    PUT    /clients/{client_id}     edit an existing one (404 if it is not there)
    PUT    /clients/reorder         persist a drag-to-reorder of the list
    DELETE /clients/{client_id}     delete one

A client is one document in Mongo, keyed by the caller's own `client_id`
(the org id an analyst types), and it owns its own keywords, its own
per-platform scrape caps and its own cron. THIS IS THE STORE. It used to be
this browser's localStorage, which meant a client existed only on the
machine that created it and could not be shared, backed up, or read by the
server that actually runs the sweeps.

WHY CREATE AND EDIT ARE DIFFERENT ROUTES. Saving went through a single
upsert keyed on `_id`, so entering a NEW client under an org id that was
already taken did not fail -- it overwrote that client's keywords, caps and
cron with the new one's, reported success, and left two customers sharing
one record with the first one's configuration gone. `POST` now refuses an
id that exists and `PUT` refuses one that does not, so neither can quietly
turn into the other.

NOTHING HERE MERGES CLIENTS. Every read returns one document, every write
touches one `_id`, and the keyword lists are stored per client and per kind
(individual vs domain) exactly as the analyst curated them -- see
`database/repositories/client_repository.py::_to_out`.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, Field

from backend.database.repositories import client_repository as clients_db
from backend.database.repositories import logo_repository as logos_db
from backend.shared.errors import ValidationError

router = APIRouter(prefix="/clients", tags=["clients"])


class KeywordGroupModel(BaseModel):
    """One curated search term and the permutations swept for it. The
    parent is what results are matched and filed against; the children are
    what actually gets searched. A parent with no children searches
    itself."""

    parent: str
    children: list[str] = Field(default_factory=list)


class ClientBody(BaseModel):
    """One client's configuration, as the Clients form produces it."""

    name: str = ""
    domain: str = ""
    # Flat lists, kept for callers that have no groups. When
    # `keyword_groups` is supplied it wins and these are re-derived from it,
    # so the two can never be stored disagreeing -- see the repository.
    name_keywords: list[str] = Field(default_factory=list)
    domain_keywords: list[str] = Field(default_factory=list)
    keyword_groups: dict[str, list[KeywordGroupModel]] = Field(default_factory=dict)
    # Per-platform result caps, scoped separately to individual-keyword and
    # domain-keyword sweeps. A platform absent from either map means
    # "scrape everything" for that keyword type.
    platform_limits_individual: dict[str, int] = Field(default_factory=dict)
    platform_limits_domain: dict[str, int] = Field(default_factory=dict)
    # {platform: {tab: {"individual"|"domain": cap}}}, for platforms with
    # more than one discovery tab (currently only Facebook).
    platform_tab_limits: dict[str, dict[str, dict[str, int]]] = Field(default_factory=dict)
    cron: Optional[str] = None


class NewClientBody(ClientBody):
    """A create additionally carries the org id, which an edit takes from
    the path instead -- an id is chosen once and never changed, since
    everything already discovered is filed under it."""

    client_id: str


class SchedulerPrefsBody(BaseModel):
    """What the Scheduler should run for this client. All optional: send
    only the fields you are changing."""

    platforms: Optional[list[str]] = Field(
        None,
        description="Platform ids to sweep. EMPTY LIST means every ready "
                    "platform -- the same thing omitting `platforms` means to "
                    "POST /discovery/jobs.")
    keyword_scope: Optional[str] = Field(
        None,
        description="'individual' | 'domain' | '' for both.")
    facebook_tabs: Optional[list[str]] = Field(
        None,
        description="Facebook tabs to sweep: 'people', 'pages', 'groups'. Empty list means all.")
    budget_minutes: Optional[int] = Field(
        None,
        description="Per-sweep time budget in minutes. 0 or None = default (15m).")


class ClientOut(BaseModel):
    """A stored client, read back. Extra keys the repository adds over time
    (run history, scheduler flags) pass through untouched."""

    model_config = {"extra": "allow"}

    client_id: str
    name: str = ""
    domain: str = ""
    name_keywords: list[str] = Field(default_factory=list)
    domain_keywords: list[str] = Field(default_factory=list)
    keyword_groups: dict = Field(default_factory=dict)
    platform_limits_individual: dict = Field(default_factory=dict)
    platform_limits_domain: dict = Field(default_factory=dict)
    platform_tab_limits: dict = Field(default_factory=dict)
    order: int = 0
    cron: Optional[str] = None
    scheduler_platforms: list[str] = Field(
        default_factory=list,
        description="Scheduler-only: which platforms to sweep. Empty = all.")
    scheduler_keyword_scope: str = Field(
        "", description="Scheduler-only: 'individual' | 'domain' | '' for both.")
    scheduler_facebook_tabs: list[str] = Field(
        default_factory=list,
        description="Scheduler-only: which FB tabs to sweep. Empty = all.")
    scheduler_budget_minutes: int = Field(
        0, description="Scheduler-only: sweep time budget in minutes. 0 = default.")


class ClientList(BaseModel):
    items: list[ClientOut]


class ReorderBody(BaseModel):
    """The FULL desired order, front to back."""

    client_ids: list[str]


def _groups_payload(body: ClientBody) -> dict:
    """Pydantic models -> the plain {type: [{parent, children}]} the
    repository stores. Empty stays empty, which is what tells the
    repository to fall back to the flat lists."""
    return {
        kw_type: [g.model_dump() for g in groups]
        for kw_type, groups in (body.keyword_groups or {}).items()
    }


def _config(body: ClientBody) -> dict:
    return {
        "name": body.name,
        "domain": body.domain,
        "name_keywords": body.name_keywords,
        "domain_keywords": body.domain_keywords,
        "platform_limits_individual": body.platform_limits_individual,
        "platform_limits_domain": body.platform_limits_domain,
        "platform_tab_limits": body.platform_tab_limits,
        "cron": body.cron,
        "keyword_groups": _groups_payload(body),
    }


@router.get("", response_model=ClientList, summary="List every client")
async def list_clients() -> dict:
    """In list order (the Scheduler tab's drag order), oldest-created
    first among clients that predate that field."""
    return {"items": await clients_db.list_all()}


@router.post("", response_model=ClientOut, status_code=status.HTTP_201_CREATED,
             summary="Create a new client")
async def create_client(body: NewClientBody) -> dict:
    """409 when the org id is already taken -- that is a DIFFERENT client
    with the same id, and silently overwriting it is what this refuses to
    do. Edit the existing one with PUT instead."""
    client_id = body.client_id.strip()
    if not client_id:
        raise ValidationError("client_id is required")
    return await clients_db.create(client_id=client_id, **_config(body))


# Declared BEFORE /{client_id}: a PUT to /clients/reorder would otherwise
# match the parameterised route with client_id="reorder".
@router.put("/reorder", response_model=ClientList, summary="Persist list order")
async def reorder_clients(body: ReorderBody) -> dict:
    await clients_db.reorder(body.client_ids)
    return {"items": await clients_db.list_all()}


@router.put("/{client_id}/scheduler-prefs", response_model=ClientOut,
            summary="What the Scheduler should run for this client")
async def set_scheduler_prefs(
    body: SchedulerPrefsBody, client_id: str = Path(...),
) -> dict:
    """Remembered until an analyst changes it again.

    A NARROW write: it touches these two fields only, so saving the client
    from the Clients form -- which sends no scheduler preferences -- can
    never reset them, and setting them here can never disturb the keywords
    or caps. See `client_repository.set_scheduler_prefs`.
    """
    return await clients_db.set_scheduler_prefs(
        client_id,
        platforms=body.platforms,
        keyword_scope=body.keyword_scope,
        facebook_tabs=body.facebook_tabs,
        budget_minutes=body.budget_minutes,
    )


@router.get("/{client_id}", response_model=ClientOut, summary="Read one client")
async def get_client(client_id: str = Path(..., description="The org id")) -> dict:
    return await clients_db.get(client_id)


@router.put("/{client_id}", response_model=ClientOut, summary="Edit a client")
async def update_client(body: ClientBody, client_id: str = Path(...)) -> dict:
    """404 when there is nothing at this id. Editing is deliberately not
    allowed to bring a client into existence: that path is POST, which is
    the one that checks the id is free."""
    await clients_db.get(client_id)  # raises NotFoundError -> 404
    return await clients_db.upsert(client_id=client_id, **_config(body))


@router.delete("/{client_id}", response_model=ClientOut, summary="Delete a client")
async def delete_client(client_id: str = Path(...)) -> dict:
    """Deletes the CLIENT RECORD -- its keywords, caps and cron. Discovery
    profiles already found under this org id live in their own collection
    and are not touched, so re-creating a client with the same id shows
    them again.

    Reference LOGOS do go: they are this client's configuration, not
    discovered data, and leaving them behind would orphan blobs nothing can
    ever reach again."""
    deleted = await clients_db.delete(client_id)
    try:
        await logos_db.delete_for_client(client_id)
    except Exception:      # noqa: BLE001 - the client is already gone
        pass
    return deleted
