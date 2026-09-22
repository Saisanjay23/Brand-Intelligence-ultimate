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

import asyncio

import pytest

from backend.config.settings import settings
from backend.database.repositories import session_repository as R
from backend.sessions import manager as M
from backend.stealth.auto_login import LoginResult

# BOUND AT IMPORT, BEFORE ANY FIXTURE RUNS. `M.sessions_db` is this very
# module, so the autouse fixture that stops these tests writing to Mongo
# replaces the attribute on it -- and a test that reads
# `R.record_relogin_attempt` inside a test body gets the mock, not the
# function it meant to test. These names keep pointing at the real ones.
_real_record = R.record_relogin_attempt
_real_update_credentials = R.update_session_credentials


def _fake_db(coll):
    """A stand-in for the repository's db(), where every collection is the
    same mock -- these tests only ever look at one."""
    class _DB(dict):
        def __getitem__(self, _name):
            return coll
    return _DB()

FB_GOOD = [
    {"name": "c_user", "value": "100", "domain": ".facebook.com", "path": "/"},
    {"name": "xs", "value": "abc", "domain": ".facebook.com", "path": "/"},
]
FB_PARTIAL = [{"name": "c_user", "value": "100", "domain": ".facebook.com", "path": "/"}]


@pytest.fixture(autouse=True)
def _clean_counters():
    # Only the in-flight lock lives in memory now. The attempt count and
    # the cooldown clock are fields on the session row -- see _item().
    M._relogin_running.clear()
    yield
    M._relogin_running.clear()


@pytest.fixture(autouse=True)
def _dont_write_to_mongo():
    """_perform_relogin now records every attempt through the repository.
    These tests have no database, and a test that quietly swallowed that
    write would stop noticing if it disappeared."""
    with patch.object(M.sessions_db, "record_relogin_attempt",
                      AsyncMock()) as rec:
        yield rec


def _item(**kw):
    base = {
        "id": "s1", "platform": "facebook", "identifier": "fb_bot_1",
        "status": "expired",
        "username": "bot@example.com", "password": "hunter2",
        "two_factor_secret": "JBSWY3DPEHPK3PXP", "cookies": [],
        # the stored self-healing record, which is where the cooldown and
        # the attempt ceiling are read from
        "relogin_attempts": 0, "relogin_last_attempt": 0.0,
        "relogin_last_success": 0.0, "relogin_total_successes": 0,
    }
    base.update(kw)
    return base


def _outcomes(rec):
    """The sequence of outcomes handed to the repository."""
    return [c.args[2] if len(c.args) > 2 else c.kwargs["outcome"]
            for c in rec.await_args_list]


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
    row = _item(relogin_last_attempt=M._now())
    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=row)), \
         patch.object(M, "_session_in_use", return_value=False):
        assert await M.maybe_auto_relogin("facebook", "s1") is False


@pytest.mark.asyncio
async def test_the_attempt_ceiling_makes_it_give_up_and_wait_for_a_person(monkeypatch):
    monkeypatch.setattr(settings, "session_relogin_max_attempts", 3)
    monkeypatch.setattr(settings, "session_relogin_cooldown_minutes", 0.0)
    row = _item(relogin_attempts=3)
    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=row)), \
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


# ------------------------------------------- the record survives a restart


