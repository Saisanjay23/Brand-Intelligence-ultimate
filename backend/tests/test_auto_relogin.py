"""Self-healing login, and the three guards that stop it burning accounts.

THE FEATURE IS THE GUARDS. Signing a logged-out account back in is a dozen
lines; doing it without destroying the account is the part worth testing.
An unguarded re-login is not self-healing, it is a scripted password attempt
every thirty minutes against an account the platform is already unhappy
with -- and the thing being protected here is the pool itself, because a
pool with no live accounts and a pool whose client genuinely has no
impersonators look identical in the product.

So the assertions below are mostly about NOT logging in:

    not while a job holds the account   a second browser on one account from
                                        one IP is this codebase's
                                        most-repeated warning
    not before the cooldown             the monitor wakes every 30 minutes;
                                        a permanently wrong password would
                                        otherwise be 48 attempts a day
    not past the attempt ceiling        a CAPTCHA or a changed password is
                                        never fixed by trying again, and
                                        every retry makes it worse

And one about ordering: quarantine happens FIRST and recovery can only ever
put a session back, never keep a broken one in circulation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.config.settings import settings
from backend.sessions import manager as M
from backend.stealth.auto_login import LoginResult

FB_GOOD = [
    {"name": "c_user", "value": "100", "domain": ".facebook.com", "path": "/"},
    {"name": "xs", "value": "abc", "domain": ".facebook.com", "path": "/"},
]
FB_PARTIAL = [{"name": "c_user", "value": "100", "domain": ".facebook.com", "path": "/"}]


@pytest.fixture(autouse=True)
def _clean_counters():
    M._relogin_attempts.clear()
    M._relogin_last.clear()
    M._relogin_running.clear()
    yield
    M._relogin_attempts.clear()
    M._relogin_last.clear()
    M._relogin_running.clear()


def _item(**kw):
    base = {
        "id": "s1", "identifier": "fb_bot_1", "status": "expired",
        "username": "bot@example.com", "password": "hunter2",
        "two_factor_secret": "JBSWY3DPEHPK3PXP", "cookies": [],
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- the guards


@pytest.mark.asyncio
async def test_a_cookie_only_account_is_never_auto_logged_in():
    """Nothing to sign in with. This must be silent and ordinary, not an
    error -- most pooled accounts are cookie-only."""
    login = AsyncMock()
    with patch.object(M.sessions_db, "get_item",
                      AsyncMock(return_value=_item(username="", password=""))), \
         patch("backend.stealth.auto_login.run_auto_login", login):
        assert await M.maybe_auto_relogin("facebook", "s1") is False
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_account_a_job_is_using_is_never_auto_logged_in():
    login = AsyncMock()
    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=_item())), \
         patch.object(M, "_session_in_use", return_value=True), \
         patch("backend.stealth.auto_login.run_auto_login", login):
        assert await M.maybe_auto_relogin("facebook", "s1") is False
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_cooldown_stops_a_second_attempt(monkeypatch):
    """The monitor wakes every 30 minutes. Without this, one permanently
    wrong password is dozens of login attempts a day on one account."""
    monkeypatch.setattr(settings, "session_relogin_cooldown_minutes", 180.0)
    M._relogin_last["facebook:s1"] = M._now()
    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=_item())), \
         patch.object(M, "_session_in_use", return_value=False):
        assert await M.maybe_auto_relogin("facebook", "s1") is False


@pytest.mark.asyncio
async def test_the_attempt_ceiling_makes_it_give_up_and_wait_for_a_person(monkeypatch):
    monkeypatch.setattr(settings, "session_relogin_max_attempts", 3)
    monkeypatch.setattr(settings, "session_relogin_cooldown_minutes", 0.0)
    M._relogin_attempts["facebook:s1"] = 3
    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=_item())), \
         patch.object(M, "_session_in_use", return_value=False):
        assert await M.maybe_auto_relogin("facebook", "s1") is False


@pytest.mark.asyncio
async def test_the_kill_switch_disables_self_healing(monkeypatch):
    monkeypatch.setattr(settings, "session_auto_relogin", False)
    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=_item())):
        assert await M.maybe_auto_relogin("facebook", "s1") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["tiktok", "youtube", "telegram"])
async def test_platforms_with_no_login_flow_are_never_attempted(platform):
    get_item = AsyncMock()
    with patch.object(M.sessions_db, "get_item", get_item):
        assert await M.maybe_auto_relogin(platform, "s1") is False
    get_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_re_logins_for_one_account_never_overlap(monkeypatch):
    monkeypatch.setattr(settings, "session_relogin_cooldown_minutes", 0.0)
    M._relogin_running.add("facebook:s1")
    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=_item())), \
         patch.object(M, "_session_in_use", return_value=False):
        assert await M.maybe_auto_relogin("facebook", "s1") is False


# ------------------------------------------------------------ the attempt


@pytest.mark.asyncio
async def test_a_successful_re_login_stores_the_fresh_cookies():
    save = AsyncMock()
    with patch("backend.stealth.auto_login.run_auto_login",
               AsyncMock(return_value=LoginResult(cookies=FB_GOOD))), \
         patch.object(M.sessions_db, "update_session_credentials", save):
        ok, detail = await M._perform_relogin("facebook", "s1", _item())

    assert ok is True and detail == ""
    save.assert_awaited_once()
    stored = save.await_args.kwargs["cookies"]
    assert {c["name"] for c in stored} == {"c_user", "xs"}


@pytest.mark.asyncio
async def test_a_successful_re_login_does_not_wipe_the_device_it_just_built():
    """The sign-in happened INSIDE that account's persistent profile, so the
    device identity it established is the whole point. Writing through the
    manager's own update_session_credentials would reset the profile and
    throw that away -- the repository call is deliberate."""
    reset = MagicMock()
    with patch("backend.stealth.auto_login.run_auto_login",
               AsyncMock(return_value=LoginResult(cookies=FB_GOOD))), \
         patch.object(M.sessions_db, "update_session_credentials", AsyncMock()), \
         patch("backend.stealth.browser.reset_profile", reset):
        await M._perform_relogin("facebook", "s1", _item())
    reset.assert_not_called()


@pytest.mark.asyncio
async def test_a_login_that_lands_on_a_checkpoint_is_not_treated_as_recovered():
    """Cookies came back, but not the ones that prove a session. Storing
    them would put a half-logged-in account back into the pool."""
    save = AsyncMock()
    with patch("backend.stealth.auto_login.run_auto_login",
               AsyncMock(return_value=LoginResult(cookies=FB_PARTIAL))), \
         patch.object(M.sessions_db, "update_session_credentials", save):
        ok, detail = await M._perform_relogin("facebook", "s1", _item())

    assert ok is False
    assert "xs" in detail
    save.assert_not_awaited()
    assert M._relogin_attempts["facebook:s1"] == 1


@pytest.mark.asyncio
async def test_a_failed_re_login_counts_and_records_why():
    update = AsyncMock()
    with patch("backend.stealth.auto_login.run_auto_login",
               AsyncMock(side_effect=TimeoutError("stopped at a CAPTCHA"))), \
         patch.object(M.sessions_db, "update_item", update):
        ok, detail = await M._perform_relogin("facebook", "s1", _item())

    assert ok is False
    assert "CAPTCHA" in detail
    assert M._relogin_attempts["facebook:s1"] == 1
    # The reason goes on the row, not only into a log an operator will
    # never open.
    assert "re-login failed" in update.await_args.kwargs["last_error"]


@pytest.mark.asyncio
async def test_recovering_clears_the_attempt_counter():
    M._relogin_attempts["facebook:s1"] = 2
    with patch("backend.stealth.auto_login.run_auto_login",
               AsyncMock(return_value=LoginResult(cookies=FB_GOOD))), \
         patch.object(M.sessions_db, "update_session_credentials", AsyncMock()):
        await M._perform_relogin("facebook", "s1", _item())
    assert "facebook:s1" not in M._relogin_attempts


@pytest.mark.asyncio
async def test_a_re_login_always_releases_its_own_running_flag():
    """A stuck flag means this account can never be re-logged-in again for
    the life of the process, silently."""
    with patch("backend.stealth.auto_login.run_auto_login",
               AsyncMock(side_effect=RuntimeError("boom"))), \
         patch.object(M.sessions_db, "update_item", AsyncMock()):
        await M._perform_relogin("facebook", "s1", _item())
    assert "facebook:s1" not in M._relogin_running


# ------------------------------------------------------- the operator button


@pytest.mark.asyncio
async def test_pressing_re_login_now_waives_the_cooldown_and_the_ceiling(monkeypatch):
    """A person pressing this has usually just fixed the password, which is
    the exact case the cooldown and the ceiling exist to wait for."""
    monkeypatch.setattr(settings, "session_relogin_cooldown_minutes", 180.0)
    monkeypatch.setattr(settings, "session_relogin_max_attempts", 1)
    M._relogin_last["facebook:s1"] = M._now()
    M._relogin_attempts["facebook:s1"] = 9

    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=_item())), \
         patch.object(M, "_session_in_use", return_value=False), \
         patch("backend.stealth.auto_login.run_auto_login",
               AsyncMock(return_value=LoginResult(cookies=FB_GOOD))), \
         patch.object(M.sessions_db, "update_session_credentials", AsyncMock()), \
         patch.object(M, "status", AsyncMock(return_value={})):
        res = await M.relogin_now("facebook", "s1")

    assert res["ok"] is True


@pytest.mark.asyncio
async def test_re_login_now_still_refuses_while_a_job_holds_the_account():
    """The one guard a button press does NOT waive: it is about not getting
    the account challenged, not about pacing."""
    from backend.shared.errors import ConflictError

    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=_item())), \
         patch.object(M, "_session_in_use", return_value=True):
        with pytest.raises(ConflictError):
            await M.relogin_now("facebook", "s1")


@pytest.mark.asyncio
async def test_re_login_now_says_so_when_there_are_no_credentials():
    from backend.shared.errors import ConflictError

    with patch.object(M.sessions_db, "get_item",
                      AsyncMock(return_value=_item(username="", password=""))):
        with pytest.raises(ConflictError):
            await M.relogin_now("facebook", "s1")


@pytest.mark.asyncio
async def test_re_login_now_on_an_unknown_session_is_a_not_found():
    from backend.shared.errors import NotFoundError

    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=None)):
        with pytest.raises(NotFoundError):
            await M.relogin_now("facebook", "nope")


# ---------------------------------------------------------------- ordering


@pytest.mark.asyncio
async def test_the_account_is_quarantined_before_recovery_is_even_attempted():
    """Recovery can only ever put a session BACK. If it fails, is refused,
    or never starts, the behaviour is exactly what it was before this
    feature existed -- quarantined, with an incident raised."""
    order: list[str] = []

    async def _failed(*a, **kw):
        order.append("quarantine")

    async def _relogin(*a, **kw):
        order.append("relogin")
        return False

    with patch.object(M.sessions_db, "record_item_health", AsyncMock(return_value=None)), \
         patch.object(M, "mark_session_failed", _failed), \
         patch.object(M, "maybe_auto_relogin", _relogin):
        await M._record_item_result("facebook", "s1", "fb_bot_1", False, "session invalid", True)

    assert order == ["quarantine", "relogin"]


@pytest.mark.asyncio
async def test_an_inconclusive_check_never_triggers_a_re_login():
    """A network blip is not evidence the session is logged out, and signing
    in on the strength of one would be a password attempt caused by the
    tool's own connectivity."""
    relogin = AsyncMock()
    with patch.object(M.sessions_db, "record_item_health", AsyncMock(return_value=None)), \
         patch.object(M, "maybe_auto_relogin", relogin):
        await M._record_item_result(
            "facebook", "s1", "fb_bot_1", False, "ReadTimeout", False)
    relogin.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_passing_check_never_triggers_a_re_login():
    relogin = AsyncMock()
    with patch.object(M.sessions_db, "record_item_health", AsyncMock(return_value=None)), \
         patch.object(M, "mark_session_ok", AsyncMock()), \
         patch.object(M, "maybe_auto_relogin", relogin):
        await M._record_item_result("facebook", "s1", "fb_bot_1", True, "", True)
    relogin.assert_not_awaited()
