"""Session pool management: pasting cookies, saving an API key, an
interactive login, proxy assignment, deletion.

The analysis tool cannot scrape anything without a live session for the
platform being scraped, so this is the one piece of operational surface it
genuinely depends on.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.api.models import (CheckResult, LoginState, SessionPool,
                                 SessionPoolList, TelegramLoginState)
from backend.sessions import manager as sessions_engine
from backend.sessions import telegram_login as telegram_login_service
from backend.shared.logging import get_logger

router = APIRouter(prefix="/sessions", tags=["sessions"])
log = get_logger("api.sessions")


class CookiesIn(BaseModel):
    blob: str
    identifier: str = ""


class CredentialsIn(BaseModel):
    identifier: str = ""
    username: str
    password: str
    two_factor_secret: str = ""
    proxy: Optional[str] = None


class ApiKeyIn(BaseModel):
    key: str
    identifier: str = ""


class SessionUpdateIn(BaseModel):
    blob: str = ""
    api_key: str = ""
    identifier: Optional[str] = None


class LoginIn(BaseModel):
    timeout_s: int = 300
    identifier: str = ""


class ProxyIn(BaseModel):
    proxy: Optional[dict] = None


class TelegramLoginStart(BaseModel):
    api_id: int
    api_hash: str
    phone: str


class TelegramLoginCode(BaseModel):
    code: str


class TelegramLoginPassword(BaseModel):
    password: str


# Every pool in ONE call, so the UI can keep the whole view live on one
# polled request instead of one per platform. Bare "" rather than "/" keeps
# it off a trailing-slash redirect.
@router.get("", response_model=SessionPoolList)
async def get_all_session_status() -> dict:
    from backend.platforms import registry

    live_health = await sessions_engine.cached_health()
    platform_ids = list(registry.PLATFORMS)
    results = await asyncio.gather(
        *(sessions_engine.status(pid, live_health) for pid in platform_ids),
        return_exceptions=True,
    )
    items = []
    for platform_id, result in zip(platform_ids, results):
        if isinstance(result, BaseException):
            log.warning(f"{platform_id}: session status unavailable -- {type(result).__name__}: {result}")
            continue
        items.append(result)
    return {"items": items}


@router.get("/{platform_id}", response_model=SessionPool)
async def get_session_status(platform_id: str) -> dict:
    live_health = await sessions_engine.cached_health()
    return await sessions_engine.status(platform_id, live_health)


@router.post("/{platform_id}/cookies", response_model=SessionPool)
async def add_cookies(platform_id: str, body: CookiesIn) -> dict:
    return await sessions_engine.save_cookies(platform_id, body.blob, body.identifier)


@router.post("/{platform_id}/credentials", response_model=SessionPool)
async def add_credentials(platform_id: str, body: CredentialsIn) -> dict:
    return await sessions_engine.save_credentials(
        platform_id, body.identifier, body.username, body.password,
        body.two_factor_secret, body.proxy,
    )


@router.post("/{platform_id}/api-key", response_model=SessionPool)
async def add_api_key(platform_id: str, body: ApiKeyIn) -> dict:
    return await sessions_engine.save_api_key(platform_id, body.key, body.identifier)


@router.put("/{platform_id}/{session_id}", response_model=SessionPool)
async def update_session(platform_id: str, session_id: str, body: SessionUpdateIn) -> dict:
    return await sessions_engine.update_session_credentials(
        platform_id, session_id, body.blob, body.api_key, body.identifier)


@router.post("/{platform_id}/login", response_model=LoginState)
async def login(platform_id: str, body: LoginIn) -> dict:
    return await sessions_engine.launch_login(platform_id, body.timeout_s, body.identifier)


@router.put("/{platform_id}/{session_id}/proxy", response_model=SessionPool)
async def set_proxy(platform_id: str, session_id: str, body: ProxyIn) -> dict:
    return await sessions_engine.set_proxy(platform_id, session_id, body.proxy)


@router.post("/proxy/test", summary="Test a proxy before assigning it")
async def test_proxy(body: ProxyIn) -> dict:
    """Launch a throwaway browser through this proxy and report what an
    origin server would actually see.

    Not bound to a platform or a session on purpose -- the point is to check
    a proxy BEFORE committing it to one. Answers the two questions that a
    saved-and-forgotten proxy silently gets wrong:

      * is traffic really leaving through it (a Chromium SOCKS fallback
        sends it out on the host's own IP while still looking configured), and
      * is the exit a datacenter range, which is the loudest network-layer
        signal there is regardless of how clean the browser looks.

    Slow by API standards (it starts a real browser, twice, to compare the
    proxied address against the direct one) -- a few seconds is expected.
    """
    from backend.stealth.proxy import probe_proxy

    return await probe_proxy(body.proxy)


@router.delete("/{platform_id}/{session_id}", response_model=SessionPool)
async def delete_session(platform_id: str, session_id: str) -> dict:
    return await sessions_engine.delete(platform_id, session_id)


@router.delete("/{platform_id}", response_model=SessionPool)
async def delete_pool(platform_id: str) -> dict:
    return await sessions_engine.delete(platform_id)


@router.post("/{platform_id}/check", response_model=CheckResult)
async def check_session(platform_id: str) -> dict:
    ok, detail = await sessions_engine.check_one(platform_id)
    return {"ok": ok, "detail": detail}


# One named account, on demand -- what someone who has just re-pasted
# cookies needs, as opposed to the sweep above which picks whichever
# session is most overdue for a check.
@router.post("/{platform_id}/{session_id}/check", response_model=CheckResult)
async def check_session_item(platform_id: str, session_id: str) -> dict:
    result = await sessions_engine.check_item(platform_id, session_id)
    return {**result, "session": await sessions_engine.status(
        platform_id, await sessions_engine.cached_health())}


# Telegram's MTProto login is multi-step (code, then optionally a 2FA
# password) so it cannot reuse the single-shot /{platform_id}/login route.
@router.post("/telegram/login/start", response_model=TelegramLoginState)
async def telegram_login_start(body: TelegramLoginStart) -> dict:
    return await telegram_login_service.send_code(body.api_id, body.api_hash, body.phone)


@router.post("/telegram/login/code", response_model=TelegramLoginState)
async def telegram_login_code(body: TelegramLoginCode) -> dict:
    return await telegram_login_service.submit_code(body.code)


@router.post("/telegram/login/password", response_model=TelegramLoginState)
async def telegram_login_password(body: TelegramLoginPassword) -> dict:
    return await telegram_login_service.submit_password(body.password)


@router.post("/telegram/login/cancel", response_model=TelegramLoginState)
async def telegram_login_cancel() -> dict:
    await telegram_login_service.cancel()
    return {"status": "cancelled"}
