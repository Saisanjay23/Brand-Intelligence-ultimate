"""Does the login flow still match the pages it drives? Run this by hand.

    python -m backend.stealth.check_login_selectors          # offline replica
    python -m backend.stealth.check_login_selectors --live   # the real pages

WHY THIS IS NOT IN backend/tests. That suite is deliberately pure logic --
no browser, no network, runs in two seconds on every save (see pytest.ini),
and this needs a real browser. It is also named so pytest will not collect
it. Run it when a login starts failing, and after any change to _FLOWS or
_USABLE_JS.

WHAT THE OFFLINE MODE PROVES. The unit tests mock the page, so they prove
the logic and nothing about Playwright itself. Two assumptions the fix
rests on are invisible to a mock:

  * that a comma-joined selector list containing :has-text() is accepted by
    the engine -- if it is not, _first_present silently drops to its slower
    per-candidate path and nobody ever finds out; and
  * that the "sel >> nth=1" string _first_usable returns really resolves to
    that element for wait_for, click and query_selector.

The replica page below reproduces what x.com actually serves: the form
rendered twice, and a password field painted on the first screen at
opacity 0 with pointer-events none, which is the thing that broke this.

WHAT THE LIVE MODE PROVES. Where EVERY platform in the registry stands:
which selectors are still real on the pages as served today, which
platforms have no browser login to automate (and why that is correct),
and which are cookie platforms with no automated login at all -- a gap,
because an expired session there stays expired until a person notices.

It walks the registry rather than a list kept in this file, so a platform
added later cannot be quietly skipped. A platform nobody checks looks
exactly like a platform that is fine.

Drift is the normal state of affairs here, not an incident. It does not
sign in and never touches a credential.

It asks for the same locale the real login asks for, because Facebook
serves the login page in the local language otherwise, and a text selector
that is right in English then reports as missing. See _live().
"""
from __future__ import annotations

import asyncio
import sys

REPLICA = """
<html><body style="margin:0">
<!-- copy 1: the duplicate X renders off-screen. Everything about it is
     'visible' to Playwright and useless to a person. -->
<form>
  <input name="username_or_email" style="opacity:0;pointer-events:none">
  <input name="password" type="password" style="opacity:0;pointer-events:none">
  <button type="submit" style="opacity:0;pointer-events:none">Continue</button>
</form>
<!-- copy 2: the one on screen -->
<form>
  <input name="username_or_email" id="real_user">
  <!-- the password field X paints BEFORE the password step is reached -->
  <input name="password" type="password" id="real_pass"
         style="opacity:0;pointer-events:none">
  <button type="button" id="sso">Continue with Apple</button>
  <button type="submit" id="real_continue">Continue</button>
</form>
<script>
  document.getElementById('real_continue').addEventListener('click', e => {
    e.preventDefault();
    const p = document.getElementById('real_pass');
    p.style.opacity = '1';
    p.style.pointerEvents = 'auto';
    // A DOM attribute, not a window global: patchright runs evaluate() in
    // an ISOLATED world, which shares the DOM but not page-script globals,
    // so a window flag reads back false even when the click landed.
    document.body.setAttribute('data-continue-clicked', 'yes');
  });
</script>
</body></html>
"""


def _driver():
    try:
        from patchright.async_api import async_playwright
        return async_playwright, "patchright"
    except ImportError:
        from playwright.async_api import async_playwright
        return async_playwright, "playwright"


async def _replica() -> list[str]:
    from backend.stealth.auto_login import (_X_CONTINUE, _first_present,
                                            _human_type)

    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"          got {got!r}\n          want {want!r}")
            fails.append(name)

    async_playwright, driver = _driver()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(REPLICA)
        print(f"replica of x.com's login form, driver: {driver}")

        user = await _first_present(
            page, ('input[name="username_or_email"]',), timeout=3000)
        check("the usable copy of the username field wins, not the first",
              user, 'input[name="username_or_email"] >> nth=1')

        await _human_type(page, user, "probe")
        check("typing goes into the copy that was vetted",
              await page.locator("#real_user").input_value(), "probe")

        check("an opacity-0 password field is NOT reported present",
              await _first_present(page, ('input[name="password"]',), timeout=1500),
              None)

        cont = await _first_present(page, _X_CONTINUE, timeout=3000)
        check("Continue is found through the combined :has-text list",
              cont, 'button[type="submit"]:has-text("Continue") >> nth=1')

        await page.locator(cont).first.click()
        check("the click reaches X's own Continue button",
              await page.get_attribute("body", "data-continue-clicked"), "yes")

        check("the password field is present once Continue reveals it",
              await _first_present(page, ('input[name="password"]',), timeout=3000),
              'input[name="password"] >> nth=1')

        await browser.close()
    return fails


# What each platform's login page is expected to look like, for the ones
# _FLOWS actually drives. Only the shapes that can be checked WITHOUT
# typing anything into somebody's login form are listed: X's password field
# and its Continue button do not exist until the username box has content,
# so for X only the first screen can be inspected.
#
# A field marked expect_absent is one whose PRESENCE is the finding -- that
# is how a flow changing back to one page gets noticed instead of quietly
# working differently.
def _expected(platform_id: str):
    from backend.stealth.auto_login import _FLOWS, _X_CONTINUE

    flow = _FLOWS.get(platform_id)
    if not flow:
        return None, []
    if platform_id == "twitter":
        return "https://x.com/i/flow/login", [
            ("user", ('input[name="username_or_email"]',
                      'input[autocomplete~="username"]',
                      'input[name="text"]'), False),
            ("password, on the first screen", ('input[name="password"]',), True),
            ("continue, before the username is typed", _X_CONTINUE, True),
        ]
    return flow["url"], [
        ("user", flow["user"], False),
        ("password", flow["password"], False),
        ("submit", flow.get("submit", ()), False),
    ]


