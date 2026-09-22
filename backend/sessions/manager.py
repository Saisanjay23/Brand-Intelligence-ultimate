"""Session pool lifecycle: paste, launch-and-log-in, rotate, quarantine,
and the periodic background health sweep that notices a session has gone
bad before a job finds out the hard way.

ROTATION, not failover: `get_healthy_session` always hands back the
least-recently-used available session, not the first ready one, handing
back #1 every time would drive every request through it until the
platform bans it, then #2, and so on. Spreading load evenly across the
pool is the entire reason a pool exists.
"""

from __future__ import annotations

import asyncio
import json
import random
import weakref
from datetime import datetime, timezone
from typing import NamedTuple, Optional

from backend.config.settings import settings
from backend.database.repositories import session_repository as sessions_db
from backend.sessions.cookies import load_cookies, normalize_cookies
from backend.shared import fast_http
from backend.shared.errors import ConflictError, NotFoundError, ValidationError
from backend.shared.logging import get_logger

log = get_logger("sessions.manager")

DEAD_STATES = {"expired", "checkpointed", "unreadable"}

# Not dead, but not usable either: `save_credentials` writes a placeholder
# row the moment someone submits a username/password and only fills its
# cookies in when the background auto-login finishes seconds-to-minutes
# later. That row is empty in between, and it used to read as available,
# with `last_used` of 0.0 it then sorted FIRST in get_healthy_session's
# least-recently-used pick, so a job starting during that window was
# preferentially handed an empty credential, ran logged out, failed, and
# quarantined an account that was never actually broken.
PENDING_STATES = {"running_login"}

# Background tasks this module starts and nobody awaits. HELD ON PURPOSE:
# asyncio keeps only a WEAK reference to a running task, so a bare
# create_task can be collected mid-flight and the work simply stops, with
# no error anywhere -- a re-login that vanishes between "starting" and any
# outcome at all. Discarding on completion keeps the set from growing.
_background: set = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# where a manual login starts, and the cookie that proves it worked
LOGIN_FLOW = {
    "facebook": ("https://www.facebook.com/login", "c_user"),
    "twitter": ("https://x.com/login", "auth_token"),
    "instagram": ("https://www.instagram.com/accounts/login/", "sessionid"),
}

CHECK_INTERVAL_S = 30 * 60  # generous on purpose, this opens a real browser

# How long a session that a REAL JOB proved healthy is trusted without a
# synthetic probe.
#
# Every probe navigates to an authenticated-only page (/me,
# /accounts/edit/, /home) in a real browser. At one pooled account per
# platform -- which is what the pool actually holds today -- the most
# overdue session is the same session every sweep, so that account was
# visiting its own settings page 48 times a day, on a perfectly regular
# 30-minute cadence, whether or not anything else was happening. Both the
# volume and the metronome regularity are the sort of thing these
# platforms score against an account.
#
# A job that completed and called mark_session_ok is STRONGER evidence
# than the probe -- it exercised the real surface rather than one login
# wall -- so within this window the probe adds risk and no information.
# An idle session is unaffected and still checked on the normal cadence.
PROVEN_FRESH_S = 90 * 60

# Fraction of CHECK_INTERVAL_S to jitter each sleep by, so the sweep does
# not land on the same wall-clock offset forever.
CHECK_JITTER = 0.2
BATCH_SIZE = 5  # sessions live-checked per platform per monitor sweep
# youtube included since 2026-08, previously excluded entirely (a dead API
# key sat unnoticed until a real job hit it); see _verify_credential_item.
MONITORED = ("facebook", "instagram", "twitter", "telegram", "tiktok", "youtube")

# How long a dead (expired/checkpointed/unreadable) pool entry stays around,
# visible and highlighted inactive so the user can delete or rewrite it,
# before it's purged on its own. Long enough that a SessionInvalid incident
# has time to reach someone; short enough that a pool doesn't quietly fill
# up with rows nobody will ever revisit.
DEAD_GRACE_DAYS = 7

_logins: dict[str, dict] = {}  # platform -> LoginRun-shaped dict
_monitor_task: Optional[asyncio.Task] = None


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _is_available(item: dict, now: float) -> bool:
    if item["status"] in DEAD_STATES or item["status"] in PENDING_STATES:
        return False
    return item["rate_limited_until"] <= now


def pool_summary_of(items: list[dict], now: float) -> dict:
    return {
        "total": len(items),
        "available": sum(1 for s in items if _is_available(s, now)),
        "dead": sum(1 for s in items if s["status"] in DEAD_STATES),
    }



def _get_platform(platform_id: str):
    from backend.platforms import registry

    try:
        return registry.get(platform_id)
    except KeyError:
        raise NotFoundError(f"unknown platform {platform_id!r}") from None


def _session_in_use(platform_id: str, session_id: str) -> bool:
    """Is something actually holding this exact session RIGHT NOW -- the
    live answer, not "was picked at some point".

    NOT cosmetic. Three callers act on it, and two of them open a browser:

      _pick_batch   the background health monitor skips a session a job is
                    driving. A probe is a second Playwright context on one
                    account from one IP, which this module's own comments
                    call the single most reliable way to earn a checkpoint.
      check_item    the analyst's per-session "Check" button, refused for
                    the same reason.
      _public       the "currently running" marker in the Sessions panel.

    ASKS BOTH RUNNERS. Discovery and analysis each own a SEPARATE JobStore
    with its own `_sessions_in_use` set (see get_healthy_session's note on
    why the cross-runner claim lives in `_claims` instead), so asking only
    analysis reported every session a DISCOVERY sweep was holding as idle.
    A Facebook sweep holds its session for the whole run, and the monitor
    wakes every 30 minutes, so the two overlapped routinely: the monitor
    opened a second Chrome on an account mid-sweep, Facebook challenged it,
    and the session was marked checkpointed -- after which analysis
    correctly reported no healthy session for a pool the analyst had just
    watched working. The visible symptom was always the LAST link in that
    chain, which is why it read as an analysis bug.

    Imported lazily and guarded per runner: a missing or half-built runner
    must never break the Sessions panel. (It did exactly that once -- this
    used to reach into a job module that had been deleted, and every
    platform's session status came back as an error.) Guarded SEPARATELY so
    one broken import cannot silently answer "idle" on behalf of the other
    runner, which is the failure this function exists to prevent.

    THE CLAIM IS ASKED FIRST, BECAUSE IT HAPPENS FIRST. `hold_session` is
    recorded inside a worker, after that worker has waited out the
    process-wide slot semaphore and its own start stagger; the CLAIM is
    taken well before that, when `_claim_sessions` reserves the accounts
    for the whole platform up front. Between the two this function used to
    answer "idle" for an account a job had already reserved and was about
    to open a browser on -- and that gap is not brief: a platform claims
    every account it wants at once, then starts its workers one at a time
    behind a global ceiling, so the last of them can sit claimed-but-not-
    yet-held for minutes.

    Which is exactly long enough for the 30-minute monitor to land in it.
    The consequence is the one every comment in this module warns about: a
    second Playwright context on one account from one IP, a challenge, and
    a session marked checkpointed while the job that reserved it was still
    waiting to start. Reading the claim closes the window at its real
    start. Safe to trust now that a claim is a lease (see `_Claim`): a
    stale one cannot pin a session out of the monitor's reach for ever,
    because a lease whose holder is gone is not a lease."""
    if _is_claimed(platform_id, session_id, _now()):
        return True
    try:
        from backend.analysis.runner import analysis_runner
        if analysis_runner.holds_session(platform_id, session_id):
            return True
    except Exception:
        pass
    try:
        from backend.discovery.runner import discovery_runner
        if discovery_runner.holds_session(platform_id, session_id):
            return True
    except Exception:
        pass
    return False


def _required_cookie_expiry(s: dict, required: tuple[str, ...]) -> float:
    """When the SOONEST of this entry's login cookies lapses, as epoch
    seconds (0 when unknown or not cookie-based).

    Soonest, not latest: the login dies with the first required cookie to
    go, so the earliest one is the real deadline. `status()` reports a
    platform-wide date computed the other way for its own separate banner;
    this is the per-account number, which is what someone deciding which
    account to re-login next actually needs.
    """
    soonest = 0.0
    for c in s.get("cookies") or []:
        if c.get("name") not in required:
            continue
        expires = c.get("expires")
        if not isinstance(expires, (int, float)) or expires <= 0:
            continue
        soonest = expires if not soonest else min(soonest, expires)
    return float(soonest)