@pytest.mark.asyncio
async def test_the_cooldown_is_read_from_the_row_so_a_restart_cannot_waive_it(
        monkeypatch):
    """THE BUG THIS EXISTS FOR.

    The cooldown and the attempt ceiling used to be enforced against two
    module-level dicts and nothing else, so every backend restart forgot
    them both. A three-hour cooldown became no cooldown; "gave up after 3
    attempts, this needs a person" became three more attempts. Restarts are
    not rare, and neither of those is bookkeeping -- they are the rails
    stealth/auto_login.py's docstring is talking about when it says a
    scripted password attempt every half hour is how an account stops
    being recoverable at all.

    Nothing about that looked broken from outside: the panel showed 0
    failed attempts, which reads as "nothing has gone wrong here" and
    actually meant "we forgot what went wrong here".

    A module with NO in-memory state at all (which is what a fresh process
    is) must still refuse, purely on what the row says.
    """
    monkeypatch.setattr(settings, "session_relogin_cooldown_minutes", 180.0)
    assert not M._relogin_running                     # i.e. freshly started
    row = _item(relogin_last_attempt=M._now() - 60)   # one minute ago

    login = AsyncMock()
    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=row)), \
         patch.object(M, "_session_in_use", return_value=False), \
         patch("backend.stealth.auto_login.run_auto_login", login):
        assert await M.maybe_auto_relogin("facebook", "s1") is False
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_attempt_ceiling_is_read_from_the_row_too(monkeypatch):
    monkeypatch.setattr(settings, "session_relogin_max_attempts", 3)
    monkeypatch.setattr(settings, "session_relogin_cooldown_minutes", 0.0)
    assert not M._relogin_running
    row = _item(relogin_attempts=3)

    login = AsyncMock()
    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=row)), \
         patch.object(M, "_session_in_use", return_value=False), \
         patch("backend.stealth.auto_login.run_auto_login", login):
        assert await M.maybe_auto_relogin("facebook", "s1") is False
    login.assert_not_awaited()


def test_the_in_flight_lock_is_the_one_thing_that_stays_in_memory():
    """It is a lock, not a record, and the difference decides where it
    lives. Persisted, a process killed mid-login would leave it set for
    ever: the self-healer would skip that account and the Re-Login Now
    button would refuse, permanently, with no way back but editing the
    database. In memory it clears itself by definition -- the process that
    held it is the process that died."""
    import inspect

    src = inspect.getsource(M._perform_relogin) + inspect.getsource(M.relogin_state)
    assert "_relogin_running" in src
    # and it is never handed to the repository
    assert "record_relogin_attempt" not in inspect.getsource(M.relogin_state)
    assert not hasattr(M, "_relogin_attempts"), (
        "the attempt counter is back in memory, where a restart loses it")
    assert not hasattr(M, "_relogin_last"), (
        "the cooldown clock is back in memory, where a restart loses it")


@pytest.mark.asyncio
async def test_every_attempt_is_written_down_before_the_browser_opens(
        _dont_write_to_mongo):
    """Stamped at the START. The cooldown runs from the beginning of an
    attempt, so a login that takes two minutes cannot let a second one
    through behind it -- and a process killed mid-login still leaves the
    cooldown recorded rather than looking like it never tried."""
    order = []
    _dont_write_to_mongo.side_effect = lambda *a, **k: order.append("recorded")

    async def _login(*a, **kw):
        order.append("browser")
        return LoginResult(cookies=FB_GOOD)

    with patch("backend.stealth.auto_login.run_auto_login", _login), \
         patch.object(M.sessions_db, "update_session_credentials", AsyncMock()):
        await M._perform_relogin("facebook", "s1", _item())

    assert order[0] == "recorded", "the attempt was not stamped before the login"
    assert _outcomes(_dont_write_to_mongo)[0] == "started"


@pytest.mark.asyncio
async def test_a_successful_re_login_stores_what_the_browser_ended_up_holding():
    """storage_state was captured on every sign-in and then dropped one
    line later, while this module's docstring said it was kept. A modern
    login keeps state in localStorage that a cookie-only capture misses,
    and a capture thrown away cannot be gone back for."""
    save = AsyncMock()
    state = {"origins": [{"origin": "https://facebook.com", "localStorage": []}]}
    with patch("backend.stealth.auto_login.run_auto_login",
               AsyncMock(return_value=LoginResult(cookies=FB_GOOD,
                                                  storage_state=state))), \
         patch.object(M.sessions_db, "update_session_credentials", save):
        ok, _ = await M._perform_relogin("facebook", "s1", _item())

    assert ok is True
    assert save.await_args.kwargs["storage_state"] == state


@pytest.mark.asyncio
async def test_a_failed_bookkeeping_write_never_fails_a_login_that_worked(caplog):
    """But it must not pass silently either: the counter it failed to write
    is a safety rail, and a rail nobody knows is missing is worse than one
    that is visibly gone."""
    with caplog.at_level("ERROR"), \
         patch.object(M.sessions_db, "record_relogin_attempt",
                      AsyncMock(side_effect=RuntimeError("mongo is down"))), \
         patch("backend.stealth.auto_login.run_auto_login",
               AsyncMock(return_value=LoginResult(cookies=FB_GOOD))), \
         patch.object(M.sessions_db, "update_session_credentials", AsyncMock()):
        ok, _ = await M._perform_relogin("facebook", "s1", _item())

    assert ok is True, "a bookkeeping failure sank a login that worked"
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "unreliable" in said, "the lost counter was never mentioned"


