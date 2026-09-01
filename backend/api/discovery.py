"""Discovery API: keywords in, candidate profiles out, triaged, sent to analysis.

    POST   /discovery/jobs                start a sweep
    GET    /discovery/jobs/{job_id}       poll it
    POST   /discovery/jobs/{job_id}/cancel
    GET    /discovery/profiles            read the results back, as cards
    POST   /discovery/profiles/status     validate or reject cards
    POST   /discovery/profiles/delete     permanently delete cards (platform/status scoped)
    POST   /discovery/profiles/analyse    send validated cards to analysis
    GET    /discovery/platforms           which platforms can be swept now

THE WORKFLOW THIS IS BUILT AROUND: keywords go in, candidate profiles come
back as cards (`GET /discovery/profiles`, `status=pending` by default). An
analyst marks each one `validated` or `rejected`
(`POST /discovery/profiles/status`). Filtering the same GET by
`status=validated` is the "Validated" tab. From there, either read the
`url` off each card directly, or call `POST /discovery/profiles/analyse`,
which pulls every validated URL and hands it straight to the analysis
engine (`backend/analysis/runner.py`) in one call -- no copy-pasting
required. Analysis's own results are memory-only and never touch this
collection; see `backend/analysis/runner.py`'s docstring for that half.

Sweeps take minutes, so job creation returns 202 with a job id and a
`poll_url`. Results are written to MongoDB as each sweep completes, so
`GET /discovery/profiles` returns rows while the job is still running --
a caller does not have to wait for the job to finish to start consuming.

`group_id` partitions a caller's own results (one brand, one customer, one
investigation) and, with platform+url, is the dedup key: re-running the
same keywords updates the rows it already found instead of duplicating
them. Any stable string works.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from fastapi import APIRouter, Path, Query, Response, status
from pydantic import BaseModel, Field

from backend.analysis.runner import analysis_runner
from backend.api.analysis import StartAnalysisAccepted
from backend.api.models import (CancelResult, JobAccepted, JobStatus, Platform,
                                 PlatformState, PlatformStateList, SkippedInput)
from backend.database.repositories import profile_repository as profiles_db
from backend.discovery.runner import discovery_runner
from backend.shared.errors import NotFoundError, ValidationError
from backend.shared.pagination import DEFAULT_LIMIT, MAX_LIMIT

router = APIRouter(prefix="/discovery", tags=["discovery"])


class ProfileStatus(str, Enum):
    """An analyst's triage decision on one discovered profile.

    `validated` is this API's name for what the underlying store still
    calls `approved` internally (see `_TO_DB_STATUS` below) -- the
    database layer is unchanged on purpose, this is a naming translation
    at the API boundary only, not a second status field."""

    pending = "pending"
    validated = "validated"
    rejected = "rejected"


_TO_DB_STATUS = {"pending": "pending", "validated": "approved", "rejected": "rejected"}
_FROM_DB_STATUS = {"pending": "pending", "approved": "validated", "rejected": "rejected"}


# ---------------------------------------------------------------- requests

class StartDiscovery(BaseModel):
    group_id: str = Field(
        ..., min_length=1, max_length=128,
        description="Partitions your results and is part of the dedup key. "
                    "Re-running the same keywords under the same group_id "
                    "updates the rows already found rather than duplicating.",
        examples=["acme-corp"],
    )
    individual_keywords: list[str] = Field(
        default_factory=list,
        description="Executive/person-name search terms -- swept under "
                    "`platform_limits_individual` and the individual cell of "
                    "`platform_tab_limits`. At least one of individual_keywords/"
                    "domain_keywords is required.",
        examples=[["Gautam Adani", "gautamadani"]],
    )
    domain_keywords: list[str] = Field(
        default_factory=list,
        description="Brand/domain search terms -- swept under "
                    "`platform_limits_domain` and the domain cell of "
                    "`platform_tab_limits`.",
        examples=[["acme", "acme corp", "acmecorp official"]],
    )
    platforms: Optional[list[Platform]] = Field(
        None,
        description="Omit to sweep every platform that has a usable session. "
                    "Naming a platform with no usable session returns it under "
                    "`skipped` with the reason, rather than failing the request.",
    )
    max_results: int = Field(
        0, ge=0,
        description="Blanket per-(keyword,tab) cap, used only where the more "
                    "specific caps below don't set a tighter one for that cell "
                    "(see platform_limits_individual/_domain and "
                    "platform_tab_limits). 0 = uncapped -- search on some "
                    "platforms is effectively endless, so a cap is usually "
                    "what you want somewhere in this stack.",
    )
    max_seconds: Optional[float] = Field(
        None, gt=0,
        description="Per-sweep time budget. Omit to use the server default.",
    )
    platform_limits_individual: dict[str, int] = Field(
        default_factory=dict,
        description="platform id -> max results per individual-type keyword "
                    "on that platform, across all its tabs. 0/absent = uncapped "
                    "by this cap specifically (a less specific cap may still apply).",
        examples=[{"facebook": 20, "twitter": 50}],
    )
    platform_limits_domain: dict[str, int] = Field(
        default_factory=dict,
        description="Same as platform_limits_individual, for domain-type keywords.",
    )
    platform_tab_limits: dict[str, dict[str, dict[str, int]]] = Field(
        default_factory=dict,
        description="platform id -> tab -> keyword type (\"individual\"/\"domain\") "
                    "-> max results for that exact cell. Only Facebook has more "
                    "than one tab (people/pages/groups) today, so this is the "
                    "only platform where it does anything beyond the flat caps "
                    "above; every other platform's tab is its whole sweep. "
                    "The MORE RESTRICTIVE of a cell's own cap here and the "
                    "matching flat cap above applies when both are set for that "
                    "cell; uncapped only when neither is.",
        examples=[{"facebook": {"people": {"individual": 5, "domain": 20}}}],
    )


# --------------------------------------------------------------- responses

class PlatformSweepState(BaseModel):
    platform: str
    display_name: str
    status: str = Field(
        ...,
        description="pending | running | done | partial | failed | skipped. "
                    "`partial` means the platform was swept but not "
                    "exhaustively (a cap fired, or a sweep stopped early); "
                    "`skipped` means it was never attempted -- see `note`.",
    )
    keywords_total: int
    keywords_done: int
    found: int = Field(..., description="Profiles written for this platform.")
    new: int = Field(..., description="Of `found`, how many were not already known.")
    note: str = ""
    current_keyword: Optional[str] = ""
    current_tab: Optional[str] = ""
    current_step: Optional[str] = ""
    item_started_at_ts: Optional[float] = None
    started_at_ts: Optional[float] = None
    finished_at_ts: Optional[float] = None


class CompletedSweepTelemetry(BaseModel):
    platform: str
    display_name: str
    keyword: str
    tab: str
    duration_seconds: float
    hits_found: int
    hits_new: int
    timestamp: str


class DiscoveryJobState(BaseModel):
    job_id: str
    group_id: str
    status: JobStatus
    keywords: list[str]
    message: str = ""
    total: int = Field(..., description="Sweep units planned (keywords x tabs, summed over platforms).")
    completed: int
    found: int
    new: int
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    started_at_ts: Optional[float] = None
    finished_at_ts: Optional[float] = None
    elapsed_seconds: Optional[float] = None
    estimated_remaining_seconds: Optional[float] = None
    platforms: list[PlatformSweepState]
    history: list[CompletedSweepTelemetry] = Field(default_factory=list)


class StartDiscoveryAccepted(JobAccepted):
    platforms_queued: list[str] = Field(..., description="Platforms this job will actually sweep.")
    skipped: list[SkippedInput] = Field(
        ..., description="Platforms that will NOT be swept, each with the reason.",
    )


class DiscoveredProfile(BaseModel):
    """One candidate found by discovery. Fields a given platform's search
    payload does not carry are null/absent rather than guessed -- see
    `source` for where this row's values came from."""

    id: str
    group_id: str
    platform: str
    url: str
    status: ProfileStatus = Field(
        ProfileStatus.pending,
        description="An analyst's triage decision. New profiles start `pending`; "
                    "set it with POST /discovery/profiles/status.",
    )
    entity_id: str = ""
    entity_type: str = Field("", description="profile | page | group | channel")
    display_name: str = ""
    username: str = ""
    profile_image_url: str = ""
    has_logo: Optional[bool] = None
    verified: Optional[bool] = None
    followers: Optional[int] = None
    friends: Optional[int] = None
    location: str = ""
    bio: str = ""
    created_at: str = Field("", description="Account creation date (YYYY-MM-DD) where the platform publishes one.")
    keywords: list[str] = Field(default_factory=list, description="Every keyword whose sweep has found this profile.")
    name_score: Optional[int] = Field(None, description="0-100 similarity of the profile name to the keyword.")
    name_exact_run: Optional[bool] = Field(
        None,
        description="True High Match: the keyword's letters appear in the profile name as one "
                    "contiguous run, punctuation/spacing/case ignored (see "
                    "shared/text.py::contiguous_letters_match). This, not a threshold on "
                    "name_score, is what the High/Medium/Low Match filter should gate on -- "
                    "name_score alone is word-order-insensitive and rates a reordered name "
                    "(e.g. \"Adani Gautam\" for keyword \"Gautam Adani\") as a full match, "
                    "which this catches and name_score cannot.",
    )
    source: str = ""
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None


