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

# THE TIMEOUT CLASS HAS TO COME FROM THE DRIVER THAT IS ACTUALLY RUNNING.
#
# stealth/browser.py prefers PATCHRIGHT and falls back to playwright, and
# the two ship SEPARATE, unrelated exception classes --
# `patchright._impl._errors.TimeoutError` is not a subclass of
# `playwright._impl._errors.TimeoutError`. This module used to import only
# playwright's, so on a patchright install every `except` here was dead
# code: the first selector that timed out escaped instead of falling
# through to the next candidate, and a login that had a perfectly good
# alternative selector one line down died on the first one.
#
# That is not hypothetical -- it is what "auto-login failed:
# Timeout 10000ms exceeded waiting for input#email" was, on a page that was
# serving input[name="email"] the whole time.
#
# Both are caught, as a tuple, so this is correct whichever driver is
# installed and stays correct if browser.py's preference ever changes.
_TIMEOUTS: tuple[type[BaseException], ...] = ()
for _mod in ("patchright.async_api", "playwright.async_api"):
    try:
        _err = __import__(_mod, fromlist=["TimeoutError"]).TimeoutError
        if _err not in _TIMEOUTS:
            _TIMEOUTS = (*_TIMEOUTS, _err)
    except Exception:                                 # noqa: BLE001
        continue
if not _TIMEOUTS:                                     # neither importable
    _TIMEOUTS = (TimeoutError,)

try:
    from patchright.async_api import Page             # type: ignore
