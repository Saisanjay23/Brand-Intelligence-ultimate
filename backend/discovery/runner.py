"""Discovery: keywords in, candidate profiles out, persisted to MongoDB.

WHAT THIS IS. Give it keywords and (optionally) which platforms to search.
It sweeps each platform's own search surface, reads the results out of the
platform's own API/GraphQL payloads, and writes every candidate it finds to
the `profiles` collection. Results are durable -- that is the whole
difference from analysis.

    discovery -> MongoDB   (this module: keywords, sweeps, persisted)
    analysis  -> memory    (backend/analysis/runner.py: pasted URLs)

The two are independent passes with no shared state in either direction.
Discovery never scores a profile and never visits one; it only finds them.

`group_id` is how a caller partitions its own results (one brand, one
customer, one investigation). It is the dedup key `profiles` is keyed on
together with platform+url, so re-running the same keywords updates the
rows it already found instead of duplicating them. Callers that have no
natural grouping can pass any stable string.

JOB SHAPE. A sweep is minutes of work, so this is asynchronous in exactly
the same shape analysis uses -- start, poll, optionally cancel -- so an
external client has one integration pattern to implement, not two.
"""

from __future__ import annotations

import asyncio
import time
import uuid
import weakref
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Optional

from backend.config.settings import settings
from backend.database.repositories import profile_repository as profiles_db
from backend.platforms import registry
from backend.services import avatar_cache
from backend.platforms.scan_options import DiscoveryOptions
from backend.sessions import manager as sessions_engine
from backend.shared.job_store import JobStore
from backend.shared.logging import get_logger
from backend.shared.models.row import Row
from backend.shared.resilience import classify_failure
from backend.shared.text import handle_from_url, name_score

log = get_logger("discovery.runner")

# Seconds between one concurrent tab's start and the next. Three searches
# leaving in the same instant is a pattern; a second apart is three tabs
# opened by hand. Cheap insurance -- it costs at most 2s per keyword and
# only when discovery_tab_concurrency > 1.
TAB_STAGGER_SEC = 1.0

# MEDIAN SECONDS BETWEEN ONE KEYWORD AND THE NEXT on the same session, per
# platform. Discovery had no gap here at all -- `for keyword in
# job.keyword_plan` ran each sweep straight into the next, so a 15-keyword
# client put 45 Facebook searches (15 keywords x three tabs) through one
# logged-in account with only page-load time between them. Analysis has had
# _PLATFORM_INTER_BATCH_DELAY for exactly this reason since it was written;
# discovery simply never grew the equivalent.
#
# Spent through the session's own `pause()` rather than a flat sleep, so
# jitter, fatigue and the occasional longer rest still shape the gap
# (stealth/human.py) -- these are medians, not fixed waits. 0 skips the
# pause entirely.
#
# Facebook is the largest on purpose: it is the only platform sweeping
# three tabs per keyword, it is the one that also visits profiles to
# reconcile names, and it is where this pool has actually had accounts
# disabled.
_PLATFORM_INTER_KEYWORD_DELAY: dict[str, float] = {
    # Official API calls, not a browser session driving a logged-in account.
    # Telegram governs itself through FloodWait, which is a real signal
    # rather than a guess, and youtube's quota is metered not ban-risked.
    "youtube": 0.0,
    "telegram": 0.0,
    "facebook": 12.0,
    "instagram": 8.0,
}
_DEFAULT_INTER_KEYWORD_DELAY = 6.0

MAX_JOBS = 200
JOB_TTL_SECONDS = 6 * 3600

QUEUED, RUNNING, DONE, CANCELLED, FAILED = "queued", "running", "done", "cancelled", "failed"
_TERMINAL = frozenset({DONE, CANCELLED, FAILED})

# Each platform's engine understands only its own tab vocabulary, so every
# platform sweeps exactly the tab(s) it supports regardless of what a caller
# asked for -- a `tabs` list applied uniformly would sweep e.g. Twitter for
# "pages", costing a whole extra pass per keyword for nothing.
PLATFORM_TABS: dict[str, list[str]] = {
    "facebook": ["people", "pages", "groups"],
    "twitter": ["people"],
    "instagram": ["people"],
    "youtube": ["channels"],
    "telegram": ["all"],
    "tiktok": ["people"],
}

# ------------------------------------------------- parallel sessions, failover
#
# See analysis/runner.py's own block of the same name for the full
# reasoning -- this mirrors it keyword-for-URL. The short version: one
# platform, several sessions, pulling KEYWORDS off one shared queue instead
# of one session working through the whole `keyword_plan` alone. A session
# that dies mid-sweep gives back exactly what its current keyword
# contributed and re-queues it for a surviving session, instead of every
# keyword still behind it in the plan going unswept with nothing to explain
# why (which is what happened before this: see `_sweep_platform`'s old
# `fatal[0]: return` shortcut).
#
# 1 SESSION IS NOT A DEGRADED PATH. A single-session pool claims one session
# and runs the identical one-keyword-at-a-time loop it always did.
_MAX_SESSIONS_PER_PLATFORM: dict[str, int] = {
    # Telethon keeps ONE local session file open at a time behind an SQLite
    # lock (see sessions/manager.py::session_for_job), so a second Telegram
    # worker would be two clients fighting over one file, not two clients.
    "telegram": 1,
    # An API-key platform has no browser session to isolate a second worker
    # behind -- session_for_job hands the key over via os.environ, so a
    # second claim would overwrite the first rather than run beside it.
    "youtube": 1,
}

# How many sessions one KEYWORD may be handed to before whatever its last
# attempt completed is left standing. 2 means one retry, on one other
# session. Bounded because the failure being recovered from is ambiguous:
# usually the session died, but sometimes the keyword itself (an unusual
# character, a platform-side block on that exact term) is what trips the
# challenge, and an uncapped retry would let one keyword walk the whole pool
# killing accounts as it goes.
_MAX_KEYWORD_ATTEMPTS = 2

# How many times a platform may go back to the pool for REPLACEMENT sessions
# after everything it had claimed died. Round 1 is the ordinary claim; round
# 2 exists for a session coming free (another job finished, or the monitor
# revived one) while this sweep was running.
_MAX_CLAIM_ROUNDS = 2

# Each worker's browser starts this many seconds after the one before it, so
# N sessions never open their first request to a platform in the same
# instant -- the same reasoning TAB_STAGGER_SEC already applies within one
# session's concurrent tabs, one level up.
WORKER_STAGGER_SEC = 2.0

