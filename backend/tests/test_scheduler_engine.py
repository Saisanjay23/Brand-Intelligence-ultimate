"""The Scheduler engine: does the queue really run one by one, and does
every ending name itself?

The engine drives real browser sweeps against real logged-in accounts, so
none of that happens here. What IS asserted is the sequencing and the
bookkeeping around it -- which is where the defects that matter live,
because they are all silent:

  * two sweeps running at once (the one thing the queue exists to prevent);
  * a client swept out of order, or skipped entirely;
  * a client reported `done, 0 found` for a sweep that never happened;
  * a coverage read that failed being recorded as "owes nothing";
  * a run left marked `running` with no process behind it.

Every one of those produces a queue that LOOKS fine. None of them raises.
So only an assertion can find them.

The discovery engine and Mongo are both replaced with fakes that record
what they were asked to do, in order.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.database.repositories import schedule_repository as schedule_db
from backend.services import scheduler_service
from backend.services.scheduler_service import OWED_UNKNOWN, SchedulerEngine

UTC = timezone.utc
pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------------ fakes

class FakePlatformSweep:
    def __init__(self, status="done", found=0, new=0, note=""):
        self.status, self.found, self.new, self.note = status, found, new, note


class FakeJob:
    """Just enough of a DiscoveryJob for the engine to watch.

    Holds a back-reference to the runner so that SETTLING decrements the
    live count. Without that, `max_live` counts sweeps ever started rather
    than sweeps running at once -- and the overlap assertion it exists for
    would pass whatever the engine did.
    """

    def __init__(self, job_id, platforms, runner, *, settle_after=0, counts=True):
        self.id = job_id
        self.platforms = platforms
        self.status = "running"
        self.message = ""
        self.found = 0
        self.new = 0
        self.completed = 0
        self.total = 10
        self._ticks = 0
        self._settle_after = settle_after
        self._runner = runner
        self._counts = counts
        self._released = False

    def tick(self):
        self._ticks += 1
        if self._ticks >= self._settle_after and self.status == "running":
            self.status = "done"
            self.message = "swept"
            self.release()

    def release(self):
        if self._counts and not self._released:
            self._released = True
            self._runner.live = max(0, self._runner.live - 1)


class FakeRunner:
    """Stands in for `discovery_runner`, and records the order it was
    asked to sweep clients in -- which is the thing under test."""

    def __init__(self):
        self.calls = []          # (group_id, kwargs) in the order received
        self.live = 0            # sweeps in flight RIGHT NOW
        self.max_live = 0        # the high-water mark
        self.cancelled = []
        self.jobs = {}
        self.skipped_for = {}    # group_id -> {platform: reason}
        self.raise_for = set()
        self._n = 0

    async def start(self, *, group_id, **kwargs):
        if group_id in self.raise_for:
            raise RuntimeError("no browser available")
        self.calls.append((group_id, kwargs))
        self._n += 1
        job_id = f"job{self._n}"
        skipped = self.skipped_for.get(group_id, {})
        platforms = {p: FakePlatformSweep("skipped", note=r)
                     for p, r in skipped.items()}
        if not skipped:
            platforms["facebook"] = FakePlatformSweep("done", found=3, new=1)
        job = FakeJob(job_id, platforms, self, settle_after=2,
                      counts=not skipped)
        job.found, job.new = (0 if skipped else 3), (0 if skipped else 1)
        self.jobs[job_id] = job

        if not skipped:
            # Live from the moment it starts until it settles, so a second
            # sweep opened before the first closed shows up in `max_live`.
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        return job, skipped

    async def cancel(self, job_id):
        self.cancelled.append(job_id)
        job = self.jobs.get(job_id)
        if job and job.status == "running":
            job.status = "cancelled"
            job.message = "cancelled"
            job.release()
        return True


class FakeStore:
    """Replaces `schedule_repository`. Keeps the singleton and the runs in
    plain dicts so assertions can read exactly what was persisted."""

    def __init__(self, queue=None, names=None):
        self.state = {
            "schedule": {"enabled": False, "mode": "daily", "at": "02:00",
                         "on_date": "", "weekdays": [], "tz": "UTC"},
            "queue": list(queue or []),
            "queue_names": dict(names or {}),
            "active_run_id": "",
            "next_run_at": None,
            "last_fired_at": None,
        }
        self.runs = {}
        self.order = []          # run ids, in creation order

    # -- the subset the engine calls
    async def get_state(self):
        return dict(self.state)

    async def save_next_run(self, next_run_at):
        self.state["next_run_at"] = next_run_at
        return dict(self.state)

    async def mark_fired(self, fired_at, next_run_at):
        self.state["last_fired_at"] = fired_at
        self.state["next_run_at"] = next_run_at
        return dict(self.state)

    async def set_active_run(self, run_id):
        self.state["active_run_id"] = run_id or ""
        return dict(self.state)

    async def create_run(self, *, run_id, trigger, entries, due_at=None,
                         late_seconds=0.0, status="running", message=""):
        doc = {"_id": run_id, "trigger": trigger, "entries": entries,
               "due_at": due_at, "late_seconds": late_seconds,
               "status": status, "message": message, "current_id": "",
               "stopping": False, "started_at": datetime.now(UTC),
               "finished_at": None}
        self.runs[run_id] = doc
        self.order.append(run_id)
        return doc

    async def record_missed(self, *, due_at, late_seconds, reason):
        return await self.create_run(
            run_id=f"missed{len(self.order)}", trigger="scheduled", entries=[],
            due_at=due_at, late_seconds=late_seconds, status="missed",
            message=reason)

    async def save_run(self, run_id, fields):
        self.runs.setdefault(run_id, {}).update(fields)

    async def finish_run(self, run_id, *, status, message):
        self.runs[run_id].update({"status": status, "message": message,
                                  "finished_at": datetime.now(UTC)})

    async def list_runs(self, limit=25):
        return [self.runs[r] for r in reversed(self.order)][:limit]

    async def trim_history(self, keep=200):
        return 0

    def new_run_id(self):
        return f"run{len(self.order) + 1}"

    # constants the engine reads off the module
    RUN_RUNNING = schedule_db.RUN_RUNNING
    RUN_DONE = schedule_db.RUN_DONE
    RUN_CANCELLED = schedule_db.RUN_CANCELLED
    RUN_FAILED = schedule_db.RUN_FAILED


class FakeClients:
    def __init__(self, docs):
        self.docs = docs

    async def try_get(self, client_id):
        return self.docs.get(client_id)


def client(cid, *, name=None, individual=("acme",), domain=(),
           scope="", platforms=()):
    return {
        "client_id": cid, "name": name or cid.title(),
        "name_keywords": list(individual), "domain_keywords": list(domain),
        "scheduler_keyword_scope": scope,
        "scheduler_platforms": list(platforms),
        "scheduler_facebook_tabs": [], "scheduler_budget_minutes": 0,
        "platform_limits_individual": {}, "platform_limits_domain": {},
        "platform_tab_limits": {},
    }


@pytest.fixture
def rig(monkeypatch):
    """An engine wired to fakes, with the waiting taken out.

    The gap between clients is a real minute in production (see
    `_CLIENT_GAP_S`) and its PRESENCE is asserted separately, by watching
    that the engine asks to sleep -- not by actually sleeping for it.
    """
    runner = FakeRunner()
    store = FakeStore()
    clients = FakeClients({})
    owed = {"value": 0, "raises": False}
    slept = []

    monkeypatch.setattr(scheduler_service, "discovery_runner", runner)
    monkeypatch.setattr(scheduler_service, "schedule_db", store)
    monkeypatch.setattr(scheduler_service, "clients_db", clients)
    monkeypatch.setattr(scheduler_service, "_POLL_S", 0.001)
    monkeypatch.setattr(scheduler_service, "_CLIENT_GAP_S", 0.0)
    monkeypatch.setattr(scheduler_service, "_FLUSH_EVERY_S", 0.0)

    class FakeCoverage:
        async def summary(self, group_id):
            if owed["raises"]:
                raise RuntimeError("mongo is down")
            return {"owed": owed["value"]}

    monkeypatch.setattr(scheduler_service, "coverage_db", FakeCoverage())

    engine = SchedulerEngine()

    # The gap between clients is a real minute in production. Recorded
    # rather than slept through, so its PRESENCE and ABSENCE are both
    # assertable without the suite taking minutes.
    real_sleep = engine._sleep_interruptible

    async def recording_sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    engine._sleep_interruptible = recording_sleep

    async def drive(timeout=5.0):
        """Let the run finish, ticking every fake job along the way."""
        deadline = asyncio.get_event_loop().time() + timeout
        while engine.busy or (engine._run_task and not engine._run_task.done()):
            for job in list(runner.jobs.values()):
                job.tick()
            await asyncio.sleep(0.002)
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError("the run never finished")

    class Rig:
        pass

    r = Rig()
    r.engine, r.runner, r.store, r.clients = engine, runner, store, clients
    r.owed, r.slept, r.drive = owed, slept, drive
    return r


def entries_of(rig, run_id="run1"):
    return rig.store.runs[run_id]["entries"]


def by_id(entries):
    return {e["client_id"]: e for e in entries}


# ------------------------------------------------- the gap between clients

class TestInterClientGap:
    """`_CLIENT_GAP_S` spaces out real activity on the POOLED ACCOUNTS.
    Every client in a queue is swept through the same logins, so
    back-to-back sweeps are the single pattern that reads most clearly as
    automation. It is owed by an entry that used an account -- and by
    nothing else."""

    async def test_a_real_sweep_is_followed_by_the_gap(self, rig):
        rig.store.state["queue"] = ["c1", "c2"]
        rig.clients.docs = {c: client(c) for c in ("c1", "c2")}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert len(rig.slept) == 1          # between the two, not after c2

    async def test_a_skipped_client_costs_no_gap(self, rig):
        """A nightly queue of ten where eight are skipped for want of a
        session used to sit idle for eight minutes, in the middle of the
        night, doing nothing -- while the two that could be swept waited
        their turn behind it."""
        rig.store.state["queue"] = ["c1", "c2", "c3"]
        rig.clients.docs = {
            "c1": client("c1", individual=(), domain=()),      # no keywords
            "c2": client("c2", individual=(), domain=()),      # no keywords
            "c3": client("c3"),                                # a real sweep
        }
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        # c1 and c2 touched no account, and c3 is last.
        assert rig.slept == []

    async def test_a_client_with_no_usable_session_costs_no_gap(self, rig):
        rig.store.state["queue"] = ["c1", "c2"]
        rig.clients.docs = {c: client(c) for c in ("c1", "c2")}
        rig.runner.skipped_for = {"c1": {"facebook": "session expired"}}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert rig.slept == []

    async def test_the_last_client_is_not_followed_by_a_gap(self, rig):
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert rig.slept == []


# -------------------------------------------------------- one at a time

class TestSequencing:
    async def test_clients_are_swept_in_queue_order(self, rig):
        rig.store.state["queue"] = ["c3", "c1", "c2"]
        rig.clients.docs = {c: client(c) for c in ("c1", "c2", "c3")}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        # The order dropped into the queue, not alphabetical, not the
        # directory's own order.
        assert [g for g, _ in rig.runner.calls] == ["c3", "c1", "c2"]

    async def test_never_two_sweeps_at_once(self, rig):
        """The single guarantee the queue exists to provide. Two clients
        swept concurrently means both competing for the same platform
        sessions."""
        rig.store.state["queue"] = ["c1", "c2", "c3", "c4"]
        rig.clients.docs = {c: client(c) for c in ("c1", "c2", "c3", "c4")}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert rig.runner.max_live == 1

    async def test_a_second_fire_during_a_run_is_declined_not_queued(self, rig):
        rig.store.state["queue"] = ["c1", "c2"]
        rig.clients.docs = {c: client(c) for c in ("c1", "c2")}
        first = await rig.engine.fire(trigger="manual")
        await asyncio.sleep(0)          # let the run task take the lock
        second = await rig.engine.fire(trigger="scheduled")
        await rig.drive()
        assert first is not None
        # Declined, not deferred: a second full sweep of every client
        # starting whenever the first ends is never what was meant.
        assert second is None

    async def test_two_fires_landing_together_still_yield_one_run(self, rig):
        """THE RACE THE LOCK ALONE DOES NOT COVER. The lock is taken inside
        the run task, which has not started when `fire` returns -- so two
        fires arriving in the same tick (the 02:00 tick and an analyst
        pressing Run) both saw an unlocked lock and were both accepted.
        They then serialised on the lock rather than overlapping, which is
        not a session collision but IS a second full sweep of every client
        that nobody asked for."""
        rig.store.state["queue"] = ["c1", "c2"]
        rig.clients.docs = {c: client(c) for c in ("c1", "c2")}
        first, second = await asyncio.gather(
            rig.engine.fire(trigger="scheduled"),
            rig.engine.fire(trigger="manual"),
        )
        await rig.drive()
        accepted = [r for r in (first, second) if r is not None]
        assert len(accepted) == 1
        # ...and exactly one run document exists, not two.
        assert len(rig.store.order) == 1
        # Each client swept once, not twice.
        assert sorted(g for g, _ in rig.runner.calls) == ["c1", "c2"]

    async def test_a_failed_fire_does_not_wedge_the_scheduler_shut(self, rig):
        """If the claim outlived a failed attempt, every later run --
        scheduled and manual alike -- would be declined for ever, and the
        only symptom would be a scheduler that silently stopped."""
        rig.clients.docs = {"c1": client("c1")}

        async def boom():
            raise RuntimeError("mongo is down")

        broken, rig.store.get_state = rig.store.get_state, boom
        with pytest.raises(RuntimeError):
            await rig.engine.fire(trigger="scheduled")
        assert rig.engine.busy is False          # the claim was given back

        rig.store.get_state = broken
        rig.store.state["queue"] = ["c1"]
        assert await rig.engine.fire(trigger="manual") is not None
        await rig.drive()
        assert rig.runner.calls

    async def test_every_client_gets_swept_even_when_one_fails_to_start(self, rig):
        """One client's failure must not take the rest of the night with
        it -- the other nine still need sweeping."""
        rig.store.state["queue"] = ["c1", "c2", "c3"]
        rig.clients.docs = {c: client(c) for c in ("c1", "c2", "c3")}
        rig.runner.raise_for = {"c2"}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        got = by_id(entries_of(rig))
        assert got["c1"]["status"] == "done"
        assert got["c2"]["status"] == "failed"
        assert "could not start" in got["c2"]["message"]
        assert got["c3"]["status"] == "done"


# --------------------------------------------- a skip must look like a skip

class TestSkipsAreNotSuccesses:
    async def test_no_keywords_is_a_skip_with_a_reason(self, rig):
        """Starting an empty sweep settles as `done, 0 found`, which in a
        list is indistinguishable from a real sweep that found nothing."""
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1", individual=(), domain=())}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        e = entries_of(rig)[0]
        assert e["status"] == "skipped"
        assert "no keywords" in e["message"]
        assert rig.runner.calls == []       # nothing was swept at all

    async def test_a_scope_that_excludes_everything_is_a_skip(self, rig):
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1", individual=("acme",),
                                         domain=(), scope="domain")}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        e = entries_of(rig)[0]
        assert e["status"] == "skipped"
        assert "domain keywords only" in e["message"]

    async def test_a_deleted_client_is_a_skip_that_says_so(self, rig):
        rig.store.state["queue"] = ["gone"]
        rig.clients.docs = {}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        e = entries_of(rig)[0]
        assert e["status"] == "skipped"
        assert "no longer in the directory" in e["message"]

    async def test_no_usable_session_is_a_skip_carrying_the_reason(self, rig):
        """The job would settle `done` a moment later, reporting a clean
        sweep that found nothing. It is not a result, it is a skip."""
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        rig.runner.skipped_for = {"c1": {"facebook": "session expired",
                                         "instagram": "checkpoint"}}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        e = entries_of(rig)[0]
        assert e["status"] == "skipped"
        assert "session expired" in e["message"]
        assert e["platforms"] == {"facebook": "skipped", "instagram": "skipped"}


# ------------------------------------------------------------ the ledger

class TestCoverage:
    async def test_owed_is_read_after_the_sweep_settles(self, rig):
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        rig.owed["value"] = 0
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert entries_of(rig)[0]["owed"] == 0

    async def test_a_failed_coverage_read_is_unknown_never_zero(self, rig):
        """Zero is the clean bill of health. A read that failed must never
        be able to impersonate one -- that is the false all-clear that
        would be hardest of all to notice, because it looks like success."""
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        rig.owed["raises"] = True
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert entries_of(rig)[0]["owed"] == OWED_UNKNOWN
        assert OWED_UNKNOWN != 0

    async def test_a_client_still_owing_gets_one_gap_closing_lap(self, rig):
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        rig.owed["value"] = 4
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        # Swept twice: the ordinary sweep, then the gap-closing lap --
        # and the second one narrowed to what is owed.
        assert len(rig.runner.calls) == 2
        assert rig.runner.calls[0][1]["only_owed"] is False
        assert rig.runner.calls[1][1]["only_owed"] is True

    async def test_exactly_one_gap_closing_lap_never_a_loop(self, rig):
        """A keyword failing for a structural reason would otherwise be
        retried for ever, burning the same session on the same doomed
        search -- the surest way to turn a small problem into a ban."""
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        rig.owed["value"] = 4           # still owing even after the lap
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert len(rig.runner.calls) == 2

    async def test_a_client_owing_nothing_is_not_swept_again(self, rig):
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        rig.owed["value"] = 0
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert len(rig.runner.calls) == 1


# ------------------------------------------- every ending names itself

class TestEndingsAreNamed:
    async def test_an_empty_queue_is_recorded_not_ignored(self, rig):
        """Otherwise an analyst finds out in a week that the nightly sweep
        has been running over an empty queue since somebody cleared it."""
        rig.store.state["queue"] = []
        run_id = await rig.engine.fire(trigger="scheduled")
        doc = rig.store.runs[run_id]
        assert doc["status"] == "done"
        assert "queue was empty" in doc["message"]

    async def test_the_trigger_is_kept_for_the_life_of_the_record(self, rig):
        """'Did last night's sweep run, or did somebody press the button
        at nine this morning?' has to be answerable from history alone."""
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        run_id = await rig.engine.fire(trigger="catch_up")
        await rig.drive()
        assert rig.store.runs[run_id]["trigger"] == "catch_up"

    async def test_a_run_always_reaches_a_terminal_status(self, rig):
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        run_id = await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert rig.store.runs[run_id]["status"] in schedule_db.RUN_TERMINAL
        assert rig.store.state["active_run_id"] == ""

    async def test_one_failure_among_many_is_not_a_failed_run(self, rig):
        """Calling a normal night 'failed' trains an analyst to ignore the
        word."""
        rig.store.state["queue"] = ["c1", "c2"]
        rig.clients.docs = {c: client(c) for c in ("c1", "c2")}
        rig.runner.raise_for = {"c2"}
        run_id = await rig.engine.fire(trigger="manual")
        await rig.drive()
        doc = rig.store.runs[run_id]
        assert doc["status"] == "done"
        assert "1 failed" in doc["message"]

    async def test_nothing_getting_through_is_a_failed_run(self, rig):
        rig.store.state["queue"] = ["c1", "c2"]
        rig.clients.docs = {c: client(c) for c in ("c1", "c2")}
        rig.runner.raise_for = {"c1", "c2"}
        run_id = await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert rig.store.runs[run_id]["status"] == "failed"

    async def test_the_summary_counts_the_uncomfortable_things(self, rig):
        """`done` on its own reads as a clean night even when clients were
        skipped and others still owe searches."""
        rig.store.state["queue"] = ["c1", "c2"]
        rig.clients.docs = {"c1": client("c1"),
                            "c2": client("c2", individual=(), domain=())}
        rig.owed["raises"] = True
        run_id = await rig.engine.fire(trigger="manual")
        await rig.drive()
        msg = rig.store.runs[run_id]["message"]
        assert "1 skipped" in msg
        assert "could not be read" in msg      # the unknown coverage


