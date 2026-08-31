"""Liveness and readiness probes. Deliberately public (no auth) and
carrying no scraped data.

Per-platform state is NOT here -- it lives at `GET /discovery/platforms`,
next to the thing it gates. One endpoint, one answer.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from backend.api.models import Health
from backend.database.connection import ping

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=Health,
            summary="Is the process up")
async def live() -> dict:
    """Always 200 while the process is running. Use this for a liveness
    probe -- it deliberately does not touch Mongo, so a database outage
    does not get the container killed and restarted pointlessly."""
    return {"status": "ok", "mongo": True}


@router.get("/health/ready", response_model=Health,
            responses={503: {"description": "Mongo unreachable -- do not route traffic here."}},
            summary="Can this instance serve requests")
async def ready(response: Response) -> dict:
    """503 when Mongo is unreachable. Mongo backs the session pool (the
    credentials every scrape logs in with) and discovery's results, so
    without it discovery cannot persist and nothing can authenticate --
    even though analysis itself holds its results in memory."""
    ok = await ping()
    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "unavailable", "mongo": ok}