def test_the_panel_is_told_whether_self_healing_has_ever_worked_here():
    """"3 failed attempts" reads completely differently on an account that
    has recovered itself eleven times than on one that has never once
    managed it, and the row could not tell you which it was."""
    row = {"id": "s1", "platform": "facebook", "identifier": "x",
           "status": "expired", "relogin_attempts": 3,
           "relogin_last_attempt": 1700000000.0,
           "relogin_last_success": 1699999000.0,
           "relogin_total_successes": 11, "rate_limited_until": 0,
           "last_used": 0, "cookies": [], "username": "u", "password": "p"}
    out = M._public(row)
    assert out["relogin_attempts"] == 3
    assert out["relogin_total_successes"] == 11
    assert out["relogin_last_success"] == 1699999000.0
    # epoch seconds, the same shape as last_used beside it -- the panel
    # renders these with new Date(x * 1000)
    assert isinstance(out["relogin_last_attempt"], float)


# ------------------------------------------- what the repository writes


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,expect", [
    ("started", {"$set": {"relogin_last_attempt": ...}}),
    ("ok", {"$set": {"relogin_attempts": 0, "relogin_last_success": ...},
            "$inc": {"relogin_total_successes": 1}}),
    ("failed", {"$inc": {"relogin_attempts": 1}}),
])
async def test_each_outcome_writes_the_update_it_says_it_does(outcome, expect):
    coll = MagicMock()
    coll.update_one = AsyncMock()
    with patch.object(R, "db", lambda: _fake_db(coll)):
        await _real_record("facebook", "s1", outcome)

    _filter, update = coll.update_one.await_args.args
    assert set(update) == set(expect)
    for op, fields in expect.items():
        assert set(update[op]) == set(fields)
        for k, v in fields.items():
            if v is not ...:
                assert update[op][k] == v


@pytest.mark.asyncio
async def test_counting_a_failure_is_atomic_not_read_then_write():
    """Two attempts racing must not lose a count. A ceiling cannot survive
    a counter that quietly under-reports."""
    coll = MagicMock()
    coll.update_one = AsyncMock()
    with patch.object(R, "db", lambda: _fake_db(coll)):
        await _real_record("facebook", "s1", "failed")

    _filter, update = coll.update_one.await_args.args
    assert "$inc" in update and "relogin_attempts" in update["$inc"]
    assert "relogin_attempts" not in update.get("$set", {})


@pytest.mark.asyncio
async def test_an_unknown_outcome_is_refused_rather_than_written():
    """A typo must not turn into a write nobody asked for, or worse, a
    silent no-op that leaves the ceiling un-counted."""
    coll = MagicMock()
    coll.update_one = AsyncMock()
    with patch.object(R, "db", lambda: _fake_db(coll)), \
         pytest.raises(ValueError):
        await _real_record("facebook", "s1", "probably_fine")
    coll.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_credentials_clear_the_failed_ladder_but_not_the_successes():
    """An operator who has just fixed the password is exactly who the
    cooldown and the ceiling were making wait, so new credentials clear
    both. How many times this account has healed itself is a fact about
    the account, not about the password that was just replaced, so that
    survives."""
    coll = MagicMock()
    coll.update_one = AsyncMock()
    coll.delete_one = AsyncMock()
    coll.find_one = AsyncMock(return_value=None)
    with patch.object(R, "db", lambda: _fake_db(coll)):
        await _real_update_credentials("facebook", "s1", password="new")

    _filter, update = coll.update_one.await_args.args
    written = update["$set"]
    assert written["relogin_attempts"] == 0
    assert written["relogin_last_attempt"] == 0.0
    assert "relogin_total_successes" not in written
    assert "relogin_last_success" not in written


