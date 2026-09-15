"""The parser-drift canary: can it tell a broken platform from a quiet one.

WHAT THIS GUARDS AGAINST. A scraping engine's worst failure is not an error,
it is a clean zero. Facebook rotates a GraphQL doc id, the payload branch
recognises nothing, the DOM fallback returns little or nothing, and every
sweep reports `no-results` -- which is a true and ordinary answer for a
keyword nobody is impersonating. The job reports done, the coverage ledger
records the keyword as searched (it was), and every indicator stays green
while the product silently stops finding anything.

`shared/extraction.py` reports the CAUSE of that per call. Nothing was
watching for the SYMPTOM: its docstring names a "parser-drift canary" in
`services/discovery_service.py`, and that file went with the old backend.

THE TWO WAYS TO GET THIS WRONG, both tested here:

  * miss a real break -- the detector vouches for a platform returning
    nothing, and nobody finds out for weeks;
  * cry wolf -- it alerts on ordinary empty searches, an operator learns to
    filter the sender within a week, and the next real break is missed
    anyway. A noisy canary is a broken canary.

The discriminator is scope: a genuinely empty search belongs to ONE client
and ONE keyword, while a broken parser takes every client on the platform
down together. Most of what follows tests that distinction holds in both
directions, plus the two ways a detector can quietly lie -- by treating
"not enough evidence" as a pass, and by comparing windows of different
lengths as though they were comparable.
"""

from __future__ import annotations

import pytest

from backend.services import engine_health_service as eh


def _w(sweeps=0, hits=0, empty=0, searches=0, clients=0, sources=None) -> dict:
    """One window's shape for one platform, as telemetry_repository emits."""
    return {
        "sweeps": sweeps, "hits": hits, "empty_sweeps": empty,
        "searches": searches, "clients": clients,
        "sources": sources if sources is not None else {"graphql": sweeps},
    }


async def _no_attrs(since, until=None) -> dict:
    """No attribute-level evidence either way, which is the normal case for
    a platform whose keys are all still matching."""
    return {}


def _healthy_baseline(days: int = 7) -> dict:
    """A normal week: 18 distinct searches a day, ~14 profiles each."""
    sweeps = 18 * days
    return _w(sweeps=sweeps, hits=14 * sweeps, empty=int(sweeps * 0.1),
              searches=18, clients=3, sources={"graphql": sweeps})


# ------------------------------------------------------- catching the break


class TestItCatchesARealBreak:
    def test_a_platform_wide_blackout_is_broken_without_needing_a_baseline(self):
        """The unambiguous case. Whatever this platform used to do, zero
        profiles across eighteen searches and three clients is not a run of
        empty keywords -- and it must not have to wait for a baseline to
        say so, because the first thing a fresh deployment does is have no
        baseline."""
        state, why = eh._verdict(
            _w(sweeps=18, hits=0, empty=18, searches=18, clients=3),
            _w(), single_client=False)

        assert state == eh.BROKEN
        assert "zero profiles" in why

    def test_a_yield_collapse_is_broken(self):
        """Still finding a trickle, which is what a half-working parser
        looks like -- it matches a few results out of a payload it mostly
        no longer understands."""
        state, why = eh._verdict(
            _w(sweeps=18, hits=18, empty=12, searches=18, clients=3),
            _healthy_baseline(), single_client=False)

        assert state == eh.BROKEN
        assert "collapsed" in why

    def test_falling_back_to_the_dom_is_degraded_before_anything_breaks(self):
        """THE WARNING THAT ARRIVES BEFORE THE OUTAGE. The engines read the
        platform's own payload first and scrape the rendered page when that
        stops parsing. Yield is unchanged, so nothing else in the system
        notices -- and the engine is now one layout change from returning
        nothing at all."""
        state, why = eh._verdict(
            _w(sweeps=18, hits=250, empty=2, searches=18, clients=3,
               sources={"dom": 18}),
            _healthy_baseline(), single_client=False)

        assert state == eh.DEGRADED
        assert "graphql" in why and "dom" in why

    def test_a_jump_in_empty_searches_is_broken(self):
        state, _ = eh._verdict(
            _w(sweeps=18, hits=40, empty=16, searches=18, clients=3),
            _healthy_baseline(), single_client=False)

        assert state == eh.BROKEN


