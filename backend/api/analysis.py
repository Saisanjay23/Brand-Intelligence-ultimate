"""Analysis API: profile URLs in, scored profiles out.

    POST   /analysis/jobs                              scrape a list of URLs
    GET    /analysis/jobs/{job_id}                     poll it
    POST   /analysis/jobs/{job_id}/cancel
    GET    /analysis/jobs/{job_id}/items/{item_id}/screenshot
    POST   /analysis/export/xlsx                       rows -> .xlsx bytes

Analysis takes URLs and nothing else. It reads no client record and
nothing discovery produced, so a caller can analyse a URL that discovery
has never seen, and the two can be driven independently.

RESULTS ARE HELD IN MEMORY ONLY and are never persisted. They are lost on
restart, when the job's TTL lapses, or when the store evicts under
pressure. Read what you need while the job is alive -- there is no
"fetch yesterday's analysis" call, by design. `GET /stats` shows how close
the store is to evicting.
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Any, Optional

from fastapi import APIRouter, Path, Response, status
from pydantic import BaseModel, Field

from backend.analysis.runner import analysis_runner
from backend.api.models import CancelResult, JobAccepted, JobStatus, SkippedInput
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


class PlatformProgress(BaseModel):
    status: str = Field(..., description="pending | running | done | failed")
    total: int
    completed: int
    display_name: str


class AnalysisJobState(BaseModel):
    job_id: str
    status: JobStatus
    target_name: str = ""
    official_feed: str = ""
    total: int
    completed: int
    message: str = ""
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
        platform_progress={
            k: PlatformProgress(
                status=v["status"], total=v["total"], completed=v["completed"],
                display_name=v["displayName"],
            ) for k, v in d["platform_progress"].items()
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