# Bound on browser workers open at once across the WHOLE process, which is
# the number that actually decides whether the host copes. `_run` sweeps
# every ready platform concurrently, so a per-platform cap cannot see the
# total. Keyed by running loop, not one module-level Semaphore: asyncio
# primitives bind to the loop that first awaits them, so a test suite that
# builds a fresh loop per test would otherwise inherit one bound to a loop
# that is already closed. Weak keys so closed loops do not accumulate.
_worker_slots: "weakref.WeakKeyDictionary[Any, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary())


def _worker_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _worker_slots.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(max(1, settings.discovery_max_browser_workers))
        _worker_slots[loop] = sem
    return sem


def _sessions_wanted(platform_id: str, keyword_count: int) -> int:
    """How many sessions it is worth claiming for this platform's sweep.
    Never more than there are keywords to sweep -- a claimed session is
    invisible to every other job for the life of the sweep, so claiming one
    that would sit idle costs another job an account for nothing.

    API-key and MTProto platforms are pinned to one: `session_for_job` hands
    those credentials over by mutating os.environ or one local session file,
    so a second claim would overwrite the first rather than run beside it.
    """
    cap = _MAX_SESSIONS_PER_PLATFORM.get(
        platform_id, settings.discovery_max_parallel_sessions)
    plat = registry.PLATFORMS.get(platform_id)
    if plat is not None and (plat.uses_api_key or plat.env_keys or not plat.session_path):
        cap = 1
    return max(1, min(cap, keyword_count))


@dataclass
class _KeywordItem:
    """One (keyword, kw_type) pair from `job.keyword_plan`, tracked for
    retry the way analysis/runner.py's AnalysisItem tracks `attempts` for a
    URL -- see _MAX_KEYWORD_ATTEMPTS."""

    keyword: str
    kw_type: str
    attempts: int = 0


@dataclass
class _PlatformSweepRun:
    """One platform's sweep, shared by every worker on it -- the queue IS
    the coordination, exactly as analysis/runner.py's _PlatformRun. A plain
    deque, not asyncio.Queue: every worker runs on one event loop, so a
    truthiness check followed by `popleft` cannot be interleaved, which
    makes the lock and task_done() bookkeeping an asyncio.Queue would add
    pure cost."""

    platform_id: str
    queue: "deque[_KeywordItem]"
    tabs: list[str]
    max_results: int
    max_seconds: Optional[float]
    platform_limits: dict[str, dict[str, int]]
    platform_tab_limits: dict[str, dict[str, dict[str, int]]]
    prog: PlatformSweep
    # Set by a worker that hit a NON-session-fatal stop -- Telegram's
    # FloodWait is the only source today. Every worker, including others
    # still mid-sweep, stops pulling once this is true, because the reason
    # has nothing to do with WHICH session is running -- another one would
    # hit the identical wall. Distinct from a session dying (see
    # _requeue_keyword), which only removes that one session and lets the
    # others carry on.
    hard_stop: bool = False
    incomplete: int = 0
    sweep_errors: list[str] = field(default_factory=list)




def _effective_cap(*caps: int) -> int:
    """The most restrictive of several result caps; 0 (uncapped) only when
    NONE of them are set. Mirrors the deleted backend/services/
    discovery_service.py::_effective_cap -- that resolution logic was lost
    with the old backend and had no replacement until this.

    A caller can pass any number of caps (the request's blanket
    `max_results`, a per-type cap, a per-(tab,type) cap, ...); whichever
    positive ones are present, the smallest wins. 0/None entries mean "not
    configured", not "unlimited zero", so they never win by being small."""
    positive = [c for c in caps if c and c > 0]
    return min(positive) if positive else 0


def _resolve_cap(
    platform_id: str, tab: str, kw_type: str, max_results: int,
    platform_limits: dict[str, dict[str, int]],
    platform_tab_limits: dict[str, dict[str, dict[str, int]]],
) -> int:
    """The cap this exact (platform, tab, keyword-type) sweep should run
    under -- the more restrictive of the request's blanket `max_results`,
    the flat per-type cap for this platform (`platform_limits_individual`/
    `_domain`, keyed by kw_type then platform), and the tab-specific cap
    for this exact cell (`platform_tab_limits`, keyed by platform then tab
    then kw_type -- currently only meaningful for facebook's people/pages/
    groups; every other platform has one tab, so its own tab_limits entry
    is normally empty and this collapses to just the flat cap). Uncapped
    (0) only when none of the three are set -- see _effective_cap."""
    type_cap = (platform_limits.get(kw_type) or {}).get(platform_id, 0)
    tab_cap = ((platform_tab_limits.get(platform_id) or {}).get(tab) or {}).get(kw_type, 0)
    return _effective_cap(max_results, type_cap, tab_cap)


def row_to_fields(row: Row, keyword: str) -> dict:
    """A discovery `Row` -> the field dict `profile_repository.save_many`
    expects. `url`/`entity_id`/`keyword` are control keys it pops off
    itself; everything else must be a name in `DISCOVERY_FIELDS` or the
    field-scoped write drops it silently."""
    src = ",".join(sorted({v.split(":", 1)[-1] for v in row.src.values()})) or "search"
    # Scored HERE rather than in each platform's converter: `Row.name_score`
    # is a plain field (only analysis's fill() ever set it), so a discovered
    # profile would otherwise be stored with a score of 0 no matter how
    # exactly its name matched -- which is what the name-match filters and
    # the risk rubric both read. `row.target` is the keyword this sweep
    # searched, set by every platform's *_to_row, so `name_exact_run`
    # (a property over profile_name vs target) already resolves correctly.
    if not row.name_score and row.profile_name:
        row.name_score = name_score(row.profile_name, keyword)
    return {
        "url": row.url,
        "entity_id": row.profile_id,
        "keyword": keyword,
        # The handle, NOT `profile_id` -- see Row.username. Falls back to the
        # URL and then to the id, so a platform that publishes neither still
        # stores something rather than a blank.
        "username": row.username or handle_from_url(row.url) or row.profile_id,
        "display_name": row.profile_name,
        "entity_type": row.entity_type,
        "discovery_source": src,
        "profile_image_url": row.profile_pic_url,
        "has_logo": row.has_custom_pic,
        "verified": row.verified,
        "name_score": row.name_score,
        "name_exact_run": row.name_exact_run,
        # Carried straight from the search payload where the platform put
        # one there (Twitter and Telegram do; see each discovery_engine's
        # *_to_row). Blank/None values are dropped by save() itself, so a
        # platform that publishes none of these simply never writes them.
        "followers": row.followers,
        "friends": row.friends,
        "location": row.location,
        "bio": row.bio,
        "created_at": row.created_iso,
    }