# -------------------------------------------------- and does not cry wolf


class TestItDoesNotCryWolf:
    def test_one_client_having_a_quiet_week_is_not_a_platform_problem(self):
        """THE FALSE POSITIVE THAT WOULD KILL THIS. Most keywords match
        nobody most of the time, so a detector that fires on empty searches
        is filtered into a junk folder within a week -- and then the real
        break is missed too. One client's searches going quiet does not
        clear the cross-client bar."""
        state, why = eh._verdict(
            _w(sweeps=18, hits=0, empty=18, searches=18, clients=1),
            _healthy_baseline(), single_client=False)

        assert state == eh.UNKNOWN
        assert "not enough" in why

    def test_a_healthy_platform_reads_healthy(self):
        state, _ = eh._verdict(
            _w(sweeps=18, hits=14 * 18, empty=2, searches=18, clients=3),
            _healthy_baseline(), single_client=False)

        assert state == eh.HEALTHY

    def test_a_longer_baseline_window_is_not_mistaken_for_a_collapse(self):
        """THE BUG THIS ALMOST SHIPPED WITH. The baseline is seven days and
        the recent window is one, so anything summed over a window is seven
        times larger in the baseline by construction. Dividing total hits
        by DISTINCT searches did exactly that and reported every healthy
        platform as collapsed. Both sides are per-sweep means, which are
        invariant to how long the window is."""
        recent = _w(sweeps=18, hits=14 * 18, empty=2, searches=18, clients=3)
        for days in (1, 7, 14):
            state, why = eh._verdict(recent, _healthy_baseline(days), single_client=False)
            assert state == eh.HEALTHY, f"{days}d baseline: {why}"

    def test_a_single_client_deployment_still_gets_a_verdict(self):
        """The cross-client check is the stronger signal where it exists,
        not a precondition for having any signal -- refusing to ever judge
        a single-tenant install would make this useless there."""
        state, _ = eh._verdict(
            _w(sweeps=18, hits=0, empty=18, searches=18, clients=1),
            _healthy_baseline(), single_client=True)

        assert state == eh.BROKEN


# ------------------------------------------- the two ways to quietly lie


class TestSilenceIsNeverAPass:
    def test_too_little_evidence_is_unknown_not_healthy(self):
        """`unknown` and `healthy` must never collapse into each other. A
        detector that reports "fine" when it has seen almost nothing is
        worse than no detector, because it is believed."""
        state, _ = eh._verdict(
            _w(sweeps=3, hits=9, empty=1, searches=3, clients=3),
            _healthy_baseline(), single_client=False)

        assert state == eh.UNKNOWN

    def test_a_platform_with_no_baseline_is_unknown_not_healthy(self):
        """It may be working perfectly. Nothing here has ever seen it work,
        so nothing here may vouch for it."""
        state, why = eh._verdict(
            _w(sweeps=18, hits=200, empty=2, searches=18, clients=3),
            _w(), single_client=False)

        assert state == eh.UNKNOWN
        assert "no baseline" in why

    @pytest.mark.asyncio
    async def test_an_unreadable_telemetry_store_reports_unknown_not_healthy(
        self, monkeypatch,
    ):
        """The failure direction that matters. A health report answering
        "fine" because its own datastore was unreachable is the exact false
        clean bill of health this whole module exists to prevent."""
        async def _boom(*a, **kw):
            raise RuntimeError("mongo unreachable")

        monkeypatch.setattr(eh.telemetry_db, "window", _boom)

        report = await eh.check_once()

        assert report["state"] == eh.UNKNOWN
        assert report["platforms"] == []
        assert "mongo unreachable" in report["error"]


# --------------------------------------------------- reporting and alerting


