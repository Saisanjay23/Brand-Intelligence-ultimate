"""Where a failure happened in OUR code, and what to do about it.

Every failure alert this system sends has to answer three questions for the
engineer who opens it: which platform, which file and line, and how to fix
it. This module answers the last two, and nothing here ever raises -- a
diagnostic that fails must degrade to "location unknown", never take the
alert (or the sweep it describes) down with it.

THREE WAYS TO A LOCATION, from most to least exact:

  1. `where(exc)` -- the traceback of an exception we still hold. The
     deepest frame inside this repository is the line that actually raised.
  2. `stop_site(platform, code)` -- a sweep that ended itself with a stop
     code (`stalled`, `checkpoint`, ...) raised nothing, so there is no
     traceback. The line that ASSIGNS that code in the platform's engine is
     the decision point, and is found by searching the source.
  3. `field_site(platform, field)` -- a profile read that came back without
     a field. The function that reads that field is where to look.

LINE NUMBERS ARE LOOKED UP, NEVER HARD-CODED. A table of line numbers is
wrong after the next commit and then quietly sends an engineer to the wrong
place. Searching the live source for the statement costs a file read (cached
by modification time) and stays correct as the code moves.
"""

from __future__ import annotations

import os
import re
import traceback
from pathlib import Path
from typing import Optional

# The repository root: this file is backend/shared/diagnostics.py.
_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"


def _rel(path: str) -> str:
    try:
        return Path(path).resolve().relative_to(_ROOT).as_posix()
    except Exception:                                   # noqa: BLE001
        return str(path)


def _ours(path: str) -> bool:
    """A frame in this repository's backend, not the stdlib or a library."""
    try:
        p = Path(path).resolve()
        return _BACKEND in p.parents and "site-packages" not in p.parts
    except Exception:                                   # noqa: BLE001
        return False


def where(exc: BaseException) -> str:
    """"backend/x/y.py:123 in func() -- `the source line`" for the deepest
    frame of `exc`'s traceback that is in our code; "" when there is none
    (an exception built but never raised, or raised entirely in a library)."""
    try:
        frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
        ours = [f for f in frames if _ours(f.filename)]
        if not ours:
            return ""
        f = ours[-1]
        src = (f.line or "").strip()
        out = f"{_rel(f.filename)}:{f.lineno} in {f.name}()"
        return f"{out} -- `{src[:160]}`" if src else out
    except Exception:                                   # noqa: BLE001
        return ""


# ----------------------------------------------------------- source search

_SOURCE_CACHE: dict[str, tuple[float, list[str]]] = {}


def _lines(relpath: str) -> list[str]:
    path = _ROOT / relpath
    try:
        mtime = os.path.getmtime(path)
        cached = _SOURCE_CACHE.get(relpath)
        if cached and cached[0] == mtime:
            return cached[1]
        lines = path.read_text(encoding="utf-8").splitlines()
        _SOURCE_CACHE[relpath] = (mtime, lines)
        return lines
    except Exception:                                   # noqa: BLE001
        return []


def locate(relpath: str, pattern: str) -> str:
    """"relpath:N" for the first line matching the regex `pattern`; just
    `relpath` when nothing matches (still the right file to open)."""
    try:
        rx = re.compile(pattern)
        for i, line in enumerate(_lines(relpath), 1):
            if rx.search(line):
                return f"{relpath}:{i}"
    except Exception:                                   # noqa: BLE001
        pass
    return relpath


def discovery_file(platform: str) -> str:
    return f"backend/platforms/{platform}/discovery_engine.py"


def analysis_file(platform: str) -> str:
    return f"backend/platforms/{platform}/analysis_engine.py"


# Stop codes that no engine sets itself: the runner does. ("error" is set
# by both; an exception the runner caught carries its own traceback
# location instead, so `stop_site` only ever needs the engine's.)
_RUNNER_STOPS = {
    "session-failed": r'"session-failed", False',
}


def stop_site(platform: str, stop: str) -> str:
    """The line in `platform`'s discovery engine that decides a sweep ended
    with `stop`. HTTP codes (`http-403`) are set from an f-string, so the
    search falls back to the prefix."""
    code = (stop or "").strip()
    if not code:
        return ""
    if code in _RUNNER_STOPS:
        return locate("backend/discovery/runner.py", _RUNNER_STOPS[code])
    path = discovery_file(platform)
    # `out.stopped = "x"` and `out.stopped, out.complete/error = "x", ...`
    exact = rf'stopped(?:\s*,\s*out\.\w+)*\s*=\s*.*"{re.escape(code)}"'
    hit = locate(path, exact)
    if hit != path:
        return hit
    if code.startswith("http-"):
        return locate(path, r'stopped\s*=\s*(f"http-|"http-)')
    if code in ("login", "checkpoint"):
        return locate(path, r"(_x_)?session_blocked\(")
    # Any other statement using the code -- never a comment or docstring.
    return locate(path, rf'^\s*[^#\s"].*=.*"{re.escape(code)}"')


