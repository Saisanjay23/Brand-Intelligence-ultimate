"""Analysis API: profile URLs in, scored profiles out.

    POST   /analysis/jobs                              scrape a list of URLs
    GET    /analysis/jobs/{job_id}                     poll it
    POST   /analysis/jobs/{job_id}/cancel
    GET    /analysis/jobs/{job_id}/items/{item_id}/screenshot
    POST   /analysis/export/xlsx                       rows -> .xlsx bytes

    GET    /analysis/results                           results saved in the last 24h
    GET    /analysis/results/{result_id}/screenshot
    POST   /analysis/results/delete                    delete selected, or all

Analysis takes URLs and nothing else. It reads no client record and
nothing discovery produced, so a caller can analyse a URL that discovery
has never seen, and the two can be driven independently.

RESULTS ARE SAVED FOR 24 HOURS AND THEN DELETED BY MONGODB ITSELF. Each
scored profile (and each URL that failed) is written to `analysis_results`
as it settles, so a reload, a tab switch or a restart no longer costs an
analyst the batch -- and 24 hours later it is gone without anyone running
anything. See database/repositories/analysis_result_repository.py for how
that expiry is enforced twice over, and why once is not enough.

THE JOB AND THE SAVED RESULT ARE DIFFERENT LIFETIMES, deliberately. A job
(`/analysis/jobs/{id}`) is live progress: in memory, bounded, gone on
restart. A saved result is the reading itself. `result_id` on every item is
the same value in both, so a caller can lay a running job over the saved
set without showing one profile twice.
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Any, Optional

from fastapi import APIRouter, Path, Query, Response, status
from pydantic import BaseModel, Field

from backend.analysis.runner import analysis_runner
from backend.api.models import CancelResult, JobAccepted, JobStatus, SkippedInput
from backend.database.repositories import analysis_result_repository as results_db
from backend.shared.errors import NotFoundError, ValidationError

router = APIRouter(prefix="/analysis", tags=["analysis"])

# One paste should not be able to pin a browser session for an hour. Well
# above any realistic single batch; a caller with more URLs submits more
# jobs, which also gives it per-batch progress instead of one opaque run.
MAX_URLS_PER_JOB = 500


# ---------------------------------------------------------------- requests

class StartAnalysis(BaseModel):
    urls: list[str] = Field(
        ..., min_length=1, max_length=MAX_URLS_PER_JOB,
        description="Profile URLs to scrape. Platform is detected per URL from "
                    "its host; anything unrecognised comes back under `skipped` "
                    "rather than failing the request.",
        examples=[["https://x.com/example", "https://www.instagram.com/example/"]],
    )
    target_name: str = Field(
        "", max_length=200,
        description="The real brand or person being impersonated. Used to score "
                    "name similarity, and written to the export as Original Name. "
                    "Omit it and name-match scoring has nothing to compare against.",
        examples=["Acme Corp"],
    )
    official_feed: str = Field(
        "", max_length=500,
        description="The genuine account, for comparison. Recorded in the export "
                    "as Original feed; not scraped.",
    )


class ExportXlsx(BaseModel):
    filename: str = Field("analysis.xlsx", max_length=200)
    rows: list[dict[str, Any]] = Field(
        ..., min_length=1,
        description="Rows to write, typically each item's `legacy_row` or "
                    "`incident_row`. Column order follows the first row's keys.",
    )


# --------------------------------------------------------------- responses

class AnalysedProfile(BaseModel):
    """One scraped profile.

    Tri-state booleans (`is_active`, `has_logo`, `has_name_match`,
    `verified`) are deliberately nullable: null means the scraper could not
    determine the field, which is NOT the same as determining it false.
    Treat null as unknown rather than coercing it."""

    id: str
    result_id: str = Field(
        "", description="Stable id for this profile's SAVED result, derived from "
                        "(platform, url). Unlike `id` -- a fresh uuid per job -- it "
                        "is the same value across runs and in GET /analysis/results, "
                        "so a live job's rows and the saved set can be matched up. "
                        "Also what addresses the saved screenshot.")
    url: str
    platform: str
    platform_name: str
    entity_id: str = ""
    status: str = Field(..., description="pending | running | done | error")
    error: str = ""
    analysed_at: Optional[str] = None

    profile_name: str = ""
    followers: Optional[int] = None
    location: str = ""
    bio: str = ""
    last_post_date: str = Field("", description="YYYY-MM-DD, blank when no date could be read.")
    is_active: Optional[bool] = None
    has_logo: Optional[bool] = None
    has_name_match: Optional[bool] = None
    name_score: int = 0
    risk_score: int = Field(2, description="2-9. Higher is more likely a real impersonation.")
    priority: str = Field("Low", description="High | Low, derived from the risk rubric.")
    profile_image_url: str = ""
    avatar_sha: str = Field(
        "", description="sha256 digest of the profile picture in the durable store, or blank.",
    )
    verified: Optional[bool] = None
    comments: str = ""
    has_screenshot: bool = Field(
        False, description="If true, fetch it from this job's screenshot endpoint.",
    )
    incident_row: dict[str, Any] = Field(
        default_factory=dict,
        description="Takedown-report column layout, built server-side from what "
                    "was actually scraped.",
    )
    legacy_row: dict[str, Any] = Field(
        default_factory=dict, description="Raw-analysis column layout.",
    )
    duration_seconds: Optional[float] = None
    started_at_ts: Optional[float] = None


class PlatformProgress(BaseModel):
    status: str = Field(..., description="pending | running | done | failed")
    total: int
    completed: int
    display_name: str
    current_url: Optional[str] = ""
    current_step: Optional[str] = ""
    item_started_at_ts: Optional[float] = None


class AnalysisJobState(BaseModel):
    job_id: str
    status: JobStatus
    target_name: str = ""
    official_feed: str = ""
    total: int
    completed: int
    message: str = ""
    started_at_ts: Optional[float] = None
    finished_at_ts: Optional[float] = None
    elapsed_seconds: Optional[float] = None
    estimated_remaining_seconds: Optional[float] = None
    platform_progress: dict[str, PlatformProgress]
    items: list[AnalysedProfile]


class StartAnalysisAccepted(JobAccepted):
    accepted: int = Field(..., description="URLs this job will actually scrape.")
    skipped: list[SkippedInput] = Field(
        ..., description="URLs that will NOT be scraped (unsupported host, "
                         "unparseable, or a duplicate within this request).",
    )


# ----------------------------------------------------------------- routes

@router.post("/jobs", response_model=StartAnalysisAccepted,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Scrape a list of profile URLs")
async def start_analysis(body: StartAnalysis) -> StartAnalysisAccepted:
    """Visits each URL and returns the scraped fields plus an evidence
    screenshot. Returns immediately -- poll `poll_url`.

    A request whose URLs are all unsupported is still accepted (202) and
    completes immediately with everything under `skipped`."""
    job, skipped = await analysis_runner.start(
        body.urls, body.target_name.strip(), body.official_feed.strip())
    return StartAnalysisAccepted(
        job_id=job.id, status=JobStatus(job.status),
        poll_url=f"/analysis/jobs/{job.id}",
        accepted=job.total,
        skipped=[SkippedInput(value=s["url"], reason=s["reason"]) for s in skipped],
    )


@router.get("/jobs/{job_id}", response_model=AnalysisJobState,
            summary="Poll a scrape")
async def get_job(job_id: str = Path(..., description="From POST /analysis/jobs")) -> AnalysisJobState:
    """Stop polling once `status` is `done`, `cancelled` or `failed`.
    `items` fills in as URLs complete, so partial results are readable
    while the job runs."""
    job = await analysis_runner.get(job_id)
    if job is None:
        raise NotFoundError(
            f"no analysis job {job_id!r} -- results are memory-only and are not "
            "persisted, so a restart or the job ageing out loses them. Re-run the scrape."
        )
    d = job.to_dict()
    return AnalysisJobState(
        job_id=d["id"], status=JobStatus(d["status"]),
        target_name=d["target_name"], official_feed=d["official_feed"],
        total=d["total"], completed=d["completed"], message=d["message"],
        started_at_ts=d.get("started_at_ts"),
        finished_at_ts=d.get("finished_at_ts"),
        elapsed_seconds=d.get("elapsed_seconds"),
        estimated_remaining_seconds=d.get("estimated_remaining_seconds"),
        platform_progress={
            k: PlatformProgress(
                status=v.get("status", "pending"), total=v.get("total", 0), completed=v.get("completed", 0),
                display_name=v.get("display_name") or v.get("displayName", k),
                current_url=v.get("current_url", ""),
                current_step=v.get("current_step", ""),
                item_started_at_ts=v.get("item_started_at_ts"),
            ) for k, v in d.get("platform_progress", {}).items()
        },
        items=[AnalysedProfile(**i) for i in d["items"]],
    )


@router.post("/jobs/{job_id}/cancel", response_model=CancelResult,
             summary="Cancel a running scrape")
async def cancel_job(job_id: str) -> dict:
    """Checked between profiles, so the job stops at the next boundary.
    Profiles already scraped stay readable on the job."""
    return {"cancelled": await analysis_runner.cancel(job_id)}


@router.get("/jobs/{job_id}/items/{item_id}/screenshot",
            response_class=Response,
            responses={200: {"content": {"image/png": {}}, "description": "PNG evidence capture"}},
            summary="Evidence screenshot for one scraped profile")
async def screenshot(job_id: str, item_id: str, download: bool = False):
    """The capture taken while the profile was being read -- frequently the
    only surviving proof it existed, since impersonating accounts are often
    removed before a report is acted on. Served from memory; it disappears
    with the job."""
    data = await analysis_runner.screenshot(job_id, item_id)
    if data is None:
        raise NotFoundError(
            f"no screenshot for item {item_id!r} on job {job_id!r} -- the job may "
            "have aged out, or that URL was never successfully reached"
        )
    disposition = "attachment" if download else "inline"
    return Response(
        content=data, media_type="image/png",
        headers={
            "Content-Disposition": f'{disposition}; filename="{item_id}.png"',
            "Cache-Control": "private, max-age=60",
        },
    )


# ------------------------------------------------------- saved results (24h)

class SavedResultPage(BaseModel):
    items: list[AnalysedProfile]
    total: int = Field(..., description="Saved results in total, not just this page.")
    retention_hours: int = Field(..., description="How long a result is kept.")


class DeleteResults(BaseModel):
    ids: list[str] = Field(
        default_factory=list, max_length=1000,
        description="`result_id`s to delete. Ignored when `all` is true.")
    all: bool = Field(
        False,
        description="Delete EVERY saved result instead of a named set. Cannot be "
                    "undone -- the caller's own confirmation is what stands in "
                    "front of it.")
    org_id: str = Field(
        "", max_length=128,
        description="With `all`, narrows the delete to one client's results. "
                    "Omit to clear everything.")


class DeleteResultsOutcome(BaseModel):
    deleted: int


@router.get("/results", response_model=SavedResultPage,
            summary="Analysis results saved in the last 24 hours")
async def list_results(
    org_id: str = Query("", max_length=128,
                        description="Only results from this client's batches. Pasted-URL "
                                    "runs have no client and carry an empty org_id."),
    platform: Optional[str] = Query(None, max_length=40),
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> SavedResultPage:
    """Every profile analysed in the retention window, newest first.

    Survives a reload and a restart, which the job endpoint does not. A
    result that has passed its expiry is never returned even if MongoDB's
    TTL monitor has not physically removed it yet -- see the repository's
    own note on why the index alone is not enough.
    """
    docs, total = await results_db.find(
        org_id=org_id.strip(), platform=(platform or "").strip(),
        limit=limit, offset=offset,
    )
    return SavedResultPage(
        items=[AnalysedProfile(**{k: v for k, v in d.items()
                                  if k in AnalysedProfile.model_fields})
               for d in docs],
        total=total, retention_hours=results_db.RETENTION_HOURS,
    )


@router.get("/results/{result_id}/screenshot", response_class=Response,
            responses={200: {"content": {"image/png": {}}, "description": "PNG evidence capture"}},
            summary="Evidence screenshot for one saved result")
async def saved_screenshot(result_id: str, download: bool = False):
    """The same capture the job endpoint serves, addressed by `result_id`
    and read from storage rather than from the job's memory -- so it is
    still there after a restart, for as long as the result is."""
    data = await results_db.get_screenshot(result_id)
    if data is None:
        raise NotFoundError(
            f"no saved screenshot for result {result_id!r} -- it may have passed its "
            f"{results_db.RETENTION_HOURS}h retention, been deleted, or that URL was "
            "never successfully reached"
        )
    disposition = "attachment" if download else "inline"
    return Response(
        content=data, media_type="image/png",
        headers={
            "Content-Disposition": f'{disposition}; filename="{result_id}.png"',
            "Cache-Control": "private, max-age=60",
        },
    )


@router.post("/results/delete", response_model=DeleteResultsOutcome,
             summary="Delete saved results -- a selection, or all of them")
async def delete_results(body: DeleteResults) -> DeleteResultsOutcome:
    """Irreversible. Each result's evidence screenshot goes with it, so
    nothing is left holding storage that no row points at any more.

    Deleting is only ever early: everything here expires on its own within
    the retention window regardless. This exists for the analyst who has
    finished with a batch and does not want to look at it for the rest of
    the day."""
    if body.all:
        return DeleteResultsOutcome(deleted=await results_db.delete_all(org_id=body.org_id.strip()))
    if not body.ids:
        raise ValidationError("pass `ids` to delete a selection, or `all: true` to delete everything")
    return DeleteResultsOutcome(deleted=await results_db.delete_many(body.ids))


@router.post("/export/xlsx", response_class=Response,
             responses={200: {"content": {
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}},
                 "description": "XLSX workbook"}},
             summary="Render rows as an .xlsx workbook")
async def export_xlsx(body: ExportXlsx):
    """Convenience for callers that want a spreadsheet without building one
    themselves. Cell values are guarded against spreadsheet formula
    injection (CWE-1236) -- these strings come from attacker-controlled
    profile text."""
    from openpyxl import Workbook

    if not body.rows:
        raise ValidationError("rows must not be empty")

    def _safe(v: object) -> object:
        if v is None:
            return ""
        if isinstance(v, (int, float, bool)):
            return v
        s = str(v).strip()
        if not s:
            return ""
        if s == "0" or (s.lstrip("-").isdigit() and not (len(s) > 1 and s.startswith("0"))):
            try:
                return int(s)
            except ValueError:
                pass
        elif re.match(r"^-?\d+\.\d+$", s):
            try:
                return float(s)
            except ValueError:
                pass
        # a leading apostrophe stops Excel treating this as a formula
        # without changing what a reader sees
        return f"'{s}" if s[:1] in ("=", "+", "-", "@") else s

    wb = Workbook()
    ws = wb.active
    ws.title = "analysis"
    cols = list(body.rows[0].keys())
    ws.append(cols)
    for row in body.rows:
        ws.append([_safe(row.get(c)) for c in cols])

    buf = BytesIO()
    wb.save(buf)
    filename = (body.filename or "analysis.xlsx").replace('"', "")
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