# The region-block notice is NOT a login page, and must never be reported
# as "the selectors are gone" -- a geoblock that reads as a parser break
# sends whoever investigates at the wrong code entirely.
#
# BORROWED, NOT COPIED. tiktok/discovery_engine.py already detects this,
# for that exact reason, and a second copy of the same patterns here is two
# things that have to be kept in step and will not be.
def _geoblocked(url: str, body: str) -> bool:
    try:
        from backend.platforms.tiktok.discovery_engine import geoblocked
    except Exception:                                 # noqa: BLE001
        return False
    return geoblocked(url, body)


async def _live() -> list[str]:
    """Walk EVERY platform in the registry and say where each one stands.

    Driven by the registry rather than a list kept here, so a platform
    added later cannot be silently skipped by this check -- which is the
    only failure mode that would matter, since a platform nobody checks
    looks exactly like a platform that is fine.

    Never signs in; never types a credential.
    """
    from backend.platforms.registry import PLATFORMS
    from backend.sessions.manager import LOGIN_FLOW
    from backend.stealth.auto_login import _FLOWS, _first_present

    problems: list[str] = []
    async_playwright, driver = _driver()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        # THE SAME LOCALE THE REAL LOGIN USES (browser.py pins en-US, which
        # drives Accept-Language). Without it this reported Facebook's
        # submit button as GONE -- because Facebook had served the page in
        # KANNADA, where the button reads "___ __ ____" and a
        # :has-text("Log in") selector correctly matches nothing.
        #
        # That is a checker that cries wolf, which is worse than no checker:
        # the next person to see it learns to ignore it. Anything this tool
        # does differently from the thing it is checking is a lie it will
        # eventually tell.
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900}, locale="en-US")

        for pid, plat in PLATFORMS.items():
            print(f"\n{pid}")

            # 1. Platforms that HAVE no browser login. Not a gap: there is
            #    nothing to sign into. Said out loud anyway, because
            #    "absent" and "absent on purpose" look identical in a table
            #    of three ticks and three blanks.
            if plat.uses_api_key:
                print(f"  n/a   authenticates with an API key "
                      f"({plat.api_key_env}) -- no login page exists")
                continue
            if plat.env_keys:
                print(f"  n/a   authenticates with {', '.join(plat.env_keys)} "
                      f"-- no browser login exists")
                continue

            # 2. A cookie platform with no automated login is a REAL gap:
            #    when its session dies it stays dead until a person pastes
            #    a fresh cookie export.
            if pid not in _FLOWS or pid not in LOGIN_FLOW:
                which = []
                if pid not in LOGIN_FLOW:
                    which.append("LOGIN_FLOW")
                if pid not in _FLOWS:
                    which.append("_FLOWS")
                print(f"  GAP   cookie platform with no automated login "
                      f"(missing from {' and '.join(which)}) -- an expired "
                      f"session here needs a person")
                problems.append(f"{pid}: no automated login")
                continue

            url, fields = _expected(pid)
            print(f"        {url}")
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(3)

                body = await page.evaluate(
                    "() => (document.body.innerText || '').slice(0, 4000)")
                if _geoblocked(page.url, body):
                    print(f"  BLOCKED  this IP gets a region-block notice at "
                          f"{page.url}, not a login page. Nothing about the "
                          f"selectors can be checked from here -- route "
                          f"through a proxy in a region where it is served.")
                    problems.append(f"{pid}: region-blocked from this IP")
                    continue

                for name, selectors, expect_absent in fields:
                    hit = await _first_present(page, selectors, timeout=8000)
                    if hit and not expect_absent:
                        print(f"  ok    {name}: {hit}")
                    elif hit and expect_absent:
                        print(f"  NOTE  {name}: present ({hit}) -- expected "
                              f"absent, so this flow has changed shape")
                        problems.append(f"{pid}.{name} appeared")
                    elif expect_absent:
                        print(f"  ok    {name}: absent, as expected")
                    else:
                        print(f"  GONE  {name}: none of {list(selectors)} is usable")
                        problems.append(f"{pid}.{name}")

                proof = LOGIN_FLOW[pid][1]
                if proof not in plat.required_cookies:
                    print(f"  WARN  proof cookie {proof!r} is not among this "
                          f"platform's required cookies {plat.required_cookies}")
                    problems.append(f"{pid}: proof cookie not required")
            except Exception as e:                    # noqa: BLE001
                print(f"  ERROR {type(e).__name__}: {e}")
                problems.append(f"{pid} (page did not load)")
            finally:
                await page.close()

        await ctx.close()
        await browser.close()
    return problems


async def main() -> int:
    live = "--live" in sys.argv
    bad = await (_live() if live else _replica())
    print()
    if bad:
        print("PROBLEMS: " + ", ".join(bad))
        return 1
    print("all good" + ("" if live else " -- add --live to check the real pages"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