# ----------------------------------------------------------------- stop

class TestStop:
    async def test_stop_cancels_the_sweep_in_flight(self, rig):
        rig.store.state["queue"] = ["c1", "c2", "c3"]
        rig.clients.docs = {c: client(c) for c in ("c1", "c2", "c3")}
        await rig.engine.fire(trigger="manual")
        # Wait until a sweep is actually up.
        for _ in range(200):
            await asyncio.sleep(0.002)
            if rig.runner.jobs:
                break
        assert await rig.engine.stop() is True
        await rig.drive()
        assert rig.runner.cancelled                 # it reached the engine

    async def test_stop_does_not_sweep_the_rest_of_the_queue(self, rig):
        rig.store.state["queue"] = ["c1", "c2", "c3"]
        rig.clients.docs = {c: client(c) for c in ("c1", "c2", "c3")}
        await rig.engine.fire(trigger="manual")
        for _ in range(200):
            await asyncio.sleep(0.002)
            if rig.runner.jobs:
                break
        await rig.engine.stop()
        await rig.drive()
        # A Stop that keeps going through eight more clients is not a Stop.
        assert len(rig.runner.calls) < 3

    async def test_stop_when_nothing_is_running_says_so(self, rig):
        assert await rig.engine.stop() is False


# -------------------------------------------------------------- the tick

