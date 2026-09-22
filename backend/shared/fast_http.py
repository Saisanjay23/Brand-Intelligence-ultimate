"""HTTP that a CDN and a login wall both believe, without launching a browser.

WHAT THIS IS FOR. Three jobs in this tool need an HTTPS request that the far
end treats as a real desktop Chrome, and for all three a Playwright context
is orders of magnitude more machinery than the question deserves:

    avatars         Meta and Twitter's picture CDNs answer 403 to a plain
                    Python client no matter what User-Agent it claims,
                    because the tell is the TLS/HTTP2 handshake, not the
                    header block. See shared/imagefetch.py.

    session canary  "are these cookies still logged in" is one redirect on
                    two of the four cookie platforms. Today it costs a
                    Chromium launch. See sessions/manager.py.

    dead-URL sweep  a profile that 404s has nothing for a browser to read,
                    and analysis pays a full page load to find that out.
                    See analysis/runner.py.

`curl_cffi` answers all three: libcurl-impersonate replays a named Chrome
build's exact JA3/JA4 and HTTP2 SETTINGS, so the handshake itself stops
being the thing that gets refused.

WHAT THIS IS NOT FOR, AND THIS BOUNDARY IS THE WHOLE DESIGN. Nothing here
replaces a browser for READING a platform. Keyword search sweeps, profile
scraping and evidence screenshots stay on Playwright, permanently, because
every one of those depends on JavaScript having run -- an HTTP body from
x.com is an empty SPA shell whether the account exists or not. This module
is only ever allowed to answer questions a raw HTTP response answers
UNAMBIGUOUSLY.

    THE RULE EVERY FUNCTION BELOW FOLLOWS: an unambiguous answer, or no
    answer at all. Never a guess dressed up as a finding.

That rule is why `check_session_alive` and `preflight_url_status` are
tri-state and why their per-platform tables are short. Absence of a failure
string is not evidence of success (the lesson stealth/browser.py's
`expect_path` was added for), and a login wall served to a datacenter IP is
not evidence that an account is dead. Anything this module cannot prove, it
declines to answer, and the caller then does exactly what it did before this
module existed. Every integration is therefore strictly additive: it can
make a path faster, it cannot make a path wrong.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from backend.shared.logging import get_logger

log = get_logger("fast_http")


# --------------------------------------------------------------- availability
#
# IMPORTED DEFENSIVELY, ON PURPOSE. curl_cffi ships compiled binaries, so it
# is the one dependency in this project that can be listed in requirements
# and still fail to import on a given host (wrong wheel, missing VC runtime,
# a libcurl that will not load). Every caller of this module has a working
# path that predates it, so an import failure has to degrade to that path
# rather than take the process down at startup.
try:
    from curl_cffi.requests import AsyncSession as _AsyncSession
    from curl_cffi.requests.exceptions import RequestException as _RequestException

    _IMPORT_ERROR = ""
except Exception as e:                                # noqa: BLE001 - see above
    _AsyncSession = None                              # type: ignore[assignment]

    class _RequestException(Exception):               # type: ignore[no-redef]
        """Stand-in so `except _RequestException` still parses."""

    _IMPORT_ERROR = f"{type(e).__name__}: {e}"


# WHICH CHROME TO BE, chosen from what the INSTALLED curl_cffi actually
# ships rather than hardcoded. A pinned target is a version dependency in
# disguise: `chrome120` is not in every build, and a target that has been
# dropped raises at request time, on the avatar path, in production. Newest
# first, because an impersonation target that is two years stale is itself a
# tell -- the point is to look like a browser someone is actually running.
_IMPERSONATE_PREFERENCE = (
    "chrome146", "chrome142", "chrome136", "chrome131",
    "chrome124", "chrome120", "chrome110", "chrome",
)


def _supported_targets() -> frozenset[str]:
    try:
        import typing

        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        return frozenset(typing.get_args(BrowserTypeLiteral))
    except Exception:                                 # noqa: BLE001
        return frozenset()


def _pick_impersonate() -> str:
    known = _supported_targets()
    if not known:
        # Could not read the list -- "chrome" is curl_cffi's own rolling
        # alias and has existed for every release that has the feature.
        return "chrome"
    for target in _IMPERSONATE_PREFERENCE:
        if target in known:
            return target
    chromes = sorted(t for t in known if t.startswith("chrome") and t[6:].isdigit())
    return chromes[-1] if chromes else "chrome"


IMPERSONATE: str = _pick_impersonate() if _AsyncSession is not None else ""


def settings_enabled() -> bool:
    """The operator's kill switch, read live rather than captured at import
    so it can be flipped in a debugging session without a restart."""
    try:
        from backend.config.settings import settings

        return bool(getattr(settings, "fast_http_enabled", True))
    except Exception:                                 # noqa: BLE001
        return True


def available() -> bool:
    """Is the accelerated path usable in this process at all? Callers branch
    on this rather than on a try/except, so the fallback is visible in the
    code that owns it."""
    return _AsyncSession is not None and settings_enabled()


def why_unavailable() -> str:
    if _AsyncSession is None:
        return f"curl_cffi could not be imported ({_IMPORT_ERROR})"
    if not settings_enabled():
        return "disabled by settings (FAST_HTTP_ENABLED=false)"
    return ""


# ------------------------------------------------------------------- failures


class FastHttpError(Exception):
    """Base for everything this module raises."""


class FastHttpTransportError(FastHttpError):
    """NO ANSWER WAS OBTAINED -- DNS, TLS, a timeout, a libcurl fault, or the
    accelerator not being available at all.

    The distinction from the class below is load-bearing: this one means
    "ask someone else", which is what lets imagefetch fall back to its
    proven aiohttp path without ever double-fetching a URL that did in fact
    answer."""


class FastHttpTooLarge(FastHttpError):
    """The response ran past the byte cap. A DEFINITE answer about the URL,
    not a transport failure, so no caller should retry it elsewhere."""


class FastHttpWrongContentType(FastHttpError):
    """A 200 whose Content-Type was not what the caller said it would
    accept, refused BEFORE the body was read.

    Exists so imagefetch keeps the property it already had under aiohttp:
    an allowlisted host answering with a megabyte of HTML instead of an
    avatar is rejected on the header, not downloaded and then discarded.
    `actual` is the type that came back, for the caller's own message."""

    def __init__(self, actual: str) -> None:
        super().__init__(f"upstream answered Content-Type {actual or '(none)'}")
        self.actual = actual