class TestTheReportAndItsAlerts:
    @pytest.mark.asyncio
    async def test_the_worst_platform_decides_the_overall_state(self, monkeypatch):
        windows = {
            "recent": {
                "facebook": _w(sweeps=18, hits=0, empty=18, searches=18, clients=3),
                "youtube": _w(sweeps=18, hits=90, empty=2, searches=18, clients=3,
                              sources={"api": 18}),
            },
            "baseline": {
                "facebook": _healthy_baseline(),
                "youtube": _w(sweeps=126, hits=5 * 126, empty=12, searches=18,
                              clients=3, sources={"api": 126}),
            },
        }
        calls = []

        async def _window(since, until=None):
            calls.append(since)
            return windows["recent"] if len(calls) == 1 else windows["baseline"]

        monkeypatch.setattr(eh.telemetry_db, "window", _window)
        # The per-attribute probe reads its own window. Left unpatched it
        # would reach the real database, which is both slow and a way for
        # one test to see another's writes.
        monkeypatch.setattr(eh.telemetry_db, "schema_window", _no_attrs)

        report = await eh.build_report()

        assert report["state"] == eh.BROKEN
        states = {p["platform"]: p["state"] for p in report["platforms"]}
        assert states == {"facebook": eh.BROKEN, "youtube": eh.HEALTHY}

    @pytest.mark.asyncio
    async def test_it_raises_one_incident_per_platform_not_one_per_check(
        self, monkeypatch,
    ):
        """The monitor runs every half hour. A drift that persists for a
        week must be one conversation, not three hundred emails -- which is
        just the noisy-canary failure arriving by a different route."""
        recorded = []

        async def _record(doc):
            recorded.append(doc)

        async def _window(since, until=None):
            return {"facebook": _w(sweeps=18, hits=0, empty=18, searches=18, clients=3)}

        monkeypatch.setattr(eh.incidents_db, "record", _record)
        monkeypatch.setattr(eh.telemetry_db, "window", _window)
        monkeypatch.setattr(eh.telemetry_db, "schema_window", _no_attrs)
        monkeypatch.setattr(eh, "_recent_alerts", {})

        await eh.check_once()
        await eh.check_once()
        await eh.check_once()

        assert len(recorded) == 1
        doc = recorded[0]
        assert doc["severity"] == "critical"
        assert doc["kind"] == "engine_health"
        # The incident has to point at the thing that can actually fix it.
        assert "extraction.py" in doc["fix"]

    def test_the_normal_extraction_path_is_learned_not_declared(self):
        """No table anywhere says what Facebook ought to return. Such a
        table is written once against whatever the platform did that month
        and then rots unattended -- the same class of problem this module
        exists to catch."""
        assert eh._modal_source({"graphql": 90, "dom": 10}) == "graphql"
        assert eh._modal_source({}) == ""
        # Deterministic on ties: this value is compared between two windows,
        # and a tie resolving differently each time would invent drift.
        assert eh._modal_source({"dom": 5, "graphql": 5}) == "dom"


# ------------------------------------------ naming the attribute that broke


