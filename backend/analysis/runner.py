"""The analysis feature, whole: paste URLs, scrape them, read the results.

WHAT THIS IS. Analysis is a standalone tool with one input box and one
Scrape button. It takes profile URLs directly from an analyst, visits each
one, and returns the fields the legacy export expects plus an evidence
screenshot. It does not read a client record, a keyword list, or anything
discovery produced, and it never writes to MongoDB.

    discovery -> MongoDB   (keyword sweeps, candidate profiles, persisted)
    analysis  -> memory    (pasted URLs, scored rows, this module)

Those two are independent passes with no shared state in either direction
(see shared/analysis_store.py for the full split). Analysis exists to read
exactly what a search sweep cannot: follower/member counts, bio, location,
last-post date, and a screenshot -- all of which need a real profile visit.

MEMORY ONLY, AND WHAT THAT COSTS. Every job, scored row and screenshot here
lives in this process and nowhere else. A restart loses all of it; so does
the TTL lapsing, or the store evicting under pressure. Nothing here can be
queried tomorrow, so an analyst who wants to keep a result must export it
(XLSX/CSV) while the job is still live.

ROBUSTNESS. Jobs are bounded and TTL'd (`MAX_JOBS`, `JOB_TTL_SECONDS`) so a
long-running process cannot accumulate them forever. Screenshots -- by far
the largest thing here, hundreds of KB to low MB each -- are held in
`shared/analysis_store.py`, which budgets real bytes and evicts oldest-first
rather than trusting an entry count. One session is taken per platform per
job, not per URL, so a 40-URL paste does not open 40 browser sessions. A URL
that fails is recorded as an errored item and never sinks the rest of the
batch. Cancellation is checked between every profile.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from backend.config.settings import settings
from backend.platforms import registry
from backend.platforms.scan_options import ScanOptions
from backend.sessions import manager as sessions_engine
from backend.shared.analysis_store import analysis_store
from backend.shared.job_store import JobStore
from backend.shared.logging import get_logger
from backend.shared.models.row import Row
from backend.shared.models.scoring import NAME_THRESHOLD, compute_incident_risk_score
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

# How many of one platform's URLs run at once, inside its single held
# session. YouTube (an official API call) and Telegram (one MTProto RPC)
# have no browser fingerprint to protect, so their whole batch runs
# concurrently. The browser-driven platforms -- Facebook, Instagram,
# Twitter, TikTok -- get a conservative step up from strictly serial (1)
# to 2, not higher: see ScanOptions.concurrency's own comment ("faster and
# more conspicuous"). Several requests at once from one session/IP reads
# far more like a bot than one every ~2.5s+jitter, and every engine's loop
# treats a CHECKPOINT/challenge as fatal to the rest of that platform's
# batch (see `_scrape_one` below) -- so pushing concurrency too high risks
# losing the REST of the URLs, not just the speed gained.
_PLATFORM_CONCURRENCY: dict[str, int] = {
    # Official Data API: quota-metered, not ban-risked, and every call is an
    # ordinary HTTPS request. Nothing to protect by going slowly.
    "youtube": 8,
    # Deliberately NOT as high as YouTube despite also being API-shaped.
    # Every one of these rides ONE MTProto client, and Telegram answers
    # bursts of resolve() with FloodWait -- which telegram/analysis_engine.py
    # turns into CHECKPOINT, and CHECKPOINT stops the whole platform's
    # remaining batch (see `_scrape_platform`). So over-driving it does not
    # just slow this platform down, it can cost every URL queued behind the
    # burst. 3 is a modest step up from serial with far less of that risk.
    "telegram": 3,
}
_DEFAULT_CONCURRENCY = 2


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
    verified: Optional[bool] = None
    comments: str = ""
    has_screenshot: bool = False

    incident_row: dict[str, Any] = field(default_factory=dict)
    legacy_row: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "url": self.url, "platform": self.platform,
            "platform_name": registry.display_name(self.platform),
            "entity_id": self.entity_id, "status": self.status,
            "error": self.error, "analysed_at": self.analysed_at,
            "profile_name": self.profile_name, "followers": self.followers,
            "location": self.location, "bio": self.bio,
            "last_post_date": self.last_post_date,
            "is_active": self.is_active, "has_logo": self.has_logo,
            "has_name_match": self.has_name_match, "name_score": self.name_score,
            "risk_score": self.risk_score, "priority": self.priority,
            "profile_image_url": self.profile_image_url, "verified": self.verified,
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
    # url -> whatever discovery already knows about that profile (see
    # POST /discovery/profiles/analyse -> _seed_from_doc). Not part of
    # to_dict()/the frontend contract -- it's an input to scraping, not a
    # result. Empty for a job started from pasted URLs, which have no
    # discovery record behind them.
    seed_by_url: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "status": self.status,
            "target_name": self.target_name, "official_feed": self.official_feed,
            "total": self.total, "completed": self.completed,
            "message": self.message,
            "platform_progress": self.platform_progress,
            "items": [i.to_dict() for i in self.items],
        }


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
                "displayName": registry.display_name(it.platform),
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
        try:
            by_platform: dict[str, list[AnalysisItem]] = {}
            for it in job.items:
                by_platform.setdefault(it.platform, []).append(it)
            # Every platform scraped CONCURRENTLY. Each is a fully separate
            # account, browser context and proxy on a fully separate host --
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
                            self._fail_item(job, it, f"{type(result).__name__}: {result}")
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

    async def _scrape_platform(
        self, job: AnalysisJob, platform_id: str, items: list[AnalysisItem],
    ) -> None:
        progress = job.platform_progress[platform_id]
        progress["status"] = "running"

        concurrency = _PLATFORM_CONCURRENCY.get(platform_id, _DEFAULT_CONCURRENCY)
        options = ScanOptions(
            # No evidence directory: a screenshot must NOT go to GridFS here.
            # `ephemeral_screenshot` is what puts the PNG on the Row as raw
            # bytes instead, which is the whole memory-only contract.
            evidence=None,
            ephemeral_screenshot=True,
            delay=settings.analysis_delay_sec,
            # Not read by the chunk loop below (that's driven by the same
            # `concurrency` local directly) -- set here only so a platform
            # engine that inspects `self.a.concurrency` (e.g. facebook/
            # analysis_engine.py's own unused run()/run_parallel() split)
            # sees the real value rather than a stale 1.
            concurrency=concurrency,
            headful=not settings.headless,
        )

        scraper = None
        held: Optional[tuple[str, str]] = None
        try:
            plat, session_item = await sessions_engine.session_for_job(platform_id)
            held = self._store.hold_session(platform_id, session_item.get("id", ""))
            if session_item.get("anonymous"):
                scraper = plat.scraper()(
                    options, [], proxy=session_item.get("proxy"), anonymous=True,
                )
            else:
                scraper = plat.scraper()(
                    options, session_item.get("cookies", []),
                    session_id=session_item.get("id", ""), proxy=session_item.get("proxy"),
                )
            inner = getattr(scraper, "session", None)
            if inner is not None:
                inner.on_cookies = sessions_engine.cookie_saver(
                    platform_id, session_item.get("id", ""))
            await scraper.start()

            if not await scraper.check_session():
                await sessions_engine.mark_session_failed(
                    platform_id, session_item.get("id", ""), "expired")
                raise RuntimeError(
                    f"{registry.display_name(platform_id)} session is not usable -- "
                    "check credentials under Sessions")
            await sessions_engine.mark_session_ok(platform_id, session_item.get("id", ""))

            i = 0
            while i < len(items):
                if job.cancel.is_set():
                    break
                chunk = items[i:i + concurrency]
                i += len(chunk)
                # Every item in the chunk visits its own page/makes its own
                # call concurrently, on the ONE session/browser context this
                # platform is holding for the whole run -- a context is
                # built to carry several pages at once, so this isn't
                # opening extra sessions, just overlapping requests on the
                # one already held. `_scrape_one` never raises (see its own
                # try/except), so no return_exceptions needed here.
                fatal = await asyncio.gather(
                    *(self._scrape_one(job, it, scraper, platform_id, session_item, stagger=idx)
                      for idx, it in enumerate(chunk))
                )
                if any(fatal):
                    # the session itself died -- stop this platform rather
                    # than burning every remaining URL against it
                    break
                if i < len(items):
                    try:
                        await scraper.pause()
                    except Exception:
                        pass

            progress["status"] = "done"
        except Exception as e:
            progress["status"] = "failed"
            detail = f"{type(e).__name__}: {e}"
            log.error(f"analysis job {job.id}: {platform_id} failed -- {detail}")
            # every URL for this platform is unread; say so on each item
            # rather than leaving them stuck at "pending" with no reason
            for it in items:
                if it.status in ("pending", "running"):
                    self._fail_item(job, it, detail)
                    job.completed += 1
                    progress["completed"] += 1
        finally:
            # Released here, not at the end of the happy path: a crash or a
            # cancel must never leave a session showing as busy forever in
            # the Sessions panel.
            self._store.release_session(held)
            if scraper is not None:
                try:
                    await scraper.stop()
                except Exception:
                    pass

    async def _scrape_one(
        self, job: AnalysisJob, it: AnalysisItem, scraper: Any,
        platform_id: str, session_item: dict, stagger: int = 0,
    ) -> bool:
        """One profile, run as part of a `_scrape_platform` chunk. Never
        raises -- every failure is recorded on `it` and swallowed, the same
        contract the old serial loop had, so `asyncio.gather` over a chunk
        never needs `return_exceptions`. Returns True when the failure was
        session-fatal (session marked failed): the caller stops starting
        further chunks when ANY item in a chunk comes back True, mirroring
        the old loop's `break` on the same condition.

        `stagger` (this item's position within its chunk) delays the start
        by that many seconds, so a chunk of 2-8 concurrent visits doesn't
        all hit the platform in the exact same instant -- the same reason
        facebook/analysis_engine.py's (currently unused) `run_parallel`
        already staggers its own tabs."""
        if stagger:
            await asyncio.sleep(stagger * 1.0)
        it.status = "running"
        fatal = False
        try:
            known = job.seed_by_url.get(it.url)
            row = await scraper.one(it.url, job.target_name, job.official_feed, known=known)
            await self._populate(job, it, row, known)
        except Exception as e:
            self._fail_item(job, it, f"{type(e).__name__}: {e}")
            if reason := classify_failure(e):
                await sessions_engine.mark_session_failed(
                    platform_id, session_item.get("id", ""), reason, detail=str(e))
                fatal = True
        finally:
            job.completed += 1
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
        it.risk_score = row.risk
        it.priority = row.priority

        # Belt-and-suspenders: an engine that hasn't been taught to read
        # `known` yet (or one that genuinely couldn't confirm a field on
        # this visit) still shouldn't blank out something discovery already
        # had. Only fills what THIS visit came back with nothing for --
        # never overwrites a real (even if differing) freshly-scraped value.
        if known:
            if known.get("main_keyword"):
                it.main_keyword = known["main_keyword"]
            if not it.profile_name and known.get("display_name"):
                it.profile_name = known["display_name"]
            if it.followers is None and known.get("followers") is not None:
                it.followers = known["followers"]
            if not it.location and known.get("location"):
                it.location = known["location"]
            if not it.bio and known.get("bio"):
                it.bio = known["bio"]
            if not it.created_date and known.get("created_at"):
                it.created_date = known["created_at"]
            if not it.profile_image_url and known.get("profile_image_url"):
                it.profile_image_url = known["profile_image_url"]
            if it.verified is None and known.get("verified") is not None:
                it.verified = known["verified"]
            if it.has_logo is None and known.get("has_logo") is not None:
                it.has_logo = known["has_logo"]
            if not it.name_score and known.get("name_score"):
                it.name_score = known["name_score"]
                # has_name_match was derived from THIS visit's score (see
                # Row.name_yes), which is 0 whenever the job carries no
                # target name to score against -- the usual case, since
                # neither the analysis form nor "Analyse Validated" sends
                # one any more. Restoring only the score would leave the
                # row self-contradictory: a real name_score sitting next to
                # "Name (Yes/No) = No", which is what _build_rows exports
                # and what compute_incident_risk_score reads. Re-derive the
                # verdict from the score actually being used.
                it.has_name_match = it.name_score >= NAME_THRESHOLD

        if row.screenshot_bytes:
            async with self._lock:
                self._screenshots[f"{job.id}:{it.id}"] = row.screenshot_bytes
            it.has_screenshot = True

        # The scored row itself lands in the shared memory-only store, which
        # is what budgets the screenshot bytes and ages results out.
        await analysis_store.put("__analysis__", it.platform, row)

        self._build_rows(job, it)

    def _fail_item(self, job: AnalysisJob, it: AnalysisItem, error: str) -> None:
        it.status = "error"
        it.error = error
        it.analysed_at = datetime.now(timezone.utc).isoformat()
        it.comments = error
        self._build_rows(job, it)

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
        yes_no = lambda v: "Yes" if v is True else "No" if v is False else ""

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
            "Comments": it.comments or "",
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