class DiscoveredProfilePage(BaseModel):
    items: list[DiscoveredProfile]
    total: int = Field(..., description="Matching rows in total, not just this page.")
    limit: int
    offset: int


def _to_profile(doc: dict) -> DiscoveredProfile:
    def iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else (v or None)

    return DiscoveredProfile(
        id=str(doc.get("id") or doc.get("_id") or ""),
        group_id=doc.get("client_id", ""),
        platform=doc.get("platform", ""),
        url=doc.get("url", ""),
        status=_FROM_DB_STATUS.get(doc.get("status", "pending"), ProfileStatus.pending),
        entity_id=doc.get("entity_id", "") or "",
        entity_type=doc.get("entity_type", "") or "",
        display_name=doc.get("display_name", "") or "",
        username=doc.get("username", "") or "",
        profile_image_url=doc.get("profile_image_url", "") or "",
        has_logo=doc.get("has_logo"),
        verified=doc.get("verified"),
        followers=doc.get("followers"),
        friends=doc.get("friends"),
        location=doc.get("location", "") or "",
        bio=doc.get("bio", "") or "",
        created_at=doc.get("created_at", "") or "",
        keywords=list(doc.get("keywords") or []),
        name_score=doc.get("name_score"),
        name_exact_run=doc.get("name_exact_run"),
        source=doc.get("discovery_source", "") or "",
        first_seen=iso(doc.get("first_seen")),
        last_seen=iso(doc.get("last_seen")),
    )