def test_the_stored_row_carries_every_field_the_rails_are_read_from():
    """_to_item is the only way a row reaches the manager. A field missing
    from this mapping reads as 0 -- which for a cooldown clock means
    "never attempted" and for a ceiling means "no failures yet". That
    exact bug already happened once here with last_ok; see its note."""
    from backend.database.repositories import session_repository as R

    item = R._to_item({"_id": "facebook:s1", "platform": "facebook",
                       "session_id": "s1", "relogin_attempts": 4,
                       "relogin_last_attempt": 123.0,
                       "relogin_last_success": 99.0,
                       "relogin_total_successes": 7,
                       "storage_state": {"origins": []}})
    assert item["relogin_attempts"] == 4
    assert item["relogin_last_attempt"] == 123.0
    assert item["relogin_last_success"] == 99.0
    assert item["relogin_total_successes"] == 7
    assert item["storage_state"] == {"origins": []}


def test_a_row_written_before_this_change_still_reads_cleanly():
    """Every existing session document predates these fields. They have to
    default, not KeyError, and 'never attempted' is the correct reading of
    an absent value here."""
    from backend.database.repositories import session_repository as R

    item = R._to_item({"_id": "facebook:old", "platform": "facebook",
                       "session_id": "old"})
    assert item["relogin_attempts"] == 0
    assert item["relogin_last_attempt"] == 0.0
    assert item["relogin_last_success"] == 0.0
    assert item["relogin_total_successes"] == 0
    assert item["storage_state"] == {}


def test_credentials_never_leak_through_the_public_row():
    """_public is what the API returns. storage_state is a live session in
    a dict -- it belongs in the database and nowhere near a response."""
    row = {"id": "s1", "platform": "facebook", "identifier": "x",
           "status": "ready", "rate_limited_until": 0, "last_used": 0,
           "cookies": [], "username": "u", "password": "hunter2",
           "two_factor_secret": "JBSWY3DPEHPK3PXP",
           "storage_state": {"origins": [{"origin": "https://facebook.com"}]}}
    out = M._public(row)
    flat = repr(out)
    assert "hunter2" not in flat
    assert "JBSWY3DPEHPK3PXP" not in flat
    assert "storage_state" not in out


# ------------------------------------------- every refusal names itself


@pytest.mark.asyncio
@pytest.mark.parametrize("setup,expect", [
    # (what is wrong, a word that has to appear in the reason given)
    ("no_credentials", "username and password"),
    ("switched_off", "switched off"),
    ("no_such_row", "no such session"),
    ("unreadable_row", "could not read"),
])
async def test_every_way_of_not_re_logging_in_says_which_one_it_was(
        caplog, monkeypatch, setup, expect):
    """THE THING THAT MADE THIS UNDEBUGGABLE.

    maybe_auto_relogin had four silent `return False` paths, and between
    them they produced the most confusing thing this module can do: a
    session goes bad, the log says so, and then nothing at all follows.
    Not "tried and failed", not "refused" -- nothing. An operator reading
    that cannot tell a broken auto-login from one that was never attempted,
    and which of the two it is decides what to do next.

    A not-attempted must never be able to pass for an
    attempted-and-found-nothing.
    """
    item = _item()
    get_item = AsyncMock(return_value=item)
    if setup == "no_credentials":
        get_item = AsyncMock(return_value=_item(username="", password=""))
    elif setup == "switched_off":
        monkeypatch.setattr(settings, "session_auto_relogin", False)
    elif setup == "no_such_row":
        get_item = AsyncMock(return_value=None)
    elif setup == "unreadable_row":
        get_item = AsyncMock(side_effect=RuntimeError("mongo is down"))

    with caplog.at_level("INFO"), \
         patch.object(M.sessions_db, "get_item", get_item), \
         patch.object(M.sessions_db, "update_item", AsyncMock()):
        assert await M.maybe_auto_relogin("facebook", "s1") is False

    said = " ".join(r.getMessage() for r in caplog.records)
    assert "SESSION_RECOVERY" in said, "the refusal was silent"
    assert expect in said, f"the reason never said {expect!r}; it said: {said}"