# Which function reads each analysed field, per platform. The pattern is a
# `def` line, found fresh each time -- see the module docstring.
_FIELD_READERS: dict[str, dict[str, str]] = {
    "facebook": {
        "display name": r"^def read_name\(",
        "followers": r"^def read_counts\(",
        "last post date": r"^def read_last_post\(",
        "screenshot": r"    async def screenshot\(",
    },
    "twitter": {
        "display name": r"    def fill\(",
        "followers": r"    def fill\(",
        "last post date": r"^async def dom_last_post\(",
        "screenshot": r"    async def screenshot\(",
    },
    "instagram": {
        "display name": r"    def fill\(",
        "followers": r"    def fill\(",
        "last post date": r"    async def read_last_post_date\(",
        "screenshot": r"    async def screenshot\(",
    },
    "tiktok": {
        "display name": r"    def fill\(",
        "followers": r"    def fill\(",
        "last post date": r"    def fill\(",
        "screenshot": r"    async def screenshot\(",
    },
    "youtube": {
        "display name": r"    def fill\(",
        "followers": r"    def fill\(",
        "last post date": r"await self\.api\.latest_upload\(",
    },
    "telegram": {
        "display name": r"    def fill\(",
        "followers": r"    def fill\(",
        "last post date": r"    def fill\(",
    },
}


def field_site(platform: str, field: str) -> str:
    path = analysis_file(platform)
    pattern = (_FIELD_READERS.get(platform) or {}).get(field)
    return locate(path, pattern) if pattern else path


# ------------------------------------------------------------- the advice

class Advice:
    """What happened, the likely cause, and the fix -- in that order."""

    __slots__ = ("title", "cause", "fix", "severity")

    def __init__(self, title: str, cause: str, fix: str, severity: str = "critical"):
        self.title, self.cause, self.fix, self.severity = title, cause, fix, severity


_SESSION_FIX = ("Open Admin -> Sessions, check this platform's account(s), and paste "
                "freshly exported cookies for any marked expired or checkpointed. If "
                "every account is healthy, log in to one by hand in a normal browser to "
                "clear any challenge the platform is showing.")

STOP_ADVICE: dict[str, Advice] = {
    "stalled": Advice(
        "Search stopped returning new results (stalled)",
        "The results page kept scrolling but no new result payloads were parsed. "
        "Either the platform changed its page or response format so our parser no "
        "longer recognises the results, or the page never loaded its results "
        "(slow network, soft throttling).",
        "Open the keyword's search by hand under the same account. If results are "
        "visible there, the parser has drifted: capture the search response in "
        "DevTools and compare it with the parsing code at the location given. If "
        "the page is empty or slow, check the proxy/network and the account's "
        "health, then re-run the missed keywords from the Scheduler."),
    "error": Advice(
        "Search raised an error",
        "Our code hit an exception while running or saving this search. The error "
        "text and location identify it exactly.",
        "Open the file and line given, reproduce with the keyword shown, and fix the "
        "failing statement. The keyword is recorded as still owed and is picked up "
        "by the next gap-closing run once fixed."),
    "checkpoint": Advice(
        "Account hit a security checkpoint",
        "The platform showed a verification/challenge page instead of search "
        "results. The account has been taken out of rotation automatically.",
        _SESSION_FIX),
    "checkpointed": Advice(
        "Account hit a security checkpoint",
        "The platform showed a verification/challenge page instead of results.",
        _SESSION_FIX),
    "login": Advice(
        "Account is logged out",
        "The search page redirected to the platform's login wall, so the stored "
        "cookies are no longer accepted.",
        _SESSION_FIX),
    "rate_limited": Advice(
        "Platform rate-limited the account",
        "Too many requests in a short time. The account is cooling off "
        "automatically.",
        "No immediate action needed if it recovers. If this repeats, lower the "
        "platform's concurrency or add another account to the session pool so the "
        "load is spread."),
    "flood-wait": Advice(
        "Telegram asked us to slow down (FloodWait)",
        "Telegram limits how many searches one account may make; the platform was "
        "stopped for the rest of this run to protect the account.",
        "Wait for the flood period to pass (the error shows how long). Reduce the "
        "number of Telegram keywords per run or add a second Telegram account."),
    "geoblocked": Advice(
        "Platform is blocked for this server's IP address",
        "The platform redirected every request to a regional notice page.",
        "Route this platform through a proxy in a region where it is available "
        "(Admin -> Sessions -> proxy), then re-run."),
    "quota": Advice(
        "YouTube API daily quota exhausted",
        "The YouTube Data API key has used its daily allowance (resets at midnight "
        "Pacific time).",
        "Wait for the reset, or add a second API key / request a quota increase in "
        "Google Cloud Console."),
    "empty-unconfirmed": Advice(
        "Empty results the platform did not confirm",
        "The search came back empty but the platform never showed its own "
        "'no results' message -- the signature of a throttled or silently blocked "
        "account rather than a genuinely empty search.",
        "Search the keyword by hand under the same account. If results exist, "
        "treat the account as throttled: rest it or rotate to another account."),
    "session-failed": Advice(
        "Every available account failed on this keyword",
        "Each account that tried the keyword died (checkpoint, logout or rate "
        "limit) and the retry budget ran out.",
        _SESSION_FIX),
    "mobile-api-failed-web-recovered": Advice(
        "Instagram's primary search API failed; the backup worked",
        "Results were still saved via the web search endpoint, but the primary "
        "mobile API is failing.",
        "Check the primary request at the location given (headers / app id / user "
        "agent). Fix it before the backup path breaks too.", "warning"),
    "cap:seconds": Advice(
        "Search ran out of its time budget",
        "The keyword still had results when the per-search time limit was reached.",
        "If complete coverage matters for this client, raise the client's run "
        "budget (Clients -> Scheduler settings) or lower its result cap.", "warning"),
    "cap:pages": Advice(
        "Search hit its page limit",
        "More result pages existed than the configured page budget allowed.",
        "Raise the page budget if full coverage is needed.", "warning"),
}


