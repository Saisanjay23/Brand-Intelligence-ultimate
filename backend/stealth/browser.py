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

import sys

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

from backend.shared.logging import get_logger
from backend.stealth.human import BASE, Human
from backend.stealth.fingerprint import (
    LAUNCH_ARGS,
    chrome_binary,
    get_identity,
)
from backend.stealth.headers import build_extra_headers
from backend.stealth.mouse_movement import humanize_interaction
from backend.stealth.navigator_spoofing import build_init_js
from backend.stealth.proxy import build_proxy_config
from backend.stealth.timezone import resolve_timezone_id

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
# Empty binary payload for fonts to avoid triggering font load failure diagnostics
EMPTY_FONT = b"\x00\x01\x00\x00" + b"\x00" * 32

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
        timezone_id: str = "Asia/Kolkata",
        session_id: str = "",
        proxy: dict | None = None,
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
        self.timezone_id = resolve_timezone_id(proxy, timezone_id)
        self.proxy = proxy
        self.identity = get_identity(session_id)
        self.viewport = self.identity["viewport"]
        self.human = Human()
        self.ctx = self.browser = self._pw = None

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
        self.browser = await self._pw.chromium.launch(**opts)

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
        ctx_locale = f"{locale},{locale.split('-')[0]}"
        ctx_opts = {
            "user_agent": self.identity["ua"],
            "extra_http_headers": build_extra_headers(locale=locale),
            "locale": ctx_locale,
            "timezone_id": self.timezone_id,
            "viewport": self.viewport,
        }
        # Playwright's per-context proxy override (Chromium only).
        proxy_config = build_proxy_config(self.proxy)
        if proxy_config:
            ctx_opts["proxy"] = proxy_config
        self.ctx = await self.browser.new_context(**ctx_opts)

        # No hardware arguments any more: hardwareConcurrency/deviceMemory
        # are reported honestly, because an init script cannot reach Web
        # Worker scope and the spoof produced a main-thread-vs-worker
        # contradiction. See navigator_spoofing.py for the measurement.
        await self.ctx.add_init_script(build_init_js())
        from backend.sessions.cookies import normalize_cookies

        safe_cookies = normalize_cookies(self.cookies)
        await self.ctx.add_cookies(safe_cookies)
        await self.ctx.route("**/*", self._filter)
        return self.ctx

    async def _filter(self, route, request):
        url = request.url.lower()
        rtype = request.resource_type

        # 1. Block known third-party telemetry, ad beacons, and analytics
        if any(tracker in url for tracker in BLOCKED_TRACKERS):
            await route.fulfill(status=200, content_type="application/javascript", body=b"")
            return

        # 2. Block video/audio media streaming chunks cleanly (prevents background buffering)
        if rtype == "media":
            await route.fulfill(status=200, content_type="video/mp4", body=b"")
            return

        # 3. Block images only when evidence/image loading is disabled
        if not self.load_images and rtype == "image":
            await route.fulfill(
                status=200, content_type="image/gif", body=TRANSPARENT_GIF
            )
            return

        await route.continue_()

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
        same fixed (args, cookies, session_id, proxy) signature by
        analysis_service/discovery_service, and threading a new parameter
        through all five engines would buy nothing over setting it on the
        instance that the service already holds.

        Best-effort throughout -- a failed save must never stop a browser
        from closing, or the next run inherits a leaked process.
        """
        if self.on_cookies is not None and self.ctx is not None:
            try:
                await self.on_cookies(await self.ctx.cookies())
            except Exception as e:
                log.warning(f"could not persist refreshed cookies: {type(e).__name__}: {e}")

        for obj, meth in (
            (self.ctx, "close"),
            (self.browser, "close"),
            (self._pw, "stop"),
        ):
            if obj:
                try:
                    await getattr(obj, meth)()
                except Exception:
                    pass

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