def _public(s: dict, plat: object = None, health: Optional[dict] = None) -> dict:
    """One pool entry as an API caller may see it, never the cookie
    values. A session cookie IS the credential.

    `plat`/`health` are optional because the shape has to stay renderable
    from the pool document alone; when they are supplied the row also
    carries the last live-check verdict and this account's own cookie
    deadline, which is what turns a red dot into something an operator can
    act on without going to the logs.
    """
    # A verdict recorded before these credentials were pasted describes the
    # ones they replaced, same rule `status()` applies to the pool-level
    # cache, applied per row.
    checked_at = (health or {}).get("checked_at")
    fresh = health is not None and not _health_predates_credentials([s], checked_at)
    # The stored `last_error` (an auto-login that failed) outlives a single
    # check and is the fallback when no fresh verdict exists.
    last_error = s.get("last_error", "")
    if fresh and not health.get("ok", True):
        last_error = health.get("detail", "") or last_error

    extra: dict = {}
    if plat is not None or health is not None:
        extra = {
            "last_error": last_error,
            "last_checked": _as_iso(checked_at) if fresh else "",
            "last_check_ok": bool(health.get("ok")) if fresh else None,
            "expires_at": _required_cookie_expiry(s, getattr(plat, "required_cookies", ()) or ()),
        }
    # HOW THIS ACCOUNT AUTHENTICATES, never WHAT WITH. The values -- a
    # password, a TOTP secret, a cookie -- are credentials and none of them
    # appear here; only the presence of stored credentials does, which is
    # what the Sessions panel needs to show a self-healing badge and to
    # decide whether "Re-Login Now" is offered at all.
    _platform_id = s.get("platform", "")
    _can_login = bool(s.get("username")) and bool(s.get("password"))
    _recovery = relogin_state(s)
    # DOES THIS ACCOUNT HAVE A BROWSER OF ITS OWN YET? Persistent profiles
    # are otherwise entirely invisible: they change how the platform sees
    # the account and leave no trace anywhere an operator looks. One stat
    # per row per poll, against a handful of rows.
    _has_profile = False
    if _platform_id and s.get("id"):
        try:
            from backend.stealth.browser import profile_dir_for

            _has_profile = profile_dir_for(_platform_id, s["id"]).is_dir()
        except Exception:                             # noqa: BLE001 - cosmetic
            _has_profile = False
    return {**extra,
        "has_browser_profile": _has_profile,
        "auth_kind": ("auto-login" if _can_login
                      else "api-key" if s.get("api_key") else "cookies"),
        "can_relogin": _can_login and _platform_id in LOGIN_FLOW,
        "relogin_running": _recovery["running"],
        "relogin_attempts": _recovery["attempts"],
        # SO THE PANEL CAN SAY WHETHER SELF-HEALING HAS EVER WORKED HERE.
        # "3 failed attempts" reads completely differently on an account
        # that has recovered itself eleven times before than on one that
        # has never once managed it, and the row could not tell you which
        # it was.
        #
        # Epoch seconds, 0 for never -- the same shape as `last_used` and
        # `rate_limited_until` beside them, which the panel already renders
        # with `new Date(x * 1000)`. (_as_iso is for the health cache's
        # datetimes and would raise on a float.)
        "relogin_last_attempt": _recovery["last_attempt"],
        "relogin_last_success": _recovery["last_success"],
        "relogin_total_successes": _recovery["total_successes"],
        "id": s["id"], "identifier": s["identifier"], "status": s["status"],
        "rate_limited_until": s["rate_limited_until"], "last_used": s["last_used"],
        "use_count": s.get("use_count", 0),
        "in_use": _session_in_use(s.get("platform", ""), s["id"]),
        "cookie_count": len(s.get("cookies", []) or []),
        "is_api_key": bool(s.get("api_key")),
        # so the Sessions panel can show "cooling off, 3rd consecutive
        # failure" instead of a bare red dot with no sense of whether this
        # is a blip or a burned account
        "consecutive_failures": s.get("consecutive_failures", 0),
        "available": _is_available(s, _now()),
        # 0 while healthy/cooling-off; once genuinely dead, when it went
        # dead, so the Sessions panel can show "inactive, auto-removed in
        # ~Nd" instead of an inactive row with no sense of how long it's
        # been sitting there unfixed.
        "dead_since": s.get("dead_since", 0.0),
        "purge_in_days": (
            round(DEAD_GRACE_DAYS - (_now() - s["dead_since"]) / 86400, 1)
            if s.get("dead_since") else None
        ),
    }


# ---------- state ----------

async def state_for(platform_id: str) -> str:
    """ready | missing | incomplete, called by platforms.registry."""
    import os
    p = _get_platform(platform_id)
    if p.uses_api_key:
        items = await sessions_db.list_pool(platform_id)
        if items and any(s.get("api_key") for s in items):
            has_key_item = False
            for it in items:
                if it.get("api_key"):
                    has_key_item = True
                    if _is_available(it, _now()):
                        os.environ[p.api_key_env] = str(it["api_key"])
                        return "ready"
            if has_key_item:
                return "exhausted"
        if os.environ.get(p.api_key_env):
            return "ready"
        return "missing"
    if p.env_keys:
        from backend.config.settings import settings
        session_path = settings.session_blob_path / "telegram.session"
        if all(os.environ.get(k) for k in p.env_keys) and session_path.exists():
            return "ready"
        items = await sessions_db.list_pool(platform_id)
        now = _now()
        # Prefer a pooled account that is actually usable right now; fall
        # back to the first complete one so a quarantined-but-real account
        # still reads as "incomplete" rather than the more alarming
        # "missing", same distinction cookie platforms make just below.
        item = next(
            (it for it in items if it.get("api_id") and it.get("api_hash") and _is_available(it, now)),
            next((it for it in items if it.get("api_id") and it.get("api_hash")), None),
        )
        if item is not None:
            if not all(os.environ.get(k) for k in p.env_keys):
                os.environ["TELEGRAM_API_ID"] = str(item.get("api_id", ""))
                os.environ["TELEGRAM_API_HASH"] = str(item.get("api_hash", ""))
            if item.get("phone"):
                os.environ["TELEGRAM_PHONE"] = str(item.get("phone", ""))
            if item.get("session_blob") and not session_path.exists():
                settings.session_blob_path.mkdir(parents=True, exist_ok=True)
                session_path.write_bytes(item["session_blob"])
            if all(os.environ.get(k) for k in p.env_keys) and session_path.exists():
                return "ready"
        return "incomplete" if items else "missing"
    items = await sessions_db.list_pool(platform_id)
    if not items:
        return "missing"

    # "ready" has to mean "a job started right now could actually run", and
    # that requires ONE session that is both complete and currently usable.
    #
    # This previously unioned cookie NAMES across the whole pool and never
    # looked at status or rate_limited_until at all, so:
    #   - twenty quarantined/checkpointed sessions still reported "ready",
    #   - two half-broken sessions could jointly satisfy required_cookies
    #     when neither one could log in on its own.
    # Everything downstream trusts this: discovery decides which platforms
    # to sweep, the scheduler's catch-up decides whether to queue analysis
    # (and re-queued a doomed job every 20 minutes on a dead pool), and
    # both health surfaces render it as a green light.
    required = set(p.required_cookies)
    now = _now()
    complete_and_available = False
    complete_but_unavailable = False
    for item in items:
        names = {c["name"] for c in (item.get("cookies") or []) if c.get("name")}
        if not required <= names:
            continue
        if _is_available(item, now):
            complete_and_available = True
            break
        complete_but_unavailable = True

    if complete_and_available:
        return "ready"
    if complete_but_unavailable:
        # a real, fully-formed session exists, it is just quarantined or
        # cooling off. Distinct from "incomplete" (a botched cookie export),
        # because the fix is different: wait, or add another account.
        return "exhausted"
    return "incomplete"


def _as_epoch(value) -> float:
    """Mongo hands back naive-but-UTC-valued datetimes (see
    database/connection.py's module docstring), read one as UTC rather
    than as this machine's local time, which in a +05:30 deployment would
    make every stored check look 5.5 hours into the future."""
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _as_iso(value) -> str:
    """A stored check timestamp as an unambiguous, tz-aware ISO string:
    the frontend parses these with `new Date()`, which reads a bare naive
    ISO string as LOCAL time and would show every check hours off in any
    non-UTC deployment."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _health_predates_credentials(items: list[dict], checked_at) -> bool:
    """Was this cached health result recorded BEFORE the credentials it
    claims to describe were last rewritten?

    The health cache is only refreshed by the 30-minute background sweep, so
    for up to half an hour after someone re-pastes cookies it still holds
    the verdict on the dead ones. `status()` uses that verdict to override a
    live-computed `ready` with `checkpointed`, which is why a repaired
    session read as fixed in the pool list and simultaneously unusable in
    the header count and the platform rail. A result older than the
    credentials it measured proves nothing about them, so it's ignored.
    """
    checked = _as_epoch(checked_at)
    if not checked:
        return True
    return any(float(s.get("credentials_updated_at") or 0) > checked for s in items)


async def status(platform_id: str, live_health: Optional[dict] = None) -> dict:
    from backend.platforms import registry

    p = _get_platform(platform_id)
    items = await sessions_db.list_pool(platform_id)
    item_health = await sessions_db.cached_item_health(platform_id)
    now = _now()

    out: dict = {
        "platform": p.id, "name": p.name,
        "state": await registry.session_state(p),
        "kind": "api-key" if p.uses_api_key else "mtproto" if p.env_keys else "cookies",
        "can_login": p.id in LOGIN_FLOW,
        "cookie_count": sum(len(s.get("cookies", []) or []) for s in items),
        "sessions": [_public(s, p, item_health.get(s["id"])) for s in items],
        "pool_total": len(items),
        "pool_ready": sum(1 for s in items if _is_available(s, now)),
        "expires": "", "message": "", "last_verified": "",
    }

    if out["kind"] == "cookies" and items:
        soonest = None
        for s in items:
            for c in s["cookies"]:
                if c.get("name") in p.required_cookies and isinstance(c.get("expires"), int) and c["expires"] > 0:
                    soonest = c["expires"] if soonest is None else max(soonest, c["expires"])
        if soonest is not None:
            out["expires"] = datetime.fromtimestamp(soonest, timezone.utc).date().isoformat()
            if soonest <= now:
                out["state"] = "expired"

    health = (live_health or {}).get(p.id)
    if health is not None and not _health_predates_credentials(items, health.get("checked_at")):
        out["last_verified"] = _as_iso(health.get("checked_at"))
        if out["state"] == "ready" and not health.get("ok", True):
            out["state"] = "checkpointed"
            out["message"] = health.get("detail", "")

    if run := _logins.get(p.id):
        out["login"] = run
    return out


def _pick_least_recently_used(available: list[dict]) -> Optional[dict]:
    if not available:
        return None
    return sorted(available, key=lambda s: s["last_used"])[0]


# Guards the read-pick-write below against two callers picking the SAME
# session before either has written anything back. `_is_available()` reads
# only `status`/`rate_limited_until` -- claiming a session doesn't change
# either of those, so a lock around the read+pick alone wouldn't stop a
# second caller from immediately re-picking the one just claimed; `_claims`
# is the actual exclusion signal, consulted (and updated) while still
# holding the lock.
#
# Deliberately NOT `backend/shared/job_store.py`'s existing hold-tracking:
# each of discovery_runner/analysis_runner owns its OWN JobStore instance
# with its OWN `_sessions_in_use` set, so a discovery job and an analysis
# job (or two discovery jobs) can't see each other's holds there -- this is
# the one module every caller of session_for_job actually shares, so it's
# the only place a cross-runner claim can live. Confirmed live/reproducible
# before this fix: two concurrent jobs on the same platform could both be
# handed the same account's cookies and open two browser contexts on one
# IP at once -- this codebase's own health-monitor comments call exactly
# that scenario the single most reliable way to earn a checkpoint.
_claim_lock = asyncio.Lock()


# A CLAIM IS A LEASE, NOT A FLAG -- because the release is not guaranteed.
#
# THE BUG THIS FIXES. The claim used to be a bare set, added to here and
# removed from only by `release_claim` in a caller's `finally`. Every entry
# therefore depended on its holder unwinding cleanly, and the thing that
# most often stops a holder unwinding cleanly is the analyst pressing Stop:
# `JobStore.cancel` hard-cancels the job's task once the five-second grace
# period is up, and a task cancelled at an `await` that sits BEFORE its own
# try/finally never runs that `finally` at all. Both runners have such an
# await -- the process-wide worker-slot semaphore, discovery's per-worker
# start stagger, and the up-to-five-minute busy wait inside
# `_claim_sessions` itself.
#
# The leak was silent and permanent. The account stayed in the set for the
# life of the PROCESS; `_is_available` still said it was fine, the Sessions
# panel still showed it ready, and every later job was refused with "no
# healthy sessions available". Stop a sweep and start it again and the
# platform could no longer run, on cookies that were never the problem --
# with a restart of the backend as the only way out.
#
# So a claim now records WHO holds it. The holder is the task that took it
# -- in both runners the per-platform coroutine, which outlives every
# worker it hands a session to -- and a claim whose holder is done
# (finished, failed, or cancelled) is not a claim any more. Stopping a job
# frees its accounts as a consequence of its task ending, with nothing left
# for a `finally` to remember to do.
#
# `_CLAIM_MAX_S` is a second backstop, for a claim taken outside any task
# or held by one that somehow never ends. It is deliberately far longer
# than any real sweep or batch: expiring a claim that is still genuinely in
# use would hand one account to two jobs at once and open two browser
# contexts on one IP, which this module's own health notes call the single
# most reliable way to earn a checkpoint. Reclaiming late is cheap;
# reclaiming early is the failure being avoided.
_CLAIM_MAX_S = 6 * 60 * 60


class _Claim(NamedTuple):
    owner: Optional["weakref.ReferenceType"]  # the task holding it, weakly
    taken_at: float


_claims: dict[tuple[str, str], _Claim] = {}


def _owner_ref() -> Optional["weakref.ReferenceType"]:
    """A weak handle on the task doing the claiming, or None if there isn't
    one. Weak so a claim that outlives its holder can never be the reason
    that holder's task object stays in memory."""
    try:
        task = asyncio.current_task()
    except RuntimeError:                     # no running loop
        return None
    return weakref.ref(task) if task is not None else None


