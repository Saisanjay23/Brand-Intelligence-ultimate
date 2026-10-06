"""The one Telegram session file, and who may open it.

Every pooled Telegram account shares session/telegram.session, and Telethon
holds it open from client construction to disconnect(). A second client on
it fails with `OperationalError: database is locked` (seen live 2026-10-05,
from the session monitor). Two holes let that happen:

    a failed start() leaked the file   callers only stop() after a start()
                                       that returned, so a connect that
                                       raised kept the file open for good
    health checks could not see jobs   the in-use guard is per session id,
                                       but the file is per platform
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.platforms.telegram import discovery_engine as T
from backend.sessions import manager as M


@pytest.fixture(autouse=True)
def _clean_counter(monkeypatch):
    monkeypatch.setattr(T, "_open_clients", 0)
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    yield


def _fake_client(*, connect_raises=None, authorised=True):
    client = MagicMock()
    client.connect = AsyncMock(side_effect=connect_raises)
    client.is_user_authorized = AsyncMock(return_value=authorised)
    client.disconnect = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_failed_connect_closes_the_file():
    client = _fake_client(connect_raises=OSError("network down"))
    with patch.object(T, "TelegramClient", return_value=client):
        tg = T.Telegram()
        with pytest.raises(OSError):
            await tg.start()
    client.disconnect.assert_awaited_once()
    assert not T.session_file_in_use()


@pytest.mark.asyncio
async def test_open_client_is_visible_until_stop():
    with patch.object(T, "TelegramClient", return_value=_fake_client()):
        tg = T.Telegram()
        await tg.start()
        assert T.session_file_in_use()
        await tg.stop()
        assert not T.session_file_in_use()
        # A second stop() must not drive the count negative.
        await tg.stop()
    assert T._open_clients == 0


@pytest.mark.asyncio
async def test_unauthorised_session_releases_the_file():
    with patch.object(T, "TelegramClient", return_value=_fake_client(authorised=False)):
        with pytest.raises(T.NotAuthorised):
            await T.Telegram().start()
    assert not T.session_file_in_use()


@pytest.mark.asyncio
async def test_health_check_steps_aside_while_the_file_is_open(monkeypatch):
    monkeypatch.setattr(T, "_open_clients", 1)
    plat = SimpleNamespace(uses_api_key=False, env_keys=("TELEGRAM_API_ID",),
                           scraper=MagicMock(side_effect=AssertionError("must not open a client")))
    with patch("backend.platforms.registry.get", return_value=plat):
        ok, detail, conclusive = await M._verify_credential_item(
            "telegram", {"api_id": 1, "api_hash": "h"})
    # Inconclusive: the account's status must be left exactly as it was.
    assert (ok, conclusive) == (False, False)
    assert "in use" in detail


# ---------------------------------------------------- t.me/c/<id> analysis
#
# Discovery writes `t.me/c/<id>` for every account with no @username -- 4
# of 5 Telegram results in the 2026-10-06 live run. Analysis refused them
# all as "private links", so most Telegram finds were never read.

from backend.platforms.telegram import analysis_engine as A  # noqa: E402


def test_numeric_id_of():
    assert A.numeric_id_of("https://t.me/c/8925111777") == 8925111777
    assert A.numeric_id_of("https://t.me/mr_hari_hari") is None
    assert A.numeric_id_of("https://t.me/joinchat/AbCd") is None


def _scraper_with(resolve):
    s = A.Scraper(SimpleNamespace(delay=0))
    s.tg = SimpleNamespace(resolve=resolve)
    return s


@pytest.mark.asyncio
async def test_id_link_is_resolved_by_number():
    ent = T.TelegramEntity(entity_id="8925111777", title="Anuj Cyfirma", has_photo=True)
    resolve = AsyncMock(return_value=ent)
    row = await _scraper_with(resolve).one("https://t.me/c/8925111777", "CYFIRMA", "")
    resolve.assert_awaited_once_with(8925111777)
    assert row.status == "OK"
    assert row.profile_name == "Anuj Cyfirma"


@pytest.mark.asyncio
async def test_uncached_id_is_an_error_not_gone():
    # GONE reads as "taken down"; an id we never cached is not that.
    resolve = AsyncMock(side_effect=T.NotCached("id 1 is not in this Telegram session's cache"))
    row = await _scraper_with(resolve).one("https://t.me/c/1", "CYFIRMA", "")
    assert row.status == "ERROR"


@pytest.mark.asyncio
async def test_resolve_raises_not_cached_only_for_ids():
    tg = T.Telegram()
    tg.client = SimpleNamespace(get_entity=AsyncMock(side_effect=ValueError("no entity")))
    with pytest.raises(T.NotCached):
        await tg.resolve(8925111777)
    assert await tg.resolve("someone") is None
