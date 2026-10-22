"""Telegram login: a bad API ID/Hash pair must say what is wrong and what to do.

Telegram answers a wrong pair with the bare "The api_id/api_hash combination
is invalid", which sent the analyst looking for a bug in this tool. The pair
is the analyst's own app credential; no code can make Telegram accept one it
has refused, so the job here is to name the cause and the fix, and to catch a
visibly malformed hash before any network call.

The session path is pointed at a temp folder: `send_code` deletes the session
file on purpose, and a test must never touch the real, working one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.sessions import telegram_login as T
from backend.shared.errors import ValidationError

GOOD_HASH = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(T.settings, "session_blob_path", tmp_path)
    # send_code writes these into the process environment on success
    monkeypatch.setenv("TELEGRAM_API_ID", "")
    monkeypatch.setenv("TELEGRAM_API_HASH", "")
    monkeypatch.setattr(T, "_pending", None)
    yield


def _client(send_code_request):
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.send_code_request = send_code_request
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    GOOD_HASH[:-1],            # one character short
    GOOD_HASH + "0",           # one too long
    GOOD_HASH[:-1] + "z",      # right length, not hex
    "",                        # missing
])
async def test_malformed_hash_is_named_before_any_network_call(monkeypatch, bad):
    made = MagicMock()
    monkeypatch.setattr(T, "TelegramClient", made)
    with pytest.raises(ValidationError) as ei:
        await T.send_code(12345678, bad, "+10000000000")
    if bad:
        assert "32" in str(ei.value) and "my.telegram.org" in str(ei.value)
    made.assert_not_called()           # no client, no connection, nothing sent


@pytest.mark.asyncio
async def test_surrounding_whitespace_in_the_hash_is_forgiven(monkeypatch):
    sent = MagicMock(phone_code_hash="h")
    client = _client(AsyncMock(return_value=sent))
    monkeypatch.setattr(T, "TelegramClient", MagicMock(return_value=client))
    out = await T.send_code(12345678, f"  {GOOD_HASH}\n", "+10000000000")
    assert out["status"] == "code_sent"
    assert T._pending.api_hash == GOOD_HASH      # stored trimmed
    await T.cancel()


@pytest.mark.asyncio
async def test_rejected_pair_explains_the_cause_and_the_fix(monkeypatch):
    err = T.ApiIdInvalidError(request=None)
    client = _client(AsyncMock(side_effect=err))
    monkeypatch.setattr(T, "TelegramClient", MagicMock(return_value=client))
    with pytest.raises(ValidationError) as ei:
        await T.send_code(12345678, GOOD_HASH, "+10000000000")
    msg = str(ei.value)
    assert "SAME app" in msg and "my.telegram.org/apps" in msg
    client.disconnect.assert_awaited()           # never leaves the client open
    assert T._pending is None                    # and no half-finished login


@pytest.mark.asyncio
async def test_other_telegram_errors_keep_their_own_message(monkeypatch):
    client = _client(AsyncMock(side_effect=RuntimeError("flood wait of 300s")))
    monkeypatch.setattr(T, "TelegramClient", MagicMock(return_value=client))
    with pytest.raises(ValidationError) as ei:
        await T.send_code(12345678, GOOD_HASH, "+10000000000")
    assert "flood wait of 300s" in str(ei.value)
    assert "SAME app" not in str(ei.value)       # not mislabelled as a bad pair


@pytest.mark.asyncio
async def test_real_session_file_is_untouched(monkeypatch, tmp_path):
    """The stale-file deletion in send_code must only ever act inside the
    (temp) session folder this test pointed it at."""
    (tmp_path / "telegram.session").write_bytes(b"x")
    client = _client(AsyncMock(side_effect=T.ApiIdInvalidError(request=None)))
    monkeypatch.setattr(T, "TelegramClient", MagicMock(return_value=client))
    with pytest.raises(ValidationError):
        await T.send_code(12345678, GOOD_HASH, "+10000000000")
    assert not (tmp_path / "telegram.session").exists()   # deleted here, by design
