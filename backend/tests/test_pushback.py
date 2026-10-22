"""Does the tool notice when a platform pushes back -- and only then slow down?

The failures this file exists to catch are all silent ones:

  * Facebook never read a response status, so a rate-limit body read as
    "zero new results" and the sweep scrolled into it until it stalled.
  * Instagram's HTTP 400 classified as nothing at all.
  * An unreadable response must never count as healthy, or it would quietly
    hand back speed the governor had taken away.
  * A healthy platform must pay NOTHING for the governor existing -- the
    user's requirement is "keep today's speed".
"""

from __future__ import annotations

import pytest

from backend.shared import pushback as pb
from backend.shared.pushback import (
    CHALLENGED, EXPIRED, OK, THROTTLED, UNKNOWN,
    Governor, GovernorConfig, Signal, classify_response,
)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


# ------------------------------------------------------------ classification

@pytest.mark.parametrize("status,body,kind", [
    (200, '{"data":{}}', OK),
    (404, "", OK),                       # a dead profile is an answer, not a block
    (429, "", THROTTLED),
    (401, "", EXPIRED),
    (403, "", EXPIRED),
    (200, '{"status":"fail","message":"Please wait a few minutes before you try again."}', THROTTLED),
    (400, '{"message":"challenge_required"}', CHALLENGED),     # body beats bare status
    (403, '{"message":"checkpoint_required"}', CHALLENGED),    # ... not "expired"
    (200, '{"message":"login_required"}', EXPIRED),
    (200, '{"errors":[{"code":88,"message":"Rate limit exceeded"}]}', THROTTLED),
    (200, '{"errors":[{"code":326}]}', CHALLENGED),
    (200, '{"errors":[{"message":"You\'re temporarily blocked"}]}', THROTTLED),
])
def test_known_shapes(status, body, kind):
    assert classify_response("any", status, body).kind == kind


def test_unreadable_is_unknown_never_ok():
    assert classify_response("instagram", None, None).kind == UNKNOWN
    # IG's wrong-pairing answer: surfaced, neither healthy nor a block
    assert classify_response("instagram", 400, "{}").kind == UNKNOWN
    assert classify_response("facebook", 502, "").kind == UNKNOWN
    assert not classify_response("facebook", 502, "").is_ok


def test_status_digits_inside_an_id_never_match():
    """The same class of bug resilience.py already guards: a 15-digit
    Facebook id that happens to contain 429 or 403 is not a status."""
    body = '{"id":"100429123456789","code":1000403}'
    assert classify_response("facebook", 200, body).kind == OK


def test_x_code_must_be_a_json_code_not_any_digits():
    assert classify_response("x", 200, '{"count":88}').kind == OK
    assert classify_response("x", 200, '{"code": 88}').kind == THROTTLED


def test_non_string_body_is_tolerated():
    assert classify_response("x", 200, b"bytes").kind == OK   # no crash, no claim


# ------------------------------------------------------------------ governor

def test_healthy_platform_pays_nothing():
    g = Governor("facebook", clock=Clock())
    for _ in range(50):
        g.record(Signal(OK))
    assert g.gap_multiplier() == 1.0
    assert g.tab_cap(3) == 3
    assert g.healthy() and not g.is_open()


def test_pushback_slows_down_and_cuts_tabs():
    c = Clock()
    g = Governor("facebook", clock=c)
    g.record(Signal(THROTTLED, "429"))
    assert g.gap_multiplier() == pytest.approx(2.0)
    assert g.tab_cap(4) == 2
    g.record(Signal(THROTTLED, "429"))
    assert g.gap_multiplier() == pytest.approx(4.0)
    assert g.tab_cap(4) == 1
    assert g.tab_cap(1) == 1                       # never below one


def test_multiplier_is_capped():
    g = Governor("instagram", GovernorConfig(gap_ceiling=8.0, breaker_threshold=99), Clock())
    for _ in range(20):
        g.record(Signal(THROTTLED))
    assert g.gap_multiplier() == pytest.approx(8.0)


def test_recovers_on_its_own_with_quiet_time():
    c = Clock()
    g = Governor("x", GovernorConfig(half_life_s=600), c)
    g.record(Signal(THROTTLED))
    assert g.gap_multiplier() == pytest.approx(2.0)
    c.advance(600)
    assert g.gap_multiplier() == pytest.approx(1.5)
    c.advance(6000)
    assert g.healthy()
    assert g.tab_cap(3) == 3


def test_ok_responses_do_not_speed_things_back_up():
    """Recovery is by the clock. A run of lucky 200s inside a throttle window
    is not evidence the window is over."""
    g = Governor("x", clock=Clock())
    g.record(Signal(THROTTLED))
    before = g.gap_multiplier()
    for _ in range(100):
        g.record(Signal(OK))
    assert g.gap_multiplier() == pytest.approx(before)


def test_unknown_never_slows_never_recovers_never_trips():
    c = Clock()
    g = Governor("facebook", GovernorConfig(breaker_threshold=2), c)
    for _ in range(50):
        g.record(Signal(UNKNOWN, "parser could not read"))
    assert g.gap_multiplier() == 1.0
    assert not g.is_open()
    assert g.unknowns == 50 and g.pushbacks == 0
    # ... and it is counted, so it can be seen
    assert g.snapshot()["unknowns"] == 50


# ------------------------------------------------------------------- breaker

def test_breaker_opens_after_repeated_pushback_then_closes():
    c = Clock()
    g = Governor("instagram", GovernorConfig(breaker_threshold=3, window_s=900, open_s=1200), c)
    g.record(Signal(THROTTLED)); c.advance(60)
    g.record(Signal(CHALLENGED)); c.advance(60)
    assert not g.is_open()
    g.record(Signal(THROTTLED, "429"))
    assert g.is_open()
    assert g.resume_in_s() == pytest.approx(1200)
    assert "3 pushback signals" in g.open_reason()
    c.advance(1201)
    assert not g.is_open()


def test_old_pushback_ages_out_of_the_window():
    c = Clock()
    g = Governor("facebook", GovernorConfig(breaker_threshold=3, window_s=900), c)
    g.record(Signal(THROTTLED)); g.record(Signal(THROTTLED))
    c.advance(1000)                                  # both are now outside the window
    g.record(Signal(THROTTLED))
    assert not g.is_open()


def test_expired_slows_but_does_not_count_toward_the_breaker():
    """An expired login is one account's problem, not evidence the platform
    is throttling the IP -- it must not switch the whole platform off."""
    g = Governor("x", GovernorConfig(breaker_threshold=2), Clock())
    for _ in range(5):
        g.record(Signal(EXPIRED))
    assert not g.is_open()


# ------------------------------------------------------------------ registry

def test_registry_returns_one_governor_per_platform():
    pb.reset_all()
    assert pb.governor_for("Facebook") is pb.governor_for("facebook")
    assert pb.governor_for("facebook") is not pb.governor_for("instagram")


def test_note_response_classifies_and_records():
    pb.reset_all()
    sig = pb.note_response("facebook", 429, "")
    assert sig.kind == THROTTLED
    assert pb.governor_for("facebook").pushbacks == 1
    snaps = {s["platform"]: s for s in pb.all_snapshots()}
    assert snaps["facebook"]["pushback_per_100"] == 100.0
    pb.reset_all()