def _claim_live(claim: _Claim, now: float) -> bool:
    """Is this lease still held by something that exists and is running?"""
    if now - claim.taken_at > _CLAIM_MAX_S:
        return False
    if claim.owner is None:
        # Taken outside a task, so there is no holder whose ending could
        # free it; only an explicit release or the ceiling above ends it.
        return True
    task = claim.owner()
    # A collected task is a finished task -- asyncio drops its own strong
    # reference once a task completes.
    return task is not None and not task.done()


def _is_claimed(platform_id: str, session_id: str, now: float) -> bool:
    """Claimed by a LIVE holder. Reaps the lease when it is not, so a
    stopped job's accounts come back on the very next look rather than
    needing the backend restarted."""
    key = (platform_id, session_id)
    claim = _claims.get(key)
    if claim is None:
        return False
    if _claim_live(claim, now):
        return True
    del _claims[key]
    log.info(f"{platform_id}/{session_id}: reclaimed -- the job holding this "
             "session stopped without releasing it")
    return False


# How long a job will WAIT for a busy pool before giving up, and how often
# it re-checks while waiting.
#
# WHY WAITING AT ALL. Discovery holds a session for the whole of its sweep
# and analysis for the whole of its batch, so on a platform with one healthy
# account the second of the two used to fail instantly with "no healthy
# sessions available" -- an error, for a pool that was working perfectly and
# would have been free shortly. That made "run discovery and analysis at the
# same time" something an analyst could not do rather than something the
# system sequenced for them.
#
# Waiting is only ever right for a BUSY pool. A pool that is empty, dead or
# rate-limited will not become available by being waited on, so those still
# fail immediately with the reason that actually applies (see
# `unavailable_reason`) -- a job that hangs for five minutes and then reports
# "expired cookies" is worse than one that says so at once.
#
# Polled rather than signalled on purpose: an asyncio primitive binds to the
# loop that first awaits it, and a module-level one outlives any single
# loop -- the exact trap discovery/runner.py's `_worker_semaphore` documents.
# A one-second granularity is invisible against a job measured in minutes.
SESSION_WAIT_S = 300.0
_SESSION_POLL_S = 1.0


async def _busy_only(platform_id: str) -> bool:
    """Is every otherwise-usable account merely CLAIMED right now?

    True means waiting can succeed. False means the pool is blocked by
    something waiting cannot fix (nothing saved, all dead, all
    rate-limited), so the caller should fail now and say why.
    """
    items = await sessions_db.list_pool(platform_id)
    now = _now()
    return any(
        _is_available(s, now) and _is_claimed(platform_id, s["id"], now)
        for s in items
    )


async def get_healthy_session(
    platform_id: str, *, wait_s: float = 0.0,
) -> Optional[dict]:
    """The least-recently-used available account, claimed for the caller.

    With `wait_s`, a pool whose accounts are all BUSY is waited on for up to
    that long rather than refused -- which is what lets a discovery sweep and
    an analysis batch both run against a single-account platform, one after
    the other, instead of whichever started second failing outright.
    """
    deadline = _now() + max(0.0, wait_s)
    while True:
        item = await _claim_one(platform_id)
        if item is not None:
            return item
        if _now() >= deadline or not await _busy_only(platform_id):
            return None
        await asyncio.sleep(_SESSION_POLL_S)


async def _claim_one(platform_id: str) -> Optional[dict]:
    """One attempt at the read-pick-claim-write above, with no waiting."""
    async with _claim_lock:
        items = await sessions_db.list_pool(platform_id)
        claimed_at = _now()
        available = [
            s for s in items
            if _is_available(s, claimed_at)
            and not _is_claimed(platform_id, s["id"], claimed_at)
        ]
        chosen = _pick_least_recently_used(available)
        if chosen is None:
            return None
        _claims[(platform_id, chosen["id"])] = _Claim(_owner_ref(), claimed_at)
    now = _now()
    await sessions_db.update_item(platform_id, chosen["id"], status="ready", rate_limited_until=0.0, last_used=now)
    # a real, durable count of how many times this session has actually
    # been handed to a job, not a health check, an atomic $inc so two
    # round-robin slots picking sessions concurrently can't drop one
    # another's count
    use_count = await sessions_db.increment_use_count(platform_id, chosen["id"])
    chosen["status"], chosen["rate_limited_until"], chosen["last_used"], chosen["use_count"] = (
        "ready", 0.0, now, use_count,
    )
    return chosen