# ----------------------------------------------------------------- the client
#
# ONE SESSION PER EVENT LOOP, not one per process and not one per call.
#
# Per call is the naive choice and it is the expensive one: a fresh TLS
# handshake for every avatar on a 25-card page, which is the exact cost
# shared/imagefetch.py's own pooled session was introduced to avoid.
#
# Per process is wrong for a different reason: curl_cffi's AsyncSession
# binds its multi-handle to the loop that first drives it, and this codebase
# creates fresh loops routinely (the test suite, `asyncio.run` in run.py). A
# session captured from a dead loop fails on its next use, once, in
# whichever caller happens to touch it first.
#
# Weak keys so a finished loop's entry disappears with it. Same pattern, and
# the same reasoning, as analysis/runner.py's `_worker_slots`.
_sessions: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()

_DEFAULT_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
}


def _session() -> Any:
    if _AsyncSession is None:
        raise FastHttpTransportError(why_unavailable())
    loop = asyncio.get_running_loop()
    s = _sessions.get(loop)
    if s is None:
        s = _AsyncSession(headers=dict(_DEFAULT_HEADERS))
        _sessions[loop] = s
    return s


async def close() -> None:
    """Called from main.py's lifespan shutdown, alongside the aiohttp and
    Mongo closes. Sessions belonging to other (already dead) loops are not
    touched -- those handles go with the loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    s = _sessions.pop(loop, None)
    if s is not None:
        try:
            await s.close()
        except Exception:                             # noqa: BLE001 - shutdown
            pass


# ------------------------------------------------------------------ primitive


@dataclass(frozen=True)
class FastResponse:
    """What one request produced. `headers` keys are lowercased, because HTTP
    header names are case-insensitive and callers should not have to remember
    which casing a given CDN chose today."""

    status: int
    headers: dict[str, str]
    body: bytes
    url: str                    # the effective URL curl ended on
    elapsed_ms: float = 0.0

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")

    @property
    def content_type(self) -> str:
        return self.header("content-type").split(";")[0].strip().lower()


# 8 MB, matching shared/imagefetch.MAX_BYTES. Deliberately NOT imported from
# there: a cap that exists to stop an unbounded read must not depend on an
# import that could fail, and imagefetch re-applies its own cap regardless.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

# Health probes and pre-flights read a page's first few KB at most; anything
# past that is a page we are not going to parse. Kept small deliberately -- a
# profile page is megabytes of JavaScript that proves nothing.
PROBE_MAX_BYTES = 256 * 1024


async def fetch(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    cookie_header: str = "",
    timeout: float = 15.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_redirects: bool = False,
    method: str = "GET",
    expect_content_type: str = "",
    read_body: bool = True,
    stop_markers: tuple[bytes, ...] = (),
    truncate: bool = False,
) -> FastResponse:
    """One request, body streamed and hard-capped at `max_bytes`.

    `allow_redirects` DEFAULTS TO FALSE, unlike every HTTP client's own
    default, because both callers that matter need to see the hop rather
    than have it taken for them: imagefetch re-checks its SSRF allowlist on
    every redirect target, and the session canary's whole verdict IS the
    redirect. Following by default would silently defeat both.

    `read_body=False` returns as soon as the status line and headers are in,
    and closes the transfer without draining it. For a caller whose whole
    question IS the status -- the dead-URL pre-flight -- that is the
    difference between a header exchange and downloading a hundred kilobytes
    of JavaScript per URL, which measurably it was.

    `stop_markers` ends the read as soon as one of them appears in the body.
    A caller whose question is answered by a marker should not go on paying
    for the rest of the page: Instagram's profile pages are 600-800 KB, and
    the marker that settles a live one sits about 9 KB in.

    `truncate` changes what hitting the cap MEANS. False (the default) says
    a body past `max_bytes` is a failure -- what an avatar fetch needs,
    since half an image is not an image. True says "read this much and stop",
    which is what a caller scanning for a marker wants: not finding one
    inside the budget is an answer (no marker), not an error.

    `expect_content_type` is a PREFIX ("image/") checked against a 200's
    Content-Type before a single body byte is read, raising
    FastHttpWrongContentType when it does not match. It is there so a caller
    that only ever wants one kind of thing does not pay to download
    something else first.

    Raises FastHttpTransportError when no answer was obtained, and
    FastHttpTooLarge when one was but it ran past the cap. Any HTTP status
    at all -- 403, 404, 500 -- comes back as a FastResponse, never as an
    exception: a status is an answer.
    """
    if not available():
        raise FastHttpTransportError(why_unavailable())

    req_headers = dict(headers or {})
    if cookie_header:
        req_headers["Cookie"] = cookie_header

    started = time.monotonic()
    session = _session()
    try:
        async with session.stream(
            method, url,
            headers=req_headers,
            timeout=timeout,
            impersonate=IMPERSONATE,
            allow_redirects=allow_redirects,
            # Never let one caller's cookies reach another caller's request
            # through the shared jar. Everything here that needs cookies
            # passes them explicitly, per request.
            discard_cookies=True,
        ) as resp:
            status = int(resp.status_code)
            hdrs = {str(k).lower(): str(v) for k, v in resp.headers.items()}
            effective = str(getattr(resp, "url", url) or url)
            if expect_content_type and status == 200:
                got = (hdrs.get("content-type") or "").split(";")[0].strip().lower()
                if not got.startswith(expect_content_type):
                    raise FastHttpWrongContentType(got)
            if not read_body:
                # The `stream` context manager closes the response on the
                # way out, which aborts the transfer. Nothing more is read.
                return FastResponse(
                    status=status, headers=hdrs, body=b"", url=effective,
                    elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                )
            chunks: list[bytes] = []
            total = 0
            # Markers can straddle a chunk boundary, so each chunk is
            # searched together with the tail of the one before it.
            overlap = (max(len(m) for m in stop_markers) - 1) if stop_markers else 0
            tail = b""
            # MUST loop with a running total. The cap is the only thing
            # between this and an unbounded read into memory, and a single
            # read() would return whatever happened to be in the first
            # chunk -- see the identical note in imagefetch.fetch_image,
            # which is there because that exact mistake served a 1-byte
            # "image" for a 110 KB avatar.
            async for chunk in resp.aiter_content():
                total += len(chunk)
                if total > max_bytes:
                    if not truncate:
                        raise FastHttpTooLarge(f"response exceeded {max_bytes} bytes")
                    chunks.append(chunk[:max_bytes - (total - len(chunk))])
                    break
                chunks.append(chunk)
                if stop_markers:
                    window = tail + chunk
                    if any(m in window for m in stop_markers):
                        break
                    tail = window[-overlap:] if overlap else b""
    except (FastHttpTooLarge, FastHttpWrongContentType):
        raise
    except asyncio.CancelledError:
        # Never swallowed: a cancel is a stop the analyst asked for, and
        # turning it into a transport error would make a caller fall back
        # and start a second request on the way out.
        raise
    except _RequestException as e:
        raise FastHttpTransportError(f"{type(e).__name__}: {e}") from e
    except Exception as e:                            # noqa: BLE001
        raise FastHttpTransportError(f"{type(e).__name__}: {e}") from e

    return FastResponse(
        status=status,
        headers=hdrs,
        body=b"".join(chunks),
        url=effective,
        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
    )


async def fetch_bytes(
    url: str,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 15.0,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> bytes:
    """The simple form: the body of a 200, or an exception.

    Convenience over `fetch` for callers with no redirect or status logic of
    their own. imagefetch deliberately does NOT use this -- it has to
    re-validate every redirect hop against its allowlist, so it needs the
    response, not just the bytes.
    """
    resp = await fetch(url, headers=headers, timeout=timeout, max_bytes=max_bytes)
    if resp.status != 200:
        raise FastHttpTransportError(f"upstream answered HTTP {resp.status}")
    return resp.body


# ------------------------------------------------------------ cookie plumbing


def cookie_header(cookies: list[dict], *, host: str = "", now: Optional[float] = None) -> str:
    """Stored Playwright-shaped cookies -> one `Cookie:` header value.

    The stored shape is sessions/cookies.py::normalize_cookies': name,
    value, domain (always leading-dot), path, secure, httpOnly, sameSite,
    and an optional `expires` in unix seconds.

    ALREADY-EXPIRED COOKIES ARE DROPPED, because sending them is worse than
    useless: it is how a session missing one cookie gets reported as logged
    out. `expires` absent means a session cookie, which has not expired.

    `host` filters by domain suffix the way a browser would. One login spans
    several hosts (x.com and twitter.com, instagram.com and
    i.instagram.com), so the match is on the stored domain's suffix, never
    on equality.
    """
    now = time.time() if now is None else now
    host = (host or "").lower().lstrip(".")
    parts: list[str] = []
    seen: set[str] = set()
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        if not name or name in seen:
            continue
        exp = c.get("expires")
        try:
            if exp is not None and float(exp) > 0 and float(exp) <= now:
                continue
        except (TypeError, ValueError):
            pass
        if host:
            dom = str(c.get("domain") or "").lower().lstrip(".")
            if dom and not (host == dom or host.endswith("." + dom)):
                continue
        seen.add(name)
        parts.append(f"{name}={c.get('value', '')}")
    return "; ".join(parts)


# What a real navigation sends alongside the impersonated handshake. Split
# out because the canary and the pre-flight both need exactly this set.
_DOCUMENT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# -------------------------------------------- job 2: the zero-browser canary


@dataclass(frozen=True)
class SessionVerdict:
    """TRI-STATE, and the third state is the important one.

        alive=True   positively confirmed still logged in
        alive=False  positively confirmed logged out or challenged
        alive=None   NOT ANSWERED. The caller must fall back to the browser
                     check; it must never record this as either outcome.

    A plain bool here would force every "I could not tell" into one of the
    two real verdicts, and both directions do damage: folded into False it
    quarantines healthy accounts until the pool is empty, folded into True
    it reports a dead pool healthy. This codebase already learned that once
    -- see sessions/manager.py's `conclusive` flag, which exists for exactly
    this reason.
    """

    alive: Optional[bool]
    reason: str = ""
    status: int = 0
    landed: str = ""

    @property
    def conclusive(self) -> bool:
        return self.alive is not None


@dataclass(frozen=True)
class _Probe:
    """One platform's "am I logged in" request, mirroring the arguments that
    platform already passes to stealth/browser.py::Session.check_session."""

    url: str
    host: str
    expect_path: str = ""
    deny_paths: tuple[str, ...] = ()


# WHICH PLATFORMS GET AN HTTP FAST PATH, AND WHY THE OTHER TWO DO NOT.
#
# The browser check judges the SETTLED url -- where the page ended up after
# JavaScript ran. Over plain HTTP the only redirect visible is the one the
# server sends. So a platform belongs here if and only if its logged-out
# bounce is a real 30x from the server; if it bounces client-side, HTTP sees
# a 200 on the authenticated URL for a dead session and would call it alive.
#
#   facebook   /me is a server 302 either way: to the account's own profile
#              when logged in, to a login door when not. The same signal the
#              browser check reads, one hop earlier.
#   instagram  /accounts/edit/ is a server 302 to /accounts/login/ when
#              logged out and a 200 when not. This is exactly what
#              expect_path was added for, and it survives the trip to HTTP.
#
#   twitter    DELIBERATELY ABSENT. x.com/home serves the same 200 SPA shell
#              logged in or out and bounces client-side -- its own
#              check_session docstring records that the logged-out text
#              matches neither RE_LOGIN nor RE_CHECKPOINT, and that
#              deny_paths on the settled url is the only thing that catches
#              it. HTTP cannot see that, so an HTTP probe here would report
#              every dead X session healthy. That is the single worst
#              failure this module could produce, so X is not probed at all.
#   tiktok     DELIBERATELY ABSENT. /upload sits behind a bot wall that
#              answers a non-browser client with a challenge regardless of
#              the cookies, which reads as "logged out" for a perfectly good
#              session. Abstaining costs a Chromium launch; guessing costs
#              the account.
_PROBES: dict[str, _Probe] = {
    "facebook": _Probe(
        url="https://www.facebook.com/me",
        host="www.facebook.com",
        deny_paths=("/", "/index.php", "/login", "/login.php", "/checkpoint"),
    ),
    "instagram": _Probe(
        url="https://www.instagram.com/accounts/edit/",
        host="www.instagram.com",
        expect_path="/accounts/edit",
    ),
}

# Landing on one of these is positive confirmation of a DEAD session on any
# platform, whatever else the probe table says.
_DEAD_PATH_MARKERS = ("/login", "/checkpoint", "/challenge", "/accounts/login")


def probe_for(platform_id: str) -> Optional[_Probe]:
    """The probe this platform would use, or None when it has none. Public
    so callers (and tests) can ask whether a fast path exists without
    issuing a request."""
    return _PROBES.get(platform_id)


# Raw-HTML tells that a "logged-in" page is actually a login wall. These are
# NOT the platforms' own RE_LOGIN patterns, on purpose: those are tuned for
# the text a rendered page shows, and this looks at markup. They can only
# ever turn a pass into "unknown" (see check_session_alive), so a false
# positive here costs one browser check and nothing else.
_LOGIN_WALL_MARKERS = (
    b"LoginAndSignupPage",
    b"/accounts/login/",
    b"loginForm",
    b'name="login_form',
    b"login_form[username]",
)


def _looks_like_login_wall(body: bytes) -> bool:
    if not body:
        return False
    head = body[:65536]
    return any(m in head for m in _LOGIN_WALL_MARKERS)


def _settled_path(raw: str) -> str:
    try:
        return (urlparse(raw).path or "/").rstrip("/") or "/"
    except Exception:                                 # noqa: BLE001
        return ""


async def check_session_alive(
    platform_id: str,
    cookies: list[dict],
    *,
    timeout: float = 10.0,
) -> SessionVerdict:
    """Are these cookies still a logged-in session? Tri-state -- read
    SessionVerdict's own docstring before using the answer.

    ~150ms against a Chromium launch, a context, a navigation and a 2.5s
    settle. That is the entire point, and it is also why this is allowed to
    answer only for the platforms in `_PROBES`: a fast wrong answer about a
    session is more expensive than a slow right one, because the pool is
    what every sweep in this tool runs on.
    """
    probe = _PROBES.get(platform_id)
    if probe is None:
        return SessionVerdict(None, f"{platform_id} has no HTTP-provable session signal")
    if not available():
        return SessionVerdict(None, why_unavailable())

    header = cookie_header(cookies, host=probe.host)
    if not header:
        # No usable cookie at all is the caller's business to interpret --
        # it may mean the item was saved without any, which is not the same
        # finding as "the platform rejected them".
        return SessionVerdict(None, "no unexpired cookies for this host")

    try:
        resp = await fetch(
            probe.url,
            cookie_header=header,
            timeout=timeout,
            max_bytes=PROBE_MAX_BYTES,
            allow_redirects=False,
            headers=_DOCUMENT_HEADERS,
        )
    except FastHttpError as e:
        return SessionVerdict(None, f"probe could not run: {e}")

    # A 30x IS the answer on both probed platforms; where it points decides.
    if resp.status in (301, 302, 303, 307, 308):
        loc = resp.header("location")
        if not loc:
            return SessionVerdict(
                None, f"HTTP {resp.status} with no Location header", status=resp.status)
        target = urljoin(probe.url, loc)
        path = _settled_path(target)
        if any(m in path for m in _DEAD_PATH_MARKERS):
            return SessionVerdict(
                False, f"redirected to {path}", status=resp.status, landed=target)
        if path in probe.deny_paths:
            return SessionVerdict(
                False, f"redirected to a logged-out door ({path})",
                status=resp.status, landed=target)
        if probe.expect_path and probe.expect_path.rstrip("/") not in path:
            return SessionVerdict(
                False, f"redirected off {probe.expect_path} to {path}",
                status=resp.status, landed=target)
        if probe.deny_paths and not probe.expect_path:
            # Facebook: /me redirecting ANYWHERE that is not a login door is
            # the account's own profile, which only a logged-in session gets.
            return SessionVerdict(
                True, f"/me resolved to {path}", status=resp.status, landed=target)
        return SessionVerdict(
            None, f"redirected to an unrecognised path ({path})",
            status=resp.status, landed=target)

    if resp.status == 200:
        if probe.expect_path:
            # A 200 STILL HAS TO BE READ, not just counted. The browser
            # check's expect_path is evaluated on a page that has rendered;
            # here it is evaluated on a status line, and a platform that
            # chooses to serve its login wall AT the authenticated URL
            # rather than redirect would satisfy the status and nothing
            # else. So the body gets one cheap look, and a login wall in it
            # DOWNGRADES TO UNKNOWN rather than to dead -- the markers are
            # heuristics over raw HTML, which is enough to withhold a pass
            # and nowhere near enough to condemn an account.
            if _looks_like_login_wall(resp.body):
                return SessionVerdict(
                    None, "200 on the authenticated path, but the body looks "
                          "like a login wall", status=200)
            # Instagram: still being served the authenticated page, with no
            # bounce and no wall, IS the positive confirmation -- the same
            # reasoning expect_path encodes for the browser check.
            return SessionVerdict(True, f"200 on {probe.expect_path}", status=200)
        # Facebook answering 200 on /me without redirecting is not a shape
        # either state produces. Say so rather than pick one.
        return SessionVerdict(None, "200 without the expected redirect", status=200)

    if resp.status in (401, 403):
        # AMBIGUOUS BY DESIGN. This is both what a rejected session looks
        # like and what a bot wall looks like, and there is no way to tell
        # them apart from here. The browser check can.
        return SessionVerdict(
            None, f"HTTP {resp.status} (rejected session or bot wall)", status=resp.status)

    return SessionVerdict(None, f"HTTP {resp.status}", status=resp.status)


# -------------------------------------------- job 3: the dead-URL pre-flight


# WHICH PLATFORMS CAN PROVE "THIS PROFILE IS GONE" OVER PLAIN HTTP.
#
# MEASURED, NOT REASONED. This table was first written from what each
# platform "should" do and every entry in it was wrong. What follows is what
# they actually answered, logged out, through the impersonated client, on
# 2026-09-22. Re-measure before changing it.
#
#   twitter (x.com)  STATUS, CORROBORATED BY THE PAGE. 404 for a missing
#                    handle (3/3), 200 for a live one (5/5), and the 404
#                    page says so in words -- "User Profile Not Found - X |
#                    404 Error" in the title, "could not be found" in its
#                    og:description. BOTH are required here (`require_both`)
#                    because a status alone cannot tell a removed profile
#                    from a client the platform has decided to block: a
#                    block page would not carry X's own removal wording.
#                    A sample too long to be a legal handle (>15 chars)
#                    answers 200 and is correctly not settled.
#
#   instagram        THE PAGE ALONE, because the status carries nothing --
#                    200 for a missing handle AND 200 for a live one. What
#                    separates them is Instagram's own error-page React root,
#                    `PolarisErrorRoot`, present on 3/3 missing profiles and
#                    absent from 3/3 live ones. Checked for leakage first,
#                    because a marker that also appears on a wall would
#                    condemn live profiles whenever we were rate-limited:
#                    it is absent from the login wall, the site root and
#                    /explore/. A live profile is recognised a second way,
#                    by its og:description, which vetoes a dead verdict
#                    outright and lands ~9 KB in -- so a live profile costs
#                    about 9 KB to clear and only a missing one is read far
#                    enough (~312 KB) to find the error marker.
#
#   tiktok           ABSENT. A missing and a live @handle were both
#                    redirected to a regional consent page and answered 200
#                    with byte-identical bodies. There is nothing to read.
#   facebook         ABSENT. 200 for both. Logged out, Facebook answers a
#                    wall rather than a 404.
#   youtube          ABSENT, and it would be pure cost: analysis reads the
#                    whole batch through ONE channels.list call, which
#                    already reports a missing channel exactly and cheaply.
#   telegram         ABSENT for the same reason -- the visit is MTProto, not
#                    a browser, so there is no page load to save.
#
# The cost of a wrong entry is not a wasted request: it is a profile
# reported to an analyst as gone that nobody ever looked at, in a record
# that feeds a takedown.


@dataclass(frozen=True)
class _DeathSignal:
    """How one platform says a profile is gone.

    `alive_markers` are checked FIRST and veto everything: a positive sign
    of a real profile outranks any death signal, because the failure it
    prevents (condemning a live account) is the one that matters.
    """

    dead_statuses: tuple[int, ...] = ()
    dead_markers: tuple[bytes, ...] = ()
    alive_markers: tuple[bytes, ...] = ()
    # True: the status AND a marker must both agree before anything settles.
    require_both: bool = False
    probe_bytes: int = 64 * 1024


_DEATH_SIGNALS: dict[str, _DeathSignal] = {
    "twitter": _DeathSignal(
        dead_statuses=(404, 410),
        dead_markers=(b"User Profile Not Found", b"could not be found"),
        require_both=True,
        probe_bytes=64 * 1024,          # markers land at ~600 B and ~3.4 KB
    ),
    "instagram": _DeathSignal(
        dead_markers=(b"PolarisErrorRoot",),
        alive_markers=(b'property="og:description"',),
        probe_bytes=512 * 1024,         # error marker lands at ~312 KB
    ),
}


@dataclass(frozen=True)
class PreflightResult:
    """`is_dead` is True ONLY on a positive confirmation.

    False means "not proven dead", which deliberately covers both a live
    profile and one this could not judge, because the caller treats those
    identically: hand it to the browser. `checked` records whether a request
    was made at all, so a platform that abstains stays distinguishable in
    the logs from one that answered.
    """

    url: str
    is_dead: bool = False
    status_code: int = 0
    reason: str = ""
    checked: bool = False

    def to_dict(self) -> dict:
        return {
            "url": self.url, "is_dead": self.is_dead,
            "status_code": self.status_code, "reason": self.reason,
            "checked": self.checked,
        }


def preflight_supported(platform_id: str) -> bool:
    return platform_id in _DEATH_SIGNALS


async def preflight_url_status(
    url: str,
    platform_id: str = "",
    *,
    timeout: float = 8.0,
) -> PreflightResult:
    """Is this profile URL provably gone, without visiting it in a browser?

    Answers `is_dead=True` only when the platform's own measured signal says
    so (see `_DEATH_SIGNALS`). Everything else -- a live-looking page, a bot
    wall, a rate limit, a timeout, an unlisted platform -- comes back
    not-dead, which means "I am not the one to decide this" and costs the
    caller exactly the browser visit it was going to make anyway.
    """
    signal = _DEATH_SIGNALS.get(platform_id) if platform_id else None
    if platform_id and signal is None:
        return PreflightResult(url, reason=f"{platform_id} has no HTTP-provable death signal")
    if signal is None:
        return PreflightResult(url, reason="no platform given, so no signal to read")
    if not available():
        return PreflightResult(url, reason=why_unavailable())

    needs_body = bool(signal.dead_markers or signal.alive_markers)
    try:
        resp = await fetch(
            url,
            timeout=timeout,
            max_bytes=signal.probe_bytes,
            # Followed here, unlike everywhere else in this module: a
            # profile URL legitimately redirects (a renamed handle, a locale
            # prefix), and what matters is the page at the end of the chain.
            allow_redirects=True,
            headers=_DOCUMENT_HEADERS,
            read_body=needs_body,
            stop_markers=signal.dead_markers + signal.alive_markers,
            # Running out of budget without finding a marker is an answer
            # ("no marker"), not a failure.
            truncate=True,
        )
    except FastHttpError as e:
        return PreflightResult(url, reason=f"pre-flight could not run: {e}")

    body = resp.body
    if any(m in body for m in signal.alive_markers):
        return PreflightResult(
            url, status_code=resp.status, reason="the page carries real profile content",
            checked=True)

    saw_marker = any(m in body for m in signal.dead_markers)
    status_is_dead = resp.status in signal.dead_statuses

    if signal.dead_statuses and signal.require_both:
        if status_is_dead and saw_marker:
            return PreflightResult(
                url, is_dead=True, status_code=resp.status,
                reason=f"the platform answered HTTP {resp.status} and the page says "
                       f"the profile does not exist",
                checked=True)
        if status_is_dead:
            # THE CAPABILITY GOING DARK, SAID OUT LOUD. The status still
            # looks like a removal but the platform's own wording is gone,
            # which means either the wording changed or this is not really a
            # removal page. Both are reasons to stop settling rows on it,
            # and neither should be discovered months later from a quiet
            # absence of pre-flight savings.
            log.warning(
                f"pre-flight: {platform_id} answered HTTP {resp.status} for {url} but the "
                f"page did not carry its own removal wording -- not settling. If this "
                f"persists, the marker in _DEATH_SIGNALS needs re-measuring")
            return PreflightResult(
                url, status_code=resp.status,
                reason=f"HTTP {resp.status} without the platform's removal wording",
                checked=True)
        return PreflightResult(url, status_code=resp.status,
                               reason=f"HTTP {resp.status}", checked=True)

    if status_is_dead or saw_marker:
        detail = (f"the platform answered HTTP {resp.status} for this profile URL"
                  if status_is_dead else
                  "the page is the platform's own 'no such profile' page")
        return PreflightResult(url, is_dead=True, status_code=resp.status,
                               reason=detail, checked=True)

    return PreflightResult(url, status_code=resp.status,
                           reason=f"HTTP {resp.status}", checked=True)


async def preflight_many(
    pairs: list[tuple[str, str]],
    *,
    concurrency: int = 10,
    timeout: float = 8.0,
    budget: float = 0.0,
) -> dict[str, PreflightResult]:
    """`[(url, platform_id), ...]` -> `{url: PreflightResult}`, bounded twice.

    BOUNDED IN CONCURRENCY because this runs in front of a job an analyst is
    waiting on: an unbounded gather over a 200-URL paste is 200 simultaneous
    TLS handshakes from one host, which is both a load problem here and a
    pattern at the far end.

    BOUNDED IN WALL CLOCK because a rate-limited platform answers slowly,
    and an optimisation that can add a minute to the start of a job is not
    an optimisation. `budget` (seconds, 0 for none) stops the pass and keeps
    whatever finished; every URL that did not finish simply has no entry,
    which the caller already treats as "not proven dead".

    NEVER RAISES for a URL's own sake. A pre-flight is an optimisation, so a
    failure in it must cost at most the optimisation: anything that goes
    wrong for one URL comes back as that URL's own not-dead result.
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    out: dict[str, PreflightResult] = {}

    async def one(url: str, platform_id: str) -> None:
        async with sem:
            try:
                out[url] = await preflight_url_status(url, platform_id, timeout=timeout)
            except asyncio.CancelledError:
                raise
            except Exception as e:                    # noqa: BLE001 - never fatal
                out[url] = PreflightResult(url, reason=f"{type(e).__name__}: {e}")

    if not pairs:
        return out
    tasks = [asyncio.create_task(one(u, p)) for u, p in pairs]
    try:
        if budget > 0:
            _done, pending = await asyncio.wait(tasks, timeout=budget)
            if pending:
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                log.info(
                    f"pre-flight budget of {budget:.0f}s reached with {len(pending)} "
                    f"of {len(tasks)} URL(s) unanswered -- those go to the browser")
        else:
            await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise
    return out
