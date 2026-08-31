"""Brand Intelligence -- impersonation discovery and analysis, as an API.

    uvicorn backend.main:app --port 8000

Three surfaces, meant to be driven by another service (in any language --
the OpenAPI spec at /openapi.json is complete enough to generate a client
from, and every route declares a real response schema):

    /discovery   keywords in  -> candidate profiles, persisted
    /analysis    URLs in      -> scraped + scored profiles, in memory only
    /sessions    the platform credentials both of the above scrape with

Plus /health for liveness/readiness probes.

It also SERVES THE UI: the built `frontend/dist` is mounted at `/` when it
exists, so one process on one port is the whole tool (see the mount at the
bottom of this file, and run.py). The mount is optional -- an API-only
deployment, or a checkout where the UI was never built, starts fine without
it.

Discovery and analysis are INDEPENDENT. Analysis reads nothing discovery
produced and takes no client/group id; discovery never visits or scores a
profile. Either can be driven on its own.

SINGLE WORKER, BY DESIGN. Job state -- and, for analysis, the results and
screenshots themselves -- live in this process's memory. A second worker
would serve a poll for a job it has never heard of. Scale by running more
instances behind separate job namespaces, not more workers over one.

ERRORS are always `{"detail": "<reason>"}` with a real HTTP status. 202 on
job creation, 404 for a job that has aged out, 422 for a malformed body.

AUTH: none. This is an internal service; put it behind your own gateway.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.analysis import router as analysis_router
from backend.api.discovery import router as discovery_router
from backend.api.health import router as health_router
from backend.api.sessions import router as sessions_router
from backend.config.settings import settings
from backend.database.connection import close as mongo_close
from backend.database.connection import ping as mongo_ping
from backend.database.repositories import profile_repository as profiles_db
from backend.database.repositories import session_repository as sessions_db
from backend.sessions import manager as sessions_engine
from backend.shared.errors import DomainError
from backend.shared.logging import configure_logging, get_logger

configure_logging()
log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Mongo backs the session pool (the credentials scrapes log in with)
    # and discovery's results. Analysis needs neither -- it runs from
    # memory -- but it still needs a session to scrape with, so a
    # Mongo-less process cannot usefully do anything.
    if await mongo_ping():
        await sessions_db.ensure_indexes()
        await profiles_db.ensure_indexes()
        from backend.platforms import registry
        for p in registry.PLATFORMS.values():
            await registry.session_state(p)
        sessions_engine.start_monitor()
        log.info("startup: mongo reachable, indexes ensured, session monitor running")
    else:
        log.warning(
            "startup: mongo unreachable -- /health/ready will report unavailable, "
            "sessions cannot be read and discovery cannot persist"
        )
    yield
    sessions_engine.stop_monitor()
    await mongo_close()


app = FastAPI(
    title="Brand Intelligence API",
    version="1.0.0",
    summary="Find and assess impersonating social-media profiles.",
    description=__doc__,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "discovery", "description":
            "Keywords in, candidate profiles out. Results are persisted and "
            "readable while the sweep is still running."},
        {"name": "analysis", "description":
            "Profile URLs in, scraped and scored profiles out, with evidence "
            "screenshots. Results are held in memory only and are never persisted."},
        {"name": "sessions", "description":
            "The per-platform credentials discovery and analysis scrape with. "
            "Nothing can be scraped for a platform with no usable session."},
        {"name": "health", "description": "Liveness and readiness probes."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """One error shape for the whole API: `{"detail": "<reason>"}` with the
    status the domain error carries (404 not found, 409 conflict, 422
    validation, 502 upstream platform)."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


app.include_router(health_router)
app.include_router(discovery_router)
app.include_router(analysis_router)
app.include_router(sessions_router)


# ------------------------------------------------------------------- the UI
#
# Serve the built frontend from this same process, so `python run.py` gives
# an analyst one URL that is the whole tool rather than a bare API that
# answers `{"detail":"Not Found"}` at `/`.
#
# MOUNTED LAST, DELIBERATELY. Starlette matches routes in registration
# order, and a mount at "/" is greedy -- registering it before the routers
# above would swallow /discovery, /analysis, /sessions, /health, /docs and
# /openapi.json. Last means the API always wins and the SPA only sees what
# is left over.
#
# Optional by design: a deployment that only wants the API (or a checkout
# where the UI was never built) starts fine and simply has no `/`. See
# run.py, which builds `frontend/dist` on first run.
_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if (_DIST / "index.html").is_file():
    # html=True serves index.html for "/" itself.
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="ui")
    log.info(f"serving the UI from {_DIST}")
else:
    log.warning(
        f"no built UI at {_DIST} -- API-only; run `python run.py --build` to build it"
    )
