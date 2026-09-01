"""Shared long-run reliability primitives for discovery/analysis.

Nothing here is platform-specific and nothing here replaces the existing
extraction-fallback-chain / parser-drift-canary machinery in
`shared/extraction.py` and `services/discovery_service.py`; that already
correctly answers "did this sweep's PARSER break". This module answers a
different question: "was this failure worth burning the session over, and
should the next attempt wait before trying again". Two things live here:

1. `classify_failure`, one place that turns a raised exception (or a
   `Sweep.stopped` code) into the SAME vocabulary `sessions/manager.py`'s
   `mark_session_failed` already expects ("checkpointed" / "rate_limited" /
   "expired" / None for "not a session problem"). Both discovery and
   analysis used to each hand-roll their own `"rate limit" in str(e).lower()`
   checks inline (analysis_service.py still does, for the per-URL case);
   collecting the token lists here means a new platform's error phrasing
   only needs to be taught once.

2. `retry_async`, a small bounded exponential-backoff-with-jitter retry
   for a single transient operation (a network call, a page navigation).
   Deliberately NOT a decorator/framework: platform engines call it inline
   around exactly the one operation that's flaky, so a caller always reads
   as plain sequential code with an explicit retry budget, not hidden
   control flow.

Endless-running is a property of the CALLERS (round_robin_service's workers
loop forever by design, catching and logging rather than dying, see its
own module docstring), not of retrying harder here. What actually breaks
"runs forever" in practice is a session going bad and nothing ever noticing,
so the same broken account gets handed out again next cycle, that's what
`classify_failure` closes off.
"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")

# Substrings looked for case-insensitively in an exception's str() (or a
# Sweep's `stopped` code, which already uses short tokens like "403"/
# "checkpoint"), ordered most-specific-first since the first match wins.
# Kept as plain substrings, not regex: every existing ad-hoc check in this
# codebase (analysis_service.py's inline `"rate limit" in err_str`, etc.)
# already used this style, and every platform's own error text was written
# to match it.
_CHECKPOINT_TOKENS = ("checkpoint", "challenge", "verify your", "suspicious login")
_AUTH_TOKENS = ("401", "403", "login", "not authenticated", "credential", "api key")
_RATE_LIMIT_TOKENS = ("429", "rate limit", "rate-limit", "too many requests", "floodwait", "quota")
_TRANSIENT_TOKENS = (
    "timeout", "timed out", "navigation failed", "econnreset", "econnrefused",
    "connection reset", "connection aborted", "net::err", "temporarily unavailable",
    "503", "502", "504",
)


def classify_failure(err: BaseException | str) -> Optional[str]:
    """A caught exception (or a `Sweep.stopped` string) -> the reason string
    `sessions.manager.mark_session_failed` wants, or None when this isn't
    session-shaped at all (a bug, a bad URL, anything a fresh session
    wouldn't fix) and the pool should be left alone.

    Checkpoint/challenge beats plain auth beats rate-limit on purpose: a
    checkpointed account IS also unauthenticated from the platform's point
    of view, but "checkpointed" is the more actionable signal (paste fresh
    cookies) versus the generic "expired" a bare 401 gets.
    """
    text = str(err).lower()
    if any(tok in text for tok in _CHECKPOINT_TOKENS):
        return "checkpointed"
    if any(tok in text for tok in _AUTH_TOKENS):
        return "expired"
    if any(tok in text for tok in _RATE_LIMIT_TOKENS):
        return "rate_limited"
    return None


def is_transient(err: BaseException | str) -> bool:
    """True for a failure worth a same-session immediate retry (a network
    blip, a slow page) as opposed to one that needs a different session
    (see `classify_failure`) or isn't network-shaped at all (a parser bug
    retrying would just hit again identically)."""
    text = str(err).lower()
    return any(tok in text for tok in _TRANSIENT_TOKENS)


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.5,
    max_delay: float = 20.0,
    retryable: Callable[[BaseException], bool] = is_transient,
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> T:
    """Run `fn()`, retrying up to `attempts` total tries on a `retryable`
    exception with exponential backoff + full jitter. Re-raises the last
    exception once attempts are exhausted, or immediately on a non-retryable
    one; this never swallows a real failure, it only spends a bounded
    amount of time confirming a blip wasn't just a blip.

    `on_retry(attempt, exc)` is a sync hook for the caller to log with its
    own context (keyword, url, platform); this module has none of that.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_exc = e
            if attempt >= attempts or not retryable(e):
                raise
            if on_retry:
                on_retry(attempt, e)
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            await asyncio.sleep(random.uniform(0, delay))
    raise last_exc  # pragma: no cover, loop always returns or raises above