class TestTick:
    async def test_a_due_schedule_fires(self, rig):
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        rig.store.state["schedule"] = {"enabled": True, "mode": "daily",
                                       "at": "02:00", "on_date": "",
                                       "weekdays": [], "tz": "UTC"}
        rig.store.state["next_run_at"] = datetime.now(UTC) - timedelta(seconds=5)
        await rig.engine._tick()
        await rig.drive()
        assert rig.runner.calls
        assert rig.store.runs["run1"]["trigger"] == "scheduled"

    async def test_a_schedule_that_is_off_never_fires(self, rig):
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        rig.store.state["schedule"]["enabled"] = False
        rig.store.state["next_run_at"] = datetime.now(UTC) - timedelta(hours=1)
        await rig.engine._tick()
        assert rig.runner.calls == []

    async def test_a_time_still_ahead_does_not_fire(self, rig):
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        rig.store.state["schedule"] = {"enabled": True, "mode": "daily",
                                       "at": "02:00", "on_date": "",
                                       "weekdays": [], "tz": "UTC"}
        rig.store.state["next_run_at"] = datetime.now(UTC) + timedelta(hours=1)
        await rig.engine._tick()
        assert rig.runner.calls == []

    async def test_the_pointer_rolls_forward_before_the_run_starts(self, rig):
        """Whatever happens to this run -- including the process dying
        inside it -- the schedule must not still point at a past time, or
        every subsequent tick fires the same sweep again, at full speed,
        against real accounts."""
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        rig.store.state["schedule"] = {"enabled": True, "mode": "daily",
                                       "at": "02:00", "on_date": "",
                                       "weekdays": [], "tz": "UTC"}
        due = datetime.now(UTC) - timedelta(seconds=5)
        rig.store.state["next_run_at"] = due
        await rig.engine._tick()
        assert rig.store.state["next_run_at"] > datetime.now(UTC)
        assert rig.store.state["last_fired_at"] is not None
        await rig.drive()

    async def test_a_late_fire_inside_the_window_runs_as_catch_up(self, rig):
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        rig.store.state["schedule"] = {"enabled": True, "mode": "daily",
                                       "at": "02:00", "on_date": "",
                                       "weekdays": [], "tz": "UTC"}
        rig.store.state["next_run_at"] = datetime.now(UTC) - timedelta(hours=2)
        await rig.engine._tick()
        await rig.drive()
        assert rig.store.runs["run1"]["trigger"] == "catch_up"
        assert rig.runner.calls

    async def test_a_fire_later_than_the_window_is_recorded_as_missed(self, rig):
        """A sweep that did not happen has to leave a trace, or it is
        indistinguishable from a schedule nobody ever set."""
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1")}
        rig.store.state["schedule"] = {"enabled": True, "mode": "daily",
                                       "at": "02:00", "on_date": "",
                                       "weekdays": [], "tz": "UTC"}
        rig.store.state["next_run_at"] = datetime.now(UTC) - timedelta(days=2)
        await rig.engine._tick()
        assert rig.runner.calls == []               # nothing was swept
        missed = [r for r in rig.store.runs.values() if r["status"] == "missed"]
        assert len(missed) == 1
        assert "NOT run" in missed[0]["message"]
        # ...and the schedule still moved on, so it is not stuck.
        assert rig.store.state["next_run_at"] > datetime.now(UTC)

    async def test_the_tick_survives_a_bad_schedule(self, rig):
        """A scheduler that stops ticking because of one malformed field
        is indistinguishable, from outside, from one that is working fine
        and simply has nothing to do."""
        rig.store.state["schedule"] = {"enabled": True, "mode": "weekly",
                                       "at": "not a time", "on_date": "",
                                       "weekdays": [], "tz": "UTC"}
        rig.store.state["next_run_at"] = datetime.now(UTC) - timedelta(seconds=5)
        await rig.engine._tick()                    # must not raise
        assert rig.runner.calls == []