@dataclass
class CompletedSweep:
    """Telemetry record for one completed keyword sweep."""

    platform: str
    display_name: str
    keyword: str
    tab: str
    duration_seconds: float
    hits_found: int
    hits_new: int
    timestamp: str
    # HOW the sweep ended, not just that it did. Without these, tuning the
    # engine's timing knobs is unmeasurable from outside: shortening a wait
    # makes a sweep faster AND makes it give up sooner, and those two look
    # identical in a duration alone. `complete` is the engine's own answer
    # to "did this run to one of its real stopping signals", and `stopped`
    # names which one.
    complete: bool = True
    stopped: str = ""
    # What the profile-visit reconciliation phase cost. It is the slowest
    # part of a sweep and the most detectable, so it is the number to watch.
    resolved_visits: int = 0
    resolve_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "display_name": self.display_name,
            "keyword": self.keyword,
            "tab": self.tab,
            "duration_seconds": round(self.duration_seconds, 2),
            "hits_found": self.hits_found,
            "hits_new": self.hits_new,
            "timestamp": self.timestamp,
            "complete": self.complete,
            "stopped": self.stopped,
            "resolved_visits": self.resolved_visits,
            "resolve_seconds": self.resolve_seconds,
        }


@dataclass
class PlatformSweep:
    """How one platform's part of a job went."""

    platform: str
    display_name: str
    status: str = "pending"  # pending | running | done | partial | failed | skipped
    keywords_total: int = 0
    keywords_done: int = 0
    found: int = 0
    new: int = 0
    note: str = ""
    # Real-time Telemetry
    current_keyword: str = ""
    current_tab: str = ""
    current_step: str = ""
    item_started_at_ts: Optional[float] = None
    started_at_ts: Optional[float] = None
    finished_at_ts: Optional[float] = None
    # How many pooled sessions are sweeping this platform in parallel right
    # now. 0 outside a sweep round; 1 is the ordinary single-session case;
    # >1 means the keyword list was split across accounts -- see
    # _MAX_SESSIONS_PER_PLATFORM.
    workers: int = 0

    def to_dict(self) -> dict:
        return {
            "platform": self.platform, "display_name": self.display_name,
            "status": self.status, "keywords_total": self.keywords_total,
            "keywords_done": self.keywords_done, "found": self.found,
            "new": self.new, "note": self.note,
            "current_keyword": self.current_keyword,
            "current_tab": self.current_tab,
            "current_step": self.current_step,
            "item_started_at_ts": self.item_started_at_ts,
            "started_at_ts": self.started_at_ts,
            "finished_at_ts": self.finished_at_ts,
            "workers": self.workers,
        }


@dataclass
class DiscoveryJob:
    id: str
    group_id: str
    # (keyword, kw_type) pairs, kw_type is "individual" | "domain" -- the
    # ORDER this sweeps in and the type each cap resolution needs (see
    # _resolve_cap). `keywords` below is derived from this for display.
    keyword_plan: list[tuple[str, str]]
    created_at: float = field(default_factory=time.time)
    status: str = QUEUED
    message: str = ""
    found: int = 0
    new: int = 0
    platforms: dict[str, PlatformSweep] = field(default_factory=dict)
    task: Optional[Any] = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    started_at_ts: Optional[float] = None
    finished_at_ts: Optional[float] = None
    history: list[CompletedSweep] = field(default_factory=list)
    # Avatar caching started behind each completed sweep. Held on the JOB
    # rather than in a local: a task with no live reference can be garbage
    # collected mid-flight, which would cache nothing under exactly the
    # load where it matters. Settled in `_run`'s finally.
    avatar_tasks: list[Any] = field(default_factory=list)

    @property
    def keywords(self) -> list[str]:
        return [kw for kw, _ in self.keyword_plan]

    @property
    def total(self) -> int:
        return sum(p.keywords_total for p in self.platforms.values())

    @property
    def completed(self) -> int:
        return sum(p.keywords_done for p in self.platforms.values())

    def to_dict(self) -> dict:
        now = time.time()
        elapsed = (self.finished_at_ts or now) - self.started_at_ts if self.started_at_ts else 0.0

        # Calculate dynamic ETA based on rolling average duration per sweep across completed items
        remaining_units = max(0, self.total - self.completed)
        est_remaining_sec: Optional[float] = None
        if self.status == RUNNING and self.started_at_ts and self.total > 0:
            if self.history and self.completed > 0:
                durations = [h.duration_seconds for h in self.history]
                avg_duration = sum(durations) / len(durations)
                active_platforms_count = max(1, sum(1 for p in self.platforms.values() if p.status == "running" or p.keywords_done < p.keywords_total))
                est_remaining_sec = round((avg_duration * remaining_units) / active_platforms_count, 1)
            else:
                active_platforms_count = max(1, sum(1 for p in self.platforms.values() if p.status != "skipped"))
                est_remaining_sec = round((8.0 * remaining_units) / active_platforms_count, 1)

        return {
            "job_id": self.id, "group_id": self.group_id, "status": self.status,
            "keywords": self.keywords, "message": self.message,
            "total": self.total, "completed": self.completed,
            "found": self.found, "new": self.new,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "started_at_ts": self.started_at_ts, "finished_at_ts": self.finished_at_ts,
            "elapsed_seconds": round(elapsed, 1),
            "estimated_remaining_seconds": est_remaining_sec,
            "platforms": [p.to_dict() for p in self.platforms.values()],
            "history": [h.to_dict() for h in self.history[-30:]],
        }


