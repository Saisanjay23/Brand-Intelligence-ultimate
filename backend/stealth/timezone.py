"""The IANA timezone id a session's browser context claims.

ONE VALUE FOR EVERY SESSION, ON PURPOSE. A claimed timezone that disagrees
with the real egress IP's geography is a worse tell than every session
sharing one, and with no per-session egress there is nothing for a
diversified timezone to agree WITH -- every context leaves from this host's
own address, so they genuinely are all in one place. Claiming so is the
honest answer.

WHY IT IS SETTABLE RATHER THAN A CONSTANT. That reasoning depends on one
fact: which country this host actually leaves from. Hardcoding
"Asia/Kolkata" was right while the answer was "always India", and becomes
wrong the moment the host runs behind a VPN -- every context then announces
Kolkata from, say, a Singapore exit, which is the exact mismatch the first
paragraph calls the worse tell. The platforms see both halves; only this
value is ours to keep honest.

So: set BROWSER_TIMEZONE_ID to the zone of whatever the traffic actually
leaves from, and leave it alone if that is India.

    # .env -- host routed through a Singapore VPN
    BROWSER_TIMEZONE_ID=Asia/Singapore

HOW TO FIND THE RIGHT VALUE, rather than guessing: ipinfo.io/json reports a
`timezone` for the address you are actually leaving from. Whatever it says
while the VPN is up is the value to put here.

THIS IS NOT PER-PLATFORM, and it cannot be. One host has one egress, so one
timezone is the truth for every context on it. A platform that needs a
different exit needs a different route, not a different claim about the same
one.

(An earlier version derived the id from a proxy's declared region, which was
the one case where a per-session non-default was safe. Proxy support has
since been removed from the tool, so that branch went with it -- but the
principle it encoded is the one above: the claim follows the egress.)
"""

from __future__ import annotations

_FALLBACK_TIMEZONE_ID = "Asia/Kolkata"


def _configured() -> str:
    """The operator's value, or the fallback.

    Read at import, the way every other setting in this tool is: the
    process reloads its .env on restart, and a browser context is built
    long after that. Wrapped in a try so a settings import problem cannot
    stop a browser from launching over a cosmetic field -- a session with
    a slightly wrong timezone still works; one that cannot start does not.
    """
    try:
        from backend.config.settings import settings

        return (getattr(settings, "browser_timezone_id", "") or "").strip() \
            or _FALLBACK_TIMEZONE_ID
    except Exception:                                 # noqa: BLE001
        return _FALLBACK_TIMEZONE_ID


DEFAULT_TIMEZONE_ID = _configured()