def stop_advice(stop: str) -> Advice:
    code = (stop or "").strip().lower()
    if code in STOP_ADVICE:
        return STOP_ADVICE[code]
    if code.startswith("http-200-status-"):
        return Advice(
            "Platform answered with a failure status inside a normal response",
            "The request succeeded at the HTTP level but the body said it failed -- "
            "usually a soft block, a challenge or a throttle on this account.",
            _SESSION_FIX)
    if m := re.match(r"http-(\d{3})$", code):
        status = int(m.group(1))
        if status in (401, 403):
            return Advice(f"Platform refused the request (HTTP {status})",
                          "The account's cookies were rejected or the request is "
                          "blocked for this account/IP.", _SESSION_FIX)
        if status == 429:
            return STOP_ADVICE["rate_limited"]
        return Advice(f"Platform returned HTTP {status}",
                      "The platform's server answered with an error status.",
                      "Retry later; if it persists, check the request at the "
                      "location given against a live capture.")
    return Advice(
        f"Search ended unexpectedly ({code or 'unknown reason'})",
        "The engine reported a stop reason this alert has no specific guidance for.",
        "Open the location given and read how this stop is decided; the keyword is "
        "recorded as still owed.")


def field_advice(platform: str, field: str) -> Advice:
    return {
        "display name": Advice(
            "Profile name not read",
            "The profile loaded but no display name was found in the platform's "
            "response or on the page -- usually a renamed field in the payload.",
            "Open one of the listed URLs in a logged-in browser, capture the profile "
            "response in DevTools, and update the name mapping at the location given."),
        "followers": Advice(
            "Follower / member count not read",
            "The count was absent from the payload and could not be read from the "
            "page header either.",
            "Compare a live capture of one listed profile with the count reader at "
            "the location given; the key or the header wording has likely changed."),
        "last post date": Advice(
            "Last post date not read",
            "The account appears to have posts, but no post date was found in the "
            "timeline payload or on the rendered page -- often the timeline had not "
            "loaded yet, or its response was renamed.",
            "Re-analyse the listed URLs once; if the date is still missing, capture "
            "the timeline request for one of them and update the reader at the "
            "location given."),
        "screenshot": Advice(
            "Evidence screenshot not captured",
            "The page did not reach a paintable state before the capture ran, or the "
            "capture itself failed.",
            "Re-analyse the listed URLs. If it keeps failing, check the screenshot "
            "routine at the location given (content wait selector / timeout).",
            "warning"),
    }.get(field, Advice(f"{field} not read", "", "Check the reader at the location given."))


def status_advice(status: str) -> Optional[Advice]:
    return {
        "CHECKPOINT": Advice(
            "Account hit a checkpoint or rate limit while reading profiles",
            "The platform challenged or throttled the account partway through the "
            "batch. The account was taken out of rotation and the rest of its URLs "
            "were handed to another account where one was available.",
            _SESSION_FIX),
        "LOGIN_REQUIRED": Advice(
            "Account was logged out while reading profiles",
            "Profile pages redirected to the login wall; the stored cookies are no "
            "longer accepted.",
            _SESSION_FIX),
        "ERROR": Advice(
            "Profile could not be read",
            "The visit failed -- the error text and location say where.",
            "Open the location given and reproduce with one of the listed URLs. If "
            "the error is a navigation timeout, check the network/proxy and re-run."),
        "PARTIAL": Advice(
            "Profile page loaded but its data payload was not recognised",
            "Only partial data could be read from the rendered page.",
            "Capture the profile's network response for one listed URL and compare "
            "it with the parser at the location given.", "warning"),
    }.get((status or "").upper())