class DiscoveryRunner:
    """Owns every live discovery job. Process-wide, bounded, in memory --
    the JOBS are in memory; the RESULTS they produce are in MongoDB and
    outlive both the job and the process."""

    def __init__(self) -> None:
        self._store: JobStore[DiscoveryJob] = JobStore(
            max_jobs=MAX_JOBS, ttl_seconds=JOB_TTL_SECONDS, terminal_statuses=_TERMINAL,
        )

    def holds_session(self, platform_id: str, session_id: str) -> bool:
        return self._store.holds_session(platform_id, session_id)

    async def platform_readiness(
        self, only: Optional[list[str]] = None,
    ) -> tuple[list[str], dict[str, str]]:
        """(ready platform ids, {skipped id: why}). Every enabled,
        discovery-capable platform is accounted for one way or the other,
        so a sweep can never silently drop one with no explanation."""
        ready: list[str] = []
        skipped: dict[str, str] = {}
        wanted = [p.strip().lower() for p in (only or []) if str(p).strip()]
        for platform_id, plat in registry.PLATFORMS.items():
            if wanted and platform_id not in wanted:
                continue
            if not plat.enabled or not plat.can_discover:
                skipped[platform_id] = "platform has no discovery phase"
                continue
            state = await registry.session_state(plat)
            if state == "ready":
                ready.append(platform_id)
            elif plat.can_run_anonymously:
                # this platform's search works logged-out; a dead session
                # costs it a field, not the whole platform
                ready.append(platform_id)
            else:
                skipped[platform_id] = f"session {state}"
        for p in wanted:
            if p not in ready and p not in skipped:
                skipped[p] = "unknown platform"
        return ready, skipped

    async def start(
        self, group_id: str,
        individual_keywords: list[str], domain_keywords: list[str],
        platforms: Optional[list[str]] = None,
        max_results: int = 0, max_seconds: Optional[float] = None,
        platform_limits_individual: Optional[dict[str, int]] = None,
        platform_limits_domain: Optional[dict[str, int]] = None,
        platform_tab_limits: Optional[dict[str, dict[str, dict[str, int]]]] = None,
    ) -> tuple[DiscoveryJob, dict[str, str]]:
        # Deduped WITHIN each type, independently -- these are two
        # separately-curated lists (executive/person names vs brand/domain
        # terms), each already the caller's own dedup boundary. A term
        # appearing in both is swept twice, once per type, which is correct:
        # its individual-type and domain-type sweeps can carry different
        # caps (platform_limits_individual vs _domain).
        ind = list(dict.fromkeys(k.strip() for k in individual_keywords if k and k.strip()))
        dom = list(dict.fromkeys(k.strip() for k in domain_keywords if k and k.strip()))
        plan = [(k, "individual") for k in ind] + [(k, "domain") for k in dom]
        ready, skipped = await self.platform_readiness(platforms)

        job = DiscoveryJob(id=uuid.uuid4().hex[:12], group_id=group_id, keyword_plan=plan)
        for pid in ready:
            tabs = PLATFORM_TABS.get(pid, ["people"])
            job.platforms[pid] = PlatformSweep(
                platform=pid, display_name=registry.display_name(pid),
                keywords_total=len(plan) * len(tabs),
            )
        for pid, why in skipped.items():
            job.platforms[pid] = PlatformSweep(
                platform=pid, display_name=registry.display_name(pid),
                status="skipped", note=why,
            )

        await self._store.put(job)

        if not plan:
            job.status = DONE
            job.message = "no usable keywords given"
        elif not ready:
            job.status = DONE
            job.message = "no platform has a usable session to sweep"
        else:
            job.task = asyncio.create_task(
                self._run(
                    job, ready, max_results, max_seconds,
                    platform_limits_individual or {}, platform_limits_domain or {},
                    platform_tab_limits or {},
                ))
        return job, skipped

    async def get(self, job_id: str) -> Optional[DiscoveryJob]:
        return await self._store.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        return await self._store.cancel(job_id)

    # --------------------------------------------------------------- sweeping

    async def _run(
        self, job: DiscoveryJob, ready: list[str],
        max_results: int, max_seconds: Optional[float],
        platform_limits_individual: dict[str, int], platform_limits_domain: dict[str, int],
        platform_tab_limits: dict[str, dict[str, dict[str, int]]],
    ) -> None:
        job.status = RUNNING
        job.started_at = datetime.now(timezone.utc).isoformat()
        job.started_at_ts = time.time()
        try:
            # Every ready platform swept CONCURRENTLY. Each is a fully
            # separate account and browser context on a fully
            # separate host (facebook.com, x.com, instagram.com, ...) --
            # there is no shared-session risk running them at once the way
            # there would be two sessions open on the SAME platform
            # simultaneously (that risk is what JobStore's per-
            # (platform, session_id) hold guards against, together with
            # see _sweep_platform below).
            # WITHIN one platform, its keywords may now ALSO run several at
            # a time when the pool has more than one usable session for it
            # (see _sweep_platform/_keyword_worker), each on its own
            # account -- never one session driven twice at
            # once, which is exactly what those two guards exist to keep
            # true regardless of how many workers a platform is running.
            #
            # Exceptions are already caught and recorded per-platform
            # inside _sweep_platform (it never raises out), so
            # return_exceptions here is a defensive backstop, not the
            # normal path -- something escaping it is logged rather than
            # silently dropped by gather.
            results = await asyncio.gather(
                *(self._sweep_platform(
                    job, pid, max_results, max_seconds,
                    platform_limits_individual, platform_limits_domain, platform_tab_limits,
                  ) for pid in ready),
                return_exceptions=True,
            )
            for pid, result in zip(ready, results):
                if isinstance(result, BaseException):
                    log.error(f"discovery job {job.id}: {pid} raised past its own handling -- {result}")
                    job.platforms[pid].status = "failed"
                    job.platforms[pid].note = f"{type(result).__name__}: {result}"

            if job.cancel.is_set():
                job.status = CANCELLED
                job.message = f"cancelled after {job.completed}/{job.total} sweeps"
            else:
                job.status = DONE
                notes = [f"{p.platform}: {p.note}" for p in job.platforms.values() if p.note]
                job.message = f"{job.found} profile(s) found, {job.new} new" + (
                    f" -- {'; '.join(notes)}" if notes else "")
        except Exception as e:
            job.status = FAILED
            job.message = f"{type(e).__name__}: {e}"
            log.error(f"discovery job {job.id} failed: {job.message}")
        finally:
            # Reported finished BEFORE the avatars are settled: the sweep's
            # own work is done, its profiles are saved and readable, and the
            # pictures are an enhancement landing behind it. Waiting here
            # first would make every job's reported duration include image
            # downloads it deliberately kept off the critical path.
            job.finished_at = datetime.now(timezone.utc).isoformat()
            job.finished_at_ts = time.time()
            await self._settle_avatars(job)
            await self._maybe_report(job)

    async def _maybe_report(self, job: DiscoveryJob) -> None:
        """Email this client's sweep report, if an operator asked for it.

        OFF UNLESS ENABLED. `report_on_sweep_complete` defaults to False, so
        an upgrade never starts mailing on its own -- a sweep finishing is a
        routine, frequent event, and the difference between a useful report
        and a mail flood is entirely whether somebody chose it.

        Runs AFTER the job is already marked finished and reported, and
        cannot fail it: a job that found profiles must not read as failed
        because an SMTP server was down.
        """
        if job.cancel.is_set():
            # A cancelled sweep has nothing worth reporting on, and mailing
            # about one would train the reader to ignore the reports.
            return
        try:
            from backend.database.repositories import alert_settings_repository as alert_db
            cfg = await alert_db.get_settings()
            if not cfg.get("report_on_sweep_complete"):
                return
            from backend.services import report_service
            task = report_service.spawn_client_report(job.group_id)
            if task is not None:
                await task
        except Exception as e:                       # noqa: BLE001 - never fatal
            log.warning(f"sweep report skipped for job {job.id}: {type(e).__name__}: {e}")

    async def _settle_avatars(self, job: DiscoveryJob) -> None:
        """Let the behind-the-sweep avatar caching finish (or stop it).

        Nothing here can fail the job -- it is already finished and
        reported. The point is that these tasks end DETERMINISTICALLY:
        awaited to completion normally, cancelled when the job was, and
        never left running past the job that owns them.
        """
        tasks = [t for t in job.avatar_tasks if t is not None]
        job.avatar_tasks = []
        if not tasks:
            return
        if job.cancel.is_set():
            for t in tasks:
                t.cancel()
        try:
            cached = await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:                        # noqa: BLE001 - never fatal
            return
        total = sum(c for c in cached if isinstance(c, int))
        if total:
            log.info(f"discovery job {job.id}: cached {total} avatar(s)")

    async def _sweep_platform(
        self, job: DiscoveryJob, platform_id: str,
        max_results: int, max_seconds: Optional[float],
        platform_limits_individual: dict[str, int], platform_limits_domain: dict[str, int],
        platform_tab_limits: dict[str, dict[str, dict[str, int]]],
    ) -> None:
        """Every keyword on one platform, swept by every session that
        platform can safely lend at once.

        A QUEUE AND N WORKERS, not a loop over `job.keyword_plan`. See
        analysis/runner.py's _scrape_platform for the identical shape and
        reasoning -- a two-account pool splits the keyword list and halves
        the sweep, and a session dying mid-sweep is RECOVERABLE: the
        keywords it had not reached are still queue entries, so a
        surviving worker takes them instead of the platform reporting a
        silent gap between keywords_done and keywords_total. With one
        session in the pool this is the same one-keyword-at-a-time loop it
        has always been -- the single-session path is not a fallback, it
        is this code with one worker.
        """
        prog = job.platforms[platform_id]
        prog.status = "running"
        prog.started_at_ts = time.time()
        registry.get(platform_id)
        tabs = PLATFORM_TABS.get(platform_id, ["people"])
        platform_limits = {"individual": platform_limits_individual, "domain": platform_limits_domain}

        run = _PlatformSweepRun(
            platform_id=platform_id,
            queue=deque(_KeywordItem(kw, kt) for kw, kt in job.keyword_plan),
            tabs=tabs,
            max_results=max_results,
            max_seconds=max_seconds,
            platform_limits=platform_limits,
            platform_tab_limits=platform_tab_limits,
            prog=prog,
        )

        want = _sessions_wanted(platform_id, len(job.keyword_plan))
        prog.workers = 0
        setup_error = ""
        worker_error = ""
        rounds = 0

        try:
            while rounds < _MAX_CLAIM_ROUNDS and run.queue and not job.cancel.is_set() and not run.hard_stop:
                rounds += 1
                try:
                    plat_obj, claimed = await self._claim_sessions(platform_id, want)
                except Exception as e:
                    if rounds == 1:
                        # Nothing to sweep with AT ALL -- the pool is empty,
                        # dead or entirely rate-limited. Recorded as this
                        # platform's failure detail so the analyst reads
                        # ConflictError's own actionable message on the
                        # progress chip rather than a generic one.
                        setup_error = f"{type(e).__name__}: {e}"
                        log.error(f"discovery job {job.id}: {platform_id} failed -- {setup_error}")
                    break
                if not claimed:
                    break

                if rounds > 1:
                    log.info(
                        f"[{platform_id}] {len(run.queue)} keyword(s) still queued after "
                        f"every session failed -- retrying on {len(claimed)} replacement "
                        f"session(s)")
                elif len(claimed) > 1:
                    log.info(
                        f"[{platform_id}] {len(job.keyword_plan)} keyword(s) split across "
                        f"{len(claimed)} sessions in parallel")

                prog.workers = len(claimed)
                results = await asyncio.gather(
                    *(self._keyword_worker(job, run, plat_obj, session_item, worker_index)
                      for worker_index, session_item in enumerate(claimed)),
                    return_exceptions=True,
                )
                prog.workers = 0
                for result in results:
                    if isinstance(result, BaseException):
                        # A worker's SETUP failed (its own session was
                        # unusable), which is not this platform's failure
                        # while another worker or another round can still
                        # drain the queue -- held onto as the detail to
                        # report only if nothing does.
                        worker_error = f"{type(result).__name__}: {result}"
                        log.error(
                            f"discovery job {job.id}: {platform_id} worker failed -- "
                            f"{worker_error}")
        except Exception as e:
            prog.status = "failed"
            prog.note = f"{type(e).__name__}: {e}"
            log.error(f"discovery job {job.id}: {platform_id} failed -- {prog.note}")
            prog.current_keyword = ""
            prog.current_tab = ""
            prog.current_step = ""
            prog.item_started_at_ts = None
            prog.finished_at_ts = time.time()
            return

        prog.current_keyword = ""
        prog.current_tab = ""
        prog.current_step = ""
        prog.item_started_at_ts = None
        prog.finished_at_ts = time.time()

        if job.cancel.is_set():
            # A cancelled platform keeps what it read; failing the rest
            # would turn the analyst's own cancel into a screenful of
            # errors.
            if run.incomplete:
                prog.status = "partial"
                if not prog.note:
                    prog.note = f"{run.incomplete} sweep(s) did not run to completion"
            else:
                prog.status = "done"
            return

        if run.queue:
            # WHATEVER NO SESSION EVER REACHED. Either every claimed session
            # died on this platform (or _MAX_CLAIM_ROUNDS ran out of
            # replacements) with keywords still unattempted -- explicit now,
            # rather than the silent gap between keywords_done and
            # keywords_total this used to leave with no note explaining it.
            if not prog.note:
                prog.note = setup_error or worker_error or (
                    "every available session for this platform failed or checkpointed -- "
                    "see the earlier failed sweep(s) for why")
            prog.status = "failed" if prog.keywords_done == 0 else "partial"
            return

        if run.incomplete:
            prog.status = "partial"
            if run.sweep_errors:
                # The diagnosis first, the count second -- the count is the
                # part an analyst can do nothing with.
                detail = " | ".join(run.sweep_errors[:2])
                prog.note = f"{detail} ({run.incomplete} sweep(s) incomplete)"
            else:
                prog.note = f"{run.incomplete} sweep(s) did not run to completion"
        else:
            # Includes the case this was built for: a session died
            # mid-sweep, another one finished its keywords, and the
            # analyst gets a complete platform anyway.
            prog.status = "done"

    async def _claim_sessions(
        self, platform_id: str, want: int,
    ) -> tuple[Any, list[dict]]:
        """Up to `want` pooled sessions, claimed for
        the life of this platform's sweep. See analysis/runner.py's
        identical method for the full reasoning; kept as its own copy so
        this module and analysis's stay independently correct."""
        plat_obj: Any = None
        claimed: list[dict] = []
        while len(claimed) < want:
            try:
                plat_obj, session_item = await sessions_engine.session_for_job(platform_id)
            except Exception:
                if not claimed:
                    raise
                break
            session_id = str(session_item.get("id") or "")
            if not session_id or session_item.get("anonymous"):
                # NOT A POOL ENTRY. `session_for_job` fell back to a
                # logged-out context (tiktok) or an env API key; there is
                # no second identity behind that to claim and it would hand
                # back this same dict forever, so it is this platform's one
                # worker.
                if not claimed:
                    claimed.append(session_item)
                break
            claimed.append(session_item)
        return plat_obj, claimed

    async def _keyword_worker(
        self, job: DiscoveryJob, run: "_PlatformSweepRun", plat_obj: Any,
        session_item: dict, worker_index: int,
    ) -> bool:
        """One held session, pulling keywords off `run.queue` until it is
        empty. -> True if this session DIED (its in-progress keyword's
        contribution was undone and re-queued for another session); False
        if it worked the queue out, was cancelled, or hit a non-session
        stop.

        Nothing about HOW a keyword is swept changes with worker count --
        its own browser context, its own cookie jar, its own DiscoveryOptions,
        the same tab-concurrency/stagger logic, the same cap resolution,
        the same `discoverer.sweep()`. A worker is a second reader of one
        queue, not a cheaper kind of read.
        """
        platform_id = run.platform_id
        prog = run.prog
        session_id = str(session_item.get("id") or "")
        label = session_item.get("identifier") or session_id or "anonymous"
        inter_keyword_delay = _PLATFORM_INTER_KEYWORD_DELAY.get(
            platform_id, _DEFAULT_INTER_KEYWORD_DELAY)

        if worker_index:
            # Staggered so N sessions never open their first request to the
            # platform in the same instant -- the same reasoning
            # TAB_STAGGER_SEC already applies within one session's own
            # concurrent tabs, one level up.
            await asyncio.sleep(worker_index * WORKER_STAGGER_SEC)

        session = None
        discoverer = None
        held: Optional[tuple[str, str]] = None
        anon_cm = None

        # Ceilinged process-wide, not per-platform -- see `_worker_semaphore`.
        async with _worker_semaphore():
            if not run.queue or job.cancel.is_set() or run.hard_stop:
                # Another worker drained it (or a hard stop fired) while
                # this one waited for a slot. Do not pay for a browser
                # launch to discover that.
                sessions_engine.release_claim(platform_id, session_id)
                return False
            try:
                held = self._store.hold_session(platform_id, session_id)

                # A per-WORKER options object, not a shared one. The
                # original single-session code safely mutated one shared
                # `options.max_results` in place between cells because
                # sweeps for one platform ran strictly sequentially; with
                # several sessions now sweeping concurrently, two workers
                # racing to mutate a shared options object would each see
                # the other's cap. Each worker gets its own, exactly as
                # each gets its own discoverer.
                options = DiscoveryOptions(
                    concurrency=settings.discovery_concurrency,
                    # The median this session paces its keyword gaps against
                    # -- see _PLATFORM_INTER_KEYWORD_DELAY and
                    # settings.discovery_delay_sec.
                    delay=settings.discovery_delay_sec,
                    max_results=run.max_results,
                    max_seconds=(
                        run.max_seconds if run.max_seconds is not None
                        else settings.discovery_max_seconds),
                    headful=not settings.headless,
                    settle=settings.discovery_settle_sec,
                    page_wait=settings.discovery_page_wait_sec,
                    patience=settings.discovery_patience,
                )
                make_discoverer = None

                if session_item.get("anonymous"):
                    anon_cm = plat_obj.anonymous_context()()
                    ctx = await anon_cm.__aenter__()
                    make_discoverer = lambda o, _c=ctx: plat_obj.discoverer()(o, _c, anonymous=True)
                    discoverer = make_discoverer(options)
                elif not plat_obj.session_path:
                    make_discoverer = lambda o: plat_obj.discoverer()(o, None)
                    discoverer = make_discoverer(options)
                else:
                    session = plat_obj.session_cls()(
                        options, session_item.get("cookies", []),
                        session_id=session_id,
                    )
                    session.on_cookies = sessions_engine.cookie_saver(platform_id, session_id)
                    await session.start()
                    # SKIPPED WHEN THE ANSWER IS ALREADY KNOWN -- see
                    # analysis/runner.py's identical reasoning.
                    if sessions_engine.proven_fresh(session_item):
                        log.info(
                            f"[{platform_id}] login probe skipped -- proven healthy "
                            f"{(time.time() - float(session_item.get('last_ok') or 0)) / 60:.0f}m ago"
                        )
                    else:
                        if not await session.check_session():
                            await sessions_engine.mark_session_failed(
                                platform_id, session_id, "expired")
                            raise RuntimeError(
                                f"{registry.display_name(platform_id)} session is not usable -- "
                                "check credentials under /sessions")
                        await sessions_engine.mark_session_ok(platform_id, session_id)
                    if session is not None and hasattr(session, "sync_cookies"):
                        await session.sync_cookies()
                    make_discoverer = lambda o, _s=session: plat_obj.discoverer()(o, _s.ctx)
                    discoverer = make_discoverer(options)

                # ONE (keyword, tab) SWEEP, as a coroutine -- unchanged from
                # the single-session original except for what it reports
                # back on a session-fatal signal: `stats` is what THIS
                # keyword-attempt has contributed so far (units/found/new),
                # handed to `_requeue_keyword` to undo precisely if this
                # attempt needs a retry, and `fatal_kind` distinguishes a
                # dead SESSION (another one should take over) from a
                # non-session stop like Telegram's FloodWait (nothing would
                # be gained by a different session, so the whole platform
                # stops instead -- see `run.hard_stop`).
                async def _sweep_tab(
                    keyword: str, kw_type: str, tab: str, stats: dict, fatal_kind: list,
                    stagger: float = 0.0, own_options: bool = False,
                ) -> str:
                    if stagger:
                        await asyncio.sleep(stagger)
                    if job.cancel.is_set() or run.hard_stop:
                        return ""
                    prog.current_keyword = keyword
                    prog.current_tab = tab
                    prog.current_step = f"Searching {tab.upper()} tab..."
                    prog.item_started_at_ts = time.time()
                    t0 = time.time()
                    cap = _resolve_cap(
                        platform_id, tab, kw_type, run.max_results,
                        run.platform_limits, run.platform_tab_limits,
                    )
                    if own_options:
                        disc = make_discoverer(replace(options, max_results=cap))
                    else:
                        options.max_results = cap
                        disc = discoverer
                    try:
                        sweep = await disc.sweep(keyword, tab)
                    except Exception as e:
                        dur = time.time() - t0
                        log.error(f"[{platform_id}] {keyword!r}/{tab}: {type(e).__name__}: {e}")
                        prog.keywords_done += 1
                        stats["units"] += 1
                        job.history.append(CompletedSweep(
                            platform=platform_id,
                            display_name=prog.display_name,
                            keyword=keyword,
                            tab=tab,
                            duration_seconds=dur,
                            hits_found=0,
                            hits_new=0,
                            timestamp=datetime.now(timezone.utc).strftime("%H:%M:%S"),
                            complete=False,
                            stopped="error",
                        ))
                        if session is not None and hasattr(session, "sync_cookies"):
                            await session.sync_cookies()
                        if reason := classify_failure(e):
                            await sessions_engine.mark_session_failed(
                                platform_id, session_id, reason, detail=str(e))
                            prog.note = f"session {reason} mid-sweep"
                            if fatal_kind[0] != "session":
                                fatal_kind[0] = "session"
                            return reason
                        run.incomplete += 1
                        return ""

                    dur = time.time() - t0
                    hits = [h for h in (sweep.hits or []) if h.url]
                    saved_count = 0
                    new_count = 0
                    if hits:
                        # Saved per completed sweep, not batched at the end,
                        # so a caller polling this job (or reading
                        # /profiles) sees results within seconds of them
                        # being found.
                        rows = [row_to_fields(h, keyword) for h in hits]
                        saved, new = await profiles_db.save_many(
                            job.group_id, platform_id, profiles_db.PHASE_DISCOVERY,
                            rows,
                        )
                        saved_count = saved
                        new_count = new
                        # Pull each picture into our own store, BEHIND this
                        # sweep rather than inside it -- see the original
                        # method's own reasoning for why this is a spawned
                        # task, not an await.
                        task = avatar_cache.spawn(job.group_id, platform_id, rows)
                        if task is not None:
                            job.avatar_tasks.append(task)
                        prog.found += saved
                        prog.new += new
                        job.found += saved
                        job.new += new
                        stats["found"] += saved
                        stats["new"] += new
                    sweep_complete = bool(getattr(sweep, "complete", True))
                    if not sweep_complete:
                        run.incomplete += 1
                        if reason := str(getattr(sweep, "error", "") or "").strip():
                            if reason not in run.sweep_errors:
                                run.sweep_errors.append(reason)
                    prog.keywords_done += 1
                    stats["units"] += 1
                    job.history.append(CompletedSweep(
                        platform=platform_id,
                        display_name=prog.display_name,
                        keyword=keyword,
                        tab=tab,
                        duration_seconds=dur,
                        hits_found=saved_count,
                        hits_new=new_count,
                        timestamp=datetime.now(timezone.utc).strftime("%H:%M:%S"),
                        complete=sweep_complete,
                        stopped=str(getattr(sweep, "stopped", "") or ""),
                        resolved_visits=int(getattr(sweep, "resolved_visits", 0) or 0),
                        resolve_seconds=float(getattr(sweep, "resolve_seconds", 0.0) or 0.0),
                    ))
                    if session is not None and hasattr(session, "sync_cookies"):
                        await session.sync_cookies()

                    stop_reason = ""
                    if getattr(sweep, "stopped", "") == "flood-wait":
                        stop_reason = "flood-wait"
                        if fatal_kind[0] != "session":
                            fatal_kind[0] = "hard"
                    else:
                        # A sweep that caught its own session-shaped problem
                        # (TikTok's CAPTCHA/checkpoint, an auth failure a
                        # platform detected mid-page rather than as a
                        # raised exception) reports it via `error`/`stopped`
                        # instead of raising -- classified and marked
                        # exactly like the exception path above, so a
                        # session that fails THIS way is just as eligible
                        # for another worker to take over from.
                        session_reason = classify_failure(
                            getattr(sweep, "error", "") or getattr(sweep, "stopped", "")
                        )
                        if session_reason:
                            await sessions_engine.mark_session_failed(
                                platform_id, session_id, session_reason,
                                detail=getattr(sweep, "error", "") or getattr(sweep, "stopped", ""),
                            )
                            fatal_kind[0] = "session"
                            stop_reason = session_reason
                    return stop_reason

                while run.queue and not job.cancel.is_set() and not run.hard_stop:
                    item = run.queue.popleft()
                    item.attempts += 1
                    prog.current_keyword = item.keyword
                    stats = {"units": 0, "found": 0, "new": 0}
                    fatal_kind = [""]

                    # CONCURRENT ONLY WHEN IT IS SAFE AND WORTH IT: more than
                    # one tab, a factory to give each its own cap, and the
                    # setting left above 1 -- identical gate to the original.
                    concurrent = (
                        len(run.tabs) > 1
                        and make_discoverer is not None
                        and settings.discovery_tab_concurrency > 1
                    )
                    if concurrent:
                        prog.current_tab = "+".join(run.tabs)
                        prog.current_step = f"Searching {len(run.tabs)} tabs..."
                        prog.item_started_at_ts = time.time()
                        sem = asyncio.Semaphore(settings.discovery_tab_concurrency)

                        async def _slot(i: int, tab: str) -> str:
                            async with sem:
                                return await _sweep_tab(
                                    item.keyword, item.kw_type, tab, stats, fatal_kind,
                                    stagger=i * TAB_STAGGER_SEC, own_options=True,
                                )

                        reasons = await asyncio.gather(
                            *(_slot(i, t) for i, t in enumerate(run.tabs)),
                            return_exceptions=True,
                        )
                        for r in reasons:
                            if isinstance(r, BaseException):
                                log.error(f"[{platform_id}] tab sweep crashed: {type(r).__name__}: {r}")
                        reason = next((r for r in reasons if isinstance(r, str) and r), "")
                    else:
                        reason = ""
                        for tab in run.tabs:
                            if job.cancel.is_set() or run.hard_stop:
                                break
                            reason = await _sweep_tab(item.keyword, item.kw_type, tab, stats, fatal_kind)
                            if reason or fatal_kind[0]:
                                break

                    if fatal_kind[0] == "hard":
                        run.hard_stop = True
                        prog.note = f"stopped early: session {reason}"
                        return False
                    if fatal_kind[0] == "session":
                        # THIS SESSION IS DONE, THE PLATFORM IS NOT. The
                        # keyword it died on goes back on the queue for
                        # another session; everything still queued was
                        # never touched and stays there.
                        self._requeue_keyword(job, run, item, stats, label)
                        return True

                    # BREATHING ROOM BEFORE THE NEXT KEYWORD. Only between
                    # keywords, never after the last one (a gap before
                    # releasing the session buys nothing and just makes the
                    # sweep look slower), and never on a cancelled job.
                    #
                    # `pause()` takes a MULTIPLIER on the session's own
                    # configured median (options.delay), not a duration, so
                    # the target seconds are converted into one rather than
                    # slept flat -- that keeps jitter, fatigue and the
                    # occasional longer rest in play (stealth/browser.py).
                    if (run.queue and inter_keyword_delay > 0
                            and not job.cancel.is_set() and not run.hard_stop
                            and session is not None and hasattr(session, "pause")):
                        base = settings.discovery_delay_sec or 0
                        mult = (inter_keyword_delay / base) if base > 0 else 1.0
                        try:
                            await session.pause(mult)
                        except Exception:
                            pass
                return False
            finally:
                prog.current_keyword = ""
                prog.current_tab = ""
                prog.current_step = ""
                prog.item_started_at_ts = None
                self._store.release_session(held)
                # The other half of get_healthy_session's cross-runner claim
                # -- see analysis/runner.py's identical release for why this
                # happens regardless of what `session_item` looked like.
                sessions_engine.release_claim(platform_id, session_id)
                # Telegram holds a lock on its local session file until its
                # discoverer is closed; a missed stop() here is what makes
                # the NEXT Telegram run fail with "database is locked".
                if discoverer is not None and hasattr(discoverer, "stop"):
                    try:
                        await discoverer.stop()
                    except Exception:
                        pass
                if session is not None:
                    try:
                        await session.stop()
                    except Exception:
                        pass
                if anon_cm is not None:
                    try:
                        await anon_cm.__aexit__(None, None, None)
                    except Exception:
                        pass

    def _requeue_keyword(
        self, job: DiscoveryJob, run: "_PlatformSweepRun", item: "_KeywordItem",
        stats: dict, label: str,
    ) -> None:
        """A keyword whose session died under it, put back for another
        session. See analysis/runner.py's _requeue for the identical
        reasoning: undo exactly what THIS attempt contributed -- never more,
        never less, since sibling workers are incrementing the same shared
        `prog`/`job` counters for their OWN keywords at the same time, so a
        snapshot-and-reset would discard their progress instead of just this
        attempt's.

        CAPPED AT _MAX_KEYWORD_ATTEMPTS, checked BEFORE any rollback: past
        that, this attempt's partial progress is left standing rather than
        erased -- "give up" must not mean "give up AND lose what was
        already found", it means no more sessions will be spent chasing the
        rest.

        A GIVE-UP COUNTS AS INCOMPLETE, not a silent "done". This keyword's
        own last word was a session dying on it, not a real answer -- if
        that also happened to be the only unattempted work left on this
        platform, `keywords_done` reaching `keywords_total` on the strength
        of a failed final attempt must not read as the sweep having gone
        cleanly, so it feeds the same `run.incomplete`/`run.sweep_errors`
        machinery an ordinary incomplete sweep does.
        """
        if item.attempts >= _MAX_KEYWORD_ATTEMPTS:
            run.incomplete += 1
            note = f"{item.keyword!r} failed on every available session"
            if note not in run.sweep_errors:
                run.sweep_errors.append(note)
            log.warning(
                f"[{run.platform_id}] {item.keyword!r} took down {item.attempts} "
                f"session(s) -- not re-queued again, whatever this attempt "
                f"completed stands")
            return
        prog = run.prog
        if stats["units"]:
            prog.keywords_done = max(0, prog.keywords_done - stats["units"])
        if stats["found"]:
            prog.found = max(0, prog.found - stats["found"])
            job.found = max(0, job.found - stats["found"])
        if stats["new"]:
            prog.new = max(0, prog.new - stats["new"])
            job.new = max(0, job.new - stats["new"])
        run.queue.append(item)
        log.info(
            f"[{run.platform_id}] session {label} failed on {item.keyword!r} -- "
            f"re-queued for another session (attempt {item.attempts + 1} of "
            f"{_MAX_KEYWORD_ATTEMPTS})")

    async def stats(self) -> dict:
        return await self._store.stats()


discovery_runner = DiscoveryRunner()