except ImportError:                                   # pragma: no cover
    from playwright.async_api import Page             # type: ignore

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
# MEASURED AGAINST THE LIVE PAGES, 2026-09-22. Every selector in the first
# version of this table was written from memory and three of them were
# simply wrong -- including one that "hardened" Instagram's working
# `input[name="email"]` into a `input[name="username"]` that has never
# existed on that page. What each platform actually serves, logged out:
#
#   facebook   input[name="email"] / input[name="pass"]. There is NO
#              `input#email`: the ids are randomised per render
#              (`_R_c9l6neappb6amH1_`), so an id selector can never match.
#              The submit control is a div[role=button] reading "Log in",
#              not a <button>; the form's own input[type=submit] is hidden.
#   instagram  THE SAME TWO NAMES as Facebook -- Meta serves one login form
#              for both now -- and the same div[role=button] "Log in".
#   twitter    one page with BOTH fields visible at once,
#              input[name="username_or_email"] and input[name="password"],
#              and no submit button of its own (the only buttons are the
#              Google/Apple/phone SSO options), so Enter is the submit.
#              x.com/i/flow/login redirects to
#              x.com/i/jf/onboarding/web?mode=login.
#
# Older spellings are KEPT as later candidates rather than deleted: these
# pages are A/B tested continuously and an account may well be served the
# previous flow. They cost nothing now that `_first_present` waits once for
# all candidates together instead of timing out on each in turn.
_FLOWS: dict[str, dict] = {
    "facebook": {
        "url": "https://www.facebook.com/login",
        "user": ('input[name="email"]', "input#email"),
        "password": ('input[name="pass"]', "input#pass"),
        "submit": ('div[role="button"]:has-text("Log in")',
                   'button[name="login"]', 'button[type="submit"]', "#loginbutton"),
        "totp": ("#approvals_code", 'input[name="approvals_code"]'),
        "totp_submit": ("#checkpointSubmitButton", 'button[type="submit"]',
                        'div[role="button"]:has-text("Continue")'),
    },
    "instagram": {
        "url": "https://www.instagram.com/accounts/login/",
        "user": ('input[name="email"]', 'input[name="username"]'),
        "password": ('input[name="pass"]', 'input[name="password"]'),
        "submit": ('div[role="button"]:has-text("Log in")', 'button[type="submit"]'),
        "totp": ('input[name="verificationCode"]', 'input[name="securityCode"]',
                 'input[autocomplete="one-time-code"]'),
        "totp_submit": ('div[role="button"]:has-text("Confirm")',
                        'button[type="button"]', 'button[type="submit"]'),
    },
    "twitter": {
        "url": "https://x.com/i/flow/login",
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
    # `.first` throughout: X renders TWO copies of its login form (measured
    # -- both carry username_or_email and password), and a bare selector
    # that resolves to two elements is a strict-mode violation, not a click.
    # `_first_present` has normally already pinned WHICH copy, via an
    # `>> nth=` suffix, so this is usually a no-op; it stays because a
    # caller passing a bare selector must not blow up.
    target = page.locator(selector).first
    await target.wait_for(state="visible", timeout=timeout)
    # AN EXPLICIT CLICK TIMEOUT, because Playwright's default is THIRTY
    # SECONDS and the thing that goes wrong here is precisely a click that
    # can never land (see _USABLE_JS). Thirty seconds of that per field
    # turns a login into a hang that outlives the caller's patience; eight
    # is far longer than a field a person can see takes to accept a click,
    # and fails fast when one cannot.
    await target.click(timeout=8000)
    # Clear whatever a remembered login left behind, by selection rather
    # than by fill, so the field still only ever sees key events.
    await page.keyboard.press("ControlOrMeta+a")
    await page.keyboard.press("Backspace")
    for ch in text:
        await page.keyboard.type(ch, delay=random.uniform(80, 180))
        if random.random() < 0.15:
            await asyncio.sleep(random.uniform(0.30, 0.60))


# WHAT "VISIBLE" HAS TO MEAN HERE, AND WHY PLAYWRIGHT'S ANSWER IS NOT IT.
#
# Playwright calls an element visible when it has a non-empty bounding box
# and is not "visibility: hidden". That is a question about rendering. The
# question a login flow is actually asking is "could a person click this",
# and the two come apart badly on exactly the pages this module drives.
#
# MEASURED ON x.com, 2026-09-22. Its login page renders the password field
# on the FIRST screen, before the password step is reached, at "opacity: 0"
# and "pointer-events: none", stacked underneath the username box. The real
# flow is: type the username, press the Continue button that only becomes a
# button once that field has content, and only then does a password field
# you can actually touch appear. Playwright reports the opacity-0 one
# visible, so:
#
#   * _first_present said the password field was already on screen, which
#     sent _login_twitter down its ONE-PAGE branch and skipped the Continue
#     press that is what reveals the real password step; and
#   * _human_type then clicked an element with "pointer-events: none",
#     which can never receive a click, so Playwright waited out its full
#     default 30-second actionability timeout and the login died there --
#     every time, on every X account. No selector in the table was wrong.
#
# The three things Playwright's check misses are each a real way a page
# hides a control from a person while leaving it in the box model: opacity
# anywhere up the ancestor chain, "pointer-events: none", and something
# else painted on top. checkVisibility handles the first (it walks
# ancestors, which a bare getComputedStyle(el).opacity does not); the hit
# test handles the third.
#
# Size is deliberately NOT re-checked here. wait_for_selector(state=
# "visible") has already enforced a non-empty box, and re-testing it
# against a viewport that can legitimately measure 0x0 (a pane that is
# hidden or not yet laid out) would reject controls that are perfectly
# fine -- measured, on a collapsed browser pane, where every element on
# facebook.com/login reports a negative origin.
_USABLE_JS = """
el => {
  if (el.disabled) return false;
  const cs = getComputedStyle(el);
  if (cs.pointerEvents === 'none') return false;
  if (typeof el.checkVisibility === 'function') {
    if (!el.checkVisibility({opacityProperty: true, visibilityProperty: true,
                             contentVisibilityAuto: true})) return false;
  } else {
    for (let p = el; p && p.nodeType === 1; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.display === 'none' || s.visibility === 'hidden') return false;
      if (parseFloat(s.opacity) === 0) return false;
    }
  }
  const r = el.getBoundingClientRect();
  const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
  if (innerWidth > 0 && innerHeight > 0 &&
      cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight) {
    const top = document.elementFromPoint(cx, cy);
    if (top && top !== el && !el.contains(top) && !top.contains(el)) return false;
  }
  return true;
}
"""


async def _first_usable(page: Page, selector: str, limit: int = 8) -> Optional[str]:
    """The first match of `selector` a person could actually use, named so
    that later steps get THAT ONE.

    Both platforms render their login form more than once -- X twice on the
    same screen, Instagram once visibly and once hidden -- so "the match"
    is not a thing that exists; there is only "the usable match". The
    returned string carries the index with it (`sel >> nth=2`, Playwright's
    own chaining syntax), so whatever types into it gets the element vetted
    here rather than whatever `.first` resolves to a second later.

    The bare selector comes back when the usable match IS the first one,
    which is the ordinary case and keeps the failure messages readable.
    """
    try:
        loc = page.locator(selector)
        count = await loc.count()
    except Exception:                                 # noqa: BLE001 - bad selector
        return None
    for i in range(min(count, limit)):
        try:
            if await loc.nth(i).evaluate(_USABLE_JS):
                return selector if i == 0 else f"{selector} >> nth={i}"
        except Exception:                             # noqa: BLE001 - detached
            continue
    return None


async def _first_present(page: Page, selectors: tuple[str, ...], timeout: int = 15000) -> Optional[str]:
    """The first of these selectors a person could actually interact with.

    ONE WAIT FOR ALL OF THEM, not one wait each. Waiting per candidate
    divides the budget: with a dead selector first, the real one does not
    even get looked at until the dead one's share has elapsed, and a page
    that renders slowly then misses both. Worse, it made the ORDER of the
    list a correctness issue rather than a preference.

    So the candidates are joined into a single CSS selector and waited for
    together -- whichever appears first satisfies the wait -- and only then
    are they checked one at a time against _USABLE_JS. The comma form is
    plain CSS and Playwright's own :has-text() extension composes with it.

    AND THEN IT KEEPS LOOKING. The combined wait returns the moment
    Playwright thinks something is visible, which on X is true of a field
    no person can touch. When nothing is usable yet, the rest of the budget
    goes on re-checking instead of being handed back as "not there" --
    otherwise the stricter test would have turned "renders half a second
    late" into an instant failure, which is the bug it was meant to fix
    wearing different clothes.

    Falls back to per-candidate waits if the combined selector is rejected
    by the engine, so an exotic selector can never make this worse than the
    version it replaces.
    """
    if not selectors:
        return None
    combined = ", ".join(selectors)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout / 1000.0
    budget = timeout                                  # the first wait gets it all
    while True:
        try:
            await page.wait_for_selector(combined, state="visible", timeout=budget)
        except _TIMEOUTS:
            return None
        except Exception:                             # noqa: BLE001 - bad combo
            per = max(1500, int(timeout / max(1, len(selectors))))
            for sel in selectors:
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=per)
                except _TIMEOUTS:
                    continue
                except Exception:                     # noqa: BLE001
                    continue
                if hit := await _first_usable(page, sel):
                    return hit
            return None

        for sel in selectors:
            if hit := await _first_usable(page, sel):
                return hit

        # Something matched but nothing was usable. Spend what is left of
        # the budget waiting for the page to turn one of them into a
        # control, rather than reporting a field that is on its way.
        if (deadline - loop.time()) <= 0:
            return None
        await asyncio.sleep(0.25)
        budget = int((deadline - loop.time()) * 1000)
        if budget <= 0:
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
        await page.locator(sel).first.click(timeout=8000)
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

    # ENTER IS A REAL FALLBACK HERE, unlike on X. Meta's form carries its
    # own input[type="submit"] -- hidden (measured 0x0), but present and
    # not disabled, which is all implicit submission requires -- so Enter
    # submits the form even when the click found nothing.
    #
    # And it can find nothing for a reason that has nothing to do with
    # drift: the submit control is matched BY ITS TEXT, and Facebook serves
    # this page in the local language when it feels like it (measured: a
    # request with no Accept-Language got Kannada, where the button reads
    # "___ __ ____"). browser.py pins en-US, which is what normally keeps
    # the text selector honest; this is what happens when that is not
    # enough.
    if not await _human_click(page, flow.get("submit", ())):
        await page.keyboard.press("Enter")

    if secret:
        await _handle_totp(page, secret, flow.get("totp", ()), flow.get("totp_submit", ()))


# X's own "go on to the password" control, measured 2026-09-22. It is a
# real <button type="submit"> labelled "Continue" -- but ONLY once the
# username field has content. While that field is empty the same spot is a
# plain <div> with no role, no tabindex and no click handler at all, which
# is why looking for a button before typing finds nothing and why the old
# 'div[role="button"]:has-text("Log in")' never matched: X does not use the
# words "Log in" on this page, and it does not use role="button" here.
#
# [type="submit"] is doing real work in that selector: the page also has
# "Continue with phone" and "Continue with Apple" buttons, which :has-text
# matches on substring. Those are type="button".
_X_CONTINUE: tuple[str, ...] = (
    'button[type="submit"]:has-text("Continue")',
    'button[data-testid="LoginForm_Login_Button"]',   # the older flow
    'div[role="button"]:has-text("Next")',
    'div[role="button"]:has-text("Log in")',
)


async def _login_twitter(page: Page, username: str, password: str, secret: str) -> None:
    """X, which currently serves TWO different login flows.

    MEASURED 2026-09-22: x.com/i/flow/login redirects to
    x.com/i/jf/onboarding/web?mode=login, which asks for the username
    first, on its own, behind a Continue button -- and renders the password
    field on that same first screen at opacity 0 with pointer-events none,
    where no person can reach it but Playwright's own visibility check
    happily reports it. Everything about telling the two flows apart
    therefore runs through _first_present, which knows the difference (see
    _USABLE_JS); a plain "is the password field visible" question gets the
    wrong answer here, confidently.

    The page also renders the whole form TWICE, which is why every locator
    goes through _first_present rather than a bare selector.

    The older screen-at-a-time flow is still handled, because X runs both
    and which one an account gets is not ours to decide.
    """
    await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(random.uniform(2.0, 3.5))

    user_sel = await _first_present(
        page,
        ('input[name="username_or_email"]',        # the current first screen
         'input[autocomplete~="username"]',        # ~= : the attribute reads
                                                   # "username webauthn", so
                                                   # an exact match fails
         'input[name="text"]'),                    # the older step-at-a-time
        timeout=25000,
    )
    if not user_sel:
        raise RuntimeError("X never showed a username field on its login page")
    await _human_type(page, user_sel, username)
    await asyncio.sleep(random.uniform(0.4, 1.0))

    # ONE PAGE OR SEVERAL? A password field a person could type into being
    # here already is what tells the two flows apart. Asked with a short
    # budget, because on the current flow it is legitimately absent and
    # waiting longer would only be spent proving a negative.
    pass_sel = await _first_present(page, ('input[name="password"]',), timeout=2500)

    if pass_sel is None:
        # Continue, and only then Enter. ENTER IS NOT A SAFE DEFAULT HERE:
        # a form submits implicitly on Enter only when it has a submit
        # button, or at most one text-ish field. This form has two (the
        # username and that hidden password), so until the Continue button
        # exists Enter does nothing whatsoever -- silently. Pressing it
        # anyway afterwards costs nothing and covers the older flow.
        if not await _human_click(page, _X_CONTINUE):
            await page.keyboard.press("Enter")
        await asyncio.sleep(random.uniform(1.8, 3.0))
        pass_sel = await _first_present(page, ('input[name="password"]',), timeout=8000)
        if pass_sel is None:
            # The interstitial: X wants the handle or phone number before it
            # will show the password field.
            extra = await _first_present(
                page, ('input[data-testid="ocfEnterTextTextInput"]',), timeout=8000)
            if extra:
                log.info("X asked for an extra identifier before the password")
                await _human_type(page, extra, username)
                if not await _human_click(page, _X_CONTINUE):
                    await page.keyboard.press("Enter")
                await asyncio.sleep(random.uniform(1.8, 3.0))
            pass_sel = await _first_present(
                page, ('input[name="password"]',), timeout=20000)
    if not pass_sel:
        raise RuntimeError("X never showed a password field anyone could type into")

    await _human_type(page, pass_sel, password)
    await asyncio.sleep(random.uniform(0.5, 1.2))
    if not await _human_click(page, ('button[data-testid="LoginForm_Login_Button"]',
                                     'button[type="submit"]:has-text("Log in")',
                                     'button[type="submit"]:has-text("Continue")',
                                     'div[role="button"]:has-text("Log in")')):
        await page.keyboard.press("Enter")

    if secret:
        await _handle_totp(
            page, secret,
            # NOT MEASURED: reaching X's 2FA screen needs a real sign-in,
            # so these two are the old flow's, unverified against the
            # current one. Left as they are rather than "improved" from
            # memory, which is how three wrong selectors got into this file
            # the first time.
            ('input[data-testid="ocfEnterTextTextInput"]',
             'input[autocomplete="one-time-code"]'),
            _X_CONTINUE)


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
