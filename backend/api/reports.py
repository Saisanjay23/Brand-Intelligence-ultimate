"""Sweep reports -- read them, or mail them.

    GET  /reports/client/{client_id}        the numbers, as JSON
    POST /reports/client/{client_id}/send   mail that client's report
    GET  /reports/combined                  every client, as JSON
    POST /reports/combined/send             mail the combined digest

THE THREE COUNTS are New (validated in the last 24h), Delta (validated
before that) and Total. They come from the same `validated_age` split the
Validated tab uses, so a report and the screen can never disagree.

WHY GET AND POST ARE SEPARATE. Reading a report is free and repeatable;
mailing one is an outward-facing side effect that lands in somebody's inbox.
Keeping them on different verbs means "show me what this would say" can
never accidentally send it -- which matters most for the combined digest,
where a stray call mails every recipient at once.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Path, Response
from pydantic import BaseModel, Field

from backend.services import report_service
from backend.shared.errors import UpstreamPlatformError

router = APIRouter(prefix="/reports", tags=["reports"])


class ValidatedCounts(BaseModel):
    new: int = Field(..., description="Validated within the last 24 hours.")
    delta: int = Field(..., description="Validated before that -- the settled backlog.")
    total: int = Field(..., description="new + delta.")


class PendingCounts(BaseModel):
    new: int = Field(..., description="First discovered in the last 24 hours.")
    total: int


class SampleProfile(BaseModel):
    name: str = ""
    platform: str = ""
    url: str = ""
    name_score: Optional[int] = None
    logo_similarity: Optional[int] = None
    logo_tier: str = ""


class ClientReport(BaseModel):
    client_id: str
    name: str
    generated_at: str
    validated: ValidatedCounts
    pending: PendingCounts
    logo_matches: int = Field(..., description="Profiles whose picture matched a reference logo.")
    samples: list[SampleProfile] = Field(
        default_factory=list,
        description="Up to 10 of the most recently validated profiles.")


class CombinedTotals(BaseModel):
    new: int
    delta: int
    total: int
    logo_matches: int
    pending: int


class CombinedReport(BaseModel):
    generated_at: str
    clients: list[ClientReport]
    totals: CombinedTotals


class SendResult(BaseModel):
    sent: bool
    detail: str = Field(..., description="Why, when `sent` is false -- most often "
                                         "no recipients or no SMTP host configured.")


class SendBody(BaseModel):
    to_emails: Optional[list[str]] = Field(
        None,
        description="Override the configured recipients. Omit to use the alert "
                    "settings' own list.")


@router.get("/client/{client_id}", response_model=ClientReport,
            summary="One client's sweep report")
async def client_report(client_id: str = Path(..., description="The org id")) -> dict:
    """Read-only. Safe to poll, and safe to call to preview what an email
    would say before sending it."""
    return await report_service.build_client_report(client_id)


@router.get("/combined", response_model=CombinedReport,
            summary="Every client, one row each")
async def combined_report() -> dict:
    return await report_service.build_combined_report()


@router.get("/client/{client_id}/html", response_class=Response,
            summary="One client's report, as the email HTML")
async def client_report_html(client_id: str = Path(...)) -> Response:
    """The same report the email carries, as a standalone HTML document --
    what the UI previews and what an analyst downloads. Rendering it here
    rather than in the browser means the file on disk and the mail in an
    inbox are byte-identical, so a report shared as evidence cannot quietly
    differ from the one that was sent."""
    rep = await report_service.build_client_report(client_id)
    # Sanitised, INCLUDING the fallback. The `or client_id` used to hand the
    # raw org id straight into a quoted header value, so a client whose name
    # is all punctuation (it sanitises to empty) and whose id contains a `"`
    # produced `filename="report-bad"id-...html"` -- a malformed header and a
    # filename an analyst cannot trust. Newlines are refused by the ASGI
    # layer, so this was never header injection; it was still a filename
    # nobody chose. Both halves go through the same filter now, and a
    # last-resort literal keeps the header well-formed when both are empty.
    def _safe(v: str) -> str:
        return "".join(c for c in (v or "") if c.isalnum() or c in " -_").strip()

    name = _safe(rep["name"]) or _safe(client_id) or "client"
    return Response(
        content=report_service.render_client_html(rep),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="report-{name}-{rep["generated_at"][:10]}.html"'},
    )


@router.get("/combined/html", response_class=Response,
            summary="The combined digest, as the email HTML")
async def combined_report_html() -> Response:
    rep = await report_service.build_combined_report()
    return Response(
        content=report_service.render_combined_html(rep),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="report-all-clients-{rep["generated_at"][:10]}.html"'},
    )


@router.post("/client/{client_id}/send", response_model=SendResult,
             summary="Email one client's report")
async def send_client_report(
    client_id: str = Path(...), body: SendBody = Body(default=SendBody()),
) -> dict:
    ok, detail = await report_service.send_client_report(client_id, body.to_emails)
    if not ok and "SMTP" in detail:
        # A misconfigured mail server is an upstream problem, not a bad
        # request -- surfacing it as 502 keeps it out of the caller's
        # "I sent something wrong" bucket.
        raise UpstreamPlatformError(detail)
    return {"sent": ok, "detail": detail}


@router.post("/combined/send", response_model=SendResult,
             summary="Email the combined digest")
async def send_combined_report(body: SendBody = Body(default=SendBody())) -> dict:
    ok, detail = await report_service.send_combined_report(body.to_emails)
    if not ok and "SMTP" in detail:
        raise UpstreamPlatformError(detail)
    return {"sent": ok, "detail": detail}
