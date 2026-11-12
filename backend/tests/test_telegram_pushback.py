"""Telegram pushback: every flood wait, dead session and limited account is
named as what it is, reaches the platform governor, and never reads as a
clean result or as a different failure.

Driven through fake clients and real Telethon error classes; nothing here
talks to Telegram.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import (AuthKeyUnregisteredError, FloodPremiumWaitError,
                             FloodWaitError, PeerFloodError,
                             SessionRevokedError, SlowModeWaitError,
                             UserDeactivatedBanError, UserDeactivatedError)

from backend.platforms.telegram import analysis_engine as TA
from backend.platforms.telegram import discovery_engine as TD
from backend.shared import pushback
from backend.shared.resilience import classify_failure

SESSION_DEAD = [AuthKeyUnregisteredError, SessionRevokedError,
                UserDeactivatedError, UserDeactivatedBanError]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pushback.reset_all()
    monkeypatch.setattr(TD, "_open_clients", 0)
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    yield
    pushback.reset_all()


def _gov():
    return pushback.governor_for("telegram")


class FakeClient:
    """`await client(request)` plus the few methods the engine calls."""

    def __init__(self, call=None, photo=None, entity=None, messages=None):
        self._call = call or (lambda req: SimpleNamespace(users=[], chats=[]))
        self.download_profile_photo = AsyncMock(side_effect=photo, return_value=b"img")
        self.get_entity = AsyncMock(return_value=entity)
        self.get_messages = AsyncMock(return_value=messages or [])
        self.requests = []

    async def __call__(self, request):
        self.requests.append(type(request).__name__)
        res = self._call(request)
        if isinstance(res, BaseException):
            raise res
        return res


def _tg(client):
    tg = TD.Telegram()
    tg.client = client
    return tg


def _user(name, photo=True):
    return SimpleNamespace(id=hash(name) % 10_000, username=name, first_name=name,
                           photo=SimpleNamespace() if photo else None)


def _channel():
    return SimpleNamespace(id=55, username="brand", title="Brand", broadcast=True,
                           photo=SimpleNamespace(), left=True,
                           date=datetime(2020, 1, 1, tzinfo=timezone.utc))


# ------------------------------------------------------------------ A1 / A2

@pytest.mark.asyncio
async def test_sweep_flood_wait_carries_seconds_and_tells_the_governor():
    d = TD.Discovery(SimpleNamespace(max_results=0))
    d.tg = SimpleNamespace(search=AsyncMock(side_effect=TD.FloodWait(42)))
    out = await d.sweep("acme")
    assert out.stopped == "flood-wait" and not out.complete
    assert out.retry_after_s == 42
    assert "42s" in out.error
    assert _gov().pushbacks == 1 and _gov().last_signal.kind == pushback.THROTTLED


@pytest.mark.asyncio
async def test_healthy_sweep_is_unchanged():
    client = FakeClient(call=lambda req: SimpleNamespace(users=[_user("acme1")], chats=[]))
    d = TD.Discovery(SimpleNamespace(max_results=0))
    d.tg = _tg(client)
    out = await d.sweep("acme")
    assert out.stopped == "exhausted" and out.complete
    assert out.retry_after_s == 0
    assert client.requests == ["SearchRequest"]
    assert out.hits[0].notes == ""
    assert _gov().total == 0


# ----------------------------------------------------------------------- A3

def _start_client(authorised, flood_due=None):
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=authorised)
    client._flood_waited_requests = (
        {TD.GetStateRequest.CONSTRUCTOR_ID: flood_due} if flood_due else {})
    return client


@pytest.mark.asyncio
async def test_long_flood_on_auth_probe_is_a_flood_not_a_dead_session():
    client = _start_client(False, flood_due=time.time() + 300)
    with patch.object(TD, "TelegramClient", return_value=client):
        with pytest.raises(TD.FloodWait) as ei:
            await TD.Telegram().start()
    assert 295 <= ei.value.seconds <= 300
    client.disconnect.assert_awaited_once()
    assert not TD.session_file_in_use()


@pytest.mark.asyncio
async def test_flood_on_auth_probe_stops_the_sweep_as_flood_wait():
    client = _start_client(False, flood_due=time.time() + 120)
    with patch.object(TD, "TelegramClient", return_value=client):
        out = await TD.Discovery(SimpleNamespace(max_results=0)).sweep("acme")
    assert out.stopped == "flood-wait" and out.retry_after_s >= 115


@pytest.mark.asyncio
@pytest.mark.parametrize("due", [None, -5])
async def test_real_logout_is_still_not_authorised(due):
    # no flood recorded, or one Telethon already slept through
    client = _start_client(False, flood_due=(time.time() + due) if due else None)
    with patch.object(TD, "TelegramClient", return_value=client):
        with pytest.raises(TD.NotAuthorised):
            await TD.Telegram().start()


@pytest.mark.asyncio
async def test_authorised_start_never_reads_the_flood_record():
    client = _start_client(True, flood_due=time.time() + 300)
    with patch.object(TD, "TelegramClient", return_value=client):
        tg = TD.Telegram()
        await tg.start()
    client.is_user_authorized.assert_awaited_once()
    assert client.flood_sleep_threshold == 0
    await tg.stop()


# ----------------------------------------------------------------------- A4

@pytest.mark.asyncio
@pytest.mark.parametrize("err", SESSION_DEAD)
async def test_call_maps_dead_session_to_not_authorised(err):
    tg = _tg(FakeClient(call=lambda req: err(request=None)))
    with pytest.raises(TD.NotAuthorised) as ei:
        await tg.search("acme")
    assert err.__name__ in str(ei.value)
    assert classify_failure(str(ei.value)) == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("err", SESSION_DEAD)
async def test_resolve_maps_dead_session(err):
    client = FakeClient()
    client.get_entity = AsyncMock(side_effect=err(request=None))
    with pytest.raises(TD.NotAuthorised):
        await _tg(client).resolve("brand")


@pytest.mark.asyncio
async def test_dead_session_during_full_detail_is_not_bio_not_read():
    client = FakeClient(call=lambda req: SessionRevokedError(request=None),
                        entity=_channel())
    with pytest.raises(TD.NotAuthorised):
        await _tg(client).resolve("brand")
    client.get_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_last_post_maps_dead_session():
    client = FakeClient()
    client.get_messages = AsyncMock(side_effect=AuthKeyUnregisteredError(request=None))
    notes = []
    with pytest.raises(TD.NotAuthorised):
        await _tg(client).last_post(object(), notes)
    assert notes == []


@pytest.mark.asyncio
async def test_dead_session_row_is_classified_expired():
    s = TA.Scraper(SimpleNamespace(delay=0))
    client = FakeClient()
    client.get_entity = AsyncMock(side_effect=UserDeactivatedBanError(request=None))
    s.tg = _tg(client)
    row = await s.one("https://t.me/brand", "Brand", "")
    assert row.status == "ERROR"
    assert classify_failure(row.notes) == "expired"


# ----------------------------------------------------------------------- A5

@pytest.mark.asyncio
@pytest.mark.parametrize("err", [FloodPremiumWaitError, SlowModeWaitError, FloodWaitError])
async def test_every_flood_error_is_a_flood_wait(err):
    tg = _tg(FakeClient(call=lambda req: err(request=None, capture=17)))
    with pytest.raises(TD.FloodWait) as ei:
        await tg.search("acme")
    assert ei.value.seconds == 17


@pytest.mark.asyncio
async def test_peer_flood_stops_the_sweep_as_a_checkpoint():
    d = TD.Discovery(SimpleNamespace(max_results=0))
    d.tg = _tg(FakeClient(call=lambda req: PeerFloodError(request=None)))
    out = await d.sweep("acme")
    assert out.stopped == "checkpoint" and not out.complete
    assert "checkpoint" in out.error
    assert classify_failure(out.error) == "checkpointed"
    assert _gov().last_signal.kind == pushback.CHALLENGED


@pytest.mark.asyncio
async def test_peer_flood_in_analysis_is_a_checkpoint_row():
    s = TA.Scraper(SimpleNamespace(delay=0))
    client = FakeClient()
    client.get_entity = AsyncMock(side_effect=PeerFloodError(request=None))
    s.tg = _tg(client)
    row = await s.one("https://t.me/brand", "Brand", "")
    assert row.status == "CHECKPOINT"
    assert "checkpoint" in row.notes
    assert _gov().last_signal.kind == pushback.CHALLENGED


# ----------------------------------------------------------------------- A6

@pytest.mark.asyncio
async def test_photo_flood_keeps_results_notes_skipped_photos_and_records_once():
    users = [_user("a1"), _user("a2"), _user("a3", photo=False)]
    client = FakeClient(call=lambda req: SimpleNamespace(users=users, chats=[]),
                        photo=[FloodWaitError(request=None, capture=30)])
    d = TD.Discovery(SimpleNamespace(max_results=0))
    d.tg = _tg(client)
    out = await d.sweep("acme")
    assert out.stopped == "exhausted" and len(out.hits) == 3
    by_name = {r.username: r for r in out.hits}
    assert TD.PHOTO_FLOOD_NOTE in by_name["a1"].notes   # the one that hit the wait
    assert TD.PHOTO_FLOOD_NOTE in by_name["a2"].notes   # skipped while paused
    assert by_name["a3"].notes == ""                     # no picture to skip
    assert client.download_profile_photo.await_count == 1
    assert _gov().pushbacks == 1


# ------------------------------------------------------------------ B3 / B4

@pytest.mark.asyncio
async def test_flood_after_entity_keeps_what_was_read():
    client = FakeClient(call=lambda req: FloodWaitError(request=None, capture=60),
                        entity=_channel())
    s = TA.Scraper(SimpleNamespace(delay=0))
    s.tg = _tg(client)
    row = await s.one("https://t.me/brand", "Brand", "")
    assert row.status == "CHECKPOINT"
    assert row.profile_name == "Brand" and row.profile_id == "brand"
    assert row.profile_pic_url.startswith("data:image/jpeg")
    assert "60s pause" in row.notes
    assert "members/bio not read (stopped early)" in row.notes
    assert "last post not read (stopped early)" in row.notes
    client.get_messages.assert_not_awaited()
    assert _gov().last_signal.kind == pushback.THROTTLED


@pytest.mark.asyncio
async def test_flood_on_numeric_link_keeps_the_id():
    client = FakeClient()
    client.get_entity = AsyncMock(side_effect=FloodWaitError(request=None, capture=9))
    s = TA.Scraper(SimpleNamespace(delay=0))
    s.tg = _tg(client)
    row = await s.one("https://t.me/c/123456", "Brand", "")
    assert row.status == "CHECKPOINT" and row.profile_id == "123456"


@pytest.mark.asyncio
async def test_fresh_checkpoint_row_falls_back_to_numeric_id():
    s = TA.Scraper(SimpleNamespace(delay=0))
    s.process = AsyncMock(side_effect=TD.FloodWait(5))   # no row attached
    row = await s.one("https://t.me/c/987", "Brand", "")
    assert row.status == "CHECKPOINT" and row.profile_id == "987"
