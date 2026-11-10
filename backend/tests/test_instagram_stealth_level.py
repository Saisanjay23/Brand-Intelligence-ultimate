"""Instagram stealth level: "high" is a coherent posture, "standard" is exactly
what the tool did before levels existed, and neither drops anything collected.

What these guard, in order of how quietly each would fail:

  * "standard" must reproduce the old numbers -- otherwise the switch that is
    supposed to restore today's behaviour restores something else.
  * a level may only LOWER parallelism, never raise it, and must not touch
    any other platform.
  * the duplicate home-page load is gone for discovery but NOT for analysis.
  * a challenge reaches the runner as an account verdict, a throttle does
    not, and several silent empty profiles in a row are noticed.
"""

from __future__ import annotations

import statistics
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.analysis import runner as AR
from backend.discovery import runner as DR
from backend.platforms.instagram import analysis_engine as IA
from backend.platforms.instagram import discovery_engine as ID
from backend.platforms.scan_options import DiscoveryOptions, ScanOptions
from backend.shared import pushback as pb
from backend.shared.pushback import CHALLENGED, EXPIRED, THROTTLED, Signal
from backend.stealth import browser as B
from backend.stealth import levels as L


@pytest.fixture
def level(monkeypatch):
    def _set(name):
        monkeypatch.setattr(L.settings, "instagram_stealth", name)
    return _set


@pytest.fixture(autouse=True)
def _fresh_governors():
    pb.reset_all()
    yield
    pb.reset_all()


# ------------------------------------------------------------------ the table

def test_standard_reproduces_the_numbers_from_before_levels(level):
    level("standard")
    assert L.value("instagram", "keyword_gap", None) == DR._PLATFORM_INTER_KEYWORD_DELAY["instagram"]
    assert L.value("instagram", "batch_gap", None) == AR._PLATFORM_INTER_BATCH_DELAY["instagram"]
    # Not the old fixed 2.5s any more: raised to reading pace on purpose.
    assert L.value("instagram", "page_gap", None) == 4.5
    assert L.session_cap("instagram", 3) == 3                     # no cap of its own


def test_high_is_slower_everywhere_and_one_account(level):
    level("high")
    std = {"keyword_gap": 8.0, "batch_gap": 3.5, "page_gap": 4.5}
    for name, old in std.items():
        assert L.value("instagram", name, None) > old, name
    assert L.session_cap("instagram", 3) == 1


def test_a_level_can_only_lower_parallelism(level):
    level("high")
    assert L.session_cap("instagram", 1) == 1
    assert L.session_cap("instagram", 5) == 1
    level("standard")
    assert L.session_cap("instagram", 1) == 1                    # never raised to more


def test_other_platforms_are_untouched_at_every_level(level):
    for name in ("standard", "high"):
        level(name)
        for plat in ("facebook", "twitter", "tiktok", "youtube", "telegram"):
            assert L.value(plat, "keyword_gap", 12.0) == 12.0
            assert L.session_cap(plat, 3) == 3
            assert L.level_for(plat) == L.STANDARD


def test_a_typo_in_the_setting_is_rejected_not_ignored():
    from pydantic import ValidationError
    from backend.config.settings import Settings
    with pytest.raises(ValidationError):
        Settings(instagram_stealth="hihg")


# ---------------------------------------------------------------- the runners

def test_discovery_claims_one_instagram_account_at_high_but_not_at_standard(level):
    level("high")
    assert DR._sessions_wanted("instagram", 10) == 1
    level("standard")
    assert DR._sessions_wanted("instagram", 10) > 1


def test_analysis_claims_one_instagram_account_at_high_but_not_at_standard(level):
    level("high")
    assert AR._sessions_wanted("instagram", 30, 1) == 1
    level("standard")
    assert AR._sessions_wanted("instagram", 30, 1) > 1


def test_high_never_changes_facebook_or_twitter_parallelism(level):
    """Compared ACROSS levels: the same call at "high" and at "standard"
    must agree, or the Instagram switch is leaking into other platforms."""
    seen = {}
    for name in ("high", "standard"):
        level(name)
        seen[name] = (
            DR._sessions_wanted("facebook", 10),
            AR._sessions_wanted("twitter", 30, 3),
            AR._sessions_wanted("facebook", 30, 3),
        )
    assert seen["high"] == seen["standard"]
    assert all(n > 1 for n in seen["high"])        # and they are genuinely parallel


