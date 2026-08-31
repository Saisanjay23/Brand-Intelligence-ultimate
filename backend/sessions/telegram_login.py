"""Interactive Telegram login, the one auth flow that cannot be a single
call. MTProto login is inherently multi-step (a code, then optionally a 2FA
password), and the state in between steps, a connected-but-unauthorised
client, plus the phone_code_hash Telegram issues with the code, has to
live somewhere between HTTP requests. This module is that somewhere: one
pending login at a time, held in memory, torn down on success, failure, or
a fresh start.

Nothing here ever sees a stored password: the account's 2FA password is
forwarded straight to Telegram's own sign-in call and never written anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from backend.config.settings import settings
from backend.shared.errors import ConflictError, UpstreamPlatformError, ValidationError
from backend.shared.logging import get_logger

log = get_logger("sessions.telegram_login")

try:
    from telethon import TelegramClient
    from telethon.errors import (PasswordHashInvalidError, PhoneCodeExpiredError,
                                  PhoneCodeInvalidError, SessionPasswordNeededError)
    HAVE_TELETHON = True
except ImportError:  # pragma: no cover
    HAVE_TELETHON = False
    TelegramClient = None  # type: ignore


@dataclass
class _Pending:
    client: "TelegramClient"
    phone: str
    phone_code_hash: str
    api_id: int = 0
    api_hash: str = ""
    step: str = "code"  # code | password


_pending: Optional[_Pending] = None


async def _teardown() -> None:
    global _pending
    if _pending is not None:
        try:
            await _pending.client.disconnect()
        except Exception:
            pass
        _pending = None


async def send_code(api_id: int, api_hash: str, phone: str) -> dict:
    """Step 1: ask Telegram to send a login code to this number."""
    if not HAVE_TELETHON:
        raise UpstreamPlatformError("telethon is not installed (pip install telethon)")
    if not (api_id and api_hash and phone):
        raise ValidationError("api_id, api_hash and phone are all required")

    await _teardown()
    settings.session_blob_path.mkdir(parents=True, exist_ok=True)
    session_path = settings.session_blob_path / "telegram"

    # Every login attempt starts from a clean MTProto handshake. Telethon's
    # session file caches the DC + auth-key from `client.connect()` alone
    # that succeeds regardless of whether api_id/api_hash are valid, so a
    # rejected attempt (or an abandoned retry) still leaves stale auth state
    # behind. Reusing that file for the next attempt is what Telegram
    # responds to with AuthRestartError on send_code_request. Deleting it
    # here is safe: this only runs when the user explicitly starts a new
    # login, the same moment any other platform's "log in again" would
    # replace its old session too.
    for stale in (session_path.with_suffix(".session"), session_path.with_suffix(".session-journal")):
        if stale.exists():
            stale.unlink()

    session_file = str(session_path)
    client = TelegramClient(session_file, api_id, api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except Exception as e:
        await client.disconnect()
        raise ValidationError(f"could not request a code: {e}") from e

    global _pending
    _pending = _Pending(client=client, phone=phone, phone_code_hash=sent.phone_code_hash, api_id=api_id, api_hash=api_hash)

    # Set now, not on success: the Telegram scanner adapter reads these
    # from the environment on its next start() regardless of how this login
    # flow ends, and a wrong id/hash is what send_code_request would have
    # already rejected above. In-process only, never written to .env --
    # `_finish()` below persists the real durable copy to Mongo
    # (save_mtproto_session), and sessions/manager.py::state_for() already
    # re-hydrates os.environ from Mongo on every startup, so a restart
    # before login completes just means logging in again, not a lost value
    # some file was supposed to be keeping safe.
    os.environ["TELEGRAM_API_ID"] = str(api_id)
    os.environ["TELEGRAM_API_HASH"] = api_hash

    log.info(f"login code requested for {phone}")
    return {"status": "code_sent", "phone": phone}


async def submit_code(code: str) -> dict:
    """Step 2: the code the user received. May demand a 2FA password next."""
    if _pending is None:
        raise ConflictError("no login in progress -- request a code first")
    try:
        await _pending.client.sign_in(_pending.phone, code, phone_code_hash=_pending.phone_code_hash)
    except SessionPasswordNeededError:
        _pending.step = "password"
        return {"status": "need_password"}
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        raise ValidationError(f"code rejected: {type(e).__name__}") from e
    return await _finish()


async def submit_password(password: str) -> dict:
    """Step 3, only for accounts with two-factor auth enabled."""
    if _pending is None or _pending.step != "password":
        raise ConflictError("no password step pending")
    try:
        await _pending.client.sign_in(password=password)
    except PasswordHashInvalidError as e:
        raise ValidationError("wrong password") from e
    return await _finish()


async def _finish() -> dict:
    global _pending
    assert _pending is not None
    me = await _pending.client.get_me()
    await _pending.client.disconnect()
    label = f"@{me.username}" if me and me.username else str(getattr(me, "id", ""))

    session_path = settings.session_blob_path / "telegram.session"
    session_blob = session_path.read_bytes() if session_path.exists() else None
    from backend.database.repositories import session_repository as sessions_db
    await sessions_db.save_mtproto_session(
        "telegram",
        identifier=label,
        api_id=_pending.api_id,
        api_hash=_pending.api_hash,
        phone=_pending.phone,
        session_blob=session_blob,
    )

    _pending = None
    log.info(f"telegram login complete -> {label}")
    return {"status": "saved", "message": f"logged in as {label}"}


async def cancel() -> None:
    await _teardown()