@pytest.mark.asyncio
async def test_a_cookie_only_account_is_told_so_on_its_own_row():
    """Not only in the log. "No password stored" is not a transient
    refusal, it is a standing fact about the account, and it is fixable by
    the person looking at the Sessions panel -- which is where they are
    when it goes red."""
    update = AsyncMock()
    with patch.object(M.sessions_db, "get_item",
                      AsyncMock(return_value=_item(username="", password=""))), \
         patch.object(M.sessions_db, "update_item", update):
        assert await M.maybe_auto_relogin("facebook", "s1") is False

    update.assert_awaited_once()
    said = update.await_args.kwargs["last_error"]
    assert "no stored" in said and "username and password" in said


@pytest.mark.asyncio
async def test_a_started_re_login_is_held_so_it_cannot_be_collected(monkeypatch):
    """asyncio keeps only a WEAK reference to a running task. A bare
    create_task can be collected mid-flight, and then the re-login simply
    stops -- between "starting" and any outcome at all, with no error
    anywhere. That is indistinguishable from the browser hanging."""
    monkeypatch.setattr(settings, "session_relogin_cooldown_minutes", 0.0)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(*a, **kw):
        started.set()
        await release.wait()
        return LoginResult(cookies=list(FB_GOOD))

    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=_item())), \
         patch.object(M.sessions_db, "update_session_credentials", AsyncMock()), \
         patch.object(M, "_session_in_use", return_value=False), \
         patch("backend.stealth.auto_login.run_auto_login", _slow):
        assert await M.maybe_auto_relogin("facebook", "s1") is True
        await asyncio.wait_for(started.wait(), timeout=2)
        assert M._background, "the task is not held anywhere"
        release.set()
        await asyncio.sleep(0)
        # A GENEROUS BUDGET, because what is being asserted is "the task
        # is eventually discarded", not "it is discarded within a second".
        # 1s was enough on an idle machine and not on a busy one, which is
        # how a correct test becomes an intermittent failure that costs
        # twenty clean re-runs to not find.
        for _ in range(500):
            if not M._background:
                break
            await asyncio.sleep(0.02)
    assert not M._background, "finished tasks are never discarded"


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
async def test_a_login_that_lands_on_a_checkpoint_is_not_treated_as_recovered(
        _dont_write_to_mongo):
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
    assert _outcomes(_dont_write_to_mongo) == ["started", "failed"]


@pytest.mark.asyncio
async def test_a_failed_re_login_counts_and_records_why(_dont_write_to_mongo):
    update = AsyncMock()
    with patch("backend.stealth.auto_login.run_auto_login",
               AsyncMock(side_effect=TimeoutError("stopped at a CAPTCHA"))), \
         patch.object(M.sessions_db, "update_item", update):
        ok, detail = await M._perform_relogin("facebook", "s1", _item())

    assert ok is False
    assert "CAPTCHA" in detail
    assert _outcomes(_dont_write_to_mongo) == ["started", "failed"]
    # The reason goes on the row, not only into a log an operator will
    # never open.
    assert "re-login failed" in update.await_args.kwargs["last_error"]


@pytest.mark.asyncio
async def test_recovering_clears_the_attempt_counter(_dont_write_to_mongo):
    with patch("backend.stealth.auto_login.run_auto_login",
               AsyncMock(return_value=LoginResult(cookies=FB_GOOD))), \
         patch.object(M.sessions_db, "update_session_credentials", AsyncMock()):
        await M._perform_relogin("facebook", "s1", _item(relogin_attempts=2))
    assert _outcomes(_dont_write_to_mongo) == ["started", "ok"]


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
    row = _item(relogin_last_attempt=M._now(), relogin_attempts=9)

    with patch.object(M.sessions_db, "get_item", AsyncMock(return_value=row)), \
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


# ------------------------------------------------- platform coverage


# A cookie platform with no automated login is a real gap: when its session
# dies it stays dead until somebody notices and pastes a fresh export. That
# is sometimes the right call -- but it has to be a CALL, written down with
# its reason, not a platform that was never got round to.
#
# Anything on this list is a decision. Anything NOT on it, and not in
# _FLOWS, fails the test below.
KNOWN_UNAUTOMATED = {
    "tiktok": "region-blocked from the IP this tool runs on -- every "
              "tiktok.com URL redirects to /<region>/about with the June "
              "2020 India ban notice (confirmed live 2026-09-22), so there "
              "is no login page here to automate OR to test a flow against. "
              "Routing TikTok through a proxy is the prerequisite, and it is "
              "the same prerequisite discovery already has.",
}


