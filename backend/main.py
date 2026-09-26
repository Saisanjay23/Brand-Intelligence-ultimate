"""Brand Intelligence -- impersonation discovery and analysis, as an API.

    uvicorn backend.main:app --port 8000

Three surfaces, meant to be driven by another service (in any language --
the OpenAPI spec at /openapi.json is complete enough to generate a client
from, and every route declares a real response schema):

    /discovery   keywords in  -> candidate profiles, persisted
    /analysis    URLs in      -> scraped + scored profiles, in memory only
    /sessions    the platform credentials both of the above scrape with

Plus /health for liveness/readiness probes, and /media/avatar, which
re-serves a profile picture from this origin when the platform's own CDN
refuses to be embedded cross-origin (see backend/api/media.py).

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

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Windows: force the Proactor event loop before anything creates one.
#
# Every browser-driven platform launches Playwright/patchright, which spawns
# a Node driver as a SUBPROCESS. Windows' SelectorEventLoop cannot spawn
# subprocesses at all -- it raises a bare `NotImplementedError` with no
# message -- and some server setups (notably `uvicorn --reload`, which runs
# the app in a supervised child process) leave that policy installed.
#
# The failure is silent and extremely misleading: the API stays up, jobs are
# accepted, sessions verify, and then EVERY browser platform fails with an
# empty `NotImplementedError:` while YouTube (a plain HTTPS API) and
# Telegram (MTProto) keep working -- so it reads as "Facebook/Instagram/
# Twitter are broken" rather than "the event loop cannot start a browser".
# Diagnosed exactly that way: identical code succeeded under `python run.py`
# and failed under `python run.py --dev --reload`.
#
# Setting the policy here rather than in run.py is deliberate: under
# --reload the app is imported by a CHILD process that never executes
# run.py, so a policy set there would not apply to the process that actually
# launches browsers.
if sys.platform == "win32":
    _policy = asyncio.get_event_loop_policy()
    if not isinstance(_policy, asyncio.WindowsProactorEventLoopPolicy):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.alerts import router as alerts_router
from backend.api.analysis import router as analysis_router
from backend.api.clients import router as clients_router
from backend.api.logos import router as logos_router
from backend.api.reports import router as reports_router
from backend.api.discovery import router as discovery_router
from backend.api.health import router as health_router
from backend.api.media import close as media_close
from backend.api.media import router as media_router
from backend.api.scheduler import router as scheduler_router
from backend.api.sessions import router as sessions_router
from backend.config.settings import settings
from backend.database.connection import close as mongo_close
from backend.database.connection import ping as mongo_ping
from backend.database.repositories import analysis_result_repository as analysis_results_db
from backend.database.repositories import avatar_repository as avatars_db
from backend.database.repositories import incident_repository as incidents_db
from backend.database.repositories import logo_repository as logos_db
from backend.database.repositories import evidence_repository as evidence_db
from backend.database.repositories import coverage_repository as coverage_db
from backend.database.repositories import profile_repository as profiles_db
from backend.database.repositories import schedule_repository as schedule_db
from backend.database.repositories import session_repository as sessions_db
from backend.database.repositories import telemetry_repository as telemetry_db
from backend.services import avatar_backfill
from backend.services.scheduler_service import scheduler_engine
from backend.sessions import manager as sessions_engine
from backend.shared.errors import DomainError
from backend.shared.logging import (
    configure_logging,
    get_logger,
    start_log_retention_monitor,
    stop_log_retention_monitor,
)

configure_logging()
log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Can this process actually start a browser?
    #
    # Playwright/patchright spawns its driver as a subprocess, which a
    # Windows SelectorEventLoop cannot do. The policy set at import time
    # (top of this file) fixes the common cases, but it CANNOT fix a loop
    # that was already created before this module was imported -- which is
    # exactly what `uvicorn --reload` does on Windows.
    #
    # Checked and shouted about here because the alternative is the failure
    # mode this actually produced in practice: the API comes up, sessions
    # verify, YouTube and Telegram jobs succeed, and every browser platform
    # returns a bare `NotImplementedError:` with no message -- which reads
    # as "Facebook and Instagram are broken" and sends you looking in
    # entirely the wrong place. One loud line at startup is worth more than
    # any amount of debugging later.
    if sys.platform == "win32":
        loop = asyncio.get_running_loop()
        if not isinstance(loop, asyncio.ProactorEventLoop):
            log.error(
                "EVENT LOOP CANNOT LAUNCH BROWSERS: this process is running on "
                f"{type(loop).__name__}, which on Windows cannot spawn subprocesses. "
                "Playwright needs one, so EVERY browser platform (Facebook, Instagram, "
                "Twitter, TikTok) will fail with an empty 'NotImplementedError'. "
                "YouTube and Telegram will keep working, which makes this look like a "
                "per-platform bug. Cause is almost always `--reload`: restart without it "
                "(`python run.py`). Frontend hot-reload via `--dev` alone is unaffected."
            )

    # Mongo backs the session pool (the credentials scrapes log in with),
    # discovery's results, and now analysis's own 24-hour result store. A
    # Mongo-less process can still scrape -- a live job runs from memory --
    # but nothing it reads will survive the tab being reloaded.
    # KEEPS TRYING, RATHER THAN DECIDING ONCE.
    #
    # This used to be a single `if await mongo_ping():`. A process that
    # started while Mongo was a few seconds from ready -- a compose stack
    # coming up, a database restarted during a deploy, a laptop waking --
    # took the else branch, logged a warning, and then ran for days with no
    # session monitor, no evidence retention, no drift canary and no
    # indexes. Nothing retried, because nothing was watching: the one check
    # had already happened. A transient dependency outage at the wrong
    # second bought a permanently half-working process, and the only
    # symptom was things quietly not happening.
    #
    # `_bring_up_storage` is idempotent (every ensure_indexes is
    # create-if-absent, every start_monitor is a no-op when its task is
    # already running), so retrying costs nothing and the success path is
    # unchanged for a process that starts with Mongo already up.
    storage_task = asyncio.create_task(_bring_up_storage())
    yield
    storage_task.cancel()
    # Stops the TICK loop only. A sweep in flight is left to its own
    # cancellation: the process is going down either way, and the run it
    # was driving is closed out as `interrupted` by the next startup
    # rather than pretended about here.
    scheduler_engine.stop_monitor()
    sessions_engine.stop_monitor()
    evidence_db.stop_retention_monitor()
    stop_log_retention_monitor()
    avatar_backfill.stop_retry_monitor()
    await media_close()
    await mongo_close()


# How long to wait between attempts to bring storage up, and how long to
# keep trying. Backed off gently: the common case resolves on the first or
# second attempt, and a database that is still absent after an hour is an
# operator problem that log-spamming will not fix.
_STORAGE_RETRY_S = 15
_STORAGE_RETRY_CEILING_S = 300
_STORAGE_GIVE_UP_S = 3600


async def _bring_up_storage() -> None:
    """Ensure indexes and start the background monitors, retrying until
    Mongo is actually reachable.

    Every step here is idempotent, so this is safe to run repeatedly and
    safe to run late. It is deliberately NOT awaited by the lifespan: the
    API must come up and serve `/health/ready` (which reports the truth
    about storage) whether or not the database is there yet -- blocking
    startup on a dependency turns a degraded service into a dead one.
    """
    waited = 0.0
    delay = float(_STORAGE_RETRY_S)
    attempt = 0
    while True:
        attempt += 1
        if await mongo_ping():
            try:
                await sessions_db.ensure_indexes()
                await profiles_db.ensure_indexes()
                await coverage_db.ensure_indexes()
                await telemetry_db.ensure_indexes()
                await avatars_db.ensure_indexes()
                await logos_db.ensure_indexes()
                await incidents_db.ensure_indexes()
                await incidents_db.purge_expired()
                # The TTL indexes that delete analysis results after 24h.
                # Without this call nothing ever expires and the collection
                # grows forever, so it belongs with the other index
                # guarantees rather than in a code path an operator has to
                # remember to run.
                await analysis_results_db.ensure_indexes()
                await schedule_db.ensure_indexes()
                from backend.platforms import registry
                for plat in registry.PLATFORMS.values():
                    await registry.session_state(plat)
                sessions_engine.start_monitor()
                evidence_db.start_retention_monitor()
                start_log_retention_monitor()
                # KEEPS PROFILE PICTURES, rather than hoping the sweep
                # caught them. A card showing a letter circle for a profile
                # that visibly has a photo is the single most common "this
                # tool is broken" report, and the cause is always the same:
                # one fetch, at sweep time, best-effort, never retried. Meta
                # signs its picture URLs for 110-307 hours, so retrying
                # hourly gets hundreds of attempts inside the window -- no
                # realistic outage survives that.
                avatar_backfill.start_retry_monitor()
                # THE SCHEDULED SWEEP'S CLOCK. Started here, with the
                # other monitors, because it needs Mongo: the schedule,
                # the queue and every run record live there. Starting it
                # before the database is reachable would mean a 02:00
                # fire that reads an empty schedule and concludes,
                # wrongly and silently, that nothing was scheduled.
                #
                # This is also what closes out any run the previous
                # process died inside -- see
                # `schedule_repository.reconcile_interrupted`.
                scheduler_engine.start()
            except Exception as e:
                # Reachable but not usable (auth, a replica-set election
                # mid-flight). Same treatment as unreachable: say so, and
                # come back rather than concluding anything permanent.
                log.error(
                    f"startup: mongo reachable but could not be prepared "
                    f"({type(e).__name__}: {e}) -- retrying in {delay:.0f}s")
            else:
                log.info(
                    "startup: mongo reachable, indexes ensured, session + "
                    "evidence-retention + log-retention + scheduler monitors running"
                    + (f" (after {attempt} attempts, {waited:.0f}s)" if attempt > 1 else ""))
                return
        elif attempt == 1:
            log.warning(
                "startup: mongo unreachable -- /health/ready will report unavailable, "
                "sessions cannot be read and discovery cannot persist. Retrying in "
                f"{delay:.0f}s; the engine will start its monitors as soon as it connects")

        if waited >= _STORAGE_GIVE_UP_S:
            log.error(
                f"startup: mongo still unreachable after {waited / 60:.0f} minutes -- "
                "giving up on automatic recovery. The API stays up and /health/ready "
                "keeps reporting the truth, but the session monitor, evidence "
                "retention and the parser-drift canary are NOT running. Restart the "
                "process once the database is back. SCHEDULED SWEEPS ARE NOT "
                "RUNNING EITHER -- nothing will fire at its set time until this "
                "process is restarted with the database reachable.")
            return
        await asyncio.sleep(delay)
        waited += delay
        delay = min(delay * 1.5, _STORAGE_RETRY_CEILING_S)


app = FastAPI(
    title="Brand Intelligence API",
    version="1.0.0",
    summary="Find and assess impersonating social-media profiles.",
    description=__doc__,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "reports", "description":
            "What a sweep found and what an analyst validated, as JSON "
            "or as an email. Reading never sends."},
        {"name": "clients", "description":
            "The org records discovery and analysis are scoped to. One "
            "document per org id, owning its own keywords and scrape caps."},
        {"name": "discovery", "description":
            "Keywords in, candidate profiles out. Results are persisted and "
            "readable while the sweep is still running."},
        {"name": "analysis", "description":
            "Profile URLs in, scraped and scored profiles out, with evidence "
            "screenshots. Results are saved for 24 hours and then deleted "
            "automatically; an analyst can delete them sooner."},
        {"name": "scheduler", "description":
            "When the client queue sweeps itself, who is in it, and how "
            "every run went. The schedule is a wall clock plus a timezone, "
            "not a UTC instant, so it holds its local time across a "
            "daylight-saving change."},
        {"name": "sessions", "description":
            "The per-platform credentials discovery and analysis scrape with. "
            "Nothing can be scraped for a platform with no usable session."},
        {"name": "media", "description":
            "Re-serves a profile picture from this origin, for CDNs whose "
            "response headers stop a browser embedding them directly."},
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
app.include_router(clients_router)
# Reference brand marks, nested under /clients/{id}/logos.
app.include_router(logos_router)
# Sweep reports: the New/Delta/Total counts, readable or mailable.
app.include_router(reports_router)
app.include_router(discovery_router)
app.include_router(analysis_router)
app.include_router(sessions_router)
# When the client queue runs, who is in it, and how every run went.
app.include_router(scheduler_router)
app.include_router(alerts_router)
# Serves remote avatars from this origin -- see backend/api/media.py for why
# Instagram's CDN cannot be embedded directly.
app.include_router(media_router)


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


class _UIFiles(StaticFiles):
    """The built UI, with cache headers that match how Vite names files.

    Everything under /assets/ carries a content hash in its name, so a given
    URL can never change: `immutable` lets a reload skip re-downloading and
    re-parsing the ~475KB bundle entirely. index.html is the opposite -- it
    is what points at the current hashes -- so it must always be revalidated.
    With no Cache-Control at all it used to be cached heuristically off its
    Last-Modified date, and a browser kept running the PREVIOUS UI after an
    update until that guess expired.
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code)
        if scope.get("path", "").startswith("/assets/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


if (_DIST / "index.html").is_file():
    # html=True serves index.html for "/" itself.
    app.mount("/", _UIFiles(directory=str(_DIST), html=True), name="ui")
    log.info(f"serving the UI from {_DIST}")
else:
    log.warning(
        f"no built UI at {_DIST} -- API-only; run `python run.py --build` to build it"
    )