async def unavailable_reason(platform_id: str) -> str:
    """Why `get_healthy_session` just returned None, in words an analyst can
    act on. Appended to every "cannot run this platform" error.

    THE MESSAGE WAS THE BUG. All three refusals said the same thing --
    "please add more cookies" -- for four unrelated situations, and only one
    of them is fixed by adding cookies. The one an analyst hits most is a
    healthy session that is simply BUSY on another job, and being told to
    add cookies for it means re-pasting credentials that were never the
    problem, or concluding the pool is broken while looking at a session the
    Sessions panel reports as ready. "unable to run even though healthy
    sessions are present" is that message, not that state.

    Each entry lands in exactly one bucket, in the order
    `get_healthy_session` would have rejected it, so the counts add up to
    the pool and cannot double-count one account.
    """
    items = await sessions_db.list_pool(platform_id)
    if not items:
        return "no accounts are saved for this platform yet"

    now = _now()
    dead = busy = limited = pending = free = 0
    soonest_free = 0.0
    for s in items:
        if s["status"] in DEAD_STATES:
            dead += 1
        elif s["status"] in PENDING_STATES:
            pending += 1
        elif s["rate_limited_until"] > now:
            limited += 1
            if not soonest_free or s["rate_limited_until"] < soonest_free:
                soonest_free = s["rate_limited_until"]
        elif _is_claimed(platform_id, s["id"], now):
            busy += 1
        else:
            # Free RIGHT NOW -- so the pick that just failed lost a race
            # with something releasing, rather than finding nothing.
            free += 1

    total = len(items)
    if free:
        return (f"{free} of {total} account(s) came free while this job was starting -- "
                "run it again")
    # BUSY FIRST. It is the only cause that resolves on its own, and the
    # only one where touching the credentials would make things worse.
    if busy:
        why = (f"{busy} of {total} account(s) are already in use by another running job -- "
               "wait for it to finish, or add another account under /sessions")
        if dead:
            why += f" (the other {dead} need re-authenticating)"
        return why
    if limited:
        mins = max(1, int((soonest_free - now) // 60)) if soonest_free else 0
        return (f"{limited} of {total} account(s) are rate-limited"
                + (f" for another ~{mins}m" if mins else "") + " -- try again later")
    if pending:
        return f"{pending} of {total} account(s) are still finishing an interactive login"
    return (f"all {total} account(s) are expired or checkpointed -- "
            "re-authenticate them under /sessions")


def proven_fresh(session_item: dict) -> bool:
    """Has real work proved this session healthy recently enough that a login
    probe would add risk and no information?

    The same PROVEN_FRESH_S window `_pick_batch` uses to skip the background
    monitor's probe, applied to the probe a JOB runs before it starts. Both
    ask the identical question -- "do we already know this works?" -- and it
    was only ever answered in one of the two places.

    A probe is not free. Measured on Facebook: `check_session()` is a real
    authenticated page load costing 12.65s, larger than all three of that
    platform's tab sweeps put together, paid on every job. And it is an
    authenticated request to the platform, on a schedule shaped by how often
    sweeps run, which is exactly the kind of pattern the rest of this module
    works to avoid.

    Skipping it is safe because a stale verdict cannot hide for long: if the
    session has died since, the sweep itself fails, `classify_failure` reads
    the failure and `mark_session_failed` records it -- the same path that
    already handles a session dying MID-sweep, which no upfront probe could
    have prevented either. The probe only ever moved that discovery a few
    seconds earlier, at the cost of a page load every time.
    """
    last_ok = float(session_item.get("last_ok") or 0.0)
    return bool(last_ok) and (_now() - last_ok) < PROVEN_FRESH_S


def release_claim(platform_id: str, session_id: str) -> None:
    """The other half of `get_healthy_session`'s claim -- callers call this
    once they're done with the session (in a `finally`, alongside their own
    JobStore `release_session`), which hands the account back at once rather
    than on the next look.

    NO LONGER THE ONLY WAY A CLAIM ENDS, and deliberately so: a lease whose
    holding task has finished or been cancelled is reaped by `_is_claimed`
    regardless (see `_Claim`), because the cases that leaked were exactly
    the ones where no `finally` ever ran. Callers should still release --
    prompt is better than eventual, and a long-lived task that reuses the
    pool would otherwise hold accounts it has finished with -- but a missed
    release is now a delay, not a broken pool.

    A no-op for a blank id (anonymous/no-session platforms never claimed
    anything to begin with), matching JobStore's own `hold_session`/
    `release_session` convention so callers don't need an extra branch."""
    if session_id:
        _claims.pop((platform_id, session_id), None)


async def session_for_job(
    platform_id: str, *, wait_s: float = 0.0,
) -> tuple[object, dict]:
    """What a discovery/analysis job needs to actually run: the Platform
    metadata plus a healthy pooled session's credentials (cookies for
    cookie-authed platforms, api_key for key-authed ones, or just
    id/identifier for MTProto since its credentials go into os.environ +
    a session file rather than being handed to the caller directly).

    `wait_s` is how long to wait for an account that is merely BUSY, and it
    is what makes discovery and analysis usable at the same time on a
    single-account platform -- see `get_healthy_session`. A pool blocked by
    anything waiting cannot fix still raises immediately, carrying the
    reason that applies."""
    plat = _get_platform(platform_id)
    if plat.env_keys:
        # Telegram: MTProto only ever has ONE local session file open at a
        # time (Telethon's own SQLite lock, see platforms/telegram/
        # discovery_engine.py), so "rotation" here means picking the next
        # available pooled account and swapping ITS credentials/session
        # blob into that one fixed file, not running several accounts
        # concurrently. get_healthy_session is the exact same
        # least-recently-used pick every other pooled platform uses, so a
        # dead/rate-limited account is skipped and the real pooled id
        # flows into mark_session_failed/mark_session_ok downstream instead
        # of the no-op empty dict this used to return.
        item = await get_healthy_session(platform_id, wait_s=wait_s)
        if item is None:
            raise ConflictError(
                f"{platform_id}: {await unavailable_reason(platform_id)}")
        # RELEASE THE CLAIM IF SETUP FAILS. `get_healthy_session` has already
        # marked this session in-use; everything below can still raise (the
        # session-blob write touches the filesystem, so a full or read-only
        # disk is enough). If it does, the caller never receives a
        # `session_item` and so never reaches its `finally` to release --
        # leaving the account claimed for the life of the process, invisible
        # to every future job and reported as "in use" by the sessions API.
        try:
            import os
            os.environ["TELEGRAM_API_ID"] = str(item.get("api_id", ""))
            os.environ["TELEGRAM_API_HASH"] = str(item.get("api_hash", ""))
            if item.get("phone"):
                os.environ["TELEGRAM_PHONE"] = str(item.get("phone", ""))
            if item.get("session_blob"):
                settings.session_blob_path.mkdir(parents=True, exist_ok=True)
                (settings.session_blob_path / "telegram.session").write_bytes(item["session_blob"])
        except Exception:
            release_claim(platform_id, item["id"])
            raise
        return plat, {"id": item["id"], "identifier": item["identifier"]}
    if plat.uses_api_key:
        item = await get_healthy_session(platform_id, wait_s=wait_s)
        if item is None:
            items = await sessions_db.list_pool(platform_id)
            if not items:
                import os
                if os.environ.get(plat.api_key_env):
                    return plat, {"id": "", "identifier": "env", "api_key": os.environ[plat.api_key_env]}
            raise ConflictError(
                f"{platform_id}: {await unavailable_reason(platform_id)}")
        try:
            import os
            os.environ[plat.api_key_env] = str(item.get("api_key", ""))
        except Exception:
            release_claim(platform_id, item["id"])
            raise
        return plat, {"id": item["id"], "identifier": item["identifier"], "api_key": item["api_key"]}
    item = await get_healthy_session(platform_id, wait_s=wait_s)
    if item is None:
        if plat.can_run_anonymously:
            # this platform's search/profile pages work logged-out (see
            # registry.Platform.anonymous_context_path) -- a dead/missing
            # session pool costs it one field, not the whole platform.
            return plat, {"id": "", "identifier": "anonymous", "anonymous": True}
        raise ConflictError(
            f"{platform_id}: {await unavailable_reason(platform_id)}")
    return plat, {"id": item["id"], "identifier": item["identifier"],
                  "cookies": item["cookies"],
                  # When real work last proved this session healthy. Carried
                  # through so a caller can decide whether its own login
                  # probe would tell it anything it does not already know --
                  # see `proven_fresh` below.
                  "last_ok": item.get("last_ok") or 0.0}


async def mark_session_failed(
    platform_id: str, session_id: str, reason: str = "expired",
    rate_limited_until: float = 0, detail: str = "",
) -> None:
    """Quarantine one session, backing off further each consecutive time.

    No-ops for key/MTProto-authed platforms, which have no pool at all
    (session_id is empty there).

    The cooldown is GRADUATED (settings.session_backoff_minutes, default
    15m -> 1h -> 6h -> 24h) and keyed on this session's own consecutive
    failure count, which `get_healthy_session` resets to zero as soon as
    the session is handed out and used successfully. A single rate-limit
    used to cost a flat 24 hours, so one bad afternoon quarantined an
    entire pool at once and left every platform dark until the next day,
    while `state_for` cheerfully kept reporting "ready" and the scheduler
    kept queueing jobs into the void.

    An explicit `rate_limited_until` still wins, for a platform that tells
    us exactly how long to wait (Telegram's FloodWait).

    A transition INTO a genuinely dead state (expired/checkpointed/
    unreadable, as opposed to a merely-cooling-off rate_limited) fires a
    SessionInvalid incident naming this exact session/key, once per
    transition, not once per subsequent failed job that still finds it
    dead, so a quarantined session doesn't spam a notification every time
    something else tries and fails against it. This is the one place that
    notification is raised, so it covers every caller: live job failures
    (analysis_service/discovery_service) and the periodic health monitor
    (_record_item_result) alike, a session dying mid-job is no longer
    silent until the next 30-minute sweep independently rechecks it.
    """
    if not session_id:
        return
    item = await sessions_db.get_item(platform_id, session_id)
    was_dead = bool(item and item.get("status") in DEAD_STATES)
    identifier = (item or {}).get("identifier") or session_id
    fails = int((item or {}).get("consecutive_failures") or 0) + 1
    fields: dict = {"status": reason, "consecutive_failures": fails}

    if rate_limited_until:
        fields["rate_limited_until"] = float(rate_limited_until)
    else:
        ladder = settings.session_backoff_minutes or [15, 60, 360, 1440]
        minutes = ladder[min(fails, len(ladder)) - 1]
        fields["rate_limited_until"] = _now() + minutes * 60
        fields["quarantine_minutes"] = minutes

    newly_dead = reason in DEAD_STATES and not was_dead
    if newly_dead:
        fields["dead_since"] = _now()

    if await sessions_db.update_item(platform_id, session_id, **fields):
        mins = (fields["rate_limited_until"] - _now()) / 60
        log.warning(
            f"{platform_id} session {session_id} marked {reason} "
            f"(failure #{fails}, cooling off ~{mins:.0f}m)"
        )
        if newly_dead:
            # A session dying is the one operational event worth a durable
            # record: it is the thing that silently stops scrapes working,
            # and the pool moves on without it, so nothing else would say
            # so. Written straight to the incidents collection -- there is
            # no diagnosis/alert-routing layer any more, and this must
            # never be able to break the failure-marking above it.
            from datetime import datetime, timezone

            from backend.database.repositories import incident_repository as incidents_db
            note = f": {detail}" if detail else ""
            await incidents_db.record({
                "platform": platform_id, "kind": "session",
                "scope": "-- all clients --", "job_id": "session-failure",
                "error_type": "SessionInvalid", "severity": "critical",
                "message": (
                    f"Session {identifier!r} (id {session_id}) is {reason} and has been taken "
                    f"out of rotation -- the pool moved on to the next available session/key. "
                    f"Delete it or paste fresh credentials to bring it back{note}."
                ),
                "cause": f"The platform rejected this session ({reason}).",
                "fix": "Re-export cookies or re-authenticate this account under Sessions.",
                "ts": datetime.now(timezone.utc),
            })
            from backend.services import email_service
            _spawn(
                email_service.send_session_failure_alert(
                    platform=platform_id,
                    identifier=identifier,
                    session_id=session_id,
                    reason=reason,
                    detail=detail,
                )
            )



async def mark_session_ok(platform_id: str, session_id: str) -> None:
    """Clear a session's quarantine after it demonstrably worked.

    The backoff ladder in `mark_session_failed` only escalates while
    failures are CONSECUTIVE, without this reset the counter would ratchet
    up over a session's whole lifetime and a healthy account would
    eventually sit on a 24h cooldown after four unrelated blips months
    apart. Called on a real read (analysis_service) and on a passing live
    health check (_record_item_result).
    """
    if not session_id:
        return
    await sessions_db.update_item(
        platform_id, session_id,
        status="ready", rate_limited_until=0.0, consecutive_failures=0, dead_since=0.0,
        # When this session was last PROVEN healthy by real work. Read by
        # _pick_batch to skip a synthetic probe that would tell it nothing
        # it does not already know -- see PROVEN_FRESH_S.
        last_ok=_now(),
    )


async def pool_summary(platform_id: str) -> dict:
    _get_platform(platform_id)
    return pool_summary_of(await sessions_db.list_pool(platform_id), _now())


# ---------- write ----------

async def save_cookies(platform_id: str, blob: str, identifier: str = "") -> dict:
    p = _get_platform(platform_id)
    if p.uses_api_key or p.env_keys:
        raise ConflictError(f"{platform_id}: uses credentials in .env, not cookies")
    cookies = load_cookies(blob, p.cookie_domain)
    if not cookies:
        raise ValidationError("no cookies for this platform in that export")
    missing = [n for n in p.required_cookies if n not in {c["name"] for c in cookies}]
    if missing:
        raise ValidationError(f"missing {', '.join(missing)} -- export while logged in")
    try:
        await sessions_db.add_item(platform_id, cookies, identifier)
    except ValueError as e:
        raise ConflictError(str(e)) from e
    return await status(platform_id)


async def refresh_cookies(platform_id: str, session_id: str, cookies: list[dict]) -> bool:
    """Write a session's REFRESHED cookie jar back over the stored one.

    WHY THIS EXISTS
        The pooled jar is loaded into a fresh browser context on every run
        and discarded when that context closes. But these platforms rotate
        their session cookies as you browse -- X reissues `ct0` constantly,
        Instagram rolls `sessionid`/`csrftoken`, Facebook refreshes `xs` --
        so without this the pool keeps replaying an ever-staler jar. The
        stored cookies are not close to expiring (measured 2026-08-23:
        Facebook `xs` +364d, Instagram `sessionid` +361d, X `auth_token`
        +158d), which is exactly why "the session expired" was the wrong
        diagnosis: they were being INVALIDATED for replaying a superseded
        token, not timing out.

    SAFETY
        Only ever an update to an existing pool row, never an insert, and
        only when the incoming jar still carries every cookie the platform
        marks required. A context that got logged out mid-run hands back a
        jar with the auth cookie missing, and writing THAT over a good
        stored one would destroy the session this is meant to preserve.

    Returns True when the stored jar was replaced.
    """
    p = _get_platform(platform_id)
    if p.uses_api_key or p.env_keys:
        return False
    if not session_id or not cookies:
        return False

    kept = normalize_cookies(cookies, p.cookie_domain)
    if not kept:
        return False
    have = {c["name"] for c in kept}
    missing = [n for n in p.required_cookies if n not in have]
    if missing:
        log.info(
            f"{platform_id}/{session_id}: not saving refreshed cookies -- "
            f"missing {', '.join(missing)} (the run ended logged out)"
        )
        return False

    ok = await sessions_db.update_item(
        platform_id, session_id,
        cookies=kept, cookies_updated_at=datetime.now(timezone.utc),
    )
    if ok:
        log.info(f"{platform_id}/{session_id}: stored {len(kept)} refreshed cookie(s)")
    return ok


def cookie_saver(platform_id: str, session_id: str):
    """An `on_cookies` callback bound to one pooled session, for
    stealth/browser.py::Session.stop(). Returns None when there is nothing
    to save back to (an anonymous run has no pool row)."""
    if not session_id:
        return None

    async def _save(cookies: list[dict]) -> None:
        await refresh_cookies(platform_id, session_id, cookies)

    return _save


async def save_credentials(
    platform_id: str,
    identifier: str,
    username: str,
    password: str,
    two_factor_secret: str = "",
) -> dict:
    p = _get_platform(platform_id)
    if p.uses_api_key or p.env_keys:
        raise ConflictError(f"{platform_id}: uses credentials in .env, not username/password")
    if not username or not password:
        raise ValidationError("username and password are required")
        
    # Start auto login in the background
    from backend.stealth.auto_login import run_auto_login
    
    # Pre-emptively save to DB with "checkpointed" status so the UI knows it's doing work
    try:
        item = await sessions_db.add_item(platform_id, [], identifier)
        await sessions_db.update_session_credentials(
            platform_id, item["id"],
            username=username,
            password=password,
            two_factor_secret=two_factor_secret,
        )
        await sessions_db.update_item(platform_id, item["id"], status="running_login")
    except ValueError as e:
        raise ConflictError(str(e)) from e

    async def _do_login():
        try:
            result = await run_auto_login(
                platform_id, username, password, two_factor_secret,
                session_id=item["id"],
            )
            cookies = result.cookies
            if cookies:
                missing = [n for n in p.required_cookies if n not in {c["name"] for c in cookies}]
                if missing:
                    log.error(f"{platform_id}: auto-login got cookies but missed {missing}")
                    await sessions_db.update_item(
                        platform_id, item["id"], status="incomplete",
                        last_error=f"signed in, but {', '.join(missing)} never appeared -- "
                                   "the login probably stopped at a checkpoint or 2FA prompt",
                    )
                else:
                    await sessions_db.update_session_credentials(platform_id, item["id"], cookies=cookies)
            else:
                await sessions_db.update_item(
                    platform_id, item["id"], status="incomplete",
                    last_error="auto-login finished without capturing any cookies",
                )
        except Exception as e:
            log.error(f"{platform_id}: auto-login failed: {e}")
            # the reason goes ON the row, not only into the log, otherwise
            # this row just reads "checkpointed" and the operator has no way
            # to tell a wrong password from a network timeout
            await sessions_db.update_item(
                platform_id, item["id"], status="checkpointed",
                last_error=f"auto-login failed: {type(e).__name__}: {e}",
            )

    # Fire and forget -- but HELD, see _spawn. This one is the whole point
    # of the "Save credentials" button: if the task is collected before the
    # browser finishes, the row sits on "running_login" for ever and no
    # error is ever written anywhere.
    _spawn(_do_login())
    
    return await status(platform_id)


async def save_api_key(platform_id: str, key: str, identifier: str = "") -> dict:
    import os

    p = _get_platform(platform_id)
    if not p.uses_api_key:
        raise ConflictError(f"{platform_id}: does not use an API key")
    key = key.strip()
    if not key:
        raise ValidationError("empty key")
    # Mongo (sessions_db, below) is the durable store, state_for() already
    # re-hydrates os.environ from it on every startup (see its own docstring),
    # so nothing here needs to survive a restart on its own. Setting it
    # in-process only (never written to .env) is what makes this key usable
    # immediately without a restart, without ever touching disk.
    os.environ[p.api_key_env] = key
    try:
        await sessions_db.save_api_key_session(platform_id, key, identifier=identifier or "YouTube API Key")
    except ValueError as e:
        raise ConflictError(str(e)) from e
    log.info(f"{platform_id}: API key saved ({identifier or 'YouTube API Key'})")
    return await status(platform_id)


async def update_session_credentials(
    platform_id: str, session_id: str, blob: str = "", api_key: str = "",
    identifier: Optional[str] = None, username: Optional[str] = None,
    password: Optional[str] = None, two_factor_secret: Optional[str] = None,
) -> dict:
    p = _get_platform(platform_id)
    fields: dict = {}
    if identifier is not None and identifier.strip():
        fields["identifier"] = identifier.strip()
    # LOGIN CREDENTIALS, WHEN SUPPLIED, AND ONLY THEN. None means "not sent",
    # which is different from "", and the difference matters: a form that
    # leaves the password box empty because the operator is only renaming
    # the account must not wipe the password that makes it self-healing.
    # Sending an explicit "" is how you deliberately clear one.
    for _name, _value in (("username", username), ("password", password),
                          ("two_factor_secret", two_factor_secret)):
        if _value is not None:
            fields[_name] = _value.strip()
    if p.uses_api_key:
        if not api_key or not api_key.strip():
            raise ValidationError("empty API key")
        fields["api_key"] = api_key.strip()
        import os
        os.environ[p.api_key_env] = fields["api_key"]
    elif not p.env_keys:
        if not blob:
            # Credentials on their own are a complete update: attaching a
            # username and password to an account whose cookies are already
            # good is exactly how a cookie-only session becomes self-healing.
            if fields:
                res = await sessions_db.update_session_credentials(
                    platform_id, session_id, **fields)
                if not res:
                    raise NotFoundError(
                        f"session {session_id!r} not found in {platform_id} pool")
                log.info(f"{platform_id}: updated {session_id} ({', '.join(sorted(fields))})")
                return await status(platform_id)
            raise ValidationError("empty cookie JSON")
        cookies = load_cookies(blob, p.cookie_domain)
        if not cookies:
            raise ValidationError("no cookies for this platform in that export")
        missing = [n for n in p.required_cookies if n not in {c["name"] for c in cookies}]
        if missing:
            raise ValidationError(f"missing {', '.join(missing)} -- export while logged in")
        fields["cookies"] = cookies
    res = await sessions_db.update_session_credentials(platform_id, session_id, **fields)
    if not res:
        raise NotFoundError(f"session {session_id!r} not found in {platform_id} pool")
    if "cookies" in fields:
        # THE DEVICE GOES WITH THE CREDENTIALS. A persistent browser profile
        # keeps its own copy of the previous login in localStorage and
        # IndexedDB, so pasting a fresh export over a profile that still
        # remembers being signed in as the old session is a browser holding
        # two identities at once. Dropping the profile costs this account
        # its device history once and is rebuilt on the next run.
        #
        # Deliberately NOT done by `refresh_cookies`, which writes the LIVE
        # jar mid-run: those are the same credentials, rotated, and wiping
        # the device on every sweep would defeat the entire point.
        from backend.stealth.browser import reset_profile

        reset_profile(platform_id, session_id)
    log.info(f"{platform_id}: updated session credentials for {session_id}")
    return await status(platform_id)




def _clear_env_credentials(platform_id: str) -> None:
    """Drop the in-process/on-disk credentials `state_for` would otherwise
    keep reporting as "ready", only safe to call once NO pool entry is
    left for this platform, since with up to 20 accounts/keys pooled, one
    dead entry going away must not blow away whichever OTHER entry is
    currently loaded there for an in-flight or upcoming job."""
    import os
    if platform_id == "youtube":
        os.environ.pop("YOUTUBE_API_KEY", None)
    elif platform_id == "telegram":
        os.environ.pop("TELEGRAM_API_ID", None)
        os.environ.pop("TELEGRAM_API_HASH", None)
        os.environ.pop("TELEGRAM_PHONE", None)
        from backend.config.settings import settings
        session_path = settings.session_blob_path / "telegram"
        for stale in (session_path.with_suffix(".session"), session_path.with_suffix(".session-journal")):
            if stale.exists():
                try:
                    stale.unlink()
                except Exception:
                    pass


async def delete(platform_id: str, session_id: str = "") -> dict:
    _get_platform(platform_id)
    pool_now_empty = True
    if session_id:
        if await sessions_db.delete_item(platform_id, session_id):
            log.info(f"{platform_id}: removed session {session_id} from pool")
        pool_now_empty = not await sessions_db.list_pool(platform_id)
    else:
        n = await sessions_db.delete_pool(platform_id)
        if n:
            log.info(f"{platform_id}: whole session pool deleted ({n})")
    if pool_now_empty:
        _clear_env_credentials(platform_id)
    return await status(platform_id)


# ---------- interactive login ----------

async def launch_login(platform_id: str, timeout_s: int = 300, identifier: str = "") -> dict:
    """Open a real browser and wait for the login cookie to appear.
    Deliberately headful and hands-off: a person logs in, this only
    watches for the cookie, it never fills a password field."""
    p = _get_platform(platform_id)
    if platform_id not in LOGIN_FLOW:
        raise ValidationError(f"{platform_id} has no interactive login")

    url, proof = LOGIN_FLOW[platform_id]
    run = {"platform": platform_id, "status": "waiting", "message": "",
           "started": datetime.now(timezone.utc).isoformat(timespec="seconds"), "finished": ""}
    _logins[platform_id] = run

    from types import SimpleNamespace

    from backend.stealth.browser import Session

    opts = SimpleNamespace(headful=True, timeout=45, delay=0)
    session = Session(opts, [], load_images=True)
    try:
        ctx = await session.start()
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        run["message"] = "log in in the browser window"
        log.info(f"{platform_id}: waiting for manual login")

        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2)
            cookies = await ctx.cookies()
            if any(c["name"] == proof for c in cookies):
                kept = load_cookies(json.dumps(cookies), p.cookie_domain)
                await sessions_db.add_item(platform_id, kept, identifier)
                run["status"], run["message"] = "saved", f"{len(kept)} cookies saved"
                log.info(f"{platform_id}: login captured ({len(kept)} cookies)")
                break
        else:
            run["status"], run["message"] = "timeout", "no login within the time limit"
    except Exception as e:
        run["status"], run["message"] = "failed", f"{type(e).__name__}: {e}"
        log.error(f"{platform_id}: login failed: {e}")
    finally:
        run["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await session.stop()
    return run


# ---------- background health monitor ----------

async def verify_session_item(
    platform_id: str, cookies: list[dict], session_id: str = "",
) -> tuple[bool, str, bool]:
    """Exercises the platform's own check_session() against ONE specific
    set of cookies, the same live check an analysis job runs at its own
    start, just invoked here without a job attached.

    Returns (ok, detail, conclusive). `conclusive` is False whenever the
    check itself couldn't run to completion, browser launch failure, or
    the probe navigation raising (timeout, DNS failure, no internet in
    this environment right now, etc.), as opposed to the navigation
    succeeding and the platform's own page content showing a login/
    checkpoint wall. Only a conclusive result is trustworthy evidence that
    the SESSION (not the network) is the problem; a transient connectivity
    blip must never be recorded as "this session is now expired."

    A FAST HTTP PRE-CHECK RUNS FIRST, AND IT IS ALLOWED TO END THIS CALL IN
    EXACTLY ONE DIRECTION.

    `shared/fast_http.check_session_alive` costs ~150ms against a Chromium
    launch plus a context plus a navigation plus a 2.5s settle, and on
    Facebook and Instagram it reads the SAME server-side redirect the
    browser check ends up reading. When it comes back positively confirming
    a live session, that is the answer and the browser is not launched.

    WHEN IT SAYS DEAD, THE BROWSER STILL RUNS. This is deliberate and it is
    not timidity. The two verdicts are not symmetrical:

        a wrong "alive"  costs one stale row in the pool, which the next
                         real job corrects the moment it uses the session --
                         every runner already calls mark_session_failed on a
                         failed visit.
        a wrong "dead"   quarantines a working account. Enough of those and
                         the pool is empty, every sweep returns nothing, and
                         nothing in the product distinguishes that from "the
                         client has no impersonators".

    A login wall served to a datacenter IP, a bot challenge, a rate limit --
    all of them look exactly like a dead session over plain HTTP, and none
    of them are. So the HTTP probe may shorten the happy path and may never
    condemn an account on its own; only the browser check does that, exactly
    as it did before this pre-check existed.
    """
    from backend.platforms import registry
    from backend.platforms.scan_options import ScanOptions

    if settings.session_fast_check_enabled:
        try:
            verdict = await fast_http.check_session_alive(platform_id, cookies)
        except Exception as e:                       # noqa: BLE001 - never fatal
            verdict = fast_http.SessionVerdict(None, f"{type(e).__name__}: {e}")
        if verdict.alive is True:
            log.info(
                f"{platform_id}: session confirmed live over HTTP "
                f"({verdict.reason}) -- no browser needed")
            return True, "", True
        if verdict.alive is False:
            # Not acted on by itself. Logged because it is real evidence and
            # the browser check that follows is about to agree or disagree
            # with it, and an operator comparing the two lines is how a
            # probe that has drifted gets noticed.
            log.info(
                f"{platform_id}: HTTP probe says the session is dead "
                f"({verdict.reason}) -- confirming in a browser before acting")

    plat = registry.get(platform_id)
    # warmup=False: a probe that warms first pays for two page loads to
    # answer one question, and puts an extra visit on an account already
    # suspected of being unwell. See stealth/browser.py::Session._warmup.
    options = ScanOptions(
        evidence=None, delay=0, concurrency=1, headful=False, warmup=False)
    try:
        # `session_id` decides which persistent browser profile this check
        # opens. Passing it means the probe arrives on the SAME device the
        # account works from -- a check that logs in from a blank profile is
        # itself the "new device" event it is trying to detect.
        scraper = plat.scraper()(options, cookies, session_id=session_id)
    except Exception as e:
        return False, f"could not construct scraper: {type(e).__name__}: {e}", False
    try:
        await scraper.start()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", False
    try:
        ok = await scraper.check_session()
        return ok, "" if ok else "session invalid or checkpointed", True
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", False
    finally:
        try:
            await scraper.stop()
        except Exception:
            pass


async def _verify_credential_item(platform_id: str, item: dict) -> tuple[bool, str, bool]:
    """`verify_session_item`'s counterpart for the two platforms that don't
    authenticate with cookies at all. YouTube (an API key) and Telegram
    (an MTProto api_id/api_hash + session blob). Both used to be silently
    excluded from this whole monitor (see check_all_once's old blanket
    "no pool for this auth kind" skip), so a dead key/session sat unnoticed
    until a real job happened to hit it.

    Populates the credential from THIS item into os.environ (and, for
    Telegram, the session blob file) before constructing the scraper,
    deliberately NOT `sessions.manager.session_for_job`, which also bumps
    the item's real `use_count` and `last_used`; those numbers are meant to
    reflect actual job work, and a health-check ping is not that.

    Same (ok, detail, conclusive) contract as verify_session_item: each
    platform's own check_session() (youtube/analysis_engine.py,
    telegram/discovery_engine.py) already returns False only on a
    conclusive, positively-confirmed rejection and raises for anything
    merely inconclusive (a network blip, a quota/flood-wait), so a raised
    exception here means exactly what it means for the cookie-based
    platforms: leave the session's status alone, this check didn't prove
    anything either way.
    """
    import os

    from backend.platforms import registry
    from backend.platforms.scan_options import ScanOptions

    plat = registry.get(platform_id)
    if plat.uses_api_key:
        api_key = item.get("api_key", "")
        if not api_key:
            return False, "pooled item has no api_key", False
        os.environ[plat.api_key_env] = str(api_key)
    elif plat.env_keys:
        if not (item.get("api_id") and item.get("api_hash")):
            return False, "pooled item is missing api_id/api_hash", False
        os.environ["TELEGRAM_API_ID"] = str(item["api_id"])
        os.environ["TELEGRAM_API_HASH"] = str(item["api_hash"])
        if item.get("phone"):
            os.environ["TELEGRAM_PHONE"] = str(item["phone"])
        if item.get("session_blob"):
            session_path = settings.session_blob_path / "telegram.session"
            if not session_path.exists():
                settings.session_blob_path.mkdir(parents=True, exist_ok=True)
                session_path.write_bytes(item["session_blob"])
    else:
        return False, f"{platform_id} is not a credential-authed platform", False

    options = ScanOptions(
        evidence=None, delay=0, concurrency=1, headful=False, warmup=False)
    try:
        scraper = plat.scraper()(options, [])
    except Exception as e:
        return False, f"could not construct scraper: {type(e).__name__}: {e}", False
    try:
        await scraper.start()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", False
    try:
        ok = await scraper.check_session()
        return ok, "" if ok else "session invalid or checkpointed", True
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", False
    finally:
        try:
            await scraper.stop()
        except Exception:
            pass


# ---------------------------------------------------------- self-healing login
#
# WHEN AN ACCOUNT WITH STORED CREDENTIALS IS FOUND LOGGED OUT, SIGN IT BACK IN.
#
# THE THREE GUARDS BELOW ARE THE WHOLE FEATURE. Automated re-login without
# them is not self-healing, it is a scripted password attempt every thirty
# minutes against an account the platform is already unhappy with -- which
# is how an account stops being recoverable at all, permanently, by the tool
# that was trying to rescue it. In order of importance:
#
#   NOT WHILE IT IS IN USE   a job holding this account means a browser is
#                            already open on it. A second one from the same
#                            IP is this module's most-repeated warning.
#   A COOLDOWN IN HOURS      the floor between two attempts on ONE account.
#                            The monitor wakes every 30 minutes; without
#                            this, a permanently wrong password becomes 48
#                            login attempts a day.
#   AN ATTEMPT CEILING       self-healing that cannot heal must stop and let
#                            a person look. A CAPTCHA, a checkpoint or a
#                            changed password will never be fixed by trying
#                            again, and each retry makes it worse.
#
# Both counters are in-process. A restart forgives them, which is the
# forgiving direction and acceptable: the cooldown's real job is to stop a
# tight loop inside one long-running process, and an operator restarting the
# service is a person paying attention.
# A RE-LOGIN IN FLIGHT, IN THIS PROCESS. The only piece of re-login state
# that stays in memory, and it stays there on purpose.
#
# Everything else the self-healer knows -- how many attempts have failed,
# when the last one started, when it last worked -- now lives on the
# session row, because losing it to a restart loses the cooldown and the
# attempt ceiling with it (see session_repository._to_item).
#
# This one must NOT follow it. It is a lock, not a record. Persisted, a
# process killed mid-login would leave it set for ever, and the account
# could never be retried by anything -- the self-healer would skip it and
# the "Re-Login Now" button would refuse, permanently, with no way back
# except editing the database. In memory it clears itself by definition:
# the process that held the lock is the process that died.
_relogin_running: set[str] = set()


def relogin_state(row: dict) -> dict:
    """What the self-healer currently thinks about one account. Read by the
    Sessions panel so "it is not retrying" is visible rather than
    mysterious.

    Takes the stored row rather than looking anything up: the counters are
    fields on it now, and the caller already has it.
    """
    key = f"{row.get('platform', '')}:{row.get('id', '')}"
    return {
        "attempts": int(row.get("relogin_attempts") or 0),
        "last_attempt": float(row.get("relogin_last_attempt") or 0.0),
        "last_success": float(row.get("relogin_last_success") or 0.0),
        "total_successes": int(row.get("relogin_total_successes") or 0),
        "running": key in _relogin_running,
    }


def _relogin_blocked(platform_id: str, session_id: str, item: dict, *,
                     force: bool) -> str:
    """Why this account may not be re-logged-in right now, or "" for go.

    `force` is an operator pressing the button: it waives the cooldown and
    the attempt ceiling, because a person who has just fixed the password is
    exactly the case those two exist to wait for. It does NOT waive the
    in-use check, which is about not getting the account challenged.

    The cooldown and the ceiling are read off `item`, which is the stored
    row, so they survive a restart -- see session_repository._to_item for
    why that matters more than it sounds like it does.
    """
    key = f"{platform_id}:{session_id}"
    if key in _relogin_running:
        return "a re-login is already running for this account"
    if _session_in_use(platform_id, session_id):
        return "a job is using this account right now"
    if force:
        return ""
    if not settings.session_auto_relogin:
        return "automatic re-login is switched off"
    last = float(item.get("relogin_last_attempt") or 0.0)
    attempts = int(item.get("relogin_attempts") or 0)
    waited = _now() - last
    cooldown = max(0.0, settings.session_relogin_cooldown_minutes) * 60.0
    if last and waited < cooldown:
        return (f"cooling off -- {(cooldown - waited) / 60:.0f} more minute(s) "
                "before another automatic attempt")
    if attempts >= max(1, settings.session_relogin_max_attempts):
        return (f"gave up after {attempts} automatic attempt(s) -- "
                "this needs a person")
    return ""


async def _perform_relogin(platform_id: str, session_id: str, item: dict) -> tuple[bool, str]:
    """One attempt. Returns (recovered, detail). Never raises."""
    from backend.stealth.auto_login import run_auto_login

    key = f"{platform_id}:{session_id}"
    identifier = item.get("identifier") or session_id
    p = _get_platform(platform_id)
    _relogin_running.add(key)
    # STAMPED BEFORE THE BROWSER OPENS. The cooldown runs from the start of
    # an attempt, so a login that takes two minutes cannot let a second one
    # through behind it -- and a process killed mid-login still leaves the
    # cooldown written down rather than looking like it never tried.
    await _record_relogin(platform_id, session_id, "started")
    try:
        log.info(
            f"[SESSION_RECOVERY] {platform_id} session '{identifier}' is logged out -- "
            "starting automated re-login")
        result = await run_auto_login(
            platform_id,
            str(item.get("username") or ""),
            str(item.get("password") or ""),
            str(item.get("two_factor_secret") or ""),
            session_id=session_id,
        )
        cookies = normalize_cookies(result.cookies, p.cookie_domain)
        missing = [n for n in p.required_cookies if n not in {c["name"] for c in cookies}]
        if missing:
            detail = (f"signed in but {', '.join(missing)} never appeared -- "
                      "the login probably stopped at a checkpoint")
            await _record_relogin(platform_id, session_id, "failed")
            log.warning(f"[SESSION_RECOVERY] {platform_id}/{identifier}: {detail}")
            return False, detail

        # Written through the REPOSITORY, not through this module's own
        # `update_session_credentials`: that one resets the browser profile,
        # which is the last thing wanted here. The login just happened
        # INSIDE that profile, and the device it established is the point.
        #
        # storage_state rides along. run_auto_login has always captured it
        # -- a modern login keeps state in localStorage that a cookie-only
        # capture drops -- and it was being thrown away one line after
        # being collected, while this module's docstring said it was kept.
        await sessions_db.update_session_credentials(
            platform_id, session_id, cookies=cookies,
            storage_state=result.storage_state or {})
        await _record_relogin(platform_id, session_id, "ok")
        log.info(
            f"[SESSION_RECOVERY] {platform_id} session '{identifier}' recovered -- "
            f"{len(cookies)} fresh cookie(s), back in the pool")
        return True, ""
    except Exception as e:                            # noqa: BLE001 - never fatal
        await _record_relogin(platform_id, session_id, "failed")
        detail = f"{type(e).__name__}: {e}"
        log.error(
            f"[SESSION_RECOVERY] {platform_id}/{identifier}: automated re-login "
            f"failed ({detail}) -- leaving the existing quarantine in place")
        try:
            await sessions_db.update_item(
                platform_id, session_id,
                last_error=f"automatic re-login failed: {detail}")
        except Exception:                             # noqa: BLE001
            pass
        return False, detail
    finally:
        _relogin_running.discard(key)


async def _record_relogin(platform_id: str, session_id: str, outcome: str) -> None:
    """Write the attempt down, and never let that write break the attempt.

    A failed bookkeeping write must not turn a re-login that WORKED into an
    exception -- but it must not pass silently either, because the counter
    it failed to write is a safety rail, and a rail nobody knows is missing
    is worse than one that is visibly gone.
    """
    try:
        await sessions_db.record_relogin_attempt(platform_id, session_id, outcome)
    except Exception as e:                            # noqa: BLE001
        log.error(
            f"[SESSION_RECOVERY] {platform_id}/{session_id}: could not record the "
            f"{outcome!r} attempt ({type(e).__name__}: {e}) -- the cooldown and "
            f"attempt ceiling for this account are now unreliable until the next "
            f"successful write")


async def maybe_auto_relogin(platform_id: str, session_id: str) -> bool:
    """Start a background re-login for a logged-out account, if it is allowed.

    Returns whether one was STARTED, not whether it worked -- the caller is
    the health monitor, which must not sit and wait for a browser to finish
    a sign-in before checking the next account in its batch.

    A failed attempt changes nothing: the quarantine the caller already
    applied stays exactly as it was, which is the fallback the plan asks for
    and also simply what happens when nothing here succeeds.

    EVERY WAY OUT OF HERE SAYS WHICH ONE IT TOOK. This used to have four
    silent `return False` paths -- platform without a login flow, the
    feature switched off, the row unreadable, no stored credentials -- and
    between them they produced the single most confusing thing this module
    can do: a session goes bad, the log says so, and then nothing. Not
    "tried and failed", not "refused": nothing. An operator reading that
    cannot tell a broken auto-login from one that was never attempted, and
    the difference is the whole of what to do next. A not-attempted must
    never be able to pass for an attempted-and-found-nothing.
    """
    who = f"{platform_id}/{session_id}"

    def _no(reason: str) -> bool:
        log.info(f"[SESSION_RECOVERY] {who}: no automatic re-login -- {reason}")
        return False

    if platform_id not in LOGIN_FLOW:
        return _no(f"{platform_id} has no automated login flow")
    if not settings.session_auto_relogin:
        return _no("automatic re-login is switched off (session_auto_relogin)")
    try:
        item = await sessions_db.get_item(platform_id, session_id)
    except Exception as e:                            # noqa: BLE001
        return _no(f"could not read the session row -- {type(e).__name__}: {e}")
    if not item:
        return _no("no such session in the pool")

    identifier = item.get("identifier") or session_id
    who = f"{platform_id}/{identifier}"
    if not (item.get("username") and item.get("password")):
        # ON THE ROW, not only in the log. This one is not a transient
        # refusal, it is a standing fact about the account, and it is
        # fixable by the person reading the Sessions panel -- which is
        # where they will be looking after it went red.
        missing = "username and password" if not (
            item.get("username") or item.get("password")
        ) else ("password" if item.get("username") else "username")
        try:
            await sessions_db.update_item(
                platform_id, session_id,
                last_error=f"logged out, and no automatic re-login is possible: "
                           f"this account has no stored {missing}")
        except Exception:                             # noqa: BLE001
            pass
        return _no(f"no stored {missing} -- this is a cookies-only account")

    if blocked := _relogin_blocked(platform_id, session_id, item, force=False):
        return _no(blocked)

    async def _bg() -> None:
        await _perform_relogin(platform_id, session_id, item)

    _spawn(_bg())
    return True


async def relogin_now(platform_id: str, session_id: str) -> dict:
    """The Sessions panel's "Re-Login Now" button: one attempt, right now,
    waiting for the answer so the operator sees a real result rather than a
    row that may or may not change later.

    Waives the cooldown and the attempt ceiling (a person pressing this has
    usually just fixed whatever was wrong) but never the in-use check.
    """
    item = await sessions_db.get_item(platform_id, session_id)
    if item is None:
        raise NotFoundError(f"{platform_id}: session {session_id!r} not in pool")
    if platform_id not in LOGIN_FLOW:
        raise ConflictError(f"{platform_id} does not support automated login")
    if not (item.get("username") and item.get("password")):
        raise ConflictError(
            f"{item.get('identifier') or session_id} has no stored credentials -- "
            "add a username and password to enable automated login")
    if blocked := _relogin_blocked(platform_id, session_id, item, force=True):
        raise ConflictError(blocked)

    ok, detail = await _perform_relogin(platform_id, session_id, item)
    return {"ok": ok, "detail": detail, "session": await status(platform_id)}


async def _record_item_result(
    platform_id: str, session_id: str, identifier: str, ok: bool, detail: str, conclusive: bool = True,
) -> None:
    await sessions_db.record_item_health(platform_id, session_id, identifier, ok, detail)
    if not conclusive:
        # the check itself failed to run (network/transport error), leave
        # the session's actual status untouched, it may well still be fine
        if not ok:
            log.warning(f"{platform_id}/{identifier}: session check inconclusive, leaving status as-is -- {detail}")
        return
    if ok:
        # a passing live check is the strongest evidence a session works:
        # clear any quarantine and reset the consecutive-failure ladder
        await mark_session_ok(platform_id, session_id)
        return
    # BUG FIXED 2026-08-22: this used to be gated on `was_ok` (was the
    # PREVIOUSLY recorded health check a pass), so only the very FIRST
    # failing check in a session's life ever reached mark_session_failed.
    # Every later 30-minute recheck of an already-broken session silently
    # did nothing -- consecutive_failures froze at 1 forever, and the
    # Sessions panel showed "1st failure" on an account that had in fact
    # failed every check for weeks.
    #
    # The gate was redundant in the first place: mark_session_failed
    # already raises the SessionInvalid incident (naming this exact
    # session) only on the actual transition into a dead state, via its
    # OWN `newly_dead` check against the session's stored status, not
    # against this function's health-check history. Calling it
    # unconditionally on every conclusive failure is also what every live
    # job failure already does (analysis_service.py, discovery_service.py
    # both call it on every failed visit, never gated on "was this the
    # first one"), so this brings the background monitor in line with
    # every other caller instead of being the one path that stops updating
    # a session's failure count after the first strike.
    await mark_session_failed(platform_id, session_id, "expired", detail=detail)
    log.warning(f"{platform_id}/{identifier}: session went bad -- {detail}")
    # QUARANTINE FIRST, THEN TRY TO HEAL. Deliberately after
    # mark_session_failed, not instead of it: the account is out of the pool
    # from this instant either way, so a re-login that fails, is refused, or
    # never starts leaves exactly the behaviour this module had before --
    # quarantined, with an incident raised. Recovery can only ever put a
    # session BACK; it cannot keep a broken one in circulation.
    await maybe_auto_relogin(platform_id, session_id)


async def _record_platform_summary(platform_id: str) -> None:
    summary = await pool_summary(platform_id)
    ok = summary["total"] == 0 or summary["available"] > 0
    detail = "" if ok else f"all {summary['total']} pooled sessions are unavailable ({summary['dead']} dead)"
    await sessions_db.record_platform_health(platform_id, ok, detail)
    if not ok:
        log.warning(f"{platform_id}: pool exhausted -- {detail}")


async def _pick_batch(platform_id: str, limit: int) -> list[tuple[str, str, list[dict]]]:
    """The next `limit` sessions most overdue for a live check.

    Sessions a running job is holding RIGHT NOW are excluded. Opening a
    second Playwright context on one account from one IP is the single most
    reliable way to earn a checkpoint, so the account being scraped this
    second is off limits, but only that account.

    This used to be enforced a whole level coarser: `check_all_once`
    skipped an entire platform whenever any job was running, and a job with
    `platform=None` (which is what EVERY round-robin discovery and analysis
    job is) counted as busy for all six platforms at once. With the
    always-on round-robin engine that is very nearly always true, so in
    practice the health monitor almost never ran and dead sessions were
    discovered by a failing sweep rather than by the sweep that exists to
    catch them first. Excluding the individual in-use sessions gives the
    same protection without standing the whole monitor down.
    """
    items = await sessions_db.list_pool(platform_id)
    now = _now()
    candidates = [
        s for s in items
        if s["status"] not in DEAD_STATES
        and s["status"] not in PENDING_STATES
        and s["rate_limited_until"] <= now
        and not _session_in_use(platform_id, s["id"])
        # Recently proven by a real job, so a probe would only add another
        # authenticated hit to an account that has already demonstrated it
        # is fine. See PROVEN_FRESH_S.
        and (now - float(s.get("last_ok") or 0.0)) >= PROVEN_FRESH_S
    ]
    if not candidates:
        return []
    stamped = []
    for s in candidates:
        last = await sessions_db.item_last_checked(platform_id, s["id"])
        stamped.append((last or datetime.min, s))
    stamped.sort(key=lambda t: t[0])
    return [(s["id"], s["identifier"], s["cookies"]) for _, s in stamped[:limit]]


async def verify_session(platform_id: str) -> tuple[bool, str, Optional[tuple[str, str]], bool]:
    from backend.platforms import registry

    plat = registry.get(platform_id)
    state = await registry.session_state(plat)
    if state != "ready":
        return False, f"session {state} (no cookies/credentials to check)", None, True
    if plat.uses_api_key or plat.env_keys:
        return True, "", None, True
    picked = await _pick_batch(platform_id, limit=1)
    if not picked:
        return False, "no available sessions in the pool to check", None, True
    session_id, identifier, cookies = picked[0]
    ok, detail, conclusive = await verify_session_item(platform_id, cookies, session_id)
    return ok, detail, (session_id, identifier), conclusive


async def check_one(platform_id: str) -> tuple[bool, str]:
    ok, detail, item, conclusive = await verify_session(platform_id)
    if item is not None:
        session_id, identifier = item
        await _record_item_result(platform_id, session_id, identifier, ok, detail, conclusive)
    await _record_platform_summary(platform_id)
    return ok, detail


async def check_item(platform_id: str, session_id: str) -> dict:
    """Live-check ONE named session, right now, on demand.

    `check_one` (the platform-wide "Verify Sweep Now") deliberately picks
    whichever session is most overdue, which is the right choice for a
    background sweep and the wrong one for a person who has just re-pasted
    cookies and wants to know whether THAT account works. Without this the
    only answer available was to wait for a sweep, and since a rewrite
    correctly drops the previous verdict, the row shows no verification at
    all until one runs.

    Refuses while a job holds this session, for the same reason the sweep
    skips in-use sessions: a second browser on one account from one IP is
    how accounts get challenged.
    """
    from backend.platforms import registry

    plat = registry.get(platform_id)
    item = await sessions_db.get_item(platform_id, session_id)
    if item is None:
        raise NotFoundError(f"{platform_id}: session {session_id!r} not in pool")
    if _session_in_use(platform_id, session_id):
        raise ConflictError(
            f"{item['identifier']} is being used by a running job right now -- "
            "checking it at the same time risks a checkpoint. Try again once the job finishes."
        )

    if plat.uses_api_key or plat.env_keys:
        ok, detail, conclusive = await _verify_credential_item(platform_id, item)
    else:
        if not item.get("cookies"):
            return {"ok": False, "detail": "no cookies saved for this account yet", "conclusive": True}
        ok, detail, conclusive = await verify_session_item(
            platform_id, item["cookies"], session_id)

    await _record_item_result(platform_id, session_id, item["identifier"], ok, detail, conclusive)
    await _record_platform_summary(platform_id)
    return {"ok": ok, "detail": detail, "conclusive": conclusive}


async def check_all_once() -> dict[str, dict]:
    from backend.platforms import registry

    out: dict[str, dict] = {}
    for platform_id in MONITORED:
        plat = registry.get(platform_id)
        # _pick_batch leaves out whichever sessions running jobs are holding
        # (see its docstring), so a busy platform still gets its IDLE
        # sessions checked instead of the whole platform standing down.
        # Checks run one at a time, here and across platforms, so at most
        # one extra browser exists at any moment no matter how busy it is.
        batch = await _pick_batch(platform_id, BATCH_SIZE)
        if not batch:
            out[platform_id] = {"skipped": "no available sessions/credentials to check"}
            await _record_platform_summary(platform_id)
            continue
        results = []
        for session_id, identifier, cookies in batch:
            if plat.uses_api_key or plat.env_keys:
                item = await sessions_db.get_item(platform_id, session_id)
                if item is None:
                    continue
                ok, detail, conclusive = await _verify_credential_item(platform_id, item)
            else:
                ok, detail, conclusive = await verify_session_item(
                    platform_id, cookies, session_id)
            await _record_item_result(platform_id, session_id, identifier, ok, detail, conclusive)
            results.append({"identifier": identifier, "ok": ok, "detail": detail})
        out[platform_id] = {"checked": len(results), "results": results}
        await _record_platform_summary(platform_id)
    return out


async def cached_health() -> dict[str, dict]:
    return await sessions_db.cached_health()


async def purge_stale_dead_sessions() -> int:
    """Delete pool rows that have sat dead (expired/checkpointed/
    unreadable) for more than DEAD_GRACE_DAYS without being rewritten or
    manually deleted, keeps a pool that nobody is tending from silently
    filling up with rows that will never work again and never come back
    towards the 20-item cap.

    Only rows carrying a `dead_since` set by `mark_session_failed` are
    eligible, so a row that was already dead before this field existed is
    never guessed at, it just sits there (as it always did) until the
    next real failure stamps a fresh `dead_since`, or a person deletes it.
    """
    from backend.platforms.registry import PLATFORMS

    cutoff = _now() - DEAD_GRACE_DAYS * 86400
    removed = 0
    for platform_id in PLATFORMS:
        items = await sessions_db.list_pool(platform_id)
        purged_here = 0
        for item in items:
            if item["status"] not in DEAD_STATES:
                continue
            dead_since = item.get("dead_since") or 0
            if dead_since and dead_since <= cutoff:
                if await sessions_db.delete_item(platform_id, item["id"]):
                    removed += 1
                    purged_here += 1
                    log.info(
                        f"{platform_id}: purged {item['id']} ({item['identifier']}) -- "
                        f"dead ({item['status']}) for over {DEAD_GRACE_DAYS}d without being replaced"
                    )
        if purged_here and purged_here == len(items):
            _clear_env_credentials(platform_id)
    return removed


async def _monitor_loop() -> None:
    while True:
        try:
            await check_all_once()
            from backend.services.session_canary_service import (
                check_token_expiries, probe_pool_liveness,
            )
            await check_token_expiries()
            # THE HALF `check_all_once` ABOVE CANNOT COVER. That sweep opens
            # a real browser, so it is rationed: BATCH_SIZE sessions per
            # platform per pass, skipping any proven fresh within
            # PROVEN_FRESH_S. A pool bigger than the batch therefore cycles
            # over several passes, and a session the platform killed early
            # -- a password change, a checkpoint, a rotation on their side --
            # sits in the pool looking healthy until its turn comes round.
            #
            # This pass costs one ordinary HTTPS request per account and so
            # can cover EVERY session every time. It decides nothing on its
            # own: a dead answer is handed to `check_item`, the same browser
            # check, just started sooner than the rotation would have. Its
            # own try/except keeps it from costing the sweep above it.
            await probe_pool_liveness()
            # CAN WE STILL LOG IN is only half the question; the other half
            # is whether scraping still works once we have. A dead parser is
            # silent where a dead session is loud, so it gets checked on the
            # same cadence rather than waiting for somebody to notice a
            # month of clean, empty sweeps. It has its own try/except and
            # returns a report rather than raising, so it cannot cost the
            # session sweep above it.
            from backend.services import engine_health_service
            await engine_health_service.check_once()
            if purged := await purge_stale_dead_sessions():
                log.info(f"session cleanup: purged {purged} stale dead session(s)")
            # Disk housekeeping for the persistent browser profiles. One per
            # pooled account per platform, tens to hundreds of megabytes
            # each, on a machine nobody is watching. Synchronous file IO, so
            # it runs in a thread rather than stalling the loop this shares
            # with every running sweep.
            try:
                from backend.stealth.browser import prune_stale_profiles

                pruned = await asyncio.to_thread(
                    prune_stale_profiles, settings.browser_profile_retention_days)
                if pruned:
                    log.info(f"session cleanup: removed {pruned} unused browser profile(s)")
            except Exception as e:                    # noqa: BLE001 - housekeeping
                log.warning(f"browser profile cleanup skipped: {type(e).__name__}: {e}")
        except Exception as e:
            log.error(f"session monitor sweep failed: {type(e).__name__}: {e}")
        # Jittered so the sweep does not fire on the same wall-clock offset
        # every half hour for the life of the process. A probe is an
        # authenticated page load on a real account; a perfectly periodic
        # one is a pattern worth not having. See CHECK_JITTER.
        await asyncio.sleep(CHECK_INTERVAL_S * (1.0 + random.uniform(-CHECK_JITTER, CHECK_JITTER)))


def start_monitor() -> None:
    global _monitor_task
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_monitor_loop())
        log.info(f"session monitor started -- checking every {CHECK_INTERVAL_S // 60}m")


def stop_monitor() -> None:
    global _monitor_task
    if _monitor_task is not None:
        _monitor_task.cancel()
        _monitor_task = None
