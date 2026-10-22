"""Did the platform push back? -- and what do we do about it.

Three small, platform-neutral pieces that share one vocabulary:

1. `classify_response`   one HTTP status + body -> a `Signal`
2. `Governor`            slows a platform down when it pushes back, recovers
                         on its own, and costs NOTHING while the platform is
                         happy
3. circuit breaker       (inside `Governor`) stops a platform altogether when
                         it keeps pushing back, instead of replaying the same
                         keyword onto the next account behind the same IP

WHY THIS EXISTS. The engines already know HOW to read a platform. What they
did not have was one honest answer to "is this empty result a real empty
result, or did we get throttled?". Facebook never looked at a response
status at all, so a rate-limit body read as "zero new results, scroll again"
until the sweep ended `stalled` -- a code that carries no session verdict, so
nothing cooled down and the next request went out at full speed. Instagram's
HTTP 400 classified as nothing. Those are the silent failures this tool is
built to refuse (see the project's "a clean zero is the dangerous outcome").

THE RULE THAT SHAPES THIS FILE: `UNKNOWN` IS NEVER `OK`. A response we could
not read is not evidence of health. `UNKNOWN` does not trip the breaker (a
parser that cannot read a body must not be able to switch a platform off),
but it is never counted as a success either, so it cannot quietly earn back
speed the governor took away.

WHAT THE TEXT TOKENS ARE, HONESTLY. The status-code mapping is standard HTTP.
The body phrases are what Meta and X have been observed to say; none of them
is a documented contract. They are heuristics, kept in plain tuples at the
top so a phrasing that changes is a one-line edit, and the live probe is
where a new one gets confirmed. Where a body is unreadable the answer is
`UNKNOWN`, not a guess.

Pure data and a clock: no asyncio primitives, no I/O. The clock is injectable
so every behaviour here is tested without sleeping.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional

# ------------------------------------------------------------------ vocabulary

OK = "ok"
THROTTLED = "throttled"      # slow down; the account is fine
CHALLENGED = "challenged"    # a human must act (checkpoint / identity check)
EXPIRED = "expired"          # the login is gone
UNKNOWN = "unknown"          # could not tell -- NEVER treated as healthy

PUSHBACK_KINDS = frozenset({THROTTLED, CHALLENGED, EXPIRED})

# The `Sweep.stopped` code an engine reports for each kind of pushback. These
# are exactly the codes `shared/resilience.classify_failure` already turns
# into a session verdict (rate_limited / checkpointed / expired), so the
# account is cooled down and the keyword is handed to another account.
STOP_CODE = {
    THROTTLED: "rate_limited",
    CHALLENGED: "checkpoint",
    EXPIRED: "login",
}


@dataclass(frozen=True)
class Signal:
    kind: str
    reason: str = ""

    @property
    def is_pushback(self) -> bool:
        return self.kind in PUSHBACK_KINDS

    @property
    def is_ok(self) -> bool:
        return self.kind == OK


# Case-insensitive substrings, most specific first inside each tuple.
_CHALLENGE_TOKENS = (
    "challenge_required", "checkpoint_required", "checkpoint required",
    "confirm your identity", "confirm it's you", "confirm it’s you",
    "suspicious activity", "account has been locked", "your account is locked",
    "/account/access", "arkose", "captcha",
)
_THROTTLE_TOKENS = (
    "feedback_required", "rate limit", "rate-limit", "rate_limited",
    "too many requests", "please wait a few minutes", "try again later",
    "temporarily blocked", "you're temporarily blocked", "we limit how often",
    "going too fast", "you can't use this feature right now",
    "overcapacity", "over capacity",
)
_EXPIRED_TOKENS = (
    "login_required", "not authenticated", "could not authenticate",
    "please log in", "log in to continue", "session expired",
)
# X's numeric error codes, matched only in a JSON `"code": N` position so an
# id that merely contains the digits can never match.
#   88  rate limit exceeded      326 account locked (challenge)
#   32  could not authenticate   64  account suspended
_X_CODE_RE = re.compile(r'"code"\s*:\s*(88|326|32|64)\b')
_X_CODE_KIND = {"88": THROTTLED, "326": CHALLENGED, "32": EXPIRED, "64": EXPIRED}


def _first(text: str, tokens) -> Optional[str]:
    for tok in tokens:
        if tok in text:
            return tok
    return None


def classify_response(platform: str, status: Optional[int], body: Optional[str] = None) -> Signal:
    """One response -> `Signal`.

    `status` may be None (a response whose status the caller could not read);
    `body` may be None or non-text. Both missing -> UNKNOWN. Order matters:
    a body that names a challenge beats the bare status, because a 400 or a
    403 carrying `challenge_required` is a challenge, not an expiry.
    """
    text = (body or "").lower() if isinstance(body, str) else ""

    if hit := _first(text, _CHALLENGE_TOKENS):
        return Signal(CHALLENGED, f"body says {hit!r}")

    m = _X_CODE_RE.search(text)
    if m:
        return Signal(_X_CODE_KIND[m.group(1)], f"x error code {m.group(1)}")

    if status == 429:
        return Signal(THROTTLED, "HTTP 429")
    if hit := _first(text, _THROTTLE_TOKENS):
        return Signal(THROTTLED, f"body says {hit!r}")
    if hit := _first(text, _EXPIRED_TOKENS):
        return Signal(EXPIRED, f"body says {hit!r}")

    if status in (401, 403):
        return Signal(EXPIRED, f"HTTP {status}")
    if status is None:
        return Signal(UNKNOWN, "no status and nothing readable in the body")
    # A 400 with no recognisable body is Instagram's wrong-pairing answer
    # more often than it is a malformed request from us, but we cannot prove
    # which. Surface it; do not call it healthy and do not call it a block.
    if status == 400:
        return Signal(UNKNOWN, "HTTP 400 with no recognisable body")
    if 500 <= status < 600:
        return Signal(UNKNOWN, f"HTTP {status} (server side)")
    if 200 <= status < 300 or status in (301, 302, 303, 307, 308, 404, 410):
        return Signal(OK, f"HTTP {status}")
    return Signal(UNKNOWN, f"HTTP {status}")


# -------------------------------------------------------------------- governor

@dataclass
class GovernorConfig:
    # Each pushback multiplies the gap, up to this ceiling.
    gap_step: float = 2.0
    gap_ceiling: float = 8.0
    # The extra gap halves every this many seconds of quiet.
    half_life_s: float = 600.0
    # Below this multiplier the platform is treated as healthy: full speed,
    # full parallelism.
    healthy_below: float = 1.25
    # Breaker: this many THROTTLED/CHALLENGED signals inside `window_s` opens
    # it, and it stays open for `open_s`.
    breaker_threshold: int = 3
    window_s: float = 900.0
    open_s: float = 1200.0


class Governor:
    """Per-platform pacing governor + circuit breaker.

    Shared across every account on a platform on purpose: today they all
    leave from one IP, so one account being throttled is evidence about all
    of them. A healthy platform sees `gap_multiplier() == 1.0` and
    `tab_cap(n) == n` -- the governor is invisible until something pushes back.
    """

    def __init__(self, platform: str, cfg: Optional[GovernorConfig] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.platform = platform
        self.cfg = cfg or GovernorConfig()
        self._clock = clock
        self._excess = 0.0           # multiplier above 1.0 at `_excess_at`
        self._excess_at = clock()
        self._events: Deque[float] = deque()   # THROTTLED/CHALLENGED timestamps
        self._open_until = 0.0
        self._open_reason = ""
        # rolling counters for the health signal (never reset by recovery)
        self.total = 0
        self.pushbacks = 0
        self.unknowns = 0
        self.last_signal: Optional[Signal] = None
        self.last_pushback_at: Optional[float] = None

    # -- internals
    def _decayed_excess(self, now: float) -> float:
        elapsed = max(0.0, now - self._excess_at)
        return self._excess * (0.5 ** (elapsed / self.cfg.half_life_s))

    def _trim(self, now: float) -> None:
        cutoff = now - self.cfg.window_s
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    # -- recording
    def record(self, signal: Signal) -> Signal:
        """Feed one classified response. Returns it unchanged for chaining."""
        now = self._clock()
        self.total += 1
        self.last_signal = signal
        if signal.kind == UNKNOWN:
            self.unknowns += 1          # never recovers speed, never trips
            return signal
        if signal.kind == OK:
            return signal               # recovery is by the clock, not by OKs
        self.pushbacks += 1
        self.last_pushback_at = now
        step = self.cfg.gap_step
        excess = self._decayed_excess(now)
        new_mult = min(self.cfg.gap_ceiling, (1.0 + excess) * step)
        self._excess, self._excess_at = new_mult - 1.0, now
        if signal.kind in (THROTTLED, CHALLENGED):
            self._events.append(now)
            self._trim(now)
            if len(self._events) >= self.cfg.breaker_threshold and now >= self._open_until:
                self._open_until = now + self.cfg.open_s
                self._open_reason = (
                    f"{len(self._events)} pushback signals in "
                    f"{int(self.cfg.window_s // 60)} min (latest: {signal.reason or signal.kind})"
                )
        return signal

    # -- reading
    def gap_multiplier(self) -> float:
        return 1.0 + self._decayed_excess(self._clock())

    def healthy(self) -> bool:
        return self.gap_multiplier() < self.cfg.healthy_below

    def tab_cap(self, configured: int) -> int:
        """Parallel tabs allowed right now: all of them while healthy, then
        halved per doubling of the gap, never below one."""
        configured = max(1, int(configured))
        mult = self.gap_multiplier()
        if mult < self.cfg.healthy_below:
            return configured
        cap = configured
        m = mult
        while m >= 2.0 and cap > 1:
            cap = max(1, cap // 2)
            m /= 2.0
        return max(1, min(configured, cap))

    def is_open(self) -> bool:
        return self._clock() < self._open_until

    def resume_in_s(self) -> float:
        return max(0.0, self._open_until - self._clock())

    def open_reason(self) -> str:
        return self._open_reason if self.is_open() else ""

    def snapshot(self) -> dict:
        """What the health view and the logs show."""
        return {
            "platform": self.platform,
            "gap_multiplier": round(self.gap_multiplier(), 2),
            "breaker_open": self.is_open(),
            "resume_in_s": round(self.resume_in_s()),
            "reason": self.open_reason(),
            "requests": self.total,
            "pushbacks": self.pushbacks,
            "unknowns": self.unknowns,
            "pushback_per_100": round(100.0 * self.pushbacks / self.total, 1) if self.total else 0.0,
            "last_signal": (self.last_signal.kind if self.last_signal else None),
        }


# ------------------------------------------------------------------- registry

_governors: Dict[str, Governor] = {}


def governor_for(platform: str) -> Governor:
    """The one governor for a platform, created on first use."""
    key = (platform or "").strip().lower() or "_default"
    gov = _governors.get(key)
    if gov is None:
        gov = _governors[key] = Governor(key)
    return gov


def all_snapshots() -> list[dict]:
    return [g.snapshot() for g in _governors.values()]


def reset_all() -> None:
    """Forget every governor (tests, and a manual 'clear the breaker')."""
    _governors.clear()


def note_response(platform: str, status: Optional[int], body: Optional[str] = None) -> Signal:
    """Classify one response and feed the platform's governor in one call.
    The shape an engine's response handler wants."""
    return governor_for(platform).record(classify_response(platform, status, body))
