"""Where this machine is leaving from, and whether an Instagram account has
met that place before.

THE PROBLEM THIS EXISTS FOR. The accounts here are COOKIE sessions exported
from the operator's own Chrome, and the tool has no proxy support: every
account leaves from whatever network this machine is on right now. A laptop
moves -- home to office, a VPN switched to another country (the Scheduler
even has a "Smart Detect (VPN / IP)" button) -- and an Instagram session
that was last seen in India and is suddenly used from Singapore is the
textbook account-takeover signal. It is answered with a checkpoint, a forced
password change, or a suspension, and nothing in the request pacing can
prevent it, because the signal is WHERE the request came from, not how fast.

WHAT IT DOES. Each Instagram/Threads account remembers the country (and the
network operator) it last worked from. Before a job gets the account:

    same country, any network   -> go ahead (home broadband changes address
                                   all the time; the country is the signal
                                   worth acting on, not the address)
    no memory yet               -> remember this place and go ahead
    DIFFERENT COUNTRY           -> hold the account; say why, in words
    lookup failed               -> go ahead, and say so in the log -- a
                                   guard that blocks every job whenever a
                                   third-party lookup is down is worse than
                                   the risk it guards against

HOW AN OPERATOR ANSWERS A HOLD. Either switch back to the network the
account was last used from (the hold lifts by itself, it is re-checked on
the next attempt), or paste fresh cookies exported from the browser while
on the NEW network -- re-pasting clears the remembered place, because a
fresh login from here is exactly what makes here legitimate.
EGRESS_GUARD=false (META_EGRESS_GUARD in settings) turns the whole thing off.

WHAT IT SENDS. One request to a public IP-geolocation service (the same two
the Scheduler's timezone detection already uses), cached for ten minutes.
That service sees this machine's public address, as every website does.
Only the COUNTRY CODE and the network operator's name are kept -- never the
address itself.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

# Same two services, same order, as api/scheduler.py::detect_timezone.
ENDPOINTS = ("https://ipinfo.io/json", "https://ipapi.co/json/")
CACHE_S = 600.0
LOOKUP_TIMEOUT_S = 4.0

OK = "ok"
FIRST = "first"
CHANGED = "changed"
UNKNOWN = "unknown"

_cache: dict[str, Any] = {"at": 0.0, "value": None}
_last_warned = 0.0


def reset() -> None:
    """For tests: forget the cached lookup."""
    _cache["at"] = 0.0
    _cache["value"] = None


def parse(body: bytes | str) -> Optional[dict]:
    """A lookup response -> {"country": "IN", "org": "AS... Name"}, or None
    when it does not say where the machine is."""
    try:
        data = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
    except Exception:                                   # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    country = str(data.get("country_code") or data.get("country") or "").strip().upper()
    # ipinfo says "IN"; ipapi.co says country="IN" too. A long name here
    # means a field we did not expect, which is treated as no answer rather
    # than compared as if it were a code.
    if len(country) != 2 or not country.isalpha():
        return None
    org = str(data.get("org") or data.get("asn") or "").strip()[:80]
    return {"country": country, "org": org}


async def current(*, fetch=None, now: Optional[float] = None) -> Optional[dict]:
    """Where this machine is leaving from right now, cached ten minutes.
    None when no service answered. `fetch` is injectable for tests."""
    t = time.time() if now is None else now
    if _cache["value"] is not None and t - _cache["at"] < CACHE_S:
        return _cache["value"]
    if fetch is None:
        from backend.shared import fast_http
        fetch = fast_http.fetch
    for endpoint in ENDPOINTS:
        try:
            res = await fetch(endpoint, timeout=LOOKUP_TIMEOUT_S)
            if getattr(res, "status", 0) == 200 and getattr(res, "body", None):
                got = parse(res.body)
                if got:
                    _cache["at"] = t
                    _cache["value"] = got
                    return got
        except Exception:                               # noqa: BLE001
            continue
    return None


def judge(remembered: Optional[dict], now: Optional[dict]) -> tuple[str, str]:
    """(verdict, reason). Pure, so every branch is a one-line test."""
    if not now:
        return UNKNOWN, "could not tell which country this machine is in"
    country = (remembered or {}).get("country") or ""
    if not country:
        return FIRST, ""
    if country == now["country"]:
        return OK, ""
    return CHANGED, (
        f"network changed: this account last worked from {country} and this machine is "
        f"now in {now['country']}. Instagram treats a sudden move like that as a possible "
        f"hijack (checkpoint or suspension), so the account is on hold. Switch back to the "
        f"original network and it resumes by itself, or paste fresh cookies exported "
        f"from the browser on this network to accept the new location"
    )


def should_remember(remembered: Optional[dict], now: dict, at: float,
                    refresh_s: float = 3600.0) -> bool:
    """Write the place back only when it is new or has changed network --
    not on every claim."""
    r = remembered or {}
    if r.get("country") != now["country"] or r.get("org") != now["org"]:
        return True
    return at - float(r.get("at") or 0) >= refresh_s


def note_unknown(log, now: Optional[float] = None) -> None:
    """One warning an hour, so a down lookup service is visible without
    filling the log."""
    global _last_warned
    t = time.time() if now is None else now
    if t - _last_warned >= 3600.0:
        _last_warned = t
        log.warning("egress guard: could not look up this machine's country -- letting the "
                    "job through without the network-change check")
