"""The analysis feature, whole: paste URLs, scrape them, read the results.

WHAT THIS IS. Analysis is a standalone tool with one input box and one
Scrape button. It takes profile URLs directly from an analyst, visits each
one, and returns the fields the legacy export expects plus an evidence
screenshot. It does not read a client record, a keyword list, or anything
discovery produced. Discovery and analysis remain independent passes with
no shared state in either direction; analysis exists to read exactly what a
search sweep cannot -- follower/member counts, bio, location, last-post
date, and a screenshot, all of which need a real profile visit.

TWO LIFETIMES, AND THEY ARE NOT THE SAME THING:

    a JOB     live progress -- in memory, here, bounded by MAX_JOBS /
              JOB_TTL_SECONDS, gone on restart. It is a view of work in
              flight, not a record of it.

    a RESULT  the reading itself -- written to MongoDB as each item settles
              and deleted 24 hours later by a TTL index. See
              database/repositories/analysis_result_repository.py.

That second half is new. Analysis output used to be memory-only, which
meant a reload or a restart cost the analyst the whole batch and the pages
had to be scraped again -- real page loads under a live session, spent on
work already done. `_settle` is the one place an item becomes final, and it
is what writes the result; both terminal paths (scored and failed) go
through it, so "every result is saved" is a property of the shape rather
than a rule six call sites have to remember.

ROBUSTNESS. Sessions are taken per platform per job, never per URL, so a
40-URL paste does not open 40 browser sessions -- a platform claims as many
pooled sessions as it can safely work with at once (see `_scrape_platform`
and _MAX_SESSIONS_PER_PLATFORM: usually one, up to three when the pool has
that many accounts, and never more than the
batch has work for). Those sessions pull from one shared queue, which is
what makes a session dying mid-run survivable: the URLs it had not finished
are still queue entries, so another session takes them instead of the batch
reporting them failed. A URL that fails for its own reasons is recorded as
an errored item, saved like any other result, and never sinks the rest of
the batch. Cancellation is checked between every chunk, and a cancelled job
keeps whatever it had already read. Persisting can never fail a job: a
storage error costs the row its durability and is logged, nothing more.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
import weakref
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from backend.config.settings import settings
from backend.platforms import registry
from backend.platforms.scan_options import ScanOptions
from backend.sessions import manager as sessions_engine
from backend.database.repositories import analysis_result_repository as results_db
from backend.shared.job_store import JobStore
from backend.shared.logging import get_logger
from backend.shared.models.row import Row
from backend.shared.models.scoring import compute_incident_risk_score
from backend.shared.resilience import classify_failure

log = get_logger("analysis.runner")

# Bounded so a process left running for weeks cannot accumulate jobs. Both
# ceilings are deliberately generous -- this is an interactive tool, an
# analyst is not going to have 200 live jobs -- and exist to stop unbounded
# growth, not to ration normal use.
MAX_JOBS = 200
JOB_TTL_SECONDS = 6 * 3600

# host -> platform id. Same vocabulary as platforms/registry.py, which is
# what these are looked up against immediately after parsing.
_PLATFORM_HOSTS: dict[str, str] = {
    "facebook.com": "facebook", "fb.com": "facebook", "fb.me": "facebook",
    "twitter.com": "twitter", "x.com": "twitter",
    "instagram.com": "instagram",
    "youtube.com": "youtube", "youtu.be": "youtube",
    "t.me": "telegram", "telegram.me": "telegram",
    "tiktok.com": "tiktok",
}

QUEUED, RUNNING, DONE, CANCELLED, FAILED = "queued", "running", "done", "cancelled", "failed"
_TERMINAL = frozenset({DONE, CANCELLED, FAILED})

# How many of one platform's URLs run at once inside its single held
# session, and how long to wait between one chunk and the next. Both sets
# of numbers are ported from unifiedtool-og's analysis runner
# (backend/api/routes/jobs.py::run_analysis), which pools one browser and
# hands out N tabs per batch: 3 tabs for the browser-driven platforms
# (ANALYSIS_CONCURRENT_TABS), 6 for the ones that are really just an API
# call (ANALYSIS_API_CONCURRENT_TABS), and Instagram pinned to 1 with the
# longest gap of any platform.
#
# The mechanism here was already og's shape -- one session per platform
# per job, URLs run in chunks of `concurrency`, each URL opening its own
# tab in the shared stealth context (see each engine's `one()`, which does
# `self.ctx.new_page()`). Only the numbers moved.
#
# What that buys and what it costs: every engine's loop treats a
# CHECKPOINT/challenge as fatal to the REST of that platform's batch (see
# `_scrape_platform`), so an over-driven session does not just risk its
# own URL -- it can cost every URL still queued behind it. These are the
# numbers og runs in production; drop a platform back a step if its logs
# start showing checkpoints or rate limits.
_PLATFORM_CONCURRENCY: dict[str, int] = {
    # Official Data API: quota-metered, not ban-risked, and every call is an
    # ordinary HTTPS request. Nominal either way -- YouTube's whole batch
    # goes through ONE channels.list call (`_scrape_youtube_batch`), so
    # nothing here actually gates it.
    "youtube": 6,
    # og groups Telegram with YouTube as an official-API platform. Worth
    # knowing what that costs here: all of these ride ONE MTProto client,
    # and Telegram answers bursts of resolve() with FloodWait -- which
    # telegram/analysis_engine.py turns into CHECKPOINT, and CHECKPOINT
    # stops the whole platform's remaining batch. This is the first number
    # to lower if FloodWait shows up.
    "telegram": 6,
    # Meta is the one place where concurrency is a BEHAVIOURAL tell rather
    # than just a load question: every visit rides one logged-in account,
    # and a person opens one profile at a time. og pins Instagram to 1
    # unconditionally, ahead of its own configured tab count, and gives it
    # the longest inter-batch gap below. Facebook is NOT pinned there -- it
    # takes the browser default with the rest.
    "instagram": 1,
}
# Facebook, Twitter, TikTok -- og's ANALYSIS_CONCURRENT_TABS.
_DEFAULT_CONCURRENCY = 3

# Seconds between one chunk and the next: og's ANALYSIS_INTER_PROFILE_DELAY
# (1.5) for browser platforms, ANALYSIS_API_INTER_PROFILE_DELAY (0.0) for
# the API ones, 3.5 for Instagram. Spent through `scraper.pause()` rather
# than a flat sleep so this app's own jitter/fatigue pacing
# (stealth/human.py) still shapes the gap; 0 skips the pause entirely.
_PLATFORM_INTER_BATCH_DELAY: dict[str, float] = {
    "youtube": 0.0,
    "telegram": 0.0,
    "instagram": 3.5,
}
_DEFAULT_INTER_BATCH_DELAY = 1.5

# ------------------------------------------------- parallel sessions, failover
#
# ONE PLATFORM, SEVERAL SESSIONS, AT ONCE. `_PLATFORM_CONCURRENCY` above is how
# many tabs ONE held session opens; this is how many SESSIONS the platform gets
# to work with at all. A 40-URL Twitter paste against a two-account pool runs
# as two workers -- two accounts, two proxies, two browser contexts -- pulling
# chunks off one shared queue. That buys roughly 2x throughput, but the reason
# it is built this way is the other half: when one session checkpoints, the
# URLs it had not finished are still QUEUE ENTRIES, so the surviving worker
# takes them. Before this, one session dying meant every URL still queued
# behind it was reported failed, with healthy accounts sitting idle in the pool
# while it happened.
#
# 1 IS NOT A DEGRADED PATH. A single-session pool claims one session and runs
# the identical chunk-and-pause loop it always did.
_MAX_SESSIONS_PER_PLATFORM: dict[str, int] = {
    # ONE batched channels.list call covers the whole platform
    # (`_scrape_youtube_batch`), so there is no per-URL work to split and a
    # second key would only mean two quota buckets spent on one job.
    "youtube": 1,
    # Telethon keeps ONE local session file open at a time behind an SQLite
    # lock (see sessions/manager.py::session_for_job), so a second Telegram
    # worker would be two clients fighting over one file, not two clients.
    "telegram": 1,
}
_DEFAULT_MAX_SESSIONS_PER_PLATFORM = 3

# How many sessions one URL may be handed to. 2 means one retry, on one other
# session, and then it is reported failed with whatever the last session gave
# it. Bounded because the failure being recovered from is ambiguous: usually
# the session died, but sometimes the URL itself is what trips the challenge,
# and an uncapped retry would let one URL walk the whole pool killing accounts
# as it goes.
_MAX_ITEM_ATTEMPTS = 2

# How many times a platform may go back to the pool for REPLACEMENT sessions
# after everything it had claimed died. Round 1 is the ordinary claim; round 2
# exists for the case where a session came free (another job finished, or the
# monitor revived one) while this batch was running.
_MAX_CLAIM_ROUNDS = 2

# Bound on browser workers open at once across the WHOLE process, which is the
# number that actually decides whether the host copes. `_run` scrapes every
# platform of a job CONCURRENTLY, so a per-platform cap cannot see the total:
# without this, a 4-platform job at 3 sessions each would try to hold twelve
# Chromium contexts, each with its own Playwright driver, plus their tabs.
#
# Keyed by running loop rather than one module-level Semaphore: asyncio
# primitives bind to the loop that first awaits them, so a test suite that
# builds a fresh loop per test would otherwise inherit one bound to a loop that
# is already closed. Weak keys so closed loops do not accumulate.
_worker_slots: "weakref.WeakKeyDictionary[Any, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary())


def _worker_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _worker_slots.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(max(1, settings.analysis_max_browser_workers))
        _worker_slots[loop] = sem
    return sem


def _sessions_wanted(platform_id: str, item_count: int, concurrency: int) -> int:
    """How many sessions it is worth claiming for this platform.

    Never more than there is work for. A claimed session is invisible to every
    other job for the life of the batch, so claiming one that would sit idle
    costs another job an account for nothing -- and one worker already reads
    `concurrency` URLs at a time, so the second session only has work once the
    batch is longer than a single chunk.

    API-key and MTProto platforms are pinned to one: `session_for_job` hands
    those credentials over by mutating os.environ or one local session file, so
    a second claim would overwrite the first rather than run beside it.
    """
    cap = _MAX_SESSIONS_PER_PLATFORM.get(
        platform_id, _DEFAULT_MAX_SESSIONS_PER_PLATFORM)
    plat = registry.PLATFORMS.get(platform_id)
    if plat is not None and (plat.uses_api_key or plat.env_keys):
        cap = 1
    chunks = -(-item_count // max(1, concurrency))
    return max(1, min(cap, item_count, chunks))


def parse_direct_url(raw: str) -> Optional[tuple[str, str, str]]:
    """(platform, normalized_url, entity_id) for one pasted URL, or None
    when its host is not a platform this tool can read. Facebook goes
    through its own id/URL normalizer so a pasted profile.php link and its
    vanity form resolve identically; every other platform takes the last
    non-empty path segment, the same heuristic discovery uses."""
    raw = (raw or "").strip()
    if not raw:
        return None
    url = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    platform = _PLATFORM_HOSTS.get(host, "")
    if not platform:
        return None

    if platform == "facebook":
        try:
            from backend.platforms.facebook.discovery_engine import normalize_url, profile_id
            url = normalize_url(url)
            return platform, url, profile_id(url)
        except Exception:
            pass

    parts = [s for s in parsed.path.rstrip("/").split("/") if s]
    entity_id = parts[-1].lstrip("@") if parts else ""
    return platform, url, entity_id


def to_ddmmyyyy(iso: Optional[str]) -> str:
    """'2026-07-16...' -> '16-07-2026'. Unrecognised/empty input passes
    through rather than becoming a guess -- a blank date must stay blank."""
    if not iso:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(iso))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else str(iso)


def _tri(flag: str) -> Optional[bool]:
    """Row's "Yes"/"No"/"" -> True/False/None. The empty string means the
    scraper could not determine the field, which is NOT the same as
    determining it false."""
    return True if flag == "Yes" else False if flag == "No" else None


@dataclass
class AnalysisItem:
    """One pasted URL and everything read from it. Field names match the
    frontend's own `AnalysisItemData` exactly -- this shape is a contract
    with `frontend/src/api/analysisApi.ts`."""

    id: str
    raw_url: str
    url: str
    platform: str
    entity_id: str
    status: str = "pending"  # pending | running | done | error
    error: str = ""
    analysed_at: Optional[str] = None
    # How many SESSIONS this URL has been tried on. Not part of to_dict() --
    # `to_dict` is a contract with the frontend and this is scheduling state,
    # not a reading. Bumped in `_scrape_one` and read by `_requeue`, which is
    # what stops a URL that is itself tripping challenges from walking the
    # whole pool. See _MAX_ITEM_ATTEMPTS.
    attempts: int = 0

    profile_name: str = ""
    # The PARENT keyword whose discovery sweep found this profile, when it
    # came from one (see discovery.py::_seed_from_doc). Reported as the
    # export's AssetName so a row surfaced by the permutation
    # "gautam.adani.hq" still exports under "Gautam Adani". Blank for a
    # pasted URL, which has no keyword behind it.
    main_keyword: str = ""
    followers: Optional[int] = None
    followers_exact: str = ""
    location: str = ""
    bio: str = ""
    last_post_date: str = ""
    created_date: str = ""
    is_active: Optional[bool] = None
    has_logo: Optional[bool] = None
    has_name_match: Optional[bool] = None
    name_score: int = 0
    risk_score: int = 2
    priority: str = "Low"
    profile_image_url: str = ""
    avatar_sha: str = ""
    verified: Optional[bool] = None
    comments: str = ""
    has_screenshot: bool = False

    incident_row: dict[str, Any] = field(default_factory=dict)
    legacy_row: dict[str, Any] = field(default_factory=dict)
    duration_seconds: Optional[float] = None
    started_at_ts: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "url": self.url, "platform": self.platform,
            # Stable across runs and across storage, unlike `id` (a fresh
            # uuid per job). It is what lets the UI lay a running job's rows
            # over the saved set without showing the same profile twice, and
            # what addresses a saved row's screenshot once its job is gone.
            "result_id": results_db.result_id(self.platform, self.url),
            "platform_name": registry.display_name(self.platform),
            "entity_id": self.entity_id, "status": self.status,
            "error": self.error, "analysed_at": self.analysed_at,
            "duration_seconds": self.duration_seconds,
            "started_at_ts": self.started_at_ts,
            "profile_name": self.profile_name, "followers": self.followers,
            "location": self.location, "bio": self.bio,
            "last_post_date": self.last_post_date,
            "is_active": self.is_active, "has_logo": self.has_logo,
            "has_name_match": self.has_name_match, "name_score": self.name_score,
            "risk_score": self.risk_score, "priority": self.priority,
            "profile_image_url": self.profile_image_url, "avatar_sha": self.avatar_sha,
            "verified": self.verified,
            "comments": self.comments, "has_screenshot": self.has_screenshot,
            "incident_row": self.incident_row, "legacy_row": self.legacy_row,
        }


@dataclass
class AnalysisJob:
    """One Scrape-button press: the URLs it was given and how far it got."""

    id: str
    created_at: float = field(default_factory=time.time)
    status: str = QUEUED
    target_name: str = ""
    official_feed: str = ""
    # The client this batch belongs to, when it has one -- only "Analyse
    # Validated Profiles" (POST /discovery/profiles/analyse) supplies these,
    # since that's the one analysis entry point with a real client behind
    # it (group_id IS the client's client_id by convention; domain is
    # forwarded separately since discovery has no client record to read it
    # from either -- see discovery.py's own analyse_validated). A job
    # started from pasted URLs has neither: analysis was built to run with
    # no client record at all, by design (see this module's own docstring),
    # so those rows fall back to a generic tag in _build_rows, matching
    # this app's own "QUICK-ANALYSIS" precedent for a client-less batch.
    org_id: str = ""
    domain: str = ""
    total: int = 0
    completed: int = 0
    message: str = ""
    items: list[AnalysisItem] = field(default_factory=list)
    platform_progress: dict[str, dict[str, Any]] = field(default_factory=dict)
    task: Optional[Any] = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    started_at_ts: Optional[float] = None
    finished_at_ts: Optional[float] = None
    # url -> whatever discovery already knows about that profile (see
    # POST /discovery/profiles/analyse -> _seed_from_doc). Not part of
    # to_dict()/the frontend contract -- it's an input to scraping, not a
    # result. Empty for a job started from pasted URLs, which have no
    # discovery record behind them.
    seed_by_url: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        now = time.time()
        elapsed = (self.finished_at_ts or now) - self.started_at_ts if self.started_at_ts else 0.0

        completed_items = [i for i in self.items if i.status in ("done", "error") and i.duration_seconds]
        remaining = max(0, self.total - self.completed)
        est_remaining_sec: Optional[float] = None
        if self.status == RUNNING and self.started_at_ts and self.total > 0:
            if completed_items:
                avg_dur = sum(i.duration_seconds for i in completed_items) / len(completed_items)
                concurrency = max(1, sum(1 for p in self.platform_progress.values() if p.get("status") == "running"))
                est_remaining_sec = round((avg_dur * remaining) / concurrency, 1)
            else:
                est_remaining_sec = round(6.0 * remaining, 1)

        return {
            "id": self.id, "status": self.status,
            "target_name": self.target_name, "official_feed": self.official_feed,
            "total": self.total, "completed": self.completed,
            "message": self.message,
            "started_at_ts": self.started_at_ts,
            "finished_at_ts": self.finished_at_ts,
            "elapsed_seconds": round(elapsed, 1),
            "estimated_remaining_seconds": est_remaining_sec,
            "platform_progress": self.platform_progress,
            "items": [i.to_dict() for i in self.items],
        }


@dataclass
class _PlatformRun:
    """One platform's work, shared by every worker on it.

    The queue IS the coordination: workers pull chunks off it until it is
    empty, and a worker whose session dies pushes its unfinished URLs back on
    for the others. A plain deque rather than an asyncio.Queue because every
    worker runs on one event loop, so a truthiness check followed by `popleft`
    cannot be interleaved -- there is no await between them -- which makes the
    lock and the task_done() bookkeeping an asyncio.Queue would add pure cost,
    and cost that has to be kept correct across re-queues.
    """

    platform_id: str
    queue: deque[AnalysisItem]
    options: ScanOptions
    concurrency: int
    inter_batch_delay: float
    progress: dict[str, Any]


class AnalysisRunner:
    """Owns every live analysis job. Process-wide, in memory, bounded."""

    def __init__(self) -> None:
        self._store: JobStore[AnalysisJob] = JobStore(
            max_jobs=MAX_JOBS, ttl_seconds=JOB_TTL_SECONDS, terminal_statuses=_TERMINAL,
            on_evict=lambda job_id: self._on_job_evicted(job_id),
        )
        # Screenshots live outside JobStore: they're keyed by "job:item",
        # not by job id alone, and dropped explicitly in `_drop` below when
        # their job is evicted -- the bytes are what actually matter (see
        # this module's docstring), so their lifecycle is handled here,
        # deliberately, rather than folded into the generic store.
        self._screenshots: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    def holds_session(self, platform_id: str, session_id: str) -> bool:
        return self._store.holds_session(platform_id, session_id)

    # ------------------------------------------------------------- lifecycle

    async def start(
        self, urls: list[str], target_name: str = "", official_feed: str = "",
        seed_by_url: Optional[dict[str, dict]] = None,
        org_id: str = "", domain: str = "",
    ) -> tuple[AnalysisJob, list[dict]]:
        """Parse the pasted URLs and kick off the scrape. Returns the job
        plus whatever was skipped (unsupported host, unparseable, or a
        duplicate of another URL in the same paste) -- reported back rather
        than silently dropped, so an analyst can see why 40 URLs became 37.

        `seed_by_url`, when given (only `POST /discovery/profiles/analyse`
        passes one), is whatever discovery already read for that URL --
        handed to each platform's `scraper.one()` so it can skip re-fetching
        a field it's already been told, and used again here as a fallback
        if the fresh scrape still comes back blank on that field (see
        `_populate`)."""
        skipped: list[dict] = []
        items: list[AnalysisItem] = []
        seen: set[str] = set()

        for raw in urls:
            raw = (raw or "").strip()
            if not raw:
                continue
            parsed = parse_direct_url(raw)
            if not parsed:
                skipped.append({"url": raw, "reason": "not a supported platform URL"})
                continue
            platform, url, entity_id = parsed
            plat = registry.PLATFORMS.get(platform)
            if plat is None or not plat.enabled:
                skipped.append({"url": raw, "reason": f"{platform} is not available"})
                continue
            if url in seen:
                skipped.append({"url": raw, "reason": "duplicate of another URL in this batch"})
                continue
            seen.add(url)
            items.append(AnalysisItem(
                id=uuid.uuid4().hex[:12], raw_url=raw, url=url,
                platform=platform, entity_id=entity_id,
            ))

        job = AnalysisJob(
            id=uuid.uuid4().hex[:12], target_name=target_name.strip(),
            official_feed=official_feed.strip(), items=items, total=len(items),
            seed_by_url=seed_by_url or {}, org_id=org_id.strip(), domain=domain.strip(),
        )
        for it in items:
            entry = job.platform_progress.setdefault(it.platform, {
                "status": "pending", "total": 0, "completed": 0,
                "display_name": registry.display_name(it.platform),
            })
            entry["total"] += 1

        await self._store.put(job)

        if not items:
            job.status = DONE
            job.message = "nothing to scrape -- no supported URLs in that list"
        else:
            job.task = asyncio.create_task(self._run(job))
        return job, skipped

    async def get(self, job_id: str) -> Optional[AnalysisJob]:
        return await self._store.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        return await self._store.cancel(job_id)

    async def screenshot(self, job_id: str, item_id: str) -> Optional[bytes]:
        """The evidence capture for one analysed URL, straight from RAM."""
        async with self._lock:
            return self._screenshots.get(f"{job_id}:{item_id}")

    def _on_job_evicted(self, job_id: str) -> None:
        """Called synchronously by JobStore the moment it drops a job (TTL
        expiry or pressure eviction) -- see JobStore's own `on_evict` param.
        Runs with no `await` inside it, so it can't be interrupted mid-way
        by another coroutine touching `_screenshots`; the same "single
        event loop, no yield point" reasoning `holds_session` already
        relies on for its own lock-free set. Dropping a job drops its
        screenshots with it -- those bytes are what actually matter (see
        this module's docstring)."""
        prefix = f"{job_id}:"
        for k in [k for k in self._screenshots if k.startswith(prefix)]:
            self._screenshots.pop(k, None)
            log.warning(f"analysis job {job_id} evicted -- dropped its screenshot(s); results are memory-only")

    # --------------------------------------------------------------- scraping

    async def _run(self, job: AnalysisJob) -> None:
        job.status = RUNNING
        job.started_at_ts = time.time()
        try:
            by_platform: dict[str, list[AnalysisItem]] = {}
            for it in job.items:
                by_platform.setdefault(it.platform, []).append(it)
            # Every platform scraped CONCURRENTLY. Each is a fully separate
            # account and browser context on a fully separate host --
            # there is no shared-session risk running Twitter and Instagram
            # at once the way there would be two sessions open on the SAME
            # platform simultaneously (guarded by JobStore's per-
            # (platform, session_id) hold instead, see _scrape_platform).
            # Cancellation is checked between every URL inside each
            # platform's own loop, so a cancel mid-run stops all of them
            # promptly rather than waiting for whichever platform happens
            # to be running.
            results = await asyncio.gather(
                *(self._scrape_platform(job, pid, items) for pid, items in by_platform.items()),
                return_exceptions=True,
            )
            for (pid, items), result in zip(by_platform.items(), results):
                if isinstance(result, BaseException):
                    log.error(f"analysis job {job.id}: {pid} raised past its own handling -- {result}")
                    job.platform_progress[pid]["status"] = "failed"
                    for it in items:
                        if it.status in ("pending", "running"):
                            await self._fail_item(job, it, f"{type(result).__name__}: {result}")
                            job.completed += 1
                            job.platform_progress[pid]["completed"] += 1

            if job.cancel.is_set():
                job.status = CANCELLED
                job.message = f"cancelled after {job.completed}/{job.total}"
            else:
                job.status = DONE
                errored = sum(1 for i in job.items if i.status == "error")
                job.message = f"{job.total - errored}/{job.total} scraped" + (
                    f", {errored} failed" if errored else "")
        except Exception as e:
            job.status = FAILED
            job.message = f"{type(e).__name__}: {e}"
            log.error(f"analysis job {job.id} failed: {job.message}")
        finally:
            job.finished_at_ts = time.time()

    async def _scrape_platform(
        self, job: AnalysisJob, platform_id: str, items: list[AnalysisItem],
    ) -> None:
        """Every URL on one platform, worked by every session that platform
        can safely lend at once.

        A QUEUE AND N WORKERS, not a loop over `items`. That shape is what
        lets a two-account pool halve a 40-URL batch, and it is the only
        shape in which a session dying mid-run is RECOVERABLE: the URLs that
        session had not finished are still queue entries, so a surviving
        worker takes them instead of the batch reporting them failed. With
        one session in the pool N is 1 and this is the same chunk-and-pause
        loop it has always been -- the single-session path is not a fallback,
        it is this code with one worker.
        """
        progress = job.platform_progress[platform_id]
        progress["status"] = "running"
        progress["current_url"] = ""
        progress["current_step"] = "Connecting session..."
        progress["item_started_at_ts"] = time.time()

        # Clamped to the work actually queued, as og does
        # (`min(parallelism, total)`): a 1-URL job should not advertise 3
        # tabs in flight to the progress banner, and ScanOptions.concurrency
        # is read by engines that batch on their own (facebook's run()).
        concurrency = max(1, min(
            _PLATFORM_CONCURRENCY.get(platform_id, _DEFAULT_CONCURRENCY), len(items)))
        run = _PlatformRun(
            platform_id=platform_id,
            queue=deque(items),
            options=ScanOptions(
                evidence=None,
                ephemeral_screenshot=True,
                delay=settings.analysis_delay_sec,
                concurrency=concurrency,
                headful=not settings.headless,
            ),
            concurrency=concurrency,
            inter_batch_delay=_PLATFORM_INTER_BATCH_DELAY.get(
                platform_id, _DEFAULT_INTER_BATCH_DELAY),
            progress=progress,
        )

        want = _sessions_wanted(platform_id, len(items), concurrency)
        progress["workers"] = 0
        setup_error = ""
        worker_error = ""
        session_died = False
        rounds = 0

        while rounds < _MAX_CLAIM_ROUNDS and run.queue and not job.cancel.is_set():
            rounds += 1
            try:
                plat, claimed = await self._claim_sessions(platform_id, want)
            except Exception as e:
                if rounds == 1:
                    # Nothing to run with AT ALL -- the pool is empty, dead or
                    # entirely rate-limited. Recorded as this platform's
                    # failure detail so the analyst reads ConflictError's own
                    # "no healthy sessions available -- please add more
                    # cookies" on the rows rather than a generic message.
                    setup_error = f"{type(e).__name__}: {e}"
                    log.error(f"analysis job {job.id}: {platform_id} failed -- {setup_error}")
                break
            if not claimed:
                break

            if rounds > 1:
                log.info(
                    f"[{platform_id}] {len(run.queue)} URL(s) still queued after every "
                    f"session failed -- retrying on {len(claimed)} replacement session(s)")
            elif len(claimed) > 1:
                log.info(
                    f"[{platform_id}] {len(items)} URL(s) split across {len(claimed)} "
                    f"sessions in parallel, {concurrency} tab(s) each")

            progress["workers"] = len(claimed)
            results = await asyncio.gather(
                *(self._platform_worker(job, run, plat, session_item)
                  for session_item in claimed),
                return_exceptions=True,
            )
            progress["workers"] = 0
            for result in results:
                if isinstance(result, BaseException):
                    # A worker's SETUP failed (its own session was unusable),
                    # which is not this platform's failure while another
                    # worker or another round can still drain the queue --
                    # held onto as the detail to report only if nothing does.
                    worker_error = f"{type(result).__name__}: {result}"
                    log.error(
                        f"analysis job {job.id}: {platform_id} worker failed -- {worker_error}")
                elif result:
                    session_died = True

        progress["current_url"] = ""
        progress["current_step"] = ""
        progress["item_started_at_ts"] = None

        # WHATEVER NO SESSION EVER REACHED. Both the URLs still sitting in the
        # queue and any that were re-queued and never picked up again -- both
        # are "pending", so status is the one thing that has to be asked.
        stranded = [it for it in items if it.status in ("pending", "running")]
        if job.cancel.is_set():
            # A cancelled platform keeps what it read and leaves the rest
            # pending, which is what `_run` reports as "cancelled after
            # N/total". Failing them here would turn the analyst's own cancel
            # into a screenful of errors.
            progress["status"] = "done"
        elif stranded:
            detail = setup_error or worker_error or (
                "every available session for this platform failed or checkpointed -- "
                "see the earlier failed item(s) on this platform for why")
            for it in stranded:
                await self._fail_item(job, it, detail)
                job.completed += 1
                progress["completed"] += 1
            progress["status"] = "failed"
        elif progress.get("status") == "failed":
            # Already reported failed by the work itself rather than by a
            # session dying -- youtube's batch does this when quota runs out.
            pass
        elif session_died and any(it.status == "error" for it in items):
            # A session died and failover did not cover everything it took
            # with it. Every URL settled, so nothing is stranded, but the
            # platform did lose readings to a session rather than to the URLs.
            progress["status"] = "failed"
        else:
            # Includes the case this was built for: a session died mid-run,
            # another one finished its URLs, and the analyst gets a complete
            # platform anyway.
            progress["status"] = "done"

    async def _claim_sessions(
        self, platform_id: str, want: int,
    ) -> tuple[Any, list[dict]]:
        """Up to `want` pooled sessions, claimed for
        the life of this platform's batch.

        Raises whatever `session_for_job` raised when it could not supply even
        ONE: that error carries the message an analyst can act on ("no healthy
        sessions available -- please add more cookies") and failing the
        platform on it is what already happened. A shortfall after the first
        is not a failure at all -- two of three accounts busy on another job
        just means two workers instead of three.
        """
        plat: Any = None
        claimed: list[dict] = []
        while len(claimed) < want:
            try:
                plat, session_item = await sessions_engine.session_for_job(platform_id)
            except Exception:
                if not claimed:
                    raise
                break
            session_id = str(session_item.get("id") or "")
            if not session_id or session_item.get("anonymous"):
                # NOT A POOL ENTRY. `session_for_job` fell back to a
                # logged-out context (tiktok) or an env API key; there is no
                # second identity behind that to claim and it would hand back
                # this same dict forever, so it is this platform's one worker.
                if not claimed:
                    claimed.append(session_item)
                break
            claimed.append(session_item)
        return plat, claimed

    async def _platform_worker(
        self, job: AnalysisJob, run: _PlatformRun, plat: Any, session_item: dict,
    ) -> bool:
        """One held session, pulling chunks off `run.queue` until it is empty.
        -> True if this session DIED (its unfinished URLs are back on the
        queue); False if it worked the queue out or was cancelled.

        Nothing about HOW a URL is read changes with worker count, and all of
        it is per-worker: its own browser context, its own cookie jar, its own
        cookie jar, its own `concurrency` tabs, the same `scraper.one()` and
        therefore the same waits, the same extraction and the same full-page
        evidence screenshot. A worker is a second reader of one queue, not a
        cheaper kind of read.
        """
        platform_id = run.platform_id
        progress = run.progress
        session_id = str(session_item.get("id") or "")
        label = session_item.get("identifier") or session_id or "anonymous"
        scraper = None
        held: Optional[tuple[str, str]] = None

        # Ceilinged process-wide, not per-platform -- see `_worker_semaphore`.
        async with _worker_semaphore():
            if not run.queue or job.cancel.is_set():
                # The other workers drained it while this one waited for a
                # slot. Do not pay for a browser launch to discover that.
                sessions_engine.release_claim(platform_id, session_id)
                return False
            try:
                held = self._store.hold_session(platform_id, session_id)
                if session_item.get("anonymous"):
                    scraper = plat.scraper()(
                        run.options, [], anonymous=True,
                    )
                else:
                    scraper = plat.scraper()(
                        run.options, session_item.get("cookies", []),
                        session_id=session_id,
                    )
                inner = getattr(scraper, "session", None)
                if inner is not None:
                    inner.on_cookies = sessions_engine.cookie_saver(platform_id, session_id)
                await scraper.start()

                # SKIPPED WHEN THE ANSWER IS ALREADY KNOWN -- the same reasoning
                # and the same PROVEN_FRESH_S window discovery/runner.py uses.
                # `check_session()` is a real authenticated page load (measured at
                # 12.65s on Facebook) and it ran before every analysis job, asking
                # a question the session monitor and the last job had already
                # answered. A session that has died since is caught by the first
                # profile visit through the identical classify_failure ->
                # mark_session_failed path, which is what already handles a
                # session dying mid-job.
                if sessions_engine.proven_fresh(session_item):
                    log.info(
                        f"[{platform_id}] login probe skipped -- proven healthy "
                        f"{(time.time() - float(session_item.get('last_ok') or 0)) / 60:.0f}m ago"
                    )
                else:
                    if not await scraper.check_session():
                        await sessions_engine.mark_session_failed(
                            platform_id, session_id, "expired")
                        raise RuntimeError(
                            f"{registry.display_name(platform_id)} session is not usable -- "
                            "check credentials under Sessions")
                    await sessions_engine.mark_session_ok(platform_id, session_id)
                if inner is not None and hasattr(inner, "sync_cookies"):
                    await inner.sync_cookies()

                if platform_id == "youtube":
                    # ONE channels.list call for the whole platform, so there
                    # is no queue to work through: it takes the lot in one go.
                    # Pinned to a single worker by `_sessions_wanted` for the
                    # same reason.
                    batch = list(run.queue)
                    run.queue.clear()
                    await self._scrape_youtube_batch(
                        job, platform_id, batch, scraper, session_item, progress)
                    return False

                while run.queue and not job.cancel.is_set():
                    chunk = [run.queue.popleft()
                             for _ in range(min(run.concurrency, len(run.queue)))]
                    fatal = await asyncio.gather(
                        *(self._scrape_one(job, it, scraper, platform_id, session_item,
                                           stagger=idx)
                          for idx, it in enumerate(chunk))
                    )
                    if inner is not None and hasattr(inner, "sync_cookies"):
                        await inner.sync_cookies()
                    if any(fatal):
                        # THIS SESSION IS DONE, THE BATCH IS NOT. The URLs it
                        # died on go back on the queue for another session;
                        # everything still queued was never touched and stays
                        # there. Both are why this worker stopping is no longer
                        # the same event as the platform stopping.
                        for it, is_fatal in zip(chunk, fatal):
                            if is_fatal:
                                self._requeue(job, run, it, label)
                        return True
                    if run.queue and run.inter_batch_delay > 0:
                        # `pause()` takes a MULTIPLIER on the configured median
                        # gap (settings.analysis_delay_sec), not a duration, so
                        # convert og's target seconds into one rather than
                        # sleeping flat -- that keeps jitter, fatigue and the
                        # occasional longer rest in play (stealth/browser.py).
                        base = settings.analysis_delay_sec or 0
                        mult = (run.inter_batch_delay / base) if base > 0 else 1.0
                        try:
                            await scraper.pause(mult)
                        except Exception:
                            pass
                return False
            finally:
                progress["current_url"] = ""
                progress["current_step"] = ""
                progress["item_started_at_ts"] = None
                self._store.release_session(held)
                sessions_engine.release_claim(platform_id, session_id)
                if scraper is not None:
                    try:
                        await scraper.stop()
                    except Exception:
                        pass

    def _requeue(
        self, job: AnalysisJob, run: _PlatformRun, it: AnalysisItem, label: str,
    ) -> None:
        """A URL whose session died under it, put back for another session.

        BOTH HALVES OF ITS FIRST ATTEMPT ARE UNDONE. `_scrape_one` has already
        counted it (`job.completed`, and this platform's own `completed`) and
        `_fail_item` -> `_settle` has already written it to MongoDB as an
        error. The counters are given back here, because a re-queued URL
        counted twice walks the progress bar past 100%; the saved row is left
        alone, because `analysis_result_repository.save` replaces by a
        result_id derived from platform+url, so the retry's row overwrites the
        failed one rather than joining it in the analyst's view.

        CAPPED AT `_MAX_ITEM_ATTEMPTS`. Past that the URL keeps the error the
        last session gave it: a URL that is itself what trips the challenge
        must not be able to walk the pool killing sessions as it goes.
        """
        if it.attempts >= _MAX_ITEM_ATTEMPTS:
            log.warning(
                f"[{run.platform_id}] {it.url} took down {it.attempts} session(s) -- "
                f"not re-queued again, reported as failed")
            return
        job.completed = max(0, job.completed - 1)
        run.progress["completed"] = max(0, int(run.progress.get("completed") or 0) - 1)
        it.status = "pending"
        it.error = ""
        it.comments = ""
        it.analysed_at = None
        it.duration_seconds = None
        it.started_at_ts = None
        run.queue.append(it)
        log.info(
            f"[{run.platform_id}] session {label} failed on {it.url} -- re-queued for "
            f"another session (attempt {it.attempts + 1} of {_MAX_ITEM_ATTEMPTS})")

    async def _scrape_youtube_batch(
        self, job: AnalysisJob, platform_id: str, items: list[AnalysisItem],
        scraper: Any, session_item: dict, progress: dict,
    ) -> None:
        for it in items:
            it.status = "running"
            it.started_at_ts = time.time()

        progress["current_step"] = f"Batch resolving {len(items)} channels..."
        progress["item_started_at_ts"] = time.time()

        jobs = [(it.url, job.target_name, job.official_feed) for it in items]
        t0 = time.time()
        try:
            rows = await scraper.run(jobs)
        except Exception as e:
            dur = time.time() - t0
            detail = f"{type(e).__name__}: {e}"
            log.error(f"analysis job {job.id}: youtube batch failed -- {detail}")
            for it in items:
                it.duration_seconds = round(dur / max(1, len(items)), 2)
                await self._fail_item(job, it, detail)
                job.completed += 1
                progress["completed"] += 1
            progress["status"] = "failed"
            return

        dur = time.time() - t0
        per_item_dur = round(dur / max(1, len(rows)), 2)
        quota_hit = False
        for it, row in zip(items, rows):
            it.duration_seconds = per_item_dur
            if row.status == "CHECKPOINT":
                quota_hit = True
                await sessions_engine.mark_session_failed(
                    platform_id, session_item.get("id", ""), "rate_limited",
                    detail=row.notes or "quota exhausted",
                )
            known = job.seed_by_url.get(it.url)
            await self._populate(job, it, row, known)
            job.completed += 1
            progress["completed"] += 1

        for it in items[len(rows):]:
            it.duration_seconds = 0.0
            await self._fail_item(job, it, "youtube quota exhausted mid-batch -- not attempted")
            job.completed += 1
            progress["completed"] += 1

        progress["status"] = "failed" if quota_hit else "done"

    async def _scrape_one(
        self, job: AnalysisJob, it: AnalysisItem, scraper: Any,
        platform_id: str, session_item: dict, stagger: int = 0,
    ) -> bool:
        if stagger:
            await asyncio.sleep(stagger * 1.0)
        it.status = "running"
        it.attempts += 1
        it.started_at_ts = time.time()
        prog = job.platform_progress.get(platform_id, {})
        prog["current_url"] = it.url
        prog["current_step"] = "Extracting profile & screenshots..."
        prog["item_started_at_ts"] = time.time()
        fatal = False
        t0 = time.time()
        try:
            known = job.seed_by_url.get(it.url)
            row = await scraper.one(it.url, job.target_name, job.official_feed, known=known)
            it.duration_seconds = round(time.time() - t0, 2)
            await self._populate(job, it, row, known)
            # SAVED HERE, NOT INSIDE _populate. `_populate` is a pure
            # mapping step -- row in, item fields out -- and is called
            # directly by tests that only care about how a row scores.
            # Persisting from inside it made those tests write to whatever
            # MongoDB the machine happened to be pointing at, which is both
            # a surprise and a real pollution of an operator's data. The
            # job's own lifecycle is what owns durability, so the write
            # belongs at this level.
            await self._settle(job, it, screenshot=row.screenshot_bytes)
        except Exception as e:
            it.duration_seconds = round(time.time() - t0, 2)
            await self._fail_item(job, it, f"{type(e).__name__}: {e}")
            if reason := classify_failure(e):
                await sessions_engine.mark_session_failed(
                    platform_id, session_item.get("id", ""), reason, detail=str(e))
                fatal = True
        finally:
            job.completed += 1
            if platform_id in job.platform_progress:
                job.platform_progress[platform_id]["completed"] += 1
        return fatal

    # -------------------------------------------------------------- mapping

    async def _populate(
        self, job: AnalysisJob, it: AnalysisItem, row: Row, known: Optional[dict] = None,
    ) -> None:
        it.status = "done" if row.status in ("OK", "PARTIAL") else "error"
        if it.status == "error":
            it.error = row.status
        it.analysed_at = datetime.now(timezone.utc).isoformat()
        it.profile_name = row.profile_name or it.entity_id
        it.followers = row.followers
        it.followers_exact = row.followers_exact
        it.location = row.location
        it.bio = row.bio
        it.last_post_date = row.last_post_iso
        it.created_date = row.created_iso
        it.is_active = _tri(row.active_yes)
        it.has_logo = _tri(row.logo_yes)
        it.has_name_match = _tri(row.name_yes)
        it.name_score = row.name_score
        it.profile_image_url = row.profile_pic_url
        it.verified = row.verified
        it.comments = row.notes

        # Belt-and-suspenders: an engine that hasn't been taught to read
        # `known` yet (or one that genuinely couldn't confirm a field on
        # this visit) still shouldn't blank out something discovery already
        # had. Only fills what THIS visit came back with nothing for --
        # never overwrites a real (even if differing) freshly-scraped value.
        if known:
            if known.get("main_keyword"):
                it.main_keyword = known["main_keyword"]
            if not it.profile_name and known.get("display_name"):
                it.profile_name = row.profile_name = known["display_name"]
            if it.followers is None and known.get("followers") is not None:
                it.followers = known["followers"]
            if not it.location and known.get("location"):
                it.location = row.location = known["location"]
            if not it.bio and known.get("bio"):
                it.bio = known["bio"]
            if not it.created_date and known.get("created_at"):
                it.created_date = known["created_at"]
            if not it.profile_image_url and known.get("profile_image_url"):
                it.profile_image_url = known["profile_image_url"]
            if known.get("avatar_sha"):
                it.avatar_sha = known["avatar_sha"]
            if it.verified is None and known.get("verified") is not None:
                it.verified = known["verified"]
            # THE LOGO VERDICT IS DISCOVERY'S, NOT ANALYSIS'S -- the one
            # field here that does not follow the "only fill a gap" rule
            # above, and deliberately so.
            #
            # `known` exists only for a profile that came through
            # POST /discovery/profiles/analyse, which is to say one an
            # analyst looked at on a discovery card and validated by hand.
            # The picture on that card is the thing they judged. Analysis
            # then re-derives the same verdict from a different visit, and
            # when the two disagree it is analysis that is wrong more often:
            # it reads whichever fbcdn URL that page render happened to
            # expose, including the viewer's own photo substituted into a
            # privacy-restricted profile (see PAGE_CONTEXT_PICTURE_KEYS).
            # Discovery's answer is the one a human has already stood
            # behind, so it wins outright rather than only filling a blank.
            if known.get("has_logo") is not None:
                it.has_logo = row.has_custom_pic = known["has_logo"]
            if not it.name_score and known.get("name_score"):
                # Restored for display only -- `Row.name_yes` no longer
                # gates on this (see its own docstring), so `has_name_match`
                # needs no re-derivation here: `_populate` already set it
                # True, unconditionally, before this merge ran.
                it.name_score = row.name_score = known["name_score"]

        if it.profile_image_url and not it.avatar_sha:
            try:
                from backend.services import avatar_cache
                # cache_one returns (sha, fingerprint) -- the fingerprint
                # comes free with the decode discovery already pays for, and
                # is what the logo-match comparison reads later. Analysis's
                # own behaviour is unchanged: it still only needs the sha.
                # (sha, fingerprint, embedding). Analysis asks for no
                # embedding: the logo tier is a DISCOVERY ranking signal, and
                # spending ~70ms an image here would slow analysis for a
                # feature it does not surface.
                sha, fp, _vec, _gen = await avatar_cache.cache_one(it.profile_image_url)
                if sha:
                    it.avatar_sha = sha
                    client_id = job.org_id or (known.get("client_id") if known else "")
                    if client_id:
                        from backend.database.repositories import profile_repository as profiles_db
                        await profiles_db.set_avatar_sha(
                            client_id, it.platform, sha, url=it.url, entity_id=it.entity_id,
                        )
                        if fp:
                            await profiles_db.set_avatar_fingerprint(
                                client_id, it.platform, fp["phash"], fp["dhash"],
                                url=it.url, entity_id=it.entity_id,
                            )
            except Exception:
                pass

        # LAST, and deliberately after the `known` merge above. Risk and
        # priority are DERIVED from name/logo/location/activity, so reading
        # them before those fields were restored scored the row on what this
        # one visit happened to see rather than on everything known about it.
        # Analysis usually carries no target name (neither the analysis form
        # nor "Analyse Validated" sends one), so `row.name_score` is 0 on
        # arrival and `Row.name_yes` reads "No" -- which pinned a confirmed
        # impersonation carrying the brand's logo, name, location and a
        # recent post at 2, the "no name match" FLOOR, when the rubric scores
        # it 9. The restores above are mirrored onto `row` rather than
        # recomputing the rubric here, so scoring.py stays the only place
        # the cascade exists.
        it.risk_score = row.risk
        it.priority = row.priority

        if row.screenshot_bytes:
            async with self._lock:
                self._screenshots[f"{job.id}:{it.id}"] = row.screenshot_bytes
            it.has_screenshot = True

        self._build_rows(job, it)

    async def _fail_item(self, job: AnalysisJob, it: AnalysisItem, error: str) -> None:
        """A URL that could not be read. SAVED LIKE ANY OTHER RESULT, not
        dropped: "we tried this profile and could not reach it" is a finding
        an analyst needs to still be there after a refresh, and losing it
        silently is how the same dead URL gets pasted in again tomorrow."""
        it.status = "error"
        it.error = error
        it.analysed_at = datetime.now(timezone.utc).isoformat()
        it.comments = error
        await self._settle(job, it)

    async def _settle(
        self, job: AnalysisJob, it: AnalysisItem, *, screenshot: Optional[bytes] = None,
    ) -> None:
        """The one point an item becomes final -- build its export rows, then
        persist it for 24 hours.

        BOTH TERMINAL PATHS COME THROUGH HERE, the scored one and the failed
        one, which is what makes "every result is saved" a property of the
        code shape rather than a rule six call sites have to remember.

        `_build_rows` is safe to run twice (it rebuilds both export layouts
        from the item's current fields), so the success path having already
        called it inside `_populate` costs a rebuild and nothing else.

        PERSISTENCE CAN NEVER FAIL THE JOB. A Mongo hiccup must not turn a
        profile that was successfully scraped into an errored item -- the
        reading already exists in memory and is already on its way to the
        analyst's screen through the poll. Same reasoning as
        incident_repository.record(): the bookkeeping is not allowed to be
        the reason the work fails.
        """
        self._build_rows(job, it)
        try:
            await results_db.save(
                it.to_dict(), job_id=job.id, org_id=job.org_id, screenshot=screenshot,
            )
        except Exception as e:
            log.warning(
                f"analysis result for {it.url} could not be saved ({type(e).__name__}: {e}) -- "
                "it is still readable on this job, but will not survive a reload"
            )

    def _build_rows(self, job: AnalysisJob, it: AnalysisItem) -> None:
        """Both export layouts, built from what was ACTUALLY scraped.

        Deliberately different from the version this replaces, which
        hardcoded Logo="Yes", Name="Yes" and priority="High" on every row
        and computed the risk score as if both had matched -- so the Risk
        Score column carried no information and an analyst could not tell a
        real logo match from an assumed one. These come from the Row's own
        resolved values (see shared/models/row.py), which is the point of
        scraping the profile at all. `Original Name`/`Original feed` are
        likewise filled from what the analyst typed instead of left blank.
        """
        platform_name = registry.display_name(it.platform)
        def yes_no(v):
            return "Yes" if v is True else "No" if v is False else ""

        # Incident / takedown-report layout (frontend: incidentExport.ts)
        it.incident_row = {
            # The client's own client_id/domain -- what the analyst typed
            # when creating the client, forwarded through from "Analyse
            # Validated Profiles" (see job.org_id/domain's own comment).
            # A job with no client behind it (pasted URLs) has neither, and
            # falls back to the same generic tag / the platform id this
            # column has always used for a client-less batch.
            "OrgId": job.org_id or "ANALYSIS",
            "Domain": job.domain or it.platform,
            "AssetType": platform_name,
            # The main keyword first: a profile found by the permutation
            # "gautam.adani.hq" must be reported under "Gautam Adani", the
            # parent, which is also the only name the UI's keyword filter
            # offers. Falls back to the handle for a pasted URL, which has
            # no keyword behind it at all.
            "AssetName": it.main_keyword or job.target_name or it.entity_id,
            "Source": it.url,
            "RiskScore": compute_incident_risk_score(
                has_logo=bool(it.has_logo), has_name_match=bool(it.has_name_match),
                followers=it.followers, location=it.location,
                last_post_iso=it.last_post_date, is_active=bool(it.is_active),
            ),
            "ThirdParty YES/NO": "NO",
            "Date (DD-MM-YYYY) (Optional)": to_ddmmyyyy(it.analysed_at),
            "Title": f"Similar {platform_name} Account {it.profile_name} Found",
            "Description": f"Name: {it.profile_name} Url: {it.url}",
            "Active (Yes/No)": yes_no(it.is_active) or "No",
            "Name (Yes/No)": yes_no(it.has_name_match),
            "Logo (Yes/No)": yes_no(it.has_logo),
            "Location": it.location or "",
            "Number of Followers": it.followers if it.followers is not None else "",
            "Last Post (DD-MM-YYYY) (Optional)": to_ddmmyyyy(it.last_post_date),
        }

        # Legacy / raw-analysis layout (frontend: legacyExport.ts). Column
        # names and order are a fixed contract with what consumes the sheet
        # downstream -- a value can be blank, a column cannot go missing.
        it.legacy_row = {
            "Original Name": job.target_name,
            "Original feed": job.official_feed,
            "IMPERSONATED": it.url,
            "Profile name": it.profile_name,
            # Blank on the platforms that genuinely never expose a join date
            # (Facebook, Instagram); real where the payload carries one
            # (Twitter, YouTube, Telegram channels).
            "Created Date": to_ddmmyyyy(it.created_date),
            "Logo (Yes / No)": yes_no(it.has_logo),
            "Followers": it.followers if it.followers is not None else "",
            # Yes/No only, never blank -- see Row.active_yes for the rule.
            "Active (Yes / No)": "Yes" if it.is_active else "No",
            "Name (Yes / No)": yes_no(it.has_name_match),
            "Location": it.location or "",
            "Last Post (DD-MM-YYYY) (Optional)": to_ddmmyyyy(it.last_post_date),
            "Risk Score": it.risk_score,
            "priority": it.priority,
            "Date": to_ddmmyyyy(it.analysed_at),
            # Always blank, never auto-filled from `it.comments` (the
            # engine's own scrape notes / error text, still visible on the
            # item itself and in its error badge) -- this column is the
            # analyst's own free-text field, and pre-seeding it with
            # scraper notes meant real analyst comments were mixed in with
            # (or overwritten by) machine-generated text on every re-export.
            "Comments": "",
        }

    async def stats(self) -> dict:
        job_stats = await self._store.stats()
        async with self._lock:
            shot_bytes = sum(len(v) for v in self._screenshots.values())
            return {
                **job_stats,
                "screenshots": len(self._screenshots),
                "screenshot_mb": round(shot_bytes / 1024 / 1024, 2),
            }


# The shared instance every caller should use.
analysis_runner = AnalysisRunner()
