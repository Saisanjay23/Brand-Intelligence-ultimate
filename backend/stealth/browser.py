"""One logged-in browser session, shared by every platform and both phases.

WHAT THIS DELIBERATELY DOES NOT DO
    No canvas / WebGL / audio fingerprint spoofing, and no playwright-stealth.
    Those patches are detectable in themselves: overriding those prototypes
    reads as a privacy extension, and Facebook responds by never finishing its
    render (an infinite spinner), Twitter with "Something went wrong", and
    Instagram by not hydrating at all. Less patching survives longer here.

WHAT ACTUALLY HELPS & HAS BEEN HARDENED
    * a real Google Chrome binary when one is installed, a genuine build has
      a better reputation than bundled Chromium
    * exact synchronization between User-Agent, Sec-CH-UA Client Hints, and JS
      runtime capabilities
    * native code masking on overrides (`webdriver`, `visibilityState`)
    * fulfilling images and fonts with valid 200 dummy payloads instead of
      aborting, preventing JS `.onerror()` alarm tracking
    * stable per-platform identity: same UA, viewport, hardware specs, locale,
      and timezone every run
    * passive Bezier pointer motion and reading micro-scrolling during checks
    * pacing, which lives in human.py and matters more than any of the above
"""

from __future__ import annotations

import asyncio
import base64
import random
import shutil
import sys
import time
from pathlib import Path

# Patchright first, vanilla Playwright as the fallback.
#
# Patchright is an API-compatible fork of Playwright that removes automation
# signals the driver itself emits -- signals that live BELOW JavaScript, so
# no amount of add_init_script can reach them. This is not a guess; it was
# measured against deviceandbrowserinfo.com's fingerprint suite, same
# machine, same launch args, same init script, minutes apart:
#
#   vanilla playwright   isBot: TRUE
#                        isAutomatedWithCDP              true
#                        isAutomatedWithCDPInWebWorker   true
#                        hasInconsistentTimingResolution true
#   patchright           isBot: FALSE   (no true signals at all)
#
# Note this does NOT show up on rebrowser-bot-detector, which reports
# "runtimeEnableLeak: no leak detected" for BOTH drivers -- one detector
# agreeing is not evidence of cleanliness, which is why the probe in
# detection_probe.py refuses to report that check as a pass.
#
# The import falls back rather than hard-failing: patchright is a stealth
# improvement, not a correctness dependency, and a deployment that has not
# installed it yet must still run. `STEALTH_DRIVER` records which one is
# live so it can be surfaced rather than silently assumed.
try:
    from patchright.async_api import async_playwright  # type: ignore
    STEALTH_DRIVER = "patchright"
except ImportError:
    try:
        from playwright.async_api import async_playwright  # type: ignore
        STEALTH_DRIVER = "playwright"
    except ImportError:
        sys.exit("pip install patchright  (or: pip install playwright && playwright install chromium)")

from typing import Optional

from backend.shared.logging import get_logger
from backend.shared.tasks import spawn
from backend.platforms.scan_options import cancelled
from backend.stealth.human import BASE, Human
from backend.stealth.fingerprint import (
    LAUNCH_ARGS,
    chrome_binary,
    get_identity,
)
from backend.stealth.headers import build_extra_headers
from backend.stealth.mouse_movement import humanize_interaction
from backend.stealth.navigator_spoofing import build_init_js
from backend.stealth.timezone import DEFAULT_TIMEZONE_ID

log = get_logger("browser")

# NOTE: there is no font blocking, and there should not be. A `BLOCK_TYPES =
# {"media", "font"}` used to sit here, declared and never read by `_filter`
# (which tests resource types directly) -- so fonts have always loaded
# normally. Removed rather than wired up: the constant implied a policy the
# code did not have, and implementing it would have been a regression. A
# browser that renders a page while fetching none of its webfonts is doing
# something no ordinary one does, and fonts are cheap.

# Transparent 1x1 GIF binary to fulfill image/media requests without triggering JS .onerror
TRANSPARENT_GIF = (
    b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\x05\x04\x04\x00\x00\x00"
    b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b"
)

BLOCKED_TRACKERS = (
    "connect.facebook.net",
    "facebook.com/tr/",
    "analytics.twitter.com",
    "telemetry.twitter.com",
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "scorecardresearch.com",
    "tiktok.com/api/v1/web/report/",
    "adroll.com",
)


