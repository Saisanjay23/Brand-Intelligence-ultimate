"""The parser-drift canary: does this engine still WORK on each platform?

WHAT WAS WATCHING WHAT, BEFORE THIS. The session pool has a monitor
(`sessions/manager.py::_monitor_loop`), a canary for expiring cookies
(`session_canary_service`), graduated quarantine, incidents and email. All
of it answers one question: can we still log in. Nothing answered the other
one: when we do log in, does the scraping still work.

Those fail differently. A dead session is loud -- the platform rejects it,
`classify_failure` names it, the pool quarantines it, an email goes out. A
dead PARSER is silent by construction. Facebook rotates a GraphQL doc id;
the payload branch recognises nothing; the DOM fallback either carries the
sweep or returns nothing; `stopped` reads `no-results`, which is a true and
common answer; the job reports done; the coverage ledger records the
keyword as searched, because it was. Every indicator in the system stays
green while the product silently stops finding impersonators.

`shared/extraction.py` was built to report the CAUSE of exactly this -- the
strategy that failed, the file and line to change, the source text of the
line -- and its docstring names the piece that was supposed to detect the
SYMPTOM: "services/discovery_service.py's parser-drift canary". That file
went with the old backend and nothing replaced it. This is that piece.

HOW IT DECIDES, AND WHY IT DOES NOT CRY WOLF. The naive version of this
alerts on empty sweeps and is useless within a week, because empty sweeps
are normal: most keywords match nobody on most platforms most of the time.
The discriminator is scope, not count.

    A genuinely empty search is specific to ONE CLIENT and ONE KEYWORD.
    A broken parser is specific to a PLATFORM, and takes every client and
    every keyword on it down together.

So nothing here looks at a single sweep. Two windows are compared -- the
last day against the week before it -- and a platform is only called
unhealthy when the change is platform-wide AND large AND measured over
enough distinct searches to mean anything.

SELF-CALIBRATING, WITH NO PER-PLATFORM NUMBERS. There is no table here
saying what Facebook ought to yield. Such a table is written once, against
whatever the platform did that month, and then rots unattended until it
starts lying -- the same class of problem this module exists to catch. The
baseline window supplies what normal means for each platform, and normal is
recomputed every time the question is asked. The one consequence worth
knowing: a platform with no baseline is reported `unknown`, never `healthy`.
A detector that has never seen a platform work must not vouch for it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from backend.database.repositories import incident_repository as incidents_db
from backend.database.repositories import telemetry_repository as telemetry_db
from backend.shared.logging import get_logger

log = get_logger("services.engine_health")

# HOW MUCH EVIDENCE BEFORE AN OPINION. A platform swept twice yesterday can
# swing from 100% to 0% on one client having no impersonators, so below this
# many distinct (client, keyword) searches the verdict is `unknown` rather
# than a guess. Set at the point where a platform-wide pattern is
# distinguishable from one client's quiet week.
MIN_SEARCHES = 12

# ...and across at least this many clients, which is the actual
# discriminator: twenty searches for one client going quiet is that client,
# twenty across three clients going quiet at once is the platform. Relaxed
# automatically for a deployment that only has one client (see `_verdict`).
MIN_CLIENTS = 2

# WHAT COUNTS AS COLLAPSE. Expressed as a fraction of the platform's OWN
# baseline, never as an absolute number of hits -- Telegram finding eight
# profiles for a keyword and Facebook finding four hundred are both normal,
# and any absolute threshold would be wrong for one of them within a month.
YIELD_COLLAPSE = 0.2       # kept under a fifth of what it used to return
EMPTY_RATE_JUMP = 0.35     # empty-search rate up by this much, absolutely

# A platform returning NOTHING AT ALL across the minimum evidence is the
# unambiguous case and does not wait for a baseline comparison: whatever it
# used to do, an engine that has found zero profiles on a platform across a
# dozen searches and several clients is not working.
TOTAL_BLACKOUT = 0

# Raised at most this often per platform, so a drift that persists for a
# week is one conversation and not a hundred and sixty emails.
_ALERT_COOLDOWN_SEC = 12 * 3600
_recent_alerts: dict[str, float] = {}

HEALTHY = "healthy"
DEGRADED = "degraded"   # working, but off its primary extraction path
BROKEN = "broken"       # yield has collapsed or gone to zero
UNKNOWN = "unknown"     # not enough evidence to say, which is not "fine"


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _modal_source(sources: dict[str, int]) -> str:
    """The extraction path a platform normally produces results on.

    Learned rather than declared, so a new platform or a renamed strategy
    needs no entry anywhere. Ties break on the name to stay deterministic
    across runs -- this value is compared between two windows, and a tie
    that resolved differently each time would invent drift.
    """
    if not sources:
        return ""
    return sorted(sources.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _rate(part: int, whole: int) -> float:
    return (part / whole) if whole else 0.0


def _verdict(recent: dict, baseline: dict, single_client: bool) -> tuple[str, str]:
    """(state, why) for one platform, from its two windows.

    Ordered most-certain-first: a blackout needs no baseline, a collapse
    needs one, and a fallback-path change is the early warning that
    precedes both. Each returns the reason in the words an engineer would
    need to start looking, because a canary that only says "unhealthy" just
    moves the investigation somewhere else.
    """
    searches = recent.get("searches", 0)
    clients = recent.get("clients", 0)
    if searches < MIN_SEARCHES or (not single_client and clients < MIN_CLIENTS):
        return UNKNOWN, (
            f"only {searches} search(es) across {clients} client(s) in the recent "
            f"window -- not enough to tell a platform problem from a quiet one")

    hits = recent.get("hits", 0)
    if hits <= TOTAL_BLACKOUT:
        return BROKEN, (
            f"zero profiles found across {searches} searches and {clients} client(s). "
            f"A platform-wide zero at this scale is an extraction failure, not a run "
            f"of empty keywords")

    # Everything below is relative to this platform's own past, so a
    # platform nobody has swept before gets no verdict rather than a
    # flattering one.
    base_searches = baseline.get("searches", 0)
    if base_searches < MIN_SEARCHES:
        return UNKNOWN, (
            f"no baseline yet ({base_searches} search(es) in the comparison window) -- "
            f"currently finding {hits} profile(s) across {searches} searches")

    # PER SWEEP, NEVER PER WINDOW. The two windows are different lengths
    # (a day against a week), so any total is proportional to the window
    # and cannot be compared across them -- see telemetry_repository.window
    # for the version of this that got it wrong and called every healthy
    # platform collapsed.
    recent_yield = _rate(hits, recent.get("sweeps", 0))
    base_yield = _rate(baseline.get("hits", 0), baseline.get("sweeps", 0))
    if base_yield > 0 and recent_yield < base_yield * YIELD_COLLAPSE:
        return BROKEN, (
            f"yield collapsed: {recent_yield:.1f} profiles per search now against "
            f"{base_yield:.1f} before -- across {clients} client(s), so this is the "
            f"platform, not one client's keywords")

    recent_empty = _rate(recent.get("empty_sweeps", 0), recent.get("sweeps", 0))
    base_empty = _rate(baseline.get("empty_sweeps", 0), baseline.get("sweeps", 0))
    if recent_empty - base_empty >= EMPTY_RATE_JUMP:
        return BROKEN, (
            f"searches coming back empty jumped from {base_empty:.0%} to "
            f"{recent_empty:.0%} across {clients} client(s)")

    # STILL WORKING, ON THE WRONG PATH. The engines fall back from the
    # platform's own payload to scraping the rendered page when the payload
    # stops parsing (see shared/extraction.py::run_strategies). Results keep
    # coming, so nothing else notices -- and the engine is now one layout
    # change away from returning nothing at all. This is the warning that
    # arrives before the outage rather than during it.
    now_src = _modal_source(recent.get("sources", {}))
    was_src = _modal_source(baseline.get("sources", {}))
    if was_src and now_src and now_src != was_src:
        return DEGRADED, (
            f"extraction has fallen back from {was_src!r} to {now_src!r}. Results are "
            f"still coming, but on the backup path -- the primary parser has stopped "
            f"matching and should be fixed before the fallback breaks too")

    return HEALTHY, (
        f"{recent_yield:.1f} profiles per search across {clients} client(s), "
        f"extracting via {now_src or 'api'}")


# An attribute has to have been LOOKED FOR this many times before its
# absence means anything. One malformed result is not a rename.
MIN_ATTR_SAMPLES = 20

# ...and it has to have been working before. A key that has never matched
# is a bug in our own code or a feature that platform never had, not drift,
# and shouting about it on every check would bury the real ones.
ATTR_WAS_WORKING = 0.5   # matched at least half the time in the baseline
ATTR_NOW_BROKEN = 0.1    # matches less than a tenth of the time now


def _attribute_drift(
    recent: dict[str, dict], baseline: dict[str, dict],
) -> list[dict[str, Any]]:
    """Attributes that used to match and have stopped, per platform.

    THE QUESTION THIS ANSWERS THAT NOTHING ELSE DOES. `build_report` can
    tell you Facebook's yield collapsed. It cannot tell you why, and on a
    search payload of several thousand nested keys, "why" is nearly all of
    the work -- an engineer still has to diff a live capture against the
    parser by hand to find the one renamed key.

    This names it: `edge.rendering_strategy.view_model` matched 0 of 240
    edges this week, and 2,400 of 2,400 last week. That is a one-line fix
    once you know it, and a bad afternoon until you do.

    Reported per attribute rather than per platform because a platform can
    break in one field while the rest keeps working -- X moving
    `screen_name` out of `legacy` breaks handles while names, avatars and
    pagination all keep matching, and the yield barely moves.
    """
    out: list[dict[str, Any]] = []
    for pid in sorted(recent):
        for key, now in sorted(recent[pid].items()):
            looked = now["hits"] + now["misses"]
            if looked < MIN_ATTR_SAMPLES:
                continue
            was = baseline.get(pid, {}).get(key)
            if not was:
                continue
            was_looked = was["hits"] + was["misses"]
            if was_looked < MIN_ATTR_SAMPLES:
                continue
            was_rate = _rate(was["hits"], was_looked)
            now_rate = _rate(now["hits"], looked)
            if was_rate < ATTR_WAS_WORKING or now_rate >= ATTR_NOW_BROKEN:
                continue
            out.append({
                "platform": pid,
                "attribute": key,
                "now": f"{now['hits']} of {looked}",
                "before": f"{was['hits']} of {was_looked}",
                "now_rate": round(now_rate, 3),
                "before_rate": round(was_rate, 3),
            })
    return out


async def build_report(hours: int = 24, baseline_days: int = 7) -> dict[str, Any]:
    """Per-platform engine health, as the API and the monitor both read it.

    Raising is deliberate when the telemetry cannot be read: a health
    report that answers "fine" because its own datastore was unreachable is
    worse than no report, and is the exact failure this module was built to
    make impossible elsewhere.
    """
    baseline_start, recent_start, now = telemetry_db.default_windows(hours, baseline_days)
    recent = await telemetry_db.window(recent_start, now)
    baseline = await telemetry_db.window(baseline_start, recent_start)
    recent_attrs = await telemetry_db.schema_window(recent_start, now)
    baseline_attrs = await telemetry_db.schema_window(baseline_start, recent_start)
    drift = _attribute_drift(recent_attrs, baseline_attrs)
    drift_by_platform: dict[str, list[dict[str, Any]]] = {}
    for d in drift:
        drift_by_platform.setdefault(d["platform"], []).append(d)

    # One client in the whole deployment cannot satisfy MIN_CLIENTS, and
    # refusing to ever give a verdict there would make this useless for a
    # single-tenant install. The cross-client check is the stronger signal
    # where it is available, not a precondition for having any signal at
    # all -- so it is dropped only when no window has ever seen more than
    # one client, which is a property of the deployment rather than of a
    # bad week.
    single_client = max(
        (v.get("clients", 0) for v in (*recent.values(), *baseline.values())),
        default=0,
    ) <= 1

    platforms: dict[str, Any] = {}
    for pid in sorted(set(recent) | set(baseline)):
        blank = {"searches": 0, "clients": 0, "hits": 0, "sweeps": 0,
                 "empty_sweeps": 0, "sources": {}}
        r = recent.get(pid, blank)
        b = baseline.get(pid, blank)
        state, why = _verdict(r, b, single_client)
        # A NAMED ATTRIBUTE BEATS EVERY STATISTICAL VERDICT. The yield
        # comparison is an inference; a key that matched 2,400 times last
        # week and zero times this week is direct evidence, and it can be
        # true while the yield still looks acceptable -- X renaming
        # `screen_name` breaks every handle without moving the result count
        # at all. So this overrides `unknown` and `healthy` both, and never
        # downgrades a verdict that was already worse.
        if (broken_attrs := drift_by_platform.get(pid)):
            state = BROKEN
            named = ", ".join(f"{d['attribute']} ({d['now']}, was {d['before']})"
                              for d in broken_attrs[:3])
            why = f"attribute(s) we target stopped matching: {named}"
        platforms[pid] = {
            "platform": pid,
            "state": state,
            "detail": why,
            "broken_attributes": drift_by_platform.get(pid, []),
            "searches": r.get("searches", 0),
            "clients": r.get("clients", 0),
            "hits": r.get("hits", 0),
            # Both per sweep, so the two are directly comparable by eye --
            # which is the whole reason the verdict above can compare them.
            "yield": round(_rate(r.get("hits", 0), r.get("sweeps", 0)), 2),
            "baseline_yield": round(_rate(b.get("hits", 0), b.get("sweeps", 0)), 2),
            "empty_rate": round(_rate(r.get("empty_sweeps", 0), r.get("sweeps", 0)), 3),
            "source": _modal_source(r.get("sources", {})),
            "baseline_source": _modal_source(b.get("sources", {})),
            "baseline_searches": b.get("searches", 0),
        }

    worst = UNKNOWN
    for rank in (BROKEN, DEGRADED, HEALTHY):
        if any(p["state"] == rank for p in platforms.values()):
            worst = rank
            break
    return {
        "state": worst,
        "window_hours": hours,
        "baseline_days": baseline_days,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "platforms": list(platforms.values()),
    }


async def check_once(hours: int = 24, baseline_days: int = 7) -> dict[str, Any]:
    """Build the report and raise an incident for anything not healthy.

    Called by the session monitor's own sweep rather than on its own timer:
    that loop already exists, already survives its own exceptions, and
    already runs on the cadence this needs. A second background task would
    be a second thing to supervise for no gain.
    """
    try:
        report = await build_report(hours, baseline_days)
    except Exception as e:
        log.error(f"engine health check could not run: {type(e).__name__}: {e}")
        return {"state": UNKNOWN, "platforms": [], "error": str(e)}

    for p in report["platforms"]:
        if p["state"] in (BROKEN, DEGRADED):
            await _raise(p)
    return report


def _cause_for(p: dict[str, Any]) -> str:
    """Why this platform is in trouble, in the words that start the fix.

    When the probe named an attribute, that IS the cause and there is no
    need to speculate -- which is the whole point of instrumenting the
    parse. The statistical wording is the fallback for a collapse nobody
    could pin to a key.
    """
    if attrs := p.get("broken_attributes"):
        names = ", ".join(a["attribute"] for a in attrs)
        return (
            f"The platform renamed, moved or removed the attribute(s) this engine "
            f"reads: {names}. They matched normally until recently and now do not, "
            f"which is a schema change on their side rather than a run of empty "
            f"searches. Sweeps keep reporting clean because a result we cannot parse "
            f"is indistinguishable from a result that was never there."
        )
    return (
        "The platform has most likely changed its search response or page "
        "structure, so the extraction chain no longer matches it. Sweeps keep "
        "reporting clean because 'no results' is indistinguishable from "
        "'nobody is impersonating this client'."
    )


def _fix_for(p: dict[str, Any], pid: str) -> str:
    """Where to go, as specifically as the evidence allows."""
    if attrs := p.get("broken_attributes"):
        first = attrs[0]["attribute"]
        where = _ATTR_HOMES.get(pid, "that platform's discovery_engine.py")
        return (
            f"Capture one live search response from {pid} and find what {first!r} "
            f"is called now. The read is in {where}. The probe key names the path "
            f"as the parser walks it, so it maps straight onto the code."
        )
    return (
        f"Search the logs for 'search[' on {pid} -- shared/extraction.py records "
        f"which strategy failed, and the exact file, line and source text to "
        f"change. Fix the primary strategy; the fallback is not a resting place."
    )


# Where each platform's targeted attributes are actually read, so an alert
# points at a function rather than at a 1,900-line file.
_ATTR_HOMES = {
    "facebook": "backend/platforms/facebook/discovery_engine.py -- iter_results() "
                "for result fields, page_state() for pagination",
    "twitter": "backend/platforms/twitter/discovery_engine.py -- _user_from_result() "
               "for user fields, search_state() for pagination",
    "instagram": "backend/platforms/instagram/discovery_engine.py -- user_from_node() "
                 "for user fields, iter_mobile_search_users() for the container",
    "tiktok": "backend/platforms/tiktok/discovery_engine.py -- user_from_node() for "
              "user fields, iter_users() for the account/author walk",
    "youtube": "backend/platforms/youtube/discovery_engine.py -- sweep(), where the "
               "v3 search items are read",
    "telegram": "backend/platforms/telegram/discovery_engine.py -- entity_from(). "
                "Telethon objects rather than a JSON payload, so check whether the "
                "client library changed before suspecting Telegram did",
}


async def _raise(p: dict[str, Any]) -> None:
    """One platform's verdict, into the incident log and the operator's
    inbox -- through exactly the plumbing session failures already use, so
    this shows up where somebody is already looking rather than in a new
    place they have to learn about."""
    pid = p["platform"]
    # Keyed on the ATTRIBUTES too, not just the state: a second field
    # breaking a day later is new information and must not be swallowed by
    # the cooldown that the first one started.
    attr_key = ",".join(a["attribute"] for a in p.get("broken_attributes", []))
    key = f"{pid}:{p['state']}:{attr_key}"
    now = _now()
    if now - _recent_alerts.get(key, 0.0) < _ALERT_COOLDOWN_SEC:
        return
    _recent_alerts[key] = now

    broken = p["state"] == BROKEN
    log.error(
        f"engine health: {pid} is {p['state'].upper()} -- {p['detail']}"
        if broken else
        f"engine health: {pid} is degraded -- {p['detail']}")
    incident = {
        "platform": pid,
        "kind": "engine_health",
        "scope": "-- all clients --",
        "job_id": "parser-drift-canary",
        "error_type": "ExtractionBroken" if broken else "ExtractionDegraded",
        "severity": "critical" if broken else "warning",
        "message": (
            f"Discovery on {pid.capitalize()} looks {p['state']}: {p['detail']}. "
            f"Sweeps are still completing and reporting success, so nothing else "
            f"will flag this."
        ),
        "cause": _cause_for(p),
        "fix": _fix_for(p, pid),
        "ts": datetime.now(timezone.utc),
    }
    await incidents_db.record(incident)

    # DEGRADATION IS RECORDED BUT NOT MAILED. It is the early warning, not
    # the outage -- results are still coming -- and mailing every one is how
    # an operator learns to filter this sender, which would cost the alert
    # that actually matters. It shows up in the incident log and on
    # GET /alerts/engine/status, where someone is already looking.
    if not broken:
        return
    try:
        from backend.services import email_service
        # The generic incident mailer, which already reads
        # `alert_on_critical_incident` and renders exactly this dict --
        # a bespoke template here would be a second thing to keep in step
        # with the incident shape for no gain.
        asyncio.create_task(email_service.send_critical_incident_alert(incident))
    except Exception as e:                       # noqa: BLE001 - never fatal
        log.warning(f"engine health alert email skipped: {type(e).__name__}: {e}")