class TestItNamesTheAttributeThatChanged:
    """The difference between "Facebook results dropped" and
    "`edge.rendering_strategy.view_model` matched 0 of 240 edges, and 2,400
    of 2,400 last week".

    The yield comparison above is an inference from output. This is direct
    evidence from the parse itself, gathered by probing each attribute the
    engines deliberately target (shared/schema_probe.py). It is the
    difference between an afternoon diffing a live capture against the
    parser and a one-line fix -- and it catches breaks the yield test
    cannot see at all.
    """

    @staticmethod
    def _attrs(hits: int, misses: int) -> dict:
        return {"hits": hits, "misses": misses}

    def test_a_key_that_used_to_match_and_stopped_is_reported(self):
        drift = eh._attribute_drift(
            recent={"facebook": {"edge.rendering_strategy.view_model": self._attrs(0, 240)}},
            baseline={"facebook": {"edge.rendering_strategy.view_model": self._attrs(2400, 0)}},
        )

        assert len(drift) == 1
        assert drift[0]["attribute"] == "edge.rendering_strategy.view_model"
        assert drift[0]["now"] == "0 of 240"
        assert drift[0]["before"] == "2400 of 2400"

    def test_a_key_that_never_worked_is_not_reported(self):
        """That is a bug in our own parser, or a field the platform never
        had. Shouting about it on every check would bury the real ones."""
        drift = eh._attribute_drift(
            recent={"facebook": {"profile.nickname": self._attrs(0, 240)}},
            baseline={"facebook": {"profile.nickname": self._attrs(0, 2400)}},
        )

        assert drift == []

    def test_one_odd_result_is_not_a_rename(self):
        """A handful of malformed objects in a payload is normal. Below the
        evidence bar the answer is silence, not a guess."""
        drift = eh._attribute_drift(
            recent={"twitter": {"result.{legacy|core}.screen_name": self._attrs(0, 3)}},
            baseline={"twitter": {"result.{legacy|core}.screen_name": self._attrs(2400, 0)}},
        )

        assert drift == []

    @pytest.mark.asyncio
    async def test_it_overrides_a_healthy_yield_and_mails(self, monkeypatch):
        """THE BREAK THE YIELD TEST CANNOT SEE. X moving `screen_name` out
        of `legacy` breaks every handle while the result COUNT does not
        move at all -- the sweep still finds twenty users per page, they
        are just unusable. A statistical verdict says healthy; the probe
        says exactly which field went."""
        recorded, mailed = [], []

        async def _record(doc):
            recorded.append(doc)

        async def _window(since, until=None):
            return {"twitter": _w(sweeps=18, hits=14 * 18, empty=2, searches=18, clients=3)}

        # `build_report` asks for the RECENT window first, then the
        # baseline. Discriminating on call order rather than on the
        # timestamps: `default_windows()` reads the clock fresh every call,
        # so two calls a microsecond apart are never equal and a timestamp
        # comparison here silently answered "baseline" to both.
        schema_calls = []

        async def _schema(since, until=None):
            schema_calls.append(since)
            if len(schema_calls) == 1:
                return {"twitter": {"result.{legacy|core}.screen_name": {"hits": 0, "misses": 360}}}
            return {"twitter": {"result.{legacy|core}.screen_name": {"hits": 2400, "misses": 0}}}

        monkeypatch.setattr(eh.incidents_db, "record", _record)
        monkeypatch.setattr(eh.telemetry_db, "window", _window)
        monkeypatch.setattr(eh.telemetry_db, "schema_window", _schema)
        monkeypatch.setattr(eh, "_recent_alerts", {})

        import backend.services.email_service as email_service
        monkeypatch.setattr(
            email_service, "send_critical_incident_alert",
            lambda incident: mailed.append(incident) or _done())

        report = await eh.check_once()

        pstate = report["platforms"][0]
        assert pstate["state"] == eh.BROKEN
        assert "screen_name" in pstate["detail"]
        # The incident has to carry the name too -- that is what reaches
        # the inbox, and a mail that only says "twitter is broken" sends
        # the reader straight back to the dashboard they came from.
        assert recorded and "screen_name" in recorded[0]["cause"]
        assert "_user_from_result" in recorded[0]["fix"]

    @pytest.mark.asyncio
    async def test_a_second_key_breaking_later_is_not_swallowed_by_the_cooldown(
        self, monkeypatch,
    ):
        """The cooldown stops one break becoming a hundred emails. It must
        not stop a NEW break being reported -- a second field going a day
        later is new information."""
        recorded = []
        state = {"attrs": ["a.one"]}

        async def _record(doc):
            recorded.append(doc)

        async def _window(since, until=None):
            return {"facebook": _w(sweeps=18, hits=200, empty=2, searches=18, clients=3)}

        # Recent window first, baseline second -- see the note above on why
        # this counts calls instead of comparing timestamps. Reset each
        # check so all three laps see the same pair.
        calls = {"n": 0}

        async def _schema(since, until=None):
            calls["n"] += 1
            if calls["n"] % 2 == 1:
                return {"facebook": {k: {"hits": 0, "misses": 240} for k in state["attrs"]}}
            return {"facebook": {k: {"hits": 2400, "misses": 0} for k in state["attrs"]}}

        monkeypatch.setattr(eh.incidents_db, "record", _record)
        monkeypatch.setattr(eh.telemetry_db, "window", _window)
        monkeypatch.setattr(eh.telemetry_db, "schema_window", _schema)
        monkeypatch.setattr(eh, "_recent_alerts", {})

        await eh.check_once()
        await eh.check_once()          # same key -- silent
        state["attrs"] = ["a.one", "a.two"]
        await eh.check_once()          # a new key -- must speak

        assert len(recorded) == 2


def _done():
    """A finished awaitable, so a lambda can stand in for an async call."""
    import asyncio
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(True)
    return fut