def test_every_cookie_platform_either_self_heals_or_says_why_not():
    """THE GAP THIS EXISTS TO STOP BEING INVISIBLE.

    Three platforms have automated login and three do not, and from the
    outside those three blanks look identical -- but two of them (YouTube,
    Telegram) have no login page in the first place, and the third is a
    cookie platform that simply cannot heal itself. Those are completely
    different facts, and a table of ticks and blanks tells you neither.

    A platform added later is the case that actually matters: it would
    inherit the blank silently, and a pool that never heals looks exactly
    like a pool that never breaks until the day it does.
    """
    from backend.platforms.registry import PLATFORMS
    from backend.stealth.auto_login import _FLOWS

    unexplained = [
        pid for pid, plat in PLATFORMS.items()
        if plat.uses_cookies and pid not in _FLOWS and pid not in KNOWN_UNAUTOMATED
    ]
    assert not unexplained, (
        f"{unexplained} use pooled cookie sessions but have no automated "
        f"login and no stated reason. Either add a flow to _FLOWS and "
        f"LOGIN_FLOW, or add an entry to KNOWN_UNAUTOMATED saying why not."
    )


def test_platforms_with_no_login_page_are_not_counted_as_gaps():
    """YouTube takes an API key and Telegram takes API credentials. Neither
    has a login page, so neither is missing anything."""
    from backend.platforms.registry import PLATFORMS
    from backend.stealth.auto_login import _FLOWS

    for pid in ("youtube", "telegram"):
        plat = PLATFORMS[pid]
        assert not plat.uses_cookies
        assert pid not in _FLOWS
        assert plat.uses_api_key or plat.env_keys


def test_the_stated_reasons_stay_attached_to_real_platforms():
    """A reason for a platform that no longer exists is worse than no
    reason: it reads as covered."""
    from backend.platforms.registry import PLATFORMS

    for pid, why in KNOWN_UNAUTOMATED.items():
        assert pid in PLATFORMS, f"{pid} is explained but is not a platform"
        assert len(why) > 40, f"{pid}'s reason does not say anything"


def test_the_cookie_that_proves_a_login_worked_is_one_the_pool_requires():
    """run_auto_login waits for LOGIN_FLOW's proof cookie and then the
    caller checks the platform's required_cookies. If the proof cookie were
    not among the required ones, a login could 'succeed' on a cookie the
    pool does not even use, and then be rejected one line later."""
    from backend.platforms.registry import PLATFORMS

    for pid, (_url, proof) in M.LOGIN_FLOW.items():
        assert proof in PLATFORMS[pid].required_cookies, (
            f"{pid}: auto-login waits for {proof!r}, which is not in "
            f"required_cookies {PLATFORMS[pid].required_cookies}"
        )


def test_auto_login_is_never_offered_for_a_platform_with_no_flow():
    """LOGIN_FLOW drives the Sessions panel's "Re-Login Now" button and
    maybe_auto_relogin's first gate. A platform in one table and not the
    other is a button that cannot work, or a flow nothing can reach."""
    from backend.stealth.auto_login import _FLOWS

    assert set(_FLOWS) == set(M.LOGIN_FLOW), (
        f"_FLOWS has {sorted(_FLOWS)}, LOGIN_FLOW has {sorted(M.LOGIN_FLOW)}"
    )


# ------------------------------------------------- selector / driver wiring


def test_the_timeout_class_matches_the_driver_that_actually_runs():
    """THE BUG THIS EXISTS FOR, 2026-09-22.

    stealth/browser.py prefers patchright; auto_login.py caught
    playwright's TimeoutError. The two are unrelated classes, so every
    `except` in the login flow was dead code -- the first selector that
    timed out escaped instead of falling through to the next candidate, and
    a Facebook login died on `input#email` while the page was serving
    `input[name="email"]` the whole time.

    Nothing about that failure looked like a wiring problem: the message was
    a plain selector timeout, which reads as "the page changed". Asserting
    the two modules agree is the only cheap way to keep them agreeing.
    """
    from backend.stealth import browser
    from backend.stealth.auto_login import _TIMEOUTS

    driver = __import__(f"{browser.STEALTH_DRIVER}.async_api",
                        fromlist=["TimeoutError"]).TimeoutError
    assert driver in _TIMEOUTS, (
        f"auto_login catches {_TIMEOUTS}, but Session runs on "
        f"{browser.STEALTH_DRIVER}, which raises {driver}"
    )


