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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from backend.config.settings import settings
from backend.database.repositories import profile_repository as profiles_db
from backend.platforms import registry
from backend.platforms.scan_options import DiscoveryOptions
from backend.sessions import manager as sessions_engine
from backend.shared.job_store import JobStore
from backend.shared.logging import get_logger
from backend.shared.models.row import Row
from backend.shared.resilience import classify_failure
from backend.shared.text import name_score

log = get_logger("discovery.runner")

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
        "username": row.profile_id,
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
            # separate account, browser context and proxy on a fully
            # separate host (facebook.com, x.com, instagram.com, ...) --
            # there is no shared-session risk running them at once the way
            # there would be two sessions open on the SAME platform
            # simultaneously (that risk is what JobStore's per-
            # (platform, session_id) hold guards against, see
            # _sweep_platform below). WITHIN one platform, its keywords
            # still run one at a time, in the exact order the client
            # configured them -- see _sweep_platform's inner loop.
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
            job.finished_at = datetime.now(timezone.utc).isoformat()
            job.finished_at_ts = time.time()

    async def _sweep_platform(
        self, job: DiscoveryJob, platform_id: str,
        max_results: int, max_seconds: Optional[float],
        platform_limits_individual: dict[str, int], platform_limits_domain: dict[str, int],
        platform_tab_limits: dict[str, dict[str, dict[str, int]]],
    ) -> None:
        prog = job.platforms[platform_id]
        prog.status = "running"
        prog.started_at_ts = time.time()
        plat = registry.get(platform_id)
        tabs = PLATFORM_TABS.get(platform_id, ["people"])
        platform_limits = {"individual": platform_limits_individual, "domain": platform_limits_domain}
        options = DiscoveryOptions(
            concurrency=settings.discovery_concurrency,
            # Placeholder -- every platform's Discoverer.sweep() reads
            # `self.a.max_results` fresh at the top of each call (confirmed
            # across all six engines), never cached at construction, so
            # setting the real per-(tab,keyword-type) cap on THIS SAME
            # options object right before each `discoverer.sweep(...)`
            # call below is enough; no per-cell Discoverer needed. Safe
            # because sweeps for one platform run strictly sequentially
            # (see this method's own loop) -- never two sweep() calls on
            # the same Discoverer concurrently mutating this.
            max_results=max_results,
            max_seconds=max_seconds if max_seconds is not None else settings.discovery_max_seconds,
            headful=not settings.headless,
        )

        session = None
        discoverer = None
        held: Optional[tuple[str, str]] = None
        anon_cm = None
        session_item: Optional[dict] = None
        try:
            plat_obj, session_item = await sessions_engine.session_for_job(platform_id)
            held = self._store.hold_session(platform_id, session_item.get("id", ""))

            if session_item.get("anonymous"):
                anon_cm = plat_obj.anonymous_context()(session_item.get("proxy"))
                ctx = await anon_cm.__aenter__()
                discoverer = plat_obj.discoverer()(options, ctx, anonymous=True)
            elif not plat_obj.session_path:
                # No browser session on this platform (YouTube's API key,
                # Telegram's MTProto). The discoverer owns its own
                # connection and must be closed by us -- see
                # telegram/discovery_engine.py::Discovery.stop().
                discoverer = plat_obj.discoverer()(options, None)
            else:
                session = plat_obj.session_cls()(
                    options, session_item.get("cookies", []),
                    session_id=session_item.get("id", ""),
                    proxy=session_item.get("proxy"),
                )
                session.on_cookies = sessions_engine.cookie_saver(
                    platform_id, session_item.get("id", ""))
                await session.start()
                if not await session.check_session():
                    await sessions_engine.mark_session_failed(
                        platform_id, session_item.get("id", ""), "expired")
                    raise RuntimeError(
                        f"{registry.display_name(platform_id)} session is not usable -- "
                        "check credentials under /sessions")
                await sessions_engine.mark_session_ok(platform_id, session_item.get("id", ""))
                discoverer = plat_obj.discoverer()(options, session.ctx)

            incomplete = 0
            stop_platform = False
            for keyword, kw_type in job.keyword_plan:
                if stop_platform:
                    break
                for tab in tabs:
                    if job.cancel.is_set():
                        break
                    prog.current_keyword = keyword
                    prog.current_tab = tab
                    prog.current_step = f"Searching {tab.upper()} tab..."
                    prog.item_started_at_ts = time.time()
                    t0 = time.time()
                    options.max_results = _resolve_cap(
                        platform_id, tab, kw_type, max_results,
                        platform_limits, platform_tab_limits,
                    )
                    try:
                        sweep = await discoverer.sweep(keyword, tab)
                    except Exception as e:
                        dur = time.time() - t0
                        log.error(f"[{platform_id}] {keyword!r}/{tab}: {type(e).__name__}: {e}")
                        prog.keywords_done += 1
                        job.history.append(CompletedSweep(
                            platform=platform_id,
                            display_name=prog.display_name,
                            keyword=keyword,
                            tab=tab,
                            duration_seconds=dur,
                            hits_found=0,
                            hits_new=0,
                            timestamp=datetime.now(timezone.utc).strftime("%H:%M:%S"),
                        ))
                        if reason := classify_failure(e):
                            await sessions_engine.mark_session_failed(
                                platform_id, session_item.get("id", ""), reason, detail=str(e))
                            prog.note = f"session {reason} mid-sweep"
                            prog.status = "partial"
                            return
                        incomplete += 1
                        continue

                    dur = time.time() - t0
                    hits = [h for h in (sweep.hits or []) if h.url]
                    saved_count = 0
                    new_count = 0
                    if hits:
                        # Saved per completed sweep, not batched at the end,
                        # so a caller polling this job (or reading /profiles)
                        # sees results within seconds of them being found.
                        saved, new = await profiles_db.save_many(
                            job.group_id, platform_id, profiles_db.PHASE_DISCOVERY,
                            [row_to_fields(h, keyword) for h in hits],
                        )
                        saved_count = saved
                        new_count = new
                        prog.found += saved
                        prog.new += new
                        job.found += saved
                        job.new += new
                    if not getattr(sweep, "complete", True):
                        incomplete += 1
                    prog.keywords_done += 1
                    job.history.append(CompletedSweep(
                        platform=platform_id,
                        display_name=prog.display_name,
                        keyword=keyword,
                        tab=tab,
                        duration_seconds=dur,
                        hits_found=saved_count,
                        hits_new=new_count,
                        timestamp=datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    ))

                    stop_reason = ""
                    if getattr(sweep, "stopped", "") == "flood-wait":
                        stop_reason = "flood-wait"
                    else:
                        # A sweep that caught its own session-shaped problem
                        # (TikTok's CAPTCHA/checkpoint, an auth failure a
                        # platform detected mid-page rather than as a raised
                        # exception) reports it via `error`/`stopped`
                        # instead of raising -- the `except Exception`
                        # branch above never sees it, so nothing would ever
                        # tell `sessions/manager.py` this session is bad.
                        session_reason = classify_failure(
                            getattr(sweep, "error", "") or getattr(sweep, "stopped", "")
                        )
                        if session_reason:
                            await sessions_engine.mark_session_failed(
                                platform_id, session_item.get("id", ""), session_reason,
                                detail=getattr(sweep, "error", "") or getattr(sweep, "stopped", ""),
                            )
                            stop_reason = session_reason
                    if stop_reason:
                        # Stop only THIS platform's remaining keywords --
                        # job.cancel is shared across every platform running
                        # concurrently in this job, so setting it would
                        # wrongly cancel the others too. Firing the next
                        # keyword immediately at a session that just told us
                        # it's rate-limited/checkpointed/expired would only
                        # make things worse.
                        log.warning(f"[{platform_id}] {stop_reason} -- stopping remaining keywords this run")
                        prog.note = f"stopped early: session {stop_reason}"
                        stop_platform = True
                        break
                if job.cancel.is_set() or stop_platform:
                    break

            if incomplete:
                prog.status = "partial"
                prog.note = f"{incomplete} sweep(s) did not run to completion"
            else:
                prog.status = "done"
        except Exception as e:
            prog.status = "failed"
            prog.note = f"{type(e).__name__}: {e}"
            log.error(f"discovery job {job.id}: {platform_id} failed -- {prog.note}")
        finally:
            prog.current_keyword = ""
            prog.current_tab = ""
            prog.current_step = ""
            prog.item_started_at_ts = None
            prog.finished_at_ts = time.time()
            self._store.release_session(held)
            # The other half of get_healthy_session's cross-runner claim
            # (see sessions/manager.py) -- `held` only guards this SAME
            # runner's own JobStore-level "in use" tracking, which can't
            # see a job on the OTHER runner (discovery vs analysis) holding
            # the same session; this is the real exclusion that must be
            # released regardless, or the session looks permanently
            # claimed to every future job on either runner.
            if session_item is not None:
                sessions_engine.release_claim(platform_id, session_item.get("id", ""))
            # Telegram holds a lock on its local session file until its
            # discoverer is closed; a missed stop() here is what makes the
            # NEXT Telegram run fail with "database is locked".
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

    async def stats(self) -> dict:
        return await self._store.stats()


discovery_runner = DiscoveryRunner()
