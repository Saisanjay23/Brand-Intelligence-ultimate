"""Liveness and readiness probes. Deliberately public (no auth) and
carrying no scraped data.

Per-platform state is NOT here -- it lives at `GET /discovery/platforms`,
next to the thing it gates. One endpoint, one answer.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from backend.api.models import Health
from backend.database.connection import ping

router = APIRouter(tags=["health"])


class LiveHealth(BaseModel):
    """The liveness answer. Its own model because `Health.mongo` is a
    required bool, and the liveness probe has no honest bool to give."""

    status: str
    mongo: Optional[bool] = Field(
        None, description="Always null here: the liveness probe does not check "
                          "Mongo. Use GET /health/ready for that.")


@router.get("/health/live", response_model=LiveHealth,
            summary="Is the process up")
async def live() -> dict:
    """Always 200 while the process is running. Use this for a liveness
    probe -- it deliberately does not touch Mongo, so a database outage
    does not get the container killed and restarted pointlessly.

    `mongo` IS NULL, NOT TRUE. It used to answer `"mongo": true` without
    looking, so anything reading this endpoint was told the database was
    up during an outage -- an unchecked "healthy". Null says what is
    actually known: nothing; `/health/ready` is the probe that checks."""
    return {"status": "ok", "mongo": None}


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
