"""The report's "Preview" must open the report, not download it.

THE DEFECT THIS GUARDS: both HTML report routes always answered with
`Content-Disposition: attachment`, so the UI's "Preview" link saved a file
exactly as "Download" did. `?inline=true` now switches the disposition to
`inline`; without it the answer is still an attachment, so Download keeps
working unchanged.

Pure: report_service's builders are replaced, so nothing touches Mongo.
"""

import asyncio

from backend.api import reports


def _report():
    return {
        "client_id": "acme",
        "name": "Acme Corp",
        "generated_at": "2026-09-07T04:00:00+00:00",
        "validated": {"new": 1, "delta": 2, "total": 3},
        "pending": {"new": 0, "total": 0},
        "logo_matches": 0,
        "samples": [],
    }


def _combined():
    return {
        "generated_at": "2026-09-07T04:00:00+00:00",
        "clients": [_report()],
        "totals": {"new": 1, "delta": 2, "total": 3, "logo_matches": 0, "pending": 0},
    }


def _patch(monkeypatch):
    async def client(_cid):
        return _report()

    async def combined():
        return _combined()

    monkeypatch.setattr(reports.report_service, "build_client_report", client)
    monkeypatch.setattr(reports.report_service, "build_combined_report", combined)


def test_client_html_downloads_by_default(monkeypatch):
    _patch(monkeypatch)
    res = asyncio.run(reports.client_report_html("acme", inline=False))
    assert res.headers["content-disposition"].startswith("attachment;")


def test_client_html_inline_renders_in_the_tab(monkeypatch):
    _patch(monkeypatch)
    res = asyncio.run(reports.client_report_html("acme", inline=True))
    cd = res.headers["content-disposition"]
    assert cd.startswith("inline;")
    # Same filename either way, so "Save page" from the preview still names it.
    assert "report-Acme Corp-2026-09-07.html" in cd


def test_combined_html_inline_and_default(monkeypatch):
    _patch(monkeypatch)
    assert asyncio.run(reports.combined_report_html(inline=False)) \
        .headers["content-disposition"].startswith("attachment;")
    assert asyncio.run(reports.combined_report_html(inline=True)) \
        .headers["content-disposition"].startswith("inline;")