# ----------------------------------------------------- what gets swept

class TestWhatIsSwept:
    async def test_the_client_is_read_when_its_turn_comes_not_when_queued(self, rig):
        """A queue set at five in the afternoon is reached at two in the
        morning. An edit made in between has to take effect, and there is
        nobody there to notice if it does not."""
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1", individual=("old",))}
        # Edited after queueing, before the run.
        rig.clients.docs["c1"] = client("c1", individual=("new",))
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert rig.runner.calls[0][1]["individual_keywords"] == ["new"]

    async def test_an_empty_platform_list_means_all_platforms(self, rig):
        """A never-configured client and a deliberately-all one must
        behave identically."""
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1", platforms=())}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert rig.runner.calls[0][1]["platforms"] is None

    async def test_scope_narrows_by_sending_an_empty_list(self, rig):
        """Narrowed at the request rather than filtered afterwards, so the
        per-type caps only apply to keywords genuinely in this sweep."""
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1", individual=("a",),
                                         domain=("b",), scope="individual")}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        kwargs = rig.runner.calls[0][1]
        assert kwargs["individual_keywords"] == ["a"]
        assert kwargs["domain_keywords"] == []

    async def test_duplicate_keywords_are_deduped(self, rig):
        rig.store.state["queue"] = ["c1"]
        rig.clients.docs = {"c1": client("c1", individual=("Acme", "acme", " ACME "))}
        await rig.engine.fire(trigger="manual")
        await rig.drive()
        assert rig.runner.calls[0][1]["individual_keywords"] == ["Acme"]