# ----------------------------------------------------------------- routes

@router.get("/platforms", response_model=PlatformStateList,
            summary="Which platforms can be swept right now")
async def platforms() -> PlatformStateList:
    """Call this before starting a job to see which platforms have a usable
    session. A platform whose `session_state` is not `ready` will be
    reported under `skipped` if you ask for it."""
    from backend.platforms import registry

    items = []
    for pid, plat in registry.PLATFORMS.items():
        items.append(PlatformState(
            platform=pid, name=plat.name, enabled=plat.enabled,
            session_state=await registry.session_state(plat),
            can_discover=plat.can_discover,
            stability_note=plat.stability_note,
        ))
    return PlatformStateList(items=items)


@router.post("/jobs", response_model=StartDiscoveryAccepted,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Start a keyword sweep")
async def start_discovery(body: StartDiscovery) -> StartDiscoveryAccepted:
    """Sweeps every selected platform for every keyword and writes what it
    finds to storage. Returns immediately -- poll `poll_url`.

    A request naming only platforms that have no usable session is still
    accepted (202) and completes immediately with every platform under
    `skipped`; that is a configuration problem to read off the response,
    not a malformed request."""
    individual = [k.strip() for k in body.individual_keywords if k and k.strip()]
    domain = [k.strip() for k in body.domain_keywords if k and k.strip()]
    if not individual and not domain:
        raise ValidationError(
            "individual_keywords or domain_keywords must contain at least one non-empty term"
        )

    job, skipped = await discovery_runner.start(
        group_id=body.group_id.strip(),
        individual_keywords=individual,
        domain_keywords=domain,
        platforms=[p.value for p in body.platforms] if body.platforms else None,
        max_results=body.max_results,
        max_seconds=body.max_seconds,
        platform_limits_individual=body.platform_limits_individual,
        platform_limits_domain=body.platform_limits_domain,
        platform_tab_limits=body.platform_tab_limits,
    )
    return StartDiscoveryAccepted(
        job_id=job.id, status=JobStatus(job.status),
        poll_url=f"/discovery/jobs/{job.id}",
        platforms_queued=[p for p, s in job.platforms.items() if s.status != "skipped"],
        skipped=[SkippedInput(value=k, reason=v) for k, v in skipped.items()],
    )


@router.get("/jobs/{job_id}", response_model=DiscoveryJobState,
            summary="Poll a sweep")
async def get_job(job_id: str = Path(..., description="From POST /discovery/jobs")) -> DiscoveryJobState:
    """Stop polling once `status` is `done`, `cancelled` or `failed`.

    Job state is held in memory and ages out; the PROFILES it wrote are in
    storage and outlive it, so a 404 here does not mean the results are
    gone -- read them from `GET /discovery/profiles`."""
    job = await discovery_runner.get(job_id)
    if job is None:
        raise NotFoundError(
            f"no discovery job {job_id!r} -- job state is in-memory and ages out; "
            "any profiles it found are still available from GET /discovery/profiles"
        )
    d = job.to_dict()
    return DiscoveryJobState(**{**d, "platforms": [PlatformSweepState(**p) for p in d["platforms"]]})


@router.post("/jobs/{job_id}/cancel", response_model=CancelResult,
             summary="Cancel a running sweep")
async def cancel_job(job_id: str) -> dict:
    """Cancellation is checked between sweeps, so a job stops at the next
    boundary rather than instantly. Profiles already written stay written."""
    cancelled = await discovery_runner.cancel(job_id)
    return {"cancelled": cancelled}


@router.get("/profiles", response_model=DiscoveredProfilePage,
            summary="Read discovered profiles")
async def list_profiles(
    group_id: str = Query(..., min_length=1, description="Required: scopes the query to your own results."),
    platform: Optional[Platform] = None,
    status_: Optional[ProfileStatus] = Query(
        None, alias="status",
        description="Filter by triage decision. Omit for every status. "
                    "`validated` is the \"Validated\" tab -- what "
                    "POST /discovery/profiles/analyse also reads from.",
    ),
    keyword: Optional[str] = Query(None, description="Only profiles found under this keyword."),
    search: Optional[str] = Query(None, max_length=200, description="Substring match on name/username/url."),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> DiscoveredProfilePage:
    """Durable results, readable while the job that produced them is still
    running. Ordering is stable, so `limit`/`offset` paging is safe."""
    docs, total, _ = await profiles_db.find(
        group_id, platform=platform.value if platform else None,
        status=_TO_DB_STATUS[status_.value] if status_ else None,
        phase=profiles_db.PHASE_DISCOVERY,
        keyword=keyword, search=search, limit=limit, offset=offset,
        include_held=True,
    )
    return DiscoveredProfilePage(
        items=[_to_profile(d) for d in docs], total=total, limit=limit, offset=offset,
    )


# ------------------------------------------------------------------ triage

class SetProfileStatus(BaseModel):
    ids: list[str] = Field(
        ..., min_length=1, max_length=1000,
        description="Profile ids from GET /discovery/profiles.",
    )
    status: ProfileStatus


class ProfileStatusResult(BaseModel):
    updated: list[str] = Field(..., description="Ids whose status was changed.")
    failed: list[SkippedInput] = Field(
        ..., description="Ids that could NOT be updated, each with why -- "
                         "one bad id never fails the rest of the batch.",
    )


@router.post("/profiles/status", response_model=ProfileStatusResult,
             summary="Validate or reject discovered profiles")
async def set_profile_status(body: SetProfileStatus) -> ProfileStatusResult:
    """An analyst's triage decision, applied to one or many cards at once.
    `validated` is what moves a profile into the set
    `POST /discovery/profiles/analyse` will pick up; `rejected` removes it
    from view without deleting it (re-discovery can reconsider a rejected
    profile if it changes -- that reconciliation lives in the store this
    calls into unchanged); `pending` undoes either decision."""
    db_status = _TO_DB_STATUS[body.status.value]
    updated: list[str] = []
    failed: list[SkippedInput] = []
    for pid in body.ids:
        try:
            await profiles_db.patch(pid, {"status": db_status})
            updated.append(pid)
        except Exception as e:
            failed.append(SkippedInput(value=pid, reason=str(e)))
    return ProfileStatusResult(updated=updated, failed=failed)


class DeletePlatformDataResult(BaseModel):
    deleted: int = Field(..., description="Discovery-phase profile rows permanently removed.")


@router.post("/profiles/delete", response_model=DeletePlatformDataResult,
             summary="Delete discovered profiles for a group, optionally scoped to one platform/status")
async def delete_platform_data(
    group_id: str = Query(..., min_length=1),
    platform: Optional[Platform] = Query(None, description="Omit to delete across every platform."),
    status_: Optional[ProfileStatus] = Query(
        None, alias="status", description="Omit to delete every triage status.",
    ),
) -> DeletePlatformDataResult:
    """Irreversibly deletes discovery-phase profile rows for this group --
    "delete what I'm currently looking at" on the Live Results filters
    (same platform/status scoping `GET /discovery/profiles` uses). Analysis
    is untouched by definition: its results are memory-only and never
    stored against this group_id in the first place. `platform` and
    `status` both omitted deletes every discovery row for this group --
    the caller (the UI's confirmation dialog) is what stands between an
    analyst and that, not this endpoint."""
    result = await profiles_db.delete_matching(
        group_id, platform.value if platform else None,
        phase=profiles_db.PHASE_DISCOVERY,
        status=_TO_DB_STATUS[status_.value] if status_ else None,
    )
    return DeletePlatformDataResult(deleted=result["deleted"])


# ---------------------------------------------------------------- to analysis

class AnalyseValidated(BaseModel):
    group_id: str = Field(..., min_length=1)
    platform: Optional[Platform] = Field(
        None, description="Scope to one platform's validated profiles. Omit for all.",
    )
    ids: Optional[list[str]] = Field(
        None,
        description="Analyse exactly these profile ids instead of every validated "
                    "one -- for sending a hand-picked subset of the Validated tab. "
                    "Each must already be `validated`; anything else is skipped.",
    )
    target_name: str = Field(
        "", max_length=200,
        description="The real brand or person being impersonated, applied to the "
                    "whole batch -- see POST /analysis/jobs for what this scores.",
    )
    official_feed: str = Field("", max_length=500)
    domain: str = Field(
        "", max_length=500,
        description="The client's own domain, as typed when the client was "
                    "created -- written to the analysis export's incident-row "
                    "Domain column (OrgId comes from group_id, which is the "
                    "client_id by convention). Discovery has no client record "
                    "to read this from itself, so the caller (which does have "
                    "the client on hand) forwards it here explicitly, same "
                    "reasoning as the platform_limits_*/platform_tab_limits "
                    "caps on POST /discovery/jobs.",
    )


# Hard ceiling on one "analyse validated" call, same reasoning as
# /analysis/jobs's own MAX_URLS_PER_JOB: an unbounded batch would pin a
# browser session for an unbounded time. A group with more validated
# profiles than this sends them in more than one call.
_MAX_VALIDATED_PER_ANALYSE = 500


async def _validated_docs(group_id: str, platform: Optional[str]) -> list[dict]:
    """Every validated profile doc for a group (+ optional platform), paged
    through in full rather than capped at one page -- a caller asking to
    "analyse everything validated" must not silently get only the first
    100."""
    docs_out: list[dict] = []
    offset = 0
    while len(docs_out) < _MAX_VALIDATED_PER_ANALYSE:
        docs, total, _ = await profiles_db.find(
            group_id, platform=platform, status="approved",
            phase=profiles_db.PHASE_DISCOVERY, limit=MAX_LIMIT, offset=offset,
            include_held=True,
        )
        docs_out.extend(d for d in docs if d.get("url"))
        offset += len(docs)
        if offset >= total or not docs:
            break
    return docs_out[:_MAX_VALIDATED_PER_ANALYSE]


# Discovery-doc fields worth carrying into analysis as a starting point --
# same names DISCOVERY_FIELDS writes them under (see profile_repository.py).
# entity_type/username/discovery_source are deliberately left out: analysis
# has no matching slot for them on Row (see shared/models/row.py) and would
# have nowhere to go.
_SEED_FIELDS = (
    "entity_id", "display_name", "profile_image_url", "has_logo", "verified",
    "followers", "friends", "location", "bio", "created_at", "name_score",
)


def _seed_from_doc(doc: dict) -> dict:
    """Whatever this discovery doc already knows, keyed the same as
    `_SEED_FIELDS` -- blank/None entries dropped, so `one()`'s "is this
    already known" check (`known.get(field)`) never has to distinguish a
    real blank from a field discovery simply never populated.

    Plus `main_keyword`: the PARENT keyword whose sweep found this profile.
    Discovery stores parents (never the permutation that actually surfaced
    the hit -- see shared/keywords.py::resolve_parent), so `keywords[0]` is
    already the reportable name. Analysis carries it into the export's
    AssetName column, which otherwise has nothing better than the raw
    handle to put there.
    """
    seed = {f: doc.get(f) for f in _SEED_FIELDS}
    seed = {k: v for k, v in seed.items() if v not in (None, "")}
    if keywords := [k for k in (doc.get("keywords") or []) if k]:
        seed["main_keyword"] = keywords[0]
    return seed


@router.post("/profiles/analyse", response_model=StartAnalysisAccepted,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Send validated profiles to analysis")
async def analyse_validated(body: AnalyseValidated) -> StartAnalysisAccepted:
    """The "Analyse Validated Profiles" action: pulls every profile
    currently `validated` for this group (optionally scoped to one
    platform, or to a hand-picked `ids` list) and hands their URLs straight
    to the analysis engine -- equivalent to reading the URLs off the
    Validated tab and pasting them into `POST /analysis/jobs` yourself,
    minus the copy-paste. The returned job is a completely ordinary
    analysis job: memory-only results, same polling contract, same export.
    Discovery and analysis remain independent after this call starts --
    this endpoint only sources the URL list (plus whatever fields discovery
    already collected, passed along as a starting point so analysis does
    not re-scrape data this profile's sweep already read -- see
    `_seed_from_doc` and `AnalysisRunner.start`'s `seed_by_url`)."""
    if body.ids:
        docs = await profiles_db.get_by_ids(body.group_id, body.ids)
        ineligible = [d for d in docs if d.get("status") != "approved"]
        eligible = [d for d in docs if d.get("status") == "approved" and d.get("url")]
        missing = len(body.ids) - len(docs)
        skipped = [
            SkippedInput(value=str(d.get("id", "")), reason=f"status is {d.get('status')!r}, not validated")
            for d in ineligible
        ] + ([SkippedInput(value="(unresolved ids)", reason=f"{missing} id(s) not found for this group")] if missing else [])
    else:
        eligible = await _validated_docs(body.group_id, body.platform.value if body.platform else None)
        skipped = []

    urls = [d["url"] for d in eligible]
    seed_by_url = {d["url"]: _seed_from_doc(d) for d in eligible}

    if not urls:
        raise ValidationError(
            "no validated profiles to analyse -- validate some with "
            "POST /discovery/profiles/status first"
        )

    job, url_skipped = await analysis_runner.start(
        urls, body.target_name.strip(), body.official_feed.strip(), seed_by_url=seed_by_url,
        org_id=body.group_id, domain=body.domain.strip(),
    )
    return StartAnalysisAccepted(
        job_id=job.id, status=JobStatus(job.status),
        poll_url=f"/analysis/jobs/{job.id}",
        accepted=job.total,
        skipped=skipped + [SkippedInput(value=s["url"], reason=s["reason"]) for s in url_skipped],
    )