# ---------------------------------------------------------------- the jitter

def test_jittered_gaps_are_not_a_fixed_number():
    draws = [L.jittered(6.0) for _ in range(400)]
    assert len(set(round(d, 3) for d in draws)) > 300           # genuinely varied
    assert min(draws) >= 3.0                                     # never below half the median
    assert 4.5 < statistics.median(draws) < 7.5                  # centred on it
    assert max(draws) > 8.0                                      # with an occasional long one


def test_a_zero_median_means_no_wait():
    assert L.jittered(0) == 0.0


@pytest.mark.asyncio
async def test_page_gap_is_cut_short_by_a_stop(level, monkeypatch):
    import time
    level("high")                                                # ~6s median
    options = SimpleNamespace(cancel=True)                       # already stopped
    t0 = time.monotonic()
    await ID._page_gap(options)
    assert time.monotonic() - t0 < 0.5


# -------------------------------------------------- one home load, not two

def _session(options):
    s = SimpleNamespace(
        o=options, platform="instagram", session_id="warmup-test", WARMUP_BY_ENGINE=True)
    page = MagicMock()
    page.goto = AsyncMock(side_effect=RuntimeError("stop here"))   # skip the dwell
    page.close = AsyncMock()
    s.ctx = MagicMock()
    s.ctx.new_page = AsyncMock(return_value=page)
    return s


@pytest.mark.asyncio
async def test_discovery_skips_the_generic_warmup_for_instagram(monkeypatch):
    monkeypatch.setattr(B, "_last_warm", {})
    s = _session(DiscoveryOptions())
    await B.Session._warmup(s)
    s.ctx.new_page.assert_not_called()


@pytest.mark.asyncio
async def test_analysis_still_warms_up(monkeypatch):
    monkeypatch.setattr(B, "_last_warm", {})
    s = _session(ScanOptions())
    await B.Session._warmup(s)
    s.ctx.new_page.assert_awaited_once()


def test_only_the_instagram_session_opts_out():
    assert ID.InstagramSession.WARMUP_BY_ENGINE is True
    assert getattr(B.Session, "WARMUP_BY_ENGINE", False) is False


# ------------------------------------------------------- reading the answer

def test_a_challenge_is_an_account_verdict():
    assert IA.pushback_verdict(Signal(CHALLENGED, "challenge_required")) == (
        "CHECKPOINT", "instagram challenge (challenge_required)")


def test_a_rejected_login_is_an_account_verdict():
    status, _ = IA.pushback_verdict(Signal(EXPIRED, "HTTP 401"))
    assert status == "LOGIN_REQUIRED"


def test_a_throttle_is_not_an_account_verdict():
    """It slows the platform down; it must not quarantine a healthy account
    and hand its queue to the next one from the same IP."""
    assert IA.pushback_verdict(Signal(THROTTLED, "HTTP 429")) is None


def test_two_empty_profiles_do_not_trigger_anything():
    state = SimpleNamespace()
    assert IA.register_no_payload(state, False) == ""
    assert IA.register_no_payload(state, False) == ""
    assert pb.governor_for("instagram").pushbacks == 0


def test_three_empty_profiles_in_a_row_slow_the_platform():
    state = SimpleNamespace()
    notes = [IA.register_no_payload(state, False) for _ in range(3)]
    assert notes[:2] == ["", ""]
    assert "possible soft block" in notes[2]
    gov = pb.governor_for("instagram")
    assert gov.pushbacks == 1
    assert gov.gap_multiplier() > 1.0


def test_an_explicit_pushback_is_not_counted_twice():
    """The response handler already told the governor. Counting the empty
    profile again would open the breaker on one bad moment."""
    state = SimpleNamespace(_no_payload_streak=10)
    assert IA.register_no_payload(state, True) == ""
    assert pb.governor_for("instagram").pushbacks == 0
