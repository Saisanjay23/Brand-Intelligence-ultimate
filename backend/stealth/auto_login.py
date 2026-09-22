"""Signing a pooled account back in by itself, the way a person would.

WHAT THIS IS FOR. Every pooled account in this tool is a dummy scraper
account the operator owns. When one is logged out -- a rotated token, a
checkpoint cleared, a password change -- the pool loses a worker and stays
down until somebody notices and pastes a fresh cookie export by hand. This
signs it back in from credentials the operator stored for exactly that
purpose, and it is the ONLY thing in this codebase that types a password.

WHY IT TYPES SLOWLY AND MOVES THE MOUSE. A login page is the most heavily
instrumented page either platform serves: keystroke timing, the gap between
fields, whether a pointer ever approached the button it claims to have
clicked. `page.fill()` sets a value with no keystrokes at all, and a
programmatic `click()` arrives with no preceding pointer motion. Both are
free to fix and both are checked.

WHERE IT RUNS. In the SESSION'S OWN persistent browser profile (see
stealth/browser.py), never a blank one. That is the whole point of doing it
here rather than in a throwaway context: the account signs in on the device
it has always used, so the login does not itself trip the "new device"
challenge this exists to recover from. The cookies it captures then belong
to a device the platform has already seen.

WHAT IT WILL NOT DO. It does not solve CAPTCHAs, clear checkpoints, or
answer identity questions. Anything that is not username/password/TOTP ends
as a failure with the reason recorded on the session row, and a person deals
with it. Callers must also rate-limit it -- see
sessions/manager.py::maybe_auto_relogin, which owns the cooldown and the
attempt ceiling, because a scripted password attempt every half hour on an
account a platform is already unhappy with is how an account stops being
recoverable at all.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

import pyotp
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from backend.config.settings import settings
from backend.shared.logging import get_logger
from backend.stealth.browser import Session
from backend.stealth.mouse_movement import hover_element_safely

log = get_logger("stealth.auto_login")


@dataclass
class LoginResult:
    """Everything one successful sign-in produced.

    `storage_state` is captured alongside the cookies because a modern login
    does not live in cookies alone -- Instagram and X both put state in
    localStorage that a cookie-only capture silently drops. It is stored for
    the record and for future use; the pool still authenticates on cookies.
    """

    cookies: list[dict] = field(default_factory=list)
    storage_state: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.cookies)


# Per-platform login flow. Kept as data rather than branches so adding a
# platform is a table entry, and so a selector that has drifted is visible
# without reading control flow.
#
# Each entry names, in order: where the form lives, how to fill it, and
# which cookie proves it worked. `alt` selectors are tried in order and the
# first one present wins -- these pages are A/B tested constantly and a
# single selector is a login that breaks on a Tuesday.
_FLOWS: dict[str, dict] = {
    "facebook": {
        "url": "https://www.facebook.com/login",
        "user": ("input#email", 'input[name="email"]'),
        "password": ("input#pass", 'input[name="pass"]'),
        "submit": ('button[name="login"]', 'button[type="submit"]', "#loginbutton"),
        "totp": ("#approvals_code", 'input[name="approvals_code"]'),
        "totp_submit": ("#checkpointSubmitButton", 'button[type="submit"]'),
    },
    "instagram": {
        "url": "https://www.instagram.com/accounts/login/",
        "user": ('input[name="username"]',),
        "password": ('input[name="password"]',),
        "submit": ('button[type="submit"]',),
        "totp": ('input[name="verificationCode"]', 'input[name="securityCode"]'),
        "totp_submit": ('button[type="button"]', 'button[type="submit"]'),
    },
    "twitter": {
        "url": "https://x.com/i/flow/login",
        # X's flow is several screens, so it is described step by step below
        # rather than as one form. See `_login_twitter`.
        "multi_step": True,
    },
}

# Which cookie proves the sign-in actually landed. Mirrors
# sessions/manager.py::LOGIN_FLOW; read from there at call time so the two
# can never drift apart.
_PROOF_TIMEOUT_S = 25.0


async def _human_type(page: Page, selector: str, text: str, *, timeout: int = 15000) -> None:
    """Type into a field the way a person does.

    NOT `page.fill()`. Fill sets the value directly and fires no key events
    at all, so a login page's keystroke-timing check sees a field that was
    populated by something that never touched a keyboard. Typing with a
    FIXED delay is only slightly better -- perfectly even 150ms gaps are
    their own signature.

    So: a jittered per-key gap, plus an occasional pause of the length a
    person takes when they glance at what they are doing. The 15% rate and
    the 300-600ms range are the plan's, and the point of them is variance,
    not any particular number.
    """
    await page.wait_for_selector(selector, state="visible", timeout=timeout)
    await page.click(selector)
    # Clear whatever a remembered login left behind, by selection rather
    # than by fill, so the field still only ever sees key events.
    await page.keyboard.press("ControlOrMeta+a")
    await page.keyboard.press("Backspace")
    for ch in text:
        await page.keyboard.type(ch, delay=random.uniform(80, 180))
        if random.random() < 0.15:
            await asyncio.sleep(random.uniform(0.30, 0.60))


async def _first_present(page: Page, selectors: tuple[str, ...], timeout: int = 10000) -> Optional[str]:
    """The first of these selectors that is actually on the page.

    These login pages are A/B tested continuously, so every step names
    several candidates. The per-candidate timeout is short because the
    common case is the first one being right; only the drifted case pays.
    """
    if not selectors:
        return None
    per = max(1200, int(timeout / max(1, len(selectors))))
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, state="visible", timeout=per)
            return sel
        except PlaywrightTimeoutError:
            continue
    return None


async def _human_click(page: Page, selectors: tuple[str, ...]) -> bool:
    """Move the pointer to the control along a Bezier curve, then click it.

    A programmatic click arrives with no pointer history whatsoever -- no
    mousemove, no hover, a mousedown at coordinates the cursor was never
    near. `hover_element_safely` does the approach; the click then happens
    where the pointer already is.
    """
    sel = await _first_present(page, selectors, timeout=6000)
    if not sel:
        return False
    try:
        await hover_element_safely(page, sel)
    except Exception:                                 # noqa: BLE001 - cosmetic
        pass
    await asyncio.sleep(random.uniform(0.15, 0.45))
    try:
        await page.click(sel, timeout=8000)
        return True
    except Exception as e:                            # noqa: BLE001
        log.debug(f"click on {sel} did not land: {type(e).__name__}: {e}")
        return False


def totp_now(secret: str) -> str:
    """The current 6-digit code for a base32 TOTP secret.

    Separated out and kept pure so it can be tested against RFC 6238
    vectors without a browser anywhere near it. Whitespace and case are
    normalised because an operator pastes these out of an authenticator
    app's setup screen, where they arrive in spaced groups of four.
    """
    cleaned = (secret or "").replace(" ", "").replace("-", "").strip().upper()
    if not cleaned:
        raise ValueError("empty TOTP secret")
    return pyotp.TOTP(cleaned).now()


async def _handle_totp(
    page: Page, secret: str, inputs: tuple[str, ...], submits: tuple[str, ...] = (),
) -> bool:
    """Fill a 2FA prompt if one appeared. False means none did, which is not
    an error -- most sign-ins never see one."""
    sel = await _first_present(page, inputs, timeout=15000)
    if not sel:
        return False
    try:
        code = totp_now(secret)
    except Exception as e:                            # noqa: BLE001
        log.error(f"could not generate a 2FA code: {type(e).__name__}: {e}")
        return False
    # The code itself is NOT logged. It is a live credential for the next
    # thirty seconds, and this log file is an audit trail people read.
    log.info("2FA prompt detected -- entering the generated code")
    await _human_type(page, sel, code)
    if submits and await _human_click(page, submits):
        return True
    await page.keyboard.press("Enter")
    return True


async def _login_form(page: Page, flow: dict, username: str, password: str, secret: str) -> None:
    """The one-page form shape: Facebook and Instagram."""
    await page.goto(flow["url"], wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(random.uniform(1.2, 2.4))

    user_sel = await _first_present(page, flow["user"], timeout=20000)
    if not user_sel:
        raise RuntimeError("the username field never appeared on the login page")
    await _human_type(page, user_sel, username)
    await asyncio.sleep(random.uniform(0.4, 1.1))

    pass_sel = await _first_present(page, flow["password"], timeout=15000)
    if not pass_sel:
        raise RuntimeError("the password field never appeared on the login page")
    await _human_type(page, pass_sel, password)
    await asyncio.sleep(random.uniform(0.5, 1.3))

    if not await _human_click(page, flow.get("submit", ())):
        await page.keyboard.press("Enter")

    if secret:
        await _handle_totp(page, secret, flow.get("totp", ()), flow.get("totp_submit", ()))


async def _login_twitter(page: Page, username: str, password: str, secret: str) -> None:
    """X's multi-screen flow.

    Each screen is its own page render with one field, and the SECOND
    screen is conditional: X sometimes interposes a "confirm your phone or
    username" step before the password. That step and the 2FA step share a
    selector (`ocfEnterTextTextInput`), which is why they cannot simply be
    waited for in sequence -- the password field is what tells the two
    apart, so it is checked for first and the extra step is only handled
    when the password field is NOT yet present.
    """
    await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(random.uniform(1.5, 3.0))

    user_sel = await _first_present(page, ('input[autocomplete="username"]',), timeout=25000)
    if not user_sel:
        raise RuntimeError("X never showed its username field")
    await _human_type(page, user_sel, username)
    await asyncio.sleep(random.uniform(0.4, 1.0))
    await page.keyboard.press("Enter")
    await asyncio.sleep(random.uniform(1.8, 3.0))

    pass_sel = await _first_present(page, ('input[name="password"]',), timeout=6000)
    if not pass_sel:
        # The interstitial: X wants the handle or phone number before it
        # will show the password field.
        extra = await _first_present(
            page, ('input[data-testid="ocfEnterTextTextInput"]',), timeout=8000)
        if extra:
            log.info("X asked for an extra identifier before the password")
            await _human_type(page, extra, username)
            await page.keyboard.press("Enter")
            await asyncio.sleep(random.uniform(1.8, 3.0))
        pass_sel = await _first_present(page, ('input[name="password"]',), timeout=20000)
    if not pass_sel:
        raise RuntimeError("X never showed its password field")

    await _human_type(page, pass_sel, password)
    await asyncio.sleep(random.uniform(0.5, 1.2))
    if not await _human_click(page, ('button[data-testid="LoginForm_Login_Button"]',)):
        await page.keyboard.press("Enter")

    if secret:
        await _handle_totp(page, secret, ('input[data-testid="ocfEnterTextTextInput"]',))


async def run_auto_login(
    platform_id: str,
    username: str,
    password: str,
    two_factor_secret: str = "",
    session_id: str = "",
) -> LoginResult:
    """Sign one account in and hand back what the browser ended up holding.

    `session_id` decides WHICH browser profile this runs in, and passing it
    is what makes the login happen on the device that account already uses
    (see this module's docstring). Omitting it still works and logs in on a
    throwaway profile -- correct, just less convincing to the platform.

    Raises on anything that is not a completed sign-in, with a message
    written for the operator who will read it on the session row.
    """
    from backend.sessions.manager import LOGIN_FLOW

    flow = _FLOWS.get(platform_id)
    if flow is None:
        raise ValueError(f"auto-login is not implemented for {platform_id}")
    _, proof_cookie = LOGIN_FLOW.get(platform_id, ("", ""))
    if not proof_cookie:
        raise ValueError(f"no proof cookie is defined for {platform_id}")

    log.info(f"[AUTO_LOGIN] {platform_id}: signing in as {username}")
    opts = SimpleNamespace(
        headful=not settings.headless,
        timeout=60,
        delay=0,
        # NOT warmed. Warming visits the home feed, and at this point in the
        # run we are not signed in -- so it would spend a page load landing
        # on the very login wall we are here to get past.
        warmup=False,
        cancel=None,
    )
    session = Session(
        opts, [], load_images=True, session_id=session_id, platform=platform_id,
    )

    try:
        ctx = await session.start()
        page = await ctx.new_page()
        try:
            if platform_id == "twitter":
                await _login_twitter(page, username, password, two_factor_secret)
            else:
                await _login_form(page, flow, username, password, two_factor_secret)

            # WAITING FOR PROOF, NOT FOR A PAGE. "The navigation finished"
            # is not "the login worked" -- a wrong password, a CAPTCHA and a
            # checkpoint all finish navigating too. The only thing that
            # settles it is the platform issuing its own session cookie.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _PROOF_TIMEOUT_S
            while loop.time() < deadline:
                await asyncio.sleep(1.5)
                cookies = await ctx.cookies()
                if any(c.get("name") == proof_cookie for c in cookies):
                    try:
                        state = await ctx.storage_state()
                    except Exception:                 # noqa: BLE001 - a bonus
                        state = {}
                    log.info(
                        f"[AUTO_LOGIN] {platform_id}: signed in, captured "
                        f"{len(cookies)} cookie(s)")
                    return LoginResult(cookies=cookies, storage_state=state or {})

            raise TimeoutError(
                f"signed-in cookie {proof_cookie!r} never appeared -- the login most "
                "likely stopped at a CAPTCHA, a checkpoint or a wrong password"
            )
        finally:
            try:
                await page.close()
            except Exception:                         # noqa: BLE001
                pass
    except Exception as e:
        log.error(f"[AUTO_LOGIN] {platform_id} failed for {username}: {type(e).__name__}: {e}")
        raise
    finally:
        # Closes the context, which is also what flushes this login's
        # localStorage and device keys into the persistent profile.
        await session.stop()