# ---------------------------------------------------------- browser profiles
#
# ONE DIRECTORY PER POOLED ACCOUNT, REUSED FOR EVER.
#
# A cookie jar is not a device. Replaying cookies into a blank context makes
# every run arrive as a brand-new browser -- no localStorage, no IndexedDB,
# none of the device keys Meta and X mint and then expect to see again, and a
# fresh Chrome machine id each time. That is precisely the shape a "new
# device, was this you?" challenge exists to catch, and it is a shape a real
# account never produces. `launch_persistent_context` keeps all of it on
# disk, so the second run looks like the same laptop as the first.
#
# WHAT MAKES THIS SAFE, AND WHY THE LEASE BELOW IS NOT OPTIONAL. Chromium
# takes an exclusive lock on a user-data-dir: a second launch against a
# directory already open either fails outright or silently drops to a
# throwaway profile. Sessions in this tool are normally kept apart by the
# claim system in sessions/manager.py (see `_session_in_use`), but that is a
# guarantee about ACCOUNTS, not about directories -- and two callers can
# still land on the same one: anything that builds a Session without a
# session id would share a directory with every other such caller on that
# platform, and a health check picked a moment before a job claims the same
# account can overlap it by a hair.
#
# So the directory itself is leased, in-process, for the life of the Session.
# A caller that cannot get the lease is not refused and does not wait: it
# runs on an ephemeral profile, exactly as this module did before
# persistence existed. Losing persistence costs stealth; blocking or
# crashing a sweep costs the run, and those are not the same price.
_profile_leases: set[str] = set()


def _lease_profile(path: Path) -> bool:
    """Claim a profile directory for this process. Lock-free on purpose:
    this module runs on one event loop and there is no await between the
    test and the add -- the same reasoning `holds_session` relies on."""
    key = str(path).lower()
    if key in _profile_leases:
        return False
    _profile_leases.add(key)
    return True


def _release_profile(path: Path) -> None:
    _profile_leases.discard(str(path).lower())


def profiles_root() -> Path:
    from backend.config.settings import settings

    return Path(settings.session_blob_path) / "browser_profiles"