def test_both_drivers_timeouts_are_caught_whichever_is_installed():
    from backend.stealth.auto_login import _TIMEOUTS

    assert len(_TIMEOUTS) >= 1
    assert all(issubclass(t, BaseException) for t in _TIMEOUTS)


class _FakePage:
    """A page that answers the two questions _first_present asks.

    `matches` maps a selector to the list of elements it resolves to, each
    one either usable (a person could click it) or not. That second state
    is the whole point: it is what an opacity-0, pointer-events-none field
    looks like to _USABLE_JS, and what Playwright's own visibility check
    cannot see.
    """

    def __init__(self, matches: dict[str, list[bool]], *, visible: bool = True):
        self.matches = matches
        self.visible = visible
        self.waits: list[tuple[str, int]] = []

    async def wait_for_selector(self, selector, *, state=None, timeout=None):
        self.waits.append((selector, timeout))
        if not self.visible:
            raise _timeout_cls()("timeout")
        return MagicMock()

    def locator(self, selector):
        elements = self.matches.get(selector, [])
        loc = MagicMock()
        loc.count = AsyncMock(return_value=len(elements))
        loc.nth = lambda i: MagicMock(
            evaluate=AsyncMock(return_value=elements[i]))
        return loc


def _timeout_cls():
    from backend.stealth.auto_login import _TIMEOUTS
    return _TIMEOUTS[0]


@pytest.mark.asyncio
async def test_a_dead_first_selector_falls_through_to_the_live_one():
    """The whole point of listing several candidates. Before the fix this
    raised on the first one instead."""
    from backend.stealth.auto_login import _first_present

    page = _FakePage({"input#email": [], 'input[name="email"]': [True]})
    got = await _first_present(page, ("input#email", 'input[name="email"]'))
    assert got == 'input[name="email"]'


@pytest.mark.asyncio
async def test_all_candidates_are_waited_for_together_not_one_at_a_time():
    """Waiting per candidate divides the budget, so a dead selector first
    means the real one may never be looked at on a slow render."""
    from backend.stealth.auto_login import _first_present

    page = _FakePage({"a": [True], "b": [True], "c": [True]})
    await _first_present(page, ("a", "b", "c"), timeout=9000)

    assert len(page.waits) == 1
    assert page.waits[0] == ("a, b, c", 9000)


@pytest.mark.asyncio
async def test_nothing_matching_returns_none_rather_than_raising():
    """The caller turns this into "the username field never appeared", which
    is an operator-readable reason. An escaping timeout is not."""
    from backend.stealth.auto_login import _first_present

    page = _FakePage({}, visible=False)
    assert await _first_present(page, ("a", "b")) is None


# ------------------------------------------------- the X opacity-0 trap


@pytest.mark.asyncio
async def test_a_field_playwright_calls_visible_but_nobody_can_click_is_not_present():
    """THE BUG THIS EXISTS FOR, MEASURED ON x.com 2026-09-22.

    X renders its password field on the FIRST login screen at opacity 0
    with pointer-events none, underneath the username box, before the
    Continue button that actually reveals the password step. Playwright
    calls that visible -- it has a box and it is not visibility:hidden --
    so _first_present used to report the password field as already present.

    Two things went wrong on the back of that one wrong answer, and both of
    them looked like something else: _login_twitter took its ONE-PAGE
    branch and never pressed Continue, and _human_type then clicked an
    element that can never receive a click, which Playwright waits a full
    thirty seconds to give up on. The failure that reached the operator was
    a bare timeout, which reads as "the page changed".
    """
    from backend.stealth.auto_login import _first_present

    page = _FakePage({'input[name="password"]': [False, False]})
    assert await _first_present(page, ('input[name="password"]',), timeout=1200) is None


