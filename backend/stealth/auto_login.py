"""Automated Stealth Login Engine for credential-backed dummy accounts."""

from __future__ import annotations

import asyncio
import pyotp
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from backend.shared.logging import get_logger
from backend.stealth.browser import Session
from types import SimpleNamespace

log = get_logger("stealth.auto_login")

async def _human_type(page: Page, selector: str, text: str, delay: int = 150):
    """Simulate human typing with realistic delays between keystrokes."""
    await page.wait_for_selector(selector, state="visible", timeout=10000)
    # Clear existing text first if any
    await page.fill(selector, "")
    await page.type(selector, text, delay=delay)


async def _handle_totp(page: Page, totp_secret: str, input_selector: str, submit_selector: str = None) -> bool:
    """Detect 2FA prompt, compute current TOTP code, and submit it."""
    try:
        # Wait briefly to see if 2FA input appears
        await page.wait_for_selector(input_selector, state="visible", timeout=15000)
        totp = pyotp.TOTP(totp_secret.replace(" ", "").upper())
        code = totp.now()
        log.info(f"2FA prompt detected, submitting OTP: {code}")
        await _human_type(page, input_selector, code, delay=200)
        
        if submit_selector:
            # Some platforms auto-submit on 6th digit, others need a click
            try:
                submit_btn = await page.wait_for_selector(submit_selector, state="visible", timeout=3000)
                if submit_btn:
                    await page.click(submit_selector)
            except PlaywrightTimeoutError:
                pass # Maybe it auto-submitted
        else:
            await page.keyboard.press("Enter")
        return True
    except PlaywrightTimeoutError:
        return False


async def run_auto_login(platform_id: str, username: str, password: str, two_factor_secret: str) -> list[dict]:
    """Launch a stealth browser, log in automatically, and return the new cookies."""
    log.info(f"Starting automated credential login for {platform_id} ({username})")
    opts = SimpleNamespace(headful=True, timeout=60, delay=0)
    # Initialize with an empty session (no initial cookies)
    session = Session(opts, [], load_images=True)

    try:
        ctx = await session.start()
        page = await ctx.new_page()

        if platform_id == "instagram":
            await page.goto("https://www.instagram.com/accounts/login/", wait_until="networkidle")
            await _human_type(page, 'input[name="email"]', username)
            await _human_type(page, 'input[name="pass"]', password)
            await page.keyboard.press("Enter")
            
            # Instagram 2FA handler
            if two_factor_secret:
                await _handle_totp(page, two_factor_secret, 'input[name="verificationCode"]', 'button[type="button"]')

        elif platform_id == "facebook":
            await page.goto("https://www.facebook.com/login", wait_until="networkidle")
            await _human_type(page, 'input[name="email"]', username)
            await _human_type(page, 'input[name="pass"]', password)
            await page.keyboard.press("Enter")
            
            # Facebook 2FA handler
            if two_factor_secret:
                await _handle_totp(page, two_factor_secret, 'input[id="approvals_code"]', '#checkpointSubmitButton')

        elif platform_id == "twitter":
            await page.goto("https://x.com/login", wait_until="domcontentloaded")
            # Twitter's flow is multi-step
            await _human_type(page, 'input[autocomplete="username"]', username)
            await page.keyboard.press("Enter")
            
            try:
                await _human_type(page, 'input[name="password"]', password)
                await page.keyboard.press("Enter")
            except PlaywrightTimeoutError:
                # Sometimes asks for email/phone verification before password
                log.warning("Twitter login flow deviated (potential phone/email verification required).")

            # Twitter 2FA handler
            if two_factor_secret:
                await _handle_totp(page, two_factor_secret, 'input[data-testid="ocfEnterTextTextInput"]')

        else:
            raise ValueError(f"Auto-login not implemented for {platform_id}")

        # Wait for the successful login proof cookie to appear
        # We check cookies repeatedly for up to 20 seconds.
        from backend.sessions.manager import LOGIN_FLOW
        _, proof_cookie = LOGIN_FLOW.get(platform_id, ("", ""))
        
        if not proof_cookie:
            raise ValueError(f"No proof cookie defined for {platform_id}")

        log.info(f"Waiting for proof cookie '{proof_cookie}'...")
        deadline = asyncio.get_running_loop().time() + 20.0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2)
            cookies = await ctx.cookies()
            if any(c["name"] == proof_cookie for c in cookies):
                log.info(f"Successfully captured {len(cookies)} cookies post-login.")
                return cookies

        raise TimeoutError("Proof cookie not found after login. Possibly hit a CAPTCHA or security checkpoint.")

    except Exception as e:
        log.error(f"Auto-login failed for {platform_id}: {e}")
        raise e
    finally:
        await session.stop()