def profile_dir_for(platform: str, session_id: str) -> Path:
    """Where one account's browser lives. Both halves are sanitised: they
    become a path segment, and one of them is operator-typed."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_"
                   for c in f"{platform or 'unknown'}_{session_id}")
    return profiles_root() / safe[:120]


def prune_stale_profiles(max_age_days: float) -> int:
    """Delete profile directories nothing has opened in `max_age_days`.

    A Chrome profile is tens to hundreds of megabytes and there is one per
    pooled account per platform, so left alone this grows without limit on a
    machine nobody is watching. Deleting one costs that account its device
    identity once, which is a stealth cost and not a correctness one: the
    cookies live in the database and the next run rebuilds the profile.
    """
    if max_age_days <= 0:
        return 0
    root = profiles_root()
    if not root.is_dir():
        return 0
    cutoff = time.time() - (max_age_days * 86400)
    removed = 0
    try:
        children = list(root.iterdir())
    except OSError:
        return 0
    for child in children:
        try:
            if not child.is_dir():
                continue
            if str(child).lower() in _profile_leases:
                continue        # open right now
            if child.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def reset_profile(platform: str, session_id: str) -> bool:
    """Throw away one account's stored browser identity.

    Called when the credentials underneath a session are REPLACED -- a
    freshly pasted cookie jar, a completed re-login. Without it the old
    profile keeps its own copy of the previous login in localStorage and
    IndexedDB, and the operator's new paste would be layered over a device
    that still remembers being signed in as the old one.

    Refuses while the profile is open, because deleting a directory out from
    under a running Chromium is how a sweep dies mid-page.
    """
    path = profile_dir_for(platform, session_id)
    if str(path).lower() in _profile_leases:
        log.info(f"not resetting {path.name}: a session is using it right now")
        return False
    if not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    log.info(f"reset browser profile {path.name} -- new credentials, new device")
    return True


# Where a real session would start. Only the four browser-driven,
# cookie-backed platforms appear: YouTube and Telegram never open a browser.
# Flags that apply ONLY to a persistent profile. Kept apart from
# LAUNCH_ARGS so the fingerprint every run presents stays identical whether
# the profile is on disk or not: neither of these is visible to JavaScript,
# they only bound what Chrome writes into the directory.
PERSISTENT_ARGS = [
    # ~80 MB of HTTP cache per profile instead of Chrome's default, which
    # grows with the disk. One directory per pooled account per platform
    # adds up quickly on a machine nobody is watching.
    "--disk-cache-size=83886080",
    "--media-cache-size=16777216",
]

PLATFORM_HOME = {
    "facebook": "https://www.facebook.com/",
    "twitter": "https://x.com/home",
    "instagram": "https://www.instagram.com/",
    "tiktok": "https://www.tiktok.com/",
}

# When each profile last had its feed warmed. In-process only, so a restart
# warms once more than strictly needed -- the harmless direction, and far
# cheaper than a database read from inside the browser layer.
_last_warm: dict[str, float] = {}


class Session:
    """A browser context carrying one account's cookies."""

    # Platforms that must fetch images for real, even when nothing is being
    # screenshotted. Subclasses set this; see the measurement below.
    #
    # Stubbing images is invisible to any JavaScript fingerprint check and
    # highly visible SERVER-SIDE. Measured on one real logged-in profile
    # visit (2026-09-01):
    #     facebook.com/<page>   253 requests, 74 of them images to
    #                           scontent.*.fna.fbcdn.net / static.xx.fbcdn.net
    #                           -- 29% of all traffic, ZERO reaching Meta
    #     instagram.com/<user>  150 requests, 33 images to
    #                           instagram.*.fna.fbcdn.net -- ZERO reaching Meta
    # A logged-in client that pulls the whole JS bundle, fires 60 XHRs and
    # then requests not one image byte from Meta's OWN first-party CDN is not
    # a shape a human browser produces, and those CDN hits are logged against
    # the same session cookies. No init script can mask an absence.
    #
    # The reason to block them was speed, and that reason did not survive
    # measurement: same page, images stubbed vs allowed, was 9.8s vs 9.9s --
    # +0.1s and +650KB per visit, because images load in parallel and block
    # nothing. Over a 500-page sweep that is about a minute and ~317MB, which
    # is not a real cost against looking like a browser to Meta specifically.
    ALWAYS_LOAD_IMAGES = False

    def __init__(
        self,
        options,
        cookies: list[dict],
        load_images: bool = False,
        timezone_id: str = DEFAULT_TIMEZONE_ID,
        session_id: str = "",
        platform: str = "",
    ):
        self.o = options
        self.cookies = cookies
        # A caller asking for images always wins; a platform that declares
        # ALWAYS_LOAD_IMAGES gets them even when the caller did not ask
        # (discovery never asks -- it takes no screenshots -- which is
        # exactly the path that was emitting the anomaly).
        self.load_images = load_images or self.ALWAYS_LOAD_IMAGES
        self.session_id = session_id
        # Optional async callback, `await on_cookies(list[dict])`, invoked
        # by stop() with the live jar before the context closes. See stop().
        self.on_cookies = None
        self.timezone_id = timezone_id or DEFAULT_TIMEZONE_ID
        self.identity = get_identity(session_id)
        self.viewport = self.identity["viewport"]
        # THE STOP SIGNAL, WIRED INTO THE PACING. Every platform's
        # between-profile gap comes through `pause()` below, and those gaps
        # (including `maybe_rest`'s 20-60s break) were flat sleeps nothing
        # could interrupt. Since only one of the twelve engines checks
        # `cancel` for itself, this is the single place that gives all of
        # them a cooperative stop -- see human.py's own note for why the
        # wait, rather than each engine, is what was taught to listen.
        self.human = Human(stop=lambda: cancelled(self.o))
        self.ctx = self.browser = self._pw = None
        # Set by start() when this session got a persistent profile, and
        # read by stop() so the lease is always handed back by whoever took
        # it. None means the run is on an ephemeral context, which is both
        # the fallback and what every run did before profiles existed.
        self._profile: Optional[Path] = None
        self._persistent = False
        # The browser-level CDP session carrying the native request filter,
        # when that wiring is in use (see _install_filter). Held so it is
        # not collected while Chrome still has requests paused on it.
        self._cdp = None
        # An explicit platform id wins; otherwise it is read off the
        # subclass's module (backend.platforms.<id>.discovery_engine), which
        # is what lets every engine keep its current constructor untouched.
        self._platform = platform or self._platform_from_module()

    @classmethod
    def _platform_from_module(cls) -> str:
        parts = (cls.__module__ or "").split(".")
        if len(parts) >= 3 and parts[0] == "backend" and parts[1] == "platforms":
            return parts[2]
        return ""

    @property
    def platform(self) -> str:
        """Which platform this session belongs to, or "" for the bare
        `Session` used outside a platform adapter."""
        return self._platform

    async def start(self):
        self._pw = await async_playwright().start()
        opts = {
            "headless": not getattr(self.o, "headful", False),
            "args": LAUNCH_ARGS,
        }
        if binary := chrome_binary():
            opts["executable_path"] = binary
            log.info(f"using installed Chrome: {binary} [driver: {STEALTH_DRIVER}]")
        if STEALTH_DRIVER != "patchright":
            log.warning(
                "running on vanilla playwright -- the driver announces itself over CDP "
                "(measured: isAutomatedWithCDP=true). `pip install patchright` to close it."
            )

        # Chrome always carries the BASE language behind the region locale:
        # a real en-US install reports navigator.languages ['en-US', 'en'],
        # never ['en-US'] alone. Playwright's `locale` option is what
        # truncates it -- measured directly against this same binary:
        #     no locale set   -> ['en-US', 'en']   (Chrome's own default)
        #     locale='en-US'  -> ['en-US']         (Playwright truncating)
        #     locale='en-US,en' -> ['en-US', 'en'] (correct, and native)
        # A single-entry languages array with no fallback is not a shape any
        # ordinary browser produces, so this passes the pair and lets Chrome
        # parse it itself rather than patching navigator afterwards.
        #
        # `locale` also drives Accept-Language and WINS over
        # extra_http_headers (which is why that header measured as a bare
        # "en-US" despite build_extra_headers already returning the correct
        # "en-US,en;q=0.9"). build_extra_headers still gets the PLAIN locale:
        # handing it "en-US,en" would make it emit "en-US,en,en;q=0.9".
        locale = "en-US"
        ctx_opts = {
            "user_agent": self.identity["ua"],
            "extra_http_headers": build_extra_headers(locale=locale),
            "locale": locale,
            "timezone_id": self.timezone_id,
            "viewport": self.viewport,
        }
        # PERSISTENT FIRST, EPHEMERAL IF ANYTHING AT ALL GOES WRONG.
        #
        # Everything below this point is identical for both kinds of
        # context, which is the property that keeps discovery and analysis
        # out of this decision entirely: they are handed a BrowserContext
        # either way and cannot tell which one they got.
        profile = self._wanted_profile()
        if profile is not None and _lease_profile(profile):
            self._profile = profile
            try:
                profile.mkdir(parents=True, exist_ok=True)
                self.ctx = await self._pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=opts["headless"],
                    args=LAUNCH_ARGS + PERSISTENT_ARGS,
                    **({"executable_path": opts["executable_path"]}
                       if "executable_path" in opts else {}),
                    **ctx_opts,
                )
                self._persistent = True
                log.info(f"browser profile {profile.name} (persistent)")
            except Exception as e:                    # noqa: BLE001
                # A stale SingletonLock from a process that was killed, a
                # corrupt profile, a permissions problem on the directory.
                # None of these are worth failing a sweep for, and all of
                # them are survivable by simply not being persistent.
                log.warning(
                    f"could not open persistent profile {profile.name} "
                    f"({type(e).__name__}: {e}) -- running on a fresh context instead")
                _release_profile(profile)
                self._profile = None
                self.ctx = None

        if self.ctx is None:
            self.browser = await self._pw.chromium.launch(**opts)
            self.ctx = await self.browser.new_context(**ctx_opts)
        else:
            # BACKWARD COMPATIBILITY. A persistent context has no separate
            # Browser object, but `stop()` and anything else reaching for
            # `.browser` must keep working. Pointing it at the context is
            # safe because stop() closes by identity, not by name.
            self.browser = self.ctx

        # NO INIT SCRIPT BY DEFAULT. Real Chrome under patchright is already
        # clean in the main world, and every JavaScript override measured
        # there only added a signal -- see navigator_spoofing.py.
        if init_js := build_init_js():
            await self.ctx.add_init_script(init_js)
        from backend.sessions.cookies import normalize_cookies

        # STORED COOKIES ARE ALWAYS INJECTED, persistent profile or not.
        #
        # It is tempting to skip this when the profile already carries a
        # login, on the grounds that the profile's jar is newer. It is not
        # worth it. The database is kept current by `sync_cookies` at every
        # stop and at four explicit points in the two runners, so the two
        # normally agree -- and where they DISAGREE, the database is the
        # operator's intent: a freshly pasted jar, or a completed re-login.
        # Trusting the profile there would mean pasting new cookies and
        # watching nothing change, which is a far worse bug than writing a
        # value that was already correct. `reset_profile` handles the other
        # direction, wiping the device when the credentials under it change.
        #
        # This also means the cookie behaviour of a persistent run is
        # byte-for-byte what it was before profiles existed.
        safe_cookies = normalize_cookies(self.cookies)
        await self.ctx.add_cookies(safe_cookies)
        await self._install_filter()
        await self._warmup()
        return self.ctx

    # ------------------------------------------------------ request filter
    #
    # ONE DECISION, TWO WAYS TO APPLY IT. `_verdict` is the whole policy --
    # which requests get a stub instead of the network -- and both wirings
    # below call it, so they cannot drift apart.
    #
    # WHY THE NATIVE WIRING EXISTS. `ctx.route("**/*")` made Playwright
    # pause EVERY request (about 250 per Facebook page) for a round trip
    # through its driver and this Python process, and Playwright disables
    # the HTTP cache whenever any route is installed -- so the 80MB profile
    # cache in PERSISTENT_ARGS was written and never read, and every page
    # re-downloaded and re-compiled the platform's JS bundles. Measured on a
    # local test site with this exact Chrome and LAUNCH_ARGS (six pages,
    # CPU summed over Python, the driver and every Chrome process):
    #     images loaded   53.3s -> 31.7s CPU, 17.5s -> 11.2s wall
    #     images stubbed  44.7s -> 36.4s CPU, 16.6s -> 13.4s wall
    # with the same requests blocked, the same stubs served, and the same
    # behaviour inside cross-origin iframes.
    #
    # The native filter is a BROWSER-LEVEL Fetch session: Chrome pauses only
    # requests matching the patterns (the trackers, media, and images when
    # stubbing), and it covers every target -- pages, popups and
    # cross-origin iframes -- which a per-page session does not. Each
    # Session owns its own Chrome, so browser-wide is exactly this session.

    def _verdict(self, url: str, rtype: str) -> Optional[tuple[bytes, str]]:
        """(body, content type) to answer this request with, or None to let
        it through. `rtype` in either spelling ("image" / "Image")."""
        url, rtype = url.lower(), rtype.lower()
        # 1. Known third-party telemetry, ad beacons, and analytics
        if any(tracker in url for tracker in BLOCKED_TRACKERS):
            return b"", "application/javascript"
        # 2. Video/audio streaming chunks (prevents background buffering)
        if rtype == "media":
            return b"", "video/mp4"
        # 3. Images only when evidence/image loading is disabled
        if not self.load_images and rtype == "image":
            return TRANSPARENT_GIF, "image/gif"
        return None

    def _fetch_patterns(self) -> list[dict]:
        """What Chrome should pause: a superset of what `_verdict` acts on
        (the handler re-checks), and nothing else."""
        stage = "Request"
        patterns = [{"urlPattern": f"*{t}*", "requestStage": stage} for t in BLOCKED_TRACKERS]
        patterns.append({"urlPattern": "*", "resourceType": "Media", "requestStage": stage})
        if not self.load_images:
            patterns.append({"urlPattern": "*", "resourceType": "Image", "requestStage": stage})
        return patterns

    async def _install_filter(self) -> None:
        from backend.config.settings import settings

        browser = getattr(self.ctx, "browser", None)
        if settings.browser_native_request_filter and browser is not None:
            try:
                cdp = await browser.new_browser_cdp_session()
                cdp.on("Fetch.requestPaused",
                       lambda ev: spawn(self._on_paused(cdp, ev)))
                await cdp.send("Fetch.enable", {"patterns": self._fetch_patterns()})
                self._cdp = cdp
                return
            except Exception as e:                      # noqa: BLE001
                log.warning(
                    f"native request filter unavailable ({type(e).__name__}: {e}) "
                    f"-- using the Playwright route instead")
        await self.ctx.route("**/*", self._filter)

    async def _on_paused(self, cdp, ev: dict) -> None:
        """Answer one paused request. MUST always answer: a paused request
        nobody continues hangs its page for good."""
        rid = ev.get("requestId")
        try:
            verdict = self._verdict(ev.get("request", {}).get("url", ""),
                                    ev.get("resourceType", ""))
            if verdict is None:
                await cdp.send("Fetch.continueRequest", {"requestId": rid})
                return
            body, ctype = verdict
            await cdp.send("Fetch.fulfillRequest", {
                "requestId": rid, "responseCode": 200,
                "responseHeaders": [{"name": "Content-Type", "value": ctype}],
                "body": base64.b64encode(body).decode("ascii"),
            })
        except Exception:                               # noqa: BLE001
            # The page navigated away or closed mid-request (the request is
            # already gone), or the fulfil failed -- in which case letting
            # it through is the answer that never hangs a page.
            try:
                await cdp.send("Fetch.continueRequest", {"requestId": rid})
            except Exception:                           # noqa: BLE001
                pass

    def _wanted_profile(self) -> Optional[Path]:
        """The directory this session should reuse, or None to run
        ephemeral.

        None whenever there is nothing stable to key on. A Session with no
        session id cannot have a profile of its own -- it would have to
        share one with every other anonymous caller on that platform, which
        is the collision the lease exists to prevent, except permanent.
        """
        from backend.config.settings import settings

        if not settings.browser_persistent_profiles:
            return None
        if not self.session_id or not self.platform:
            return None
        return profile_dir_for(self.platform, self.session_id)

    async def _warmup(self) -> None:
        """Look at the home feed before going to work.

        WHY. A real session does not open cold on a search results URL or a
        stranger's profile; it starts somewhere ordinary and moves. One
        short home-feed view also lets the platform do the token exchange it
        expects a returning browser to do, which is the thing a cookie
        replay never performs.

        NEVER FATAL, AND NEVER A VERDICT. Every failure here is swallowed:
        a warm-up that hits a checkpoint has still told us nothing the
        health check does not already own, and turning it into an error
        would let a courtesy page load fail a sweep. It is also skipped
        outright for health checks (options.warmup=False) -- paying for two
        page loads to answer one question, on an account already suspected
        of being unwell, is the opposite of careful.
        """
        from backend.config.settings import settings

        if not settings.browser_warmup_enabled:
            return
        if not getattr(self.o, "warmup", True):
            return
        if cancelled(self.o):
            return
        home = PLATFORM_HOME.get(self.platform)
        if not home or self.ctx is None:
            return

        key = f"{self.platform}:{self.session_id or 'ephemeral'}"
        gap = max(0.0, settings.browser_warmup_min_gap_minutes) * 60.0
        last = _last_warm.get(key, 0.0)
        if gap and (time.time() - last) < gap:
            return
        _last_warm[key] = time.time()

        page = None
        try:
            page = await self.ctx.new_page()
            await page.goto(home, wait_until="domcontentloaded", timeout=12000)
            await asyncio.sleep(random.uniform(2.0, 3.8))
            if cancelled(self.o):
                return
            # A wheel event, not a scrollTo: this is what makes the feed
            # actually render its next slice, which is what triggers the
            # token exchange worth being here for.
            await page.mouse.wheel(0, random.randint(150, 320))
            await asyncio.sleep(random.uniform(0.8, 1.5))
        except Exception as e:                        # noqa: BLE001 - courtesy
            log.debug(f"warm-up for {self.platform} did not complete (non-fatal): "
                      f"{type(e).__name__}: {e}")
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:                     # noqa: BLE001
                    pass

    async def _filter(self, route, request):
        """The Playwright-route wiring of `_verdict` -- the fallback, and
        what `browser_native_request_filter = False` selects."""
        verdict = self._verdict(request.url, request.resource_type)
        if verdict is None:
            await route.continue_()
            return
        body, ctype = verdict
        await route.fulfill(status=200, content_type=ctype, body=body)

    async def sync_cookies(self) -> None:
        """Persists the live context cookie jar mid-session.

        Saves updated rotation tokens (e.g. Meta fr/xs/datr or Instagram sessionid/csrftoken)
        so that interruptions, long sweeps, or unexpected aborts don't leave MongoDB with
        stale superseded tokens.
        """
        if self.on_cookies is not None and self.ctx is not None:
            try:
                cookies = await self.ctx.cookies()
                if cookies:
                    await self.on_cookies(cookies)
            except Exception as e:
                log.warning(f"could not persist refreshed cookies: {type(e).__name__}: {e}")

    async def stop(self):
        """Closes the context and browser.

        Before anything closes, the LIVE cookie jar is handed to
        `on_cookies` if a caller set one. That callback is what keeps a
        pooled session alive: Facebook, Instagram and X all rotate their
        session cookies during ordinary browsing, and a context that is
        thrown away without saving them means the next run replays the
        older jar. Replaying a superseded session token is one of the
        signals these platforms treat as account takeover, so the account
        gets challenged or logged out -- not because the cookie expired
        (the stored ones are good for months) but because it went stale.

        `on_cookies` is an ATTRIBUTE rather than a constructor argument on
        purpose: every platform's Session subclass is constructed with the
        same fixed (args, cookies, session_id) signature by
        analysis_service/discovery_service, and threading a new parameter
        through all five engines would buy nothing over setting it on the
        instance that the service already holds.

        Best-effort throughout -- a failed save must never stop a browser
        from closing, or the next run inherits a leaked process.
        """
        await self.sync_cookies()

        # CLOSED BY IDENTITY, NOT BY NAME. On a persistent context
        # `self.browser IS self.ctx`, so walking the pair blindly would
        # close the same object twice -- harmless today only because the
        # second call is swallowed, which is a bad thing to depend on.
        closers = [(self.ctx, "close")]
        if self.browser is not None and self.browser is not self.ctx:
            closers.append((self.browser, "close"))
        closers.append((self._pw, "stop"))
        for obj, meth in closers:
            if obj:
                try:
                    await getattr(obj, meth)()
                except Exception:
                    pass

        # THE LEASE GOES BACK EVEN IF EVERY CLOSE ABOVE FAILED. A directory
        # left leased is a session that can never use its own profile again
        # for the life of the process, which degrades silently -- exactly
        # the kind of failure that gets noticed months later, if ever.
        if self._profile is not None:
            _release_profile(self._profile)
            self._profile = None
        self._persistent = False

    async def pause(self, mult: float = 1.0):
        """Between-profile pacing, jittered and fatigued.

        `options.delay` is the MEDIAN gap in seconds, and now actually is
        one. It used to be divided by a bare 6.0, which silently made the
        setting mean 44% of its face value -- measured over 4000 draws with
        the shipped `analysis_delay_sec = 2.5`, the real median gap was
        1.10s, not 2.5s. Scaling against `BASE["between_profiles"]` instead
        makes the configured number the number you get, so anyone tuning
        pacing for stealth is tuning the thing they think they are.

        (Jitter, fatigue and circadian multipliers still apply on top -- see
        human.py. Those shape the distribution; they do not move the median
        far, which is the point.)
        """
        # A RUN BEING STOPPED HAS NOTHING LEFT TO PACE. Pacing exists to
        # shape the gaps BETWEEN requests; once the job is cancelled there
        # is no next request to space out, so waiting here only delays the
        # stop. Checked up front as well as inside the sleep so the common
        # case costs nothing at all.
        if cancelled(self.o):
            return
        configured = getattr(self.o, "delay", 0) or 0
        scale = (configured / BASE["between_profiles"]) if configured else 1.0
        await self.human.pause("between_profiles", scale * mult)
        # Every platform's per-profile pause funnels through here, so this is
        # the one place that needs to fire for should_rest()/maybe_rest() to
        # actually do anything, previously computed but never called.
        nap = await self.human.maybe_rest()
        if nap:
            log.info(f"human pacing: taking a {nap:.0f}s break")

    async def interact(self, page, scroll: bool = True, moves: int = 3) -> None:
        """Executes passive human pointer motion and micro-scrolling on a page."""
        await humanize_interaction(page, scroll=scroll, moves=moves)

    async def wait_for_visible_content(
        self, page, min_chars: int = 200, timeout_ms: int = 4000, poll_ms: int = 250,
        *, content_selector: str = "", content_timeout_ms: int = 5000,
        settle_images_ms: int = 1500,
    ) -> None:
        """Block until the page has actually PAINTED real content, not just
        parsed enough DOM to satisfy `domcontentloaded` or a data-readiness
        check.

        The root cause of every Facebook evidence screenshot this engine
        had ever captured being the exact same byte-identical loading
        splash was `build_extra_headers()` forcing `Upgrade-Insecure-
        Requests` onto every request in the context, including cross-origin
        CDN subresources. Facebook's CDN rejected the resulting CORS
        preflight for every JS/CSS bundle its client-side app needs, so the
        page could never get past its own splash no matter how long
        anything waited (see headers.py for the fix). This wait is the
        remaining defense-in-depth once that's fixed: field extraction
        here still comes from data that can land before the screen finishes
        painting (embedded JSON script tags, intercepted API responses), so
        a slow render on an off day could still get shot mid-transition.

        Polls the page's RENDERED text via Playwright's own `inner_text()`,
        not `page.evaluate("() => document.body.innerText")`, which
        returns 0 in this headless Chromium configuration even once real
        content is on screen (confirmed live: the raw DOM property stayed
        0 for 8+ seconds on a page that Playwright's own accessor read
        correctly from frame one; `inner_text()` is what the rest of this
        codebase already uses for exactly this reason, e.g. `visit()`'s own
        `h.text[tag] = await page.inner_text("body")`). A splash screen
        carries only its own handful of characters (a logo, "from Meta")
        while any real profile page's chrome alone (nav, buttons, section
        labels) clears `min_chars` immediately. Gives up after `timeout_ms`
        and lets the caller shoot whatever is actually there rather than
        blocking evidence capture indefinitely on a profile that genuinely
        never finishes rendering.
        """
        elapsed = 0
        while elapsed < timeout_ms:
            try:
                text = await page.inner_text("body")
            except Exception:
                return  # page navigated away/closed mid-check, nothing to wait for
            if len(text) >= min_chars:
                break
            await page.wait_for_timeout(poll_ms)
            elapsed += poll_ms

        # The character floor above is necessary and NOT sufficient. Measured
        # live (2026-08-22) on real evidence captures: it is satisfied by the
        # page's own chrome long before any of the profile's content exists,
        # so every screenshot this engine produced showed a correct, complete
        # header sitting above a LOADING SPINNER where the posts belong --
        # on Instagram the whole post grid, on X the whole timeline.
        #
        # For impersonation evidence that is the wrong half of the page to
        # lose: the header proves the account copied a name and a photo, and
        # the posts are what show it is actively being used. Facebook's wait
        # returned in 0.07s for exactly this reason -- it was never waiting
        # for anything.
        #
        # `content_selector` is the platform's own hook for "a real item is
        # on screen". Bounded separately and generously, because an account
        # with genuinely no posts never satisfies it and must NOT be made to
        # pay the full timeout on every capture -- it simply shoots what is
        # there, which for that account is the truth.
        if content_selector:
            try:
                await page.wait_for_selector(
                    content_selector, timeout=content_timeout_ms, state="attached")
            except Exception:
                pass  # genuinely empty timeline, or slower than the budget

        # Give the images that are actually ON SCREEN a moment to decode.
        # A tile that exists in the DOM but has not painted screenshots as a
        # blank rectangle, which is indistinguishable in the evidence from a
        # profile that posts blank images.
        if settle_images_ms > 0:
            try:
                await page.wait_for_function(
                    """() => {
                        const vis = Array.from(document.images).filter(i => {
                          const r = i.getBoundingClientRect();
                          return r.width > 0 && r.top < innerHeight && r.bottom > 0;
                        });
                        return vis.length === 0
                            || vis.every(i => i.complete && i.naturalWidth > 0);
                    }""",
                    timeout=settle_images_ms,
                )
            except Exception:
                pass

    async def check_session(
        self, probe_url: str, login_re, checkpoint_re, *, expect_path: str = "",
        deny_paths: tuple[str, ...] = (),
    ) -> bool:
        """Is this cookie set still logged in and unchallenged?

        `expect_path` is a POSITIVE confirmation: the path fragment the
        probe URL must still be on once the page settles. Pass it for any
        platform whose logged-out redirect does not land on an obviously
        named page.

        That parameter exists because negative-signal-only detection gave a
        confirmed false positive in production. Instagram's authenticated
        /accounts/edit/ bounces a dead session to `https://www.instagram.com/#`
       , a URL containing neither "/login" nor "/checkpoint", and the
        wall it renders ("Continue", "Use another profile", "Create new
        account") matches none of the login patterns either. So a logged-out
        session was reported healthy indefinitely: the pool kept handing it
        out, every sweep using it returned nothing, and the drift canary
        then blamed the platform for changing its payload shape. Absence of
        a known failure string is not evidence of success; still being on
        the authenticated page is.

        `deny_paths` is the same confirmation for a probe whose AUTHENTICATED
        destination is not a fixed path, so `expect_path` cannot name it.
        Facebook's /me is the case: logged in it redirects to the account's
        own profile (`/<vanity>` or `/profile.php`), which differs per
        account, but logged out it lands on exactly one of a small, fixed
        set of doors, `/` or `/index.php`, and landing on one of THOSE
        is proof the authenticated page was not reachable. Matched on the
        settled path exactly, never as a prefix, so it cannot swallow a real
        profile path.
        """
        page = await self.ctx.new_page()
        try:
            await page.goto(
                probe_url, wait_until="domcontentloaded", timeout=self.o.timeout * 1000
            )
            await page.wait_for_timeout(2500)
            await self.interact(page, scroll=True, moves=2)
            # re-read AFTER settling: a client-side bounce to the login wall
            # can land after domcontentloaded, and reading the URL too early
            # sees the page we asked for rather than the one we got
            body = await page.inner_text("body")
            if "/checkpoint" in page.url or checkpoint_re.search(body):
                log.error("session CHECKPOINTED -- clear it in a real browser")
                return False
            if "/login" in page.url or login_re.search(body):
                log.error("session INVALID -- cookies expired or incomplete")
                return False
            if expect_path or deny_paths:
                from urllib.parse import urlparse

                landed = urlparse(page.url).path.rstrip("/")
                if expect_path and expect_path.rstrip("/") not in landed:
                    log.error(
                        f"session INVALID -- redirected off {expect_path!r} to {page.url} "
                        "(authenticated page not reachable, so these cookies are not logged in)"
                    )
                    return False
                # "" is the settled path of the site root once rstripped
                if any(landed == p.rstrip("/") for p in deny_paths):
                    log.error(
                        f"session INVALID -- {probe_url} landed on the logged-out "
                        f"door {page.url} (these cookies are not logged in)"
                    )
                    return False
            log.info(f"session valid -> {page.url}")
            return True
        finally:
            if page and not page.is_closed():
                try:
                    await page.close()
                except Exception:
                    pass