@pytest.mark.asyncio
async def test_the_usable_copy_wins_when_the_form_is_rendered_twice():
    """X renders the whole login form twice and Instagram renders it once
    visibly and once hidden. `.first` is therefore not "the one on screen",
    it is a coin toss that happens to land right today -- so the index of
    the copy that IS usable travels with the selector."""
    from backend.stealth.auto_login import _first_present

    page = _FakePage({'input[name="pass"]': [False, True]})
    got = await _first_present(page, ('input[name="pass"]',))
    assert got == 'input[name="pass"] >> nth=1'


@pytest.mark.asyncio
async def test_a_field_that_becomes_usable_late_is_still_found():
    """The stricter test must not turn "renders half a second late" into an
    instant failure -- that would be the same bug in different clothes. The
    combined wait returns as soon as Playwright sees something; what is
    left of the budget goes on re-checking."""
    from backend.stealth.auto_login import _first_present

    page = _FakePage({'input[name="password"]': [False]})
    calls = {"n": 0}

    def locator(selector):
        calls["n"] += 1
        loc = MagicMock()
        loc.count = AsyncMock(return_value=1)
        # usable only from the third look onwards
        loc.nth = lambda i: MagicMock(
            evaluate=AsyncMock(return_value=calls["n"] >= 3))
        return loc

    page.locator = locator
    got = await _first_present(page, ('input[name="password"]',), timeout=4000)
    assert got == 'input[name="password"]'
    assert calls["n"] >= 3


@pytest.mark.asyncio
async def test_typing_never_waits_the_full_default_click_timeout():
    """Playwright's default click timeout is 30 seconds. The thing that
    goes wrong here is exactly a click that can never land, so the default
    turns one bad field into a login that hangs."""
    from backend.stealth.auto_login import _human_type

    clicked: dict = {}
    target = MagicMock()
    target.wait_for = AsyncMock()

    async def _click(**kw):
        clicked.update(kw)

    target.click = _click
    page = MagicMock()
    page.locator = lambda sel: MagicMock(first=target)
    page.keyboard.press = AsyncMock()
    page.keyboard.type = AsyncMock()

    await _human_type(page, 'input[name="pass"]', "x")

    assert clicked.get("timeout"), "click() was left on the 30-second default"
    assert clicked["timeout"] <= 10000


def test_x_is_submitted_by_its_own_button_before_enter_is_tried():
    """MEASURED 2026-09-22. A form submits implicitly on Enter only when it
    has a submit button, or at most one text-ish field. X's login form has
    two (the username and the hidden password), and its Continue control is
    a plain handler-less div until the username field has content -- so
    Enter on its own does nothing at all, silently, and the old selectors
    ('Log in', role=button) match nothing on that page.
    """
    from backend.stealth.auto_login import _X_CONTINUE

    assert 'button[type="submit"]:has-text("Continue")' in _X_CONTINUE
    # [type="submit"] is load-bearing: "Continue with phone" and "Continue
    # with Apple" are on the same screen and :has-text matches substrings.
    assert all("type=" in s or "data-testid" in s or "role=" in s
               for s in _X_CONTINUE)


@pytest.mark.parametrize("platform,field,expected", [
    ("facebook", "user", 'input[name="email"]'),
    ("facebook", "password", 'input[name="pass"]'),
    # Meta serves ONE login form for both products now. This was the
    # selector the first version of the table broke by "hardening" it into
    # input[name="username"], which has never existed on that page.
    ("instagram", "user", 'input[name="email"]'),
    ("instagram", "password", 'input[name="pass"]'),
])
def test_the_measured_selector_is_the_first_candidate(platform, field, expected):
    """Measured against the live pages 2026-09-22. Order still matters for
    speed even though it is no longer a correctness issue."""
    from backend.stealth.auto_login import _FLOWS

    assert _FLOWS[platform][field][0] == expected


def test_no_id_selectors_survive_on_metas_login_form():
    """Meta randomises element ids per render (measured:
    `_R_c9l6neappb6amH1_`), so an id selector can match only by accident."""
    from backend.stealth.auto_login import _FLOWS

    first_choices = [_FLOWS[p][f][0] for p in ("facebook", "instagram")
                     for f in ("user", "password")]
    assert not [s for s in first_choices if s.startswith("#") or "input#" in s]
