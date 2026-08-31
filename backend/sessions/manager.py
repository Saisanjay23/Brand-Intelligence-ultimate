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
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from backend.config.settings import settings
from backend.database.repositories import session_repository as sessions_db
from backend.sessions.cookies import load_cookies, normalize_cookies
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
    live answer, not "was picked at some point". Drives the "currently
    running" marker in the Sessions panel.

    Asks the analysis runner, which registers a session the moment
    `session_for_job()` hands it one and releases it in a `finally` when
    that platform's batch ends, so a crashed or cancelled run cannot leave
    a session marked busy forever.

    Imported lazily and guarded: this is a cosmetic indicator, and a
    missing/half-built runner must never be able to break the Sessions
    panel itself. (It did exactly that once -- this used to reach into a
    job module that had been deleted, and every platform's session status
    came back as an error.)"""
    try:
        from backend.analysis.runner import analysis_runner
    except Exception:
        return False
    return analysis_runner.holds_session(platform_id, session_id)


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
    proxy = s.get("proxy") or {}
    proxy_host = ""
    if proxy.get("server"):
        parsed = urlparse(proxy["server"])
        proxy_host = parsed.hostname or proxy["server"].split("://")[-1].split("@")[-1].split(":")[0]

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
    return {**extra,
        "id": s["id"], "identifier": s["identifier"], "status": s["status"],
        "rate_limited_until": s["rate_limited_until"], "last_used": s["last_used"],
        "use_count": s.get("use_count", 0),
        "in_use": _session_in_use(s.get("platform", ""), s["id"]),
        "cookie_count": len(s.get("cookies", []) or []), "proxy_host": proxy_host,
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


async def get_healthy_session(platform_id: str) -> Optional[dict]:
    items = await sessions_db.list_pool(platform_id)
    available = [s for s in items if _is_available(s, _now())]
    chosen = _pick_least_recently_used(available)
    if chosen is None:
        return None
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


async def session_for_job(platform_id: str) -> tuple[object, dict]:
    """What a discovery/analysis job needs to actually run: the Platform
    metadata plus a healthy pooled session's credentials (+proxy for
    cookie-authed platforms, api_key for key-authed ones, or just
    id/identifier for MTProto since its credentials go into os.environ +
    a session file rather than being handed to the caller directly)."""
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
        item = await get_healthy_session(platform_id)
        if item is None:
            raise ConflictError(f"{platform_id}: no healthy accounts available -- please add more accounts or check credentials")
        import os
        os.environ["TELEGRAM_API_ID"] = str(item.get("api_id", ""))
        os.environ["TELEGRAM_API_HASH"] = str(item.get("api_hash", ""))
        if item.get("phone"):
            os.environ["TELEGRAM_PHONE"] = str(item.get("phone", ""))
        if item.get("session_blob"):
            settings.session_blob_path.mkdir(parents=True, exist_ok=True)
            (settings.session_blob_path / "telegram.session").write_bytes(item["session_blob"])
        return plat, {"id": item["id"], "identifier": item["identifier"]}
    if plat.uses_api_key:
        item = await get_healthy_session(platform_id)
        if item is None:
            items = await sessions_db.list_pool(platform_id)
            if not items:
                import os
                if os.environ.get(plat.api_key_env):
                    return plat, {"id": "", "identifier": "env", "api_key": os.environ[plat.api_key_env]}
            raise ConflictError(f"{platform_id}: no healthy API keys available -- please add more keys or check quotas")
        import os
        os.environ[plat.api_key_env] = str(item.get("api_key", ""))
        return plat, {"id": item["id"], "identifier": item["identifier"], "api_key": item["api_key"]}
    item = await get_healthy_session(platform_id)
    if item is None:
        if plat.can_run_anonymously:
            # this platform's search/profile pages work logged-out (see
            # registry.Platform.anonymous_context_path) -- a dead/missing
            # session pool costs it one field, not the whole platform.
            return plat, {"id": "", "identifier": "anonymous", "anonymous": True}
        raise ConflictError(f"{platform_id}: no healthy sessions available -- please add more cookies")
    return plat, {"id": item["id"], "identifier": item["identifier"], "cookies": item["cookies"], "proxy": item["proxy"]}


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
    proxy: str | None = None
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
        item = await sessions_db.add_item(platform_id, [], identifier, proxy)
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
            cookies = await run_auto_login(platform_id, username, password, two_factor_secret, proxy)
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
            # to tell a wrong password from a proxy timeout
            await sessions_db.update_item(
                platform_id, item["id"], status="checkpointed",
                last_error=f"auto-login failed: {type(e).__name__}: {e}",
            )

    # Fire and forget
    import asyncio
    asyncio.create_task(_do_login())
    
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


async def update_session_credentials(platform_id: str, session_id: str, blob: str = "", api_key: str = "", identifier: Optional[str] = None) -> dict:
    p = _get_platform(platform_id)
    fields: dict = {}
    if identifier is not None and identifier.strip():
        fields["identifier"] = identifier.strip()
    if p.uses_api_key:
        if not api_key or not api_key.strip():
            raise ValidationError("empty API key")
        fields["api_key"] = api_key.strip()
        import os
        os.environ[p.api_key_env] = fields["api_key"]
    elif not p.env_keys:
        if not blob:
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
    log.info(f"{platform_id}: updated session credentials for {session_id}")
    return await status(platform_id)


# Playwright's own accepted proxy schemes. See stealth/proxy.py::
# build_proxy_config, which passes `server` straight through to Playwright's
# context-launch option unvalidated, this is the one place that stands
# between a malformed string and a browser launch failing three steps into
# a job, instead of at the moment the proxy is actually configured.
_ALLOWED_PROXY_SCHEMES = ("http", "https", "socks4", "socks5", "socks5h")


def _validate_proxy(proxy: dict) -> dict:
    """Rejects anything Playwright would reject (or silently misuse), and
    strips the result down to exactly the keys `build_proxy_config` reads,
    so a caller-supplied dict can never smuggle unrelated fields into the
    stored session document."""
    server = str(proxy.get("server") or "").strip()
    if not server:
        raise ValidationError("proxy.server is required")
    parsed = urlparse(server)
    if parsed.scheme not in _ALLOWED_PROXY_SCHEMES:
        raise ValidationError(
            f"proxy.server must start with one of {'/'.join(s + '://' for s in _ALLOWED_PROXY_SCHEMES)} -- got {server!r}"
        )
    if not parsed.hostname:
        raise ValidationError(f"proxy.server has no host: {server!r}")
    if not parsed.port:
        raise ValidationError(f"proxy.server has no port: {server!r}")

    out: dict = {"server": server}
    if username := str(proxy.get("username") or "").strip():
        out["username"] = username
    if password := str(proxy.get("password") or "").strip():
        out["password"] = password
    if tz := str(proxy.get("timezone_id") or "").strip():
        # not validated against the IANA database here, resolve_timezone_id
        # (stealth/timezone.py) just hands whatever string this is straight
        # to Playwright's own `timezone_id` context option, which will
        # itself reject a bogus zone at browser-launch time; duplicating
        # that whole database here isn't worth it for a value an analyst
        # typed once and will notice is wrong the first time a session runs.
        out["timezone_id"] = tz
    return out


async def set_proxy(platform_id: str, session_id: str, proxy: Optional[dict]) -> dict:
    p = _get_platform(platform_id)
    if proxy and (p.uses_api_key or p.env_keys):
        # A per-session PROXY is a Playwright context option, it only
        # means anything for the cookie-authed platforms that actually
        # launch a browser through sessions/manager.py::session_for_job.
        # YouTube (api_key) talks to a REST API directly, never a browser;
        # Telegram (env_keys/MTProto) connects via Telethon, which has its
        # own separate proxy mechanism this field was never wired to.
        # Accepting either here would store a proxy that LOOKS configured
        # in the pool but is silently never read by anything, exactly the
        # trap this check exists to close.
        raise ConflictError(
            f"{platform_id}: has no per-session browser proxy "
            f"({'API-key' if p.uses_api_key else 'MTProto'} access doesn't route through one)"
        )
    item = await sessions_db.get_item(platform_id, session_id)
    if item is None:
        raise NotFoundError(f"{platform_id}: session {session_id!r} not in pool")
    if proxy:
        await sessions_db.update_item(platform_id, session_id, proxy=_validate_proxy(proxy))
    else:
        await sessions_db.unset_proxy(platform_id, session_id)
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

async def verify_session_item(platform_id: str, cookies: list[dict]) -> tuple[bool, str, bool]:
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
    """
    from backend.platforms import registry
    from backend.platforms.scan_options import ScanOptions

    plat = registry.get(platform_id)
    options = ScanOptions(evidence=None, delay=0, concurrency=1, headful=False)
    try:
        scraper = plat.scraper()(options, cookies)
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

    options = ScanOptions(evidence=None, delay=0, concurrency=1, headful=False)
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
    ok, detail, conclusive = await verify_session_item(platform_id, cookies)
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
        ok, detail, conclusive = await verify_session_item(platform_id, item["cookies"])

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
                ok, detail, conclusive = await verify_session_item(platform_id, cookies)
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
            if purged := await purge_stale_dead_sessions():
                log.info(f"session cleanup: purged {purged} stale dead session(s)")
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
