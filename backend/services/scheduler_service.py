"""The Scheduler engine: fire at the set time, sweep the queue one by one.

This is the run loop that used to live in the browser tab
(`frontend/src/services/scheduleRunner.ts`). It was moved here for one
reason: a scheduled run has to happen at 02:00 whether or not anybody left
a tab open, and a loop made of `setTimeout` in a page cannot promise that.
Closing the tab, closing the browser, or simply locking the laptop ended
it, and ended it SILENTLY -- the analyst came back to a queue that looked
exactly like one that had never been told to run.

ONE OWNER, ONE LOOP. The manual "Run queue" button and the scheduled fire
are the same code path here, differing only in a `trigger` label. That is
deliberate twice over:

  * Two loops -- one in the page, one here -- could run at the same time,
    and two concurrent sweeps of different clients means both competing for
    the same platform sessions. Serialising clients is the entire reason
    this queue exists; a second runner would quietly undo it.
  * A scheduled run happens at 02:00 with nobody watching. If it were its
    own code path it would be the least-exercised path in the tool and the
    first to rot. Sharing it with the button an analyst presses several
    times a day means the 02:00 path is tested every working afternoon.

WHAT IS GUARANTEED, AND WHAT IS NOT

  Guaranteed: while this process is up, a due schedule fires. A fire that
  is late because the process was down gets honoured if it is late by less
  than the grace window (`_CATCH_UP_GRACE`), and RECORDED AS MISSED if it
  is later than that. A run interrupted by a restart is closed out as
  interrupted rather than left looking live. Every ending names itself.

  Not guaranteed: anything while this process is not running. Nothing in a
  single-process tool can be. What IS promised is that the tool never
  pretends otherwise -- there is no state in which a run silently did not
  happen and nothing says so.

SEQUENCING IS STRICT. One client's discovery job at a time, top to bottom,
with a real gap between them (`_CLIENT_GAP_S`). Every client in the queue
is swept through the same pooled platform accounts, so back-to-back sweeps
on one account is the single pattern that reads most clearly as automation.
The per-KEYWORD gap is the other half of this and lives in
`backend/discovery/runner.py` (`_PLATFORM_INTER_KEYWORD_DELAY`).
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.database.repositories import client_repository as clients_db
from backend.database.repositories import coverage_repository as coverage_db
from backend.database.repositories import schedule_repository as schedule_db
from backend.discovery.runner import discovery_runner
from backend.services import schedule_math
from backend.services.schedule_math import Schedule
from backend.shared.logging import get_logger

log = get_logger("services.scheduler")

# How often the loop asks "is anything due yet?". Twenty seconds is far
# finer than any schedule an analyst can set (the UI's finest grain is one
# minute) and costs one tiny read of one document.
_TICK_S = 20.0

# Breathing room between clients. Inherited from the browser runner, and
# the reasoning is unchanged: every client in the queue is swept through
# the SAME pooled accounts, so a four-client queue without this is four
# sweeps of one Facebook login inside a couple of minutes. A minute is
# invisible next to a sweep that already takes minutes.
_CLIENT_GAP_S = 60.0

# How late a fire can be and still be honoured. See
# `schedule_math.catch_up_decision` for the reasoning.
_CATCH_UP_GRACE = timedelta(hours=6)

# How long one client may hold the queue before it is cut loose.
#
# THE QUEUE MUST ALWAYS MAKE PROGRESS. A discovery job carries its own time
# budget, but a budget is enforced by the sweep's own checkpoints and a
# sweep wedged on something that never returns reaches no checkpoint. One
# stuck client would then hold every client behind it for ever, and the
# 02:00 sweep of nine other clients would simply never happen -- with the
# run still showing as healthily "running" the whole time. This is the
# backstop that turns that into one failed client and nine swept ones.
_CLIENT_CEILING_S = 4 * 60 * 60

# How often the run loop looks at the job it is driving.
_POLL_S = 2.0

# How often mere PROGRESS is written through to Mongo. Transitions that
# matter (a client settling, a run ending) are written immediately
# regardless -- see `_flush`.
_FLUSH_EVERY_S = 30.0

# `owed` when the coverage ledger could not be read. DELIBERATELY NOT 0:
# zero is the clean bill of health, and a failed read must never be able
# to impersonate one.
OWED_UNKNOWN = -1


def _start_jitter_s(trigger: str) -> float:
    """Random delay before a scheduled run starts; 0 for a manual one."""
    if trigger not in ("scheduled", "catch_up"):
        return 0.0
    try:
        from backend.config.settings import settings
        minutes = float(getattr(settings, "scheduler_start_jitter_minutes", 0) or 0)
    except Exception:                                  # noqa: BLE001
        minutes = 0.0
    return random.uniform(0.0, minutes * 60.0) if minutes > 0 else 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dedupe(keywords: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for kw in keywords or []:
        key = (kw or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(kw.strip())
    return out


def _swept(entry: dict) -> bool:
    """Did this entry actually put a search in front of a platform?

    THE GAP IS FOR ACCOUNTS, NOT FOR CLIENTS. `_CLIENT_GAP_S` exists to
    space out real activity on the pooled logins, so it is owed only by an
    entry that used one. A client skipped before its sweep began -- no
    keywords, a scope that excludes everything, deleted from the directory,
    no platform with a usable session -- touched nothing, and making the
    queue wait a minute for it buys no safety at all.

    It costs real time: a nightly queue of ten clients where eight are
    skipped for want of a session used to sit idle for eight minutes doing
    nothing, in the middle of the night, while the two that could be swept
    waited their turn.

    RECORDED, NOT INFERRED. The obvious proxy -- "does it have a job id?"
    -- is wrong, and wrong in the case that matters most: a job is created
    even when every one of its platforms turns out to have no usable
    session, so an entry that swept precisely nothing still carries an id.
    `swept` is set at the one point where a real platform sweep is known to
    be in flight, and nowhere else.
    """
    return bool(entry.get("swept"))


def _blank_entry(client_id: str, name: str) -> dict:
    return {
        "client_id": client_id,
        "name": name or client_id,
        "status": "pending",
        "job_id": "",
        "message": "",
        "found": 0,
        "new_profiles": 0,
        # Per-platform outcome, keyed by platform id, straight off the
        # job. The aggregate status cannot express the case that matters
        # most -- Instagram and X finished but Facebook lost its session
        # halfway -- which is neither a success nor a failure.
        "platforms": {},
        # Per-platform scraped counts and the note explaining a failure.
        "platform_details": {},
        # What this client still owes on the durable coverage ledger once
        # its sweep settled. See OWED_UNKNOWN.
        "owed": OWED_UNKNOWN,
        # True once at least one platform sweep was genuinely put in
        # flight for this client. See `_swept`.
        "swept": False,
        "closing_gaps": False,
        "started_at": None,
        "finished_at": None,
    }


class SchedulerEngine:
    """One instance, module-level (`scheduler_engine` at the bottom).

    Holds the authoritative state of the run in flight in memory, and
    flushes it to Mongo on every transition so a restart leaves a record
    rather than a mystery.
    """

    def __init__(self) -> None:
        self._tick_task: Optional[asyncio.Task] = None
        self._run_task: Optional[asyncio.Task] = None
        # Held for the whole of a run. The one thing that makes "never two
        # sweeps at once" true rather than merely intended: a manual press
        # landing while a scheduled run is going cannot get past this.
        self._lock = asyncio.Lock()
        self._run: Optional[dict] = None        # the live run document
        self._stopping = False
        self._current_job_id = ""
        self._cancel_sent_for = ""
        self._last_flush = 0.0
        # CLAIMED THE MOMENT A FIRE IS ACCEPTED, before any await.
        #
        # The lock alone cannot do this job. It is taken inside the run
        # TASK, which does not start until the current coroutine yields --
        # so between `fire()` checking the lock and that task acquiring it
        # there are several awaits, and a second fire arriving in that
        # window sees an unlocked lock and is accepted too. The two then
        # serialise on the lock rather than overlapping, which is not a
        # session collision but IS a second full sweep of every client that
        # nobody asked for, starting whenever the first one ends.
        #
        # Set and read with no await in between, so there is no window.
        self._claimed = False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the tick loop. Idempotent -- safe to call on every
        storage-recovery attempt in `main.py`'s lifespan."""
        if self._tick_task and not self._tick_task.done():
            return
        self._tick_task = asyncio.create_task(self._tick_loop())
        log.info(
            f"scheduler: tick loop started (checking every {_TICK_S:.0f}s, "
            f"catch-up grace {schedule_math.humanize(_CATCH_UP_GRACE)})")

    def stop_monitor(self) -> None:
        """Stop ticking. Does NOT abort a run in flight -- that is `stop()`,
        which is an analyst's decision, not a shutdown's."""
        if self._tick_task:
            self._tick_task.cancel()
            self._tick_task = None

    @property
    def busy(self) -> bool:
        """Is a run happening, or about to? Both, deliberately -- see
        `_claimed`. A caller asking "can I start one?" must get `True` for
        a run that has been accepted but has not reached its lock yet."""
        return self._claimed or self._lock.locked()

    # ----------------------------------------------------------- the ticker

    async def _tick_loop(self) -> None:
        """Ask every `_TICK_S` whether the schedule is due.

        NEVER DIES OF ONE BAD TICK. A tick that raises -- Mongo blinked, a
        stored schedule is malformed -- logs and comes back on the next
        one. A scheduler that stops ticking because of a transient error is
        indistinguishable, from the outside, from one that is working fine
        and simply has nothing to do.
        """
        # A run left `running` by a process that died is closed out before
        # the first tick, so the first thing an analyst sees after a
        # restart is the truth about last night.
        try:
            await schedule_db.reconcile_interrupted()
        except Exception as e:                              # noqa: BLE001
            log.error(f"scheduler: could not reconcile interrupted runs: "
                      f"{type(e).__name__}: {e}")

        # First pass decides about a firing time that went by while this
        # process was down. Done once, before the steady loop, because the
        # steady loop's job is "is it due NOW" and this one's is "was it
        # due while we were away".
        try:
            await self._catch_up_on_start()
        except Exception as e:                              # noqa: BLE001
            log.error(f"scheduler: catch-up check failed: {type(e).__name__}: {e}")

        while True:
            try:
                await asyncio.sleep(_TICK_S)
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:                          # noqa: BLE001
                log.error(f"scheduler: tick failed, continuing: "
                          f"{type(e).__name__}: {e}")

    async def _tick(self) -> None:
        state = await schedule_db.get_state()
        schedule = Schedule.from_dict(state.get("schedule"))
        if not schedule.enabled:
            return

        due_at = state.get("next_run_at")
        if due_at is None:
            # Enabled with no next run stored: either it was never
            # computed, or a one-time schedule has now passed. Recompute so
            # a daily/weekly schedule cannot be left with a dead pointer.
            nxt, _ = schedule_math.next_run_after(schedule, _now())
            if nxt is not None:
                await schedule_db.save_next_run(nxt)
            return

        if _now() < due_at:
            return

        decision, lateness = schedule_math.catch_up_decision(
            due_at, _now(), _CATCH_UP_GRACE)
        if decision == "not_due":
            return

        # ROLL THE POINTER FORWARD FIRST, ALWAYS. Whatever happens to this
        # run -- it succeeds, it fails, the process dies inside it -- the
        # schedule must not still be pointing at a time in the past, or
        # every subsequent tick fires the same sweep again.
        nxt, shift = schedule_math.next_run_after(schedule, _now())
        await schedule_db.mark_fired(_now(), nxt)
        if shift:
            log.info(f"scheduler: {shift.describe()}")

        if decision == "missed":
            reason = (
                f"this run was due {schedule_math.humanize(lateness)} ago and the "
                f"backend was not running then. That is more than the "
                f"{schedule_math.humanize(_CATCH_UP_GRACE)} catch-up window, so it "
                f"was NOT run. Press Run now to sweep immediately, or wait for the "
                f"next scheduled time.")
            log.warning(f"scheduler: missed run -- {reason}")
            await schedule_db.record_missed(
                due_at=due_at, late_seconds=lateness.total_seconds(), reason=reason)
            return

        trigger = "catch_up" if lateness > timedelta(seconds=90) else "scheduled"
        if trigger == "catch_up":
            log.warning(
                f"scheduler: firing a run that was due "
                f"{schedule_math.humanize(lateness)} ago (within the "
                f"{schedule_math.humanize(_CATCH_UP_GRACE)} catch-up window)")
        await self.fire(trigger=trigger, due_at=due_at,
                        late_seconds=lateness.total_seconds())

    async def _catch_up_on_start(self) -> None:
        """Decide about a firing time that passed while this process was
        down. Same rules as a tick -- this exists only so the decision is
        made immediately at startup rather than up to `_TICK_S` later."""
        await self._tick()

    # --------------------------------------------------------------- firing

    async def fire(
        self, *, trigger: str, due_at: Optional[datetime] = None,
        late_seconds: float = 0.0,
    ) -> Optional[str]:
        """Start a run over the queue. Returns the run id, or None when one
        was already in flight.

        REFUSES TO OVERLAP. A manual press landing during a scheduled run
        -- or a second scheduled fire somehow arriving during the first --
        is declined rather than queued behind it. Queueing it would mean an
        analyst pressing Run at 02:01 gets a second full sweep of every
        client at 03:30 that nobody asked for.
        """
        if self.busy:
            log.info(f"scheduler: {trigger} fire declined -- a run is already going")
            return None
        # Claimed HERE, synchronously, before the first await below. Every
        # `return None` and every failure path past this point has to give
        # it back, which is what the try/except around the rest of this
        # method is for.
        self._claimed = True

        try:
            state = await schedule_db.get_state()
            queue: list[str] = list(state.get("queue") or [])
            names: dict[str, str] = dict(state.get("queue_names") or {})

            run_id = schedule_db.new_run_id()
            entries = [_blank_entry(cid, names.get(cid, "")) for cid in queue]

            if not entries:
                # An empty queue is a real outcome and gets a real record.
                # The alternative -- doing nothing quietly -- is how an
                # analyst finds out in a week that the nightly sweep has
                # been running over an empty queue since somebody cleared it.
                msg = ("the schedule fired but the run queue was empty -- nothing to "
                       "sweep. Add clients to the queue on the Scheduler page.")
                log.warning(f"scheduler: {msg}")
                await schedule_db.create_run(
                    run_id=run_id, trigger=trigger, entries=[], due_at=due_at,
                    late_seconds=late_seconds, status=schedule_db.RUN_DONE, message=msg)
                self._claimed = False       # nothing was started
                return run_id

            await schedule_db.create_run(
                run_id=run_id, trigger=trigger, entries=entries, due_at=due_at,
                late_seconds=late_seconds, message=f"{len(entries)} client(s) queued")
            await schedule_db.set_active_run(run_id)
        except Exception:
            # The claim must not outlive the attempt, or a single failed
            # fire wedges the Scheduler shut until the process restarts --
            # every later run, scheduled and manual, declined for ever.
            self._claimed = False
            raise

        # From here the run task owns the claim and releases it in its
        # own `finally`.
        self._run_task = asyncio.create_task(self._run_queue(run_id, entries, trigger))
        return run_id

    async def stop(self) -> bool:
        """Cancel whatever is in flight and unwind the run.

        Immediate rather than "finishes this client first": a Stop that
        takes another eleven minutes is not a Stop.
        """
        if not self._lock.locked():
            return False
        self._stopping = True
        if self._run:
            self._run["stopping"] = True
            await schedule_db.save_run(self._run["_id"], {"stopping": True})
        if self._current_job_id:
            await self._cancel_now(self._current_job_id)
        return True

    # ------------------------------------------------------------ the queue

    async def _run_queue(self, run_id: str, entries: list[dict], trigger: str) -> None:
        async with self._lock:
            self._stopping = False
            self._current_job_id = ""
            self._cancel_sent_for = ""
            self._last_flush = 0.0
            self._run = {"_id": run_id, "entries": entries, "current_id": "",
                         "stopping": False, "trigger": trigger}
            started = time.time()
            # Seeded so that every exit below -- including one this code
            # never anticipated -- closes the run out as SOMETHING. A run
            # left at `running` with no process behind it is the one state
            # that lies to the next person who looks.
            status = schedule_db.RUN_FAILED
            message = "the run ended unexpectedly"
            log.info(f"scheduler: run {run_id} ({trigger}) starting over "
                     f"{len(entries)} client(s)")
            try:
                # A SCHEDULED start wanders by a few minutes; a person pressing
                # Run is never made to wait. See scheduler_start_jitter_minutes.
                jitter_s = _start_jitter_s(trigger)
                if jitter_s:
                    log.info(f"scheduler: run {run_id} starts in {jitter_s / 60:.1f} min "
                             "(randomised so scheduled runs do not begin on the same minute)")
                    await self._sleep_interruptible(jitter_s)
                for entry in entries:
                    if self._stopping:
                        break
                    await self._run_entry(entry)
                    await self._set_current("")
                    if self._stopping:
                        break
                    # Only between clients, never after the last one --
                    # and only after one that actually SWEPT.
                    if entry is not entries[-1] and _swept(entry):
                        # Varied, not a fixed minute: identical gaps between
                        # identical bursts of activity are a rhythm.
                        await self._sleep_interruptible(
                            _CLIENT_GAP_S * random.uniform(0.7, 1.6))

                if not self._stopping:
                    await self._close_gaps(entries)

                status, message = self._summarise(entries, started)
            except asyncio.CancelledError:
                status, message = (schedule_db.RUN_CANCELLED,
                                   "the run was cancelled")
                raise
            except Exception as e:                          # noqa: BLE001
                log.error(f"scheduler: run {run_id} raised: {type(e).__name__}: {e}")
                status = schedule_db.RUN_FAILED
                message = f"the run stopped unexpectedly: {type(e).__name__}: {e}"
            finally:
                # Whatever happened above, this run stops claiming to be
                # live -- in the database as well as in memory. The final
                # entry state is flushed first, unthrottled, so the record
                # matches what actually happened rather than whatever the
                # last throttled tick managed to save.
                try:
                    await self._flush(force=True)
                    await schedule_db.finish_run(run_id, status=status, message=message)
                    await schedule_db.set_active_run("")
                    await schedule_db.trim_history()
                except Exception as e:                      # noqa: BLE001
                    log.error(f"scheduler: could not close out run {run_id}: "
                              f"{type(e).__name__}: {e}")
                self._run = None
                self._stopping = False
                self._current_job_id = ""
                self._claimed = False
                log.info(f"scheduler: run {run_id} finished -- {message}")

    def _summarise(self, entries: list[dict], started: float) -> tuple[str, str]:
        """One line saying how the run went, and the status to file it under.

        COUNTS THE UNCOMFORTABLE THINGS OUT LOUD. `done` on its own reads
        as a clean night even when three clients were skipped for want of a
        session and two still owe searches, which is the most common real
        outcome and the one worth acting on.
        """
        done = sum(1 for e in entries if e["status"] == "done")
        failed = sum(1 for e in entries if e["status"] == "failed")
        skipped = sum(1 for e in entries if e["status"] == "skipped")
        owing = sum(1 for e in entries if e.get("owed", OWED_UNKNOWN) > 0)
        unknown = sum(1 for e in entries if e.get("owed", OWED_UNKNOWN) == OWED_UNKNOWN
                      and e["status"] in ("done", "failed"))
        mins = (time.time() - started) / 60.0

        bits = [f"{done} swept"]
        if failed:
            bits.append(f"{failed} failed")
        if skipped:
            bits.append(f"{skipped} skipped")
        if owing:
            bits.append(f"{owing} still owing searches after the gap-closing lap")
        if unknown:
            bits.append(f"{unknown} whose coverage could not be read")
        summary = f"{', '.join(bits)} in {mins:.0f}m"

        if self._stopping:
            return schedule_db.RUN_CANCELLED, f"stopped by an analyst -- {summary}"
        # A run is only `failed` when NOTHING got through. One failed
        # client out of ten is a normal night, not a failed run, and
        # calling it one trains an analyst to ignore the word.
        if failed and not done:
            return schedule_db.RUN_FAILED, summary
        return schedule_db.RUN_DONE, summary

    # ------------------------------------------------------------ one client

    async def _run_entry(self, entry: dict, *, only_owed: bool = False) -> None:
        """Sweep one client, start to finish.

        A direct port of the browser runner's `runEntry`, including every
        one of its refusals. Each of those exists because the alternative
        was a row that said `done, 0 found` for a sweep that never
        happened -- which is indistinguishable, in a list, from a real
        sweep that genuinely found nothing.
        """
        client_id = entry["client_id"]
        client = await self._live_client(client_id)
        if client:
            entry["name"] = client.get("name") or entry["name"] or client_id

        individual = _dedupe(list((client or {}).get("name_keywords") or []))
        domain = _dedupe(list((client or {}).get("domain_keywords") or []))

        if client is None:
            await self._settle(entry, "skipped",
                               "this client is no longer in the directory -- it was "
                               "deleted after being queued")
            return
        if not individual and not domain:
            await self._settle(entry, "skipped",
                               "no keywords configured -- add some on the Clients page")
            return

        # A keyword scope that excludes everything this client actually has
        # would start an empty sweep, which settles as `done, 0 found`. It
        # is a configuration mistake, and it says so.
        scope = (client.get("scheduler_keyword_scope") or "").strip()
        if scope == "individual" and not individual:
            await self._settle(entry, "skipped",
                               "set to individual keywords only, but this client has none")
            return
        if scope == "domain" and not domain:
            await self._settle(entry, "skipped",
                               "set to domain keywords only, but this client has none")
            return

        entry["status"] = "running"
        entry["message"] = "closing missed searches…" if only_owed else "starting sweep…"
        entry["started_at"] = _now()
        entry["finished_at"] = None
        entry["closing_gaps"] = only_owed
        await self._set_current(client_id)
        await self._flush(force=True)

        wanted = list(client.get("scheduler_platforms") or [])
        fb_tabs = list(client.get("scheduler_facebook_tabs") or [])
        budget_minutes = int(client.get("scheduler_budget_minutes") or 0)

        try:
            job, skipped = await discovery_runner.start(
                group_id=client_id,
                # Narrowed by sending an EMPTY list for the excluded type
                # rather than filtering afterwards, so the per-type caps
                # only ever apply to keywords genuinely part of this sweep.
                individual_keywords=[] if scope == "domain" else individual,
                domain_keywords=[] if scope == "individual" else domain,
                # An empty `scheduler_platforms` means every ready platform,
                # which is exactly what `None` means to the runner: one
                # spelling of "all", so a never-configured client and a
                # deliberately-all one behave identically.
                platforms=wanted or None,
                platform_limits_individual=client.get("platform_limits_individual") or {},
                platform_limits_domain=client.get("platform_limits_domain") or {},
                platform_tab_limits=client.get("platform_tab_limits") or {},
                facebook_tabs=fb_tabs or None,
                max_seconds=budget_minutes * 60 if budget_minutes > 0 else None,
                only_owed=only_owed,
            )
        except Exception as e:                              # noqa: BLE001
            await self._settle(entry, "failed", f"could not start: {type(e).__name__}: {e}")
            return

        entry["job_id"] = job.id
        self._current_job_id = job.id
        entry["message"] = "sweeping…"
        await self._flush(force=True)

        queued = [p for p, s in job.platforms.items() if s.status != "skipped"]
        if not queued:
            # Accepted with nothing to sweep: every platform was skipped
            # for want of a usable session. The job settles as `done` a
            # moment later, so watching it would report a clean sweep that
            # found nothing. It is a skip, and it now says so, with reasons.
            why = ", ".join(f"{k}: {v}" for k, v in skipped.items())
            entry["platforms"] = {p: "skipped" for p in skipped}
            await self._settle(
                entry, "skipped",
                f"nothing swept -- {why}" if why
                else "nothing swept -- no platform has a usable session")
            return

        # Past every refusal: at least one platform is genuinely being
        # searched for this client, on a real pooled account. This is the
        # one place that is true, and the only thing `_swept` reads.
        entry["swept"] = True

        # A Stop pressed while `start` was still running has no job id to
        # cancel; this is where that cancel finally reaches the engine.
        if self._stopping:
            await self._cancel_now(job.id)

        await self._watch(entry, job)

    async def _watch(self, entry: dict, job: Any) -> None:
        """Follow one job to its end, mirroring it into the entry.

        In-process, so there is no network between here and the job -- but
        there IS a ceiling. See `_CLIENT_CEILING_S`.
        """
        deadline = time.time() + _CLIENT_CEILING_S
        while True:
            await asyncio.sleep(_POLL_S)
            if self._stopping:
                await self._cancel_now(job.id)

            if job.status in ("done", "failed", "cancelled"):
                # ASKED AFTER THE JOB SETTLES, ALWAYS -- including after a
                # failure, which is precisely when the answer matters most.
                # The job's own report cannot say which keywords went
                # unsearched; the ledger can, and it outlives the job.
                owed = await self._owed_for(entry["client_id"])
                if job.status == "done":
                    status = "done"
                elif job.status == "cancelled":
                    # A cancel WE asked for is a stop, and the entry stays
                    # re-runnable. A cancel we did NOT ask for -- another
                    # tab, a direct API call -- gets its own terminal
                    # status so nothing picks it straight back up.
                    status = "stopped" if self._stopping else "cancelled"
                else:
                    status = "failed"
                entry["found"] = job.found
                entry["new_profiles"] = job.new
                entry["platforms"] = self._platform_outcomes(job)
                entry["platform_details"] = self._platform_details(job)
                entry["owed"] = owed
                await self._settle(entry, status, job.message or job.status)
                return

            if time.time() > deadline:
                # See `_CLIENT_CEILING_S`. Cut it loose so the rest of the
                # queue still gets swept tonight, and say exactly why.
                await self._cancel_now(job.id)
                entry["platforms"] = self._platform_outcomes(job)
                entry["platform_details"] = self._platform_details(job)
                entry["owed"] = await self._owed_for(entry["client_id"])
                await self._settle(
                    entry, "failed",
                    f"gave up after {_CLIENT_CEILING_S / 3600:.0f}h -- this sweep was "
                    f"not finishing and the rest of the queue was waiting behind it. "
                    f"Anything it found before now was saved.")
                return

            entry["found"] = job.found
            entry["new_profiles"] = job.new
            entry["platforms"] = self._platform_outcomes(job)
            entry["platform_details"] = self._platform_details(job)
            entry["message"] = job.message or f"{job.completed}/{job.total} sweeps"
            await self._flush()

    # -------------------------------------------------------- gap-closing lap

    async def _close_gaps(self, entries: list[dict]) -> None:
        """ONE EXTRA LAP, OVER THE GAPS ONLY.

        The requirement the Scheduler exists to meet is that every keyword
        saved under every queued client actually gets searched on every
        platform in scope. A single pass cannot guarantee that and never
        could: a pool can run dry halfway down a ten-client queue, a
        platform can checkpoint, an account can be cooling off when a
        client's turn comes and be free again twenty minutes later. Those
        are ordinary operating conditions, not bugs to be eliminated -- so
        the answer is to come back for what was missed rather than to
        pretend it did not happen.

        Cheap because it is scoped: `only_owed` narrows each platform to
        the exact (tab, keyword) cells its ledger still lists as owed, so a
        client that missed two keywords costs two searches, not a re-sweep.

        EXACTLY ONE LAP, NOT A LOOP. A keyword failing for a structural
        reason (the account is genuinely dead, the platform is geoblocked
        for this IP) would otherwise be retried for ever, burning the same
        session on the same doomed search -- both the worst thing to do to
        an account pool and the surest way to turn a small problem into a
        ban. One lap closes the transient gaps; whatever still owes after
        it is a real problem, and stays visible as owed work for a human.
        """
        owing = [e for e in entries if e.get("owed", OWED_UNKNOWN) > 0]
        if not owing:
            return
        log.info(f"scheduler: gap-closing lap over {len(owing)} client(s)")
        for entry in owing:
            if self._stopping:
                break
            entry["status"] = "pending"
            entry["job_id"] = ""
            entry["message"] = f"closing {entry['owed']} missed search(es)…"
            await self._flush()
            await self._run_entry(entry, only_owed=True)
            await self._set_current("")
            if self._stopping:
                break
            if entry is not owing[-1] and _swept(entry):
                await self._sleep_interruptible(_CLIENT_GAP_S)

    # ------------------------------------------------------------- plumbing

    async def _live_client(self, client_id: str) -> Optional[dict]:
        """The client's CURRENT record, read at the moment its turn comes up.

        A scheduled queue can be set at five in the afternoon and reached at
        two in the morning. Reading the keywords and caps that were true
        when it was queued would mean an edit made in between is silently
        ignored -- and for an overnight run there is nobody there to notice.
        """
        try:
            return await clients_db.try_get(client_id)
        except Exception as e:                              # noqa: BLE001
            log.warning(f"scheduler: could not read client {client_id}: "
                        f"{type(e).__name__}: {e}")
            return None

    async def _owed_for(self, group_id: str) -> int:
        """What this client still owes, straight from the durable ledger.

        ANY FAILURE ANSWERS "UNKNOWN", NEVER "NOTHING". Reporting a client
        as fully covered because a read timed out is the exact false clean
        bill of health the ledger exists to prevent -- and the one that
        would be hardest to ever notice, since it looks identical to
        success.
        """
        try:
            return int((await coverage_db.summary(group_id))["owed"])
        except Exception as e:                              # noqa: BLE001
            log.warning(f"scheduler: coverage read failed for {group_id} -- "
                        f"recording owed as unknown: {type(e).__name__}: {e}")
            return OWED_UNKNOWN

    async def _cancel_now(self, job_id: str) -> None:
        if not job_id or self._cancel_sent_for == job_id:
            return
        self._cancel_sent_for = job_id
        try:
            await discovery_runner.cancel(job_id)
        except Exception as e:                              # noqa: BLE001
            # Clearing the marker lets the watch loop try again on its next
            # tick rather than assuming a cancel that never landed.
            self._cancel_sent_for = ""
            log.warning(f"scheduler: cancel of {job_id} failed: "
                        f"{type(e).__name__}: {e}")

    @staticmethod
    def _platform_outcomes(job: Any) -> dict[str, str]:
        """Platform id -> outcome, straight off the job. Reported as-is:
        `partial` and `failed` are the sweep's own words for what happened
        to that platform, and flattening them into the entry's single
        status is exactly what loses the detail worth acting on."""
        return {pid: p.status for pid, p in (job.platforms or {}).items()}

    @staticmethod
    def _platform_details(job: Any) -> dict[str, dict]:
        return {
            pid: {"found": p.found, "new": p.new, "note": p.note}
            for pid, p in (job.platforms or {}).items()
        }

    async def _settle(self, entry: dict, status: str, message: str) -> None:
        entry["status"] = status
        entry["message"] = message
        entry["closing_gaps"] = False
        entry["finished_at"] = _now()
        self._current_job_id = ""
        await self._flush(force=True)

    async def _set_current(self, client_id: str) -> None:
        if self._run is not None:
            self._run["current_id"] = client_id
        await self._flush(force=True)

    async def _flush(self, *, force: bool = False) -> None:
        """Push the live run state to Mongo. Best effort -- see
        `schedule_repository.save_run`.

        THROTTLED, because the watch loop calls this every couple of
        seconds and a queue of ten clients can run for hours: unthrottled
        that is tens of thousands of writes of the same growing document
        for a progress bar nobody is watching at 3am. The durable record
        only has to be good enough to answer "where had it got to when the
        power went", and thirty seconds of granularity answers that.

        `force` is for the transitions that MUST survive a crash landing
        in the next instant -- a client settling, a run ending. Those are
        the writes the history is actually made of.
        """
        if self._run is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_flush) < _FLUSH_EVERY_S:
            return
        self._last_flush = now
        await schedule_db.save_run(self._run["_id"], {
            "entries": self._run["entries"],
            "current_id": self._run.get("current_id", ""),
            "stopping": self._stopping,
        })

    async def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep, but notice a Stop. A minute-long gap that ignores Stop is
        a minute of a button that looks broken."""
        remaining = seconds
        while remaining > 0:
            if self._stopping:
                return
            step = min(1.0, remaining)
            await asyncio.sleep(step)
            remaining -= step

    # ---------------------------------------------------------------- reads

    async def snapshot(self) -> dict:
        """Everything the Scheduler page renders, in one read."""
        state = await schedule_db.get_state()
        schedule = Schedule.from_dict(state.get("schedule"))
        next_at, shift = schedule_math.next_run_after(schedule, _now())

        run = None
        if self._run is not None:
            run = {
                "run_id": self._run["_id"],
                "trigger": self._run.get("trigger", "manual"),
                "status": schedule_db.RUN_RUNNING,
                "entries": self._run["entries"],
                "current_id": self._run.get("current_id", ""),
                "stopping": self._stopping,
            }
        else:
            recent = await schedule_db.list_runs(limit=1)
            if recent:
                last = recent[0]
                run = {
                    "run_id": last["_id"],
                    "trigger": last.get("trigger", "manual"),
                    "status": last.get("status", schedule_db.RUN_DONE),
                    "entries": last.get("entries") or [],
                    "current_id": "",
                    "stopping": False,
                    "message": last.get("message", ""),
                    "started_at": last.get("started_at"),
                    "finished_at": last.get("finished_at"),
                    "due_at": last.get("due_at"),
                    "late_seconds": last.get("late_seconds", 0.0),
                }

        return {
            "schedule": schedule.to_dict(),
            "queue": list(state.get("queue") or []),
            "queue_names": dict(state.get("queue_names") or {}),
            "running": self.busy,
            "next_run_at": next_at,
            "last_fired_at": state.get("last_fired_at"),
            "wall_clock_shift": shift.to_dict() if shift else None,
            "catch_up_grace_minutes": int(_CATCH_UP_GRACE.total_seconds() // 60),
            "run": run,
        }


scheduler_engine = SchedulerEngine()
