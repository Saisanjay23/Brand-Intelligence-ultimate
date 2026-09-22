"""Shared long-run reliability primitives for discovery/analysis.

Nothing here is platform-specific and nothing here replaces the existing
extraction-fallback-chain / parser-drift-canary machinery in
`shared/extraction.py`, which already correctly answers "did this sweep's
PARSER break". This module answers a different question: "was this failure
worth burning the session over, and should the next attempt wait before
trying again". Three things live here:

1. `classify_failure`, one place that turns a raised exception (or a
   `Sweep.stopped` code) into the SAME vocabulary `sessions/manager.py`'s
   `mark_session_failed` already expects ("checkpointed" / "rate_limited" /
   "expired" / None for "not a session problem"). Discovery and analysis
   used to each hand-roll their own `"rate limit" in str(e).lower()` checks
   inline; collecting the token lists here means a new platform's error
   phrasing only needs to be taught once.

2. `retry_async`, a small bounded exponential-backoff-with-jitter retry
   for a single transient operation (a network call, a page navigation).
   Deliberately NOT a decorator/framework: platform engines call it inline
   around exactly the one operation that's flaky, so a caller always reads
   as plain sequential code with an explicit retry budget, not hidden
   control flow.

`retry_async`'s budget is deliberately bounded, not endless -- a caller that
wants to keep going indefinitely (a long-running scheduler, say) is
responsible for its own outer loop and its own catch-and-log-rather-than-die
policy; this module only ever retries one operation a bounded number of
times. What actually breaks "runs forever" in practice is a session going
bad and nothing ever noticing, so the same broken account gets handed out
again next cycle, that's what `classify_failure` closes off.

3. `sweep_outcome` / `describe_stop` / `summarise_stops`, which turn a
`Sweep`'s `(stopped, complete)` pair into what an analyst is actually owed:
whether the sweep got what was asked of it, merely ran out of budget, or
broke -- and, for the last two, WHICH of those in words. See the block
above `sweep_outcome` for why one boolean was not enough.
"""

from __future__ import annotations

import re
from typing import Optional

# Substrings looked for case-insensitively in an exception's str() (or a
# Sweep's `stopped` code, which already uses short tokens like "403"/
# "checkpoint"), ordered most-specific-first since the first match wins.
# Kept as plain substrings, not regex: every existing ad-hoc check in this
# codebase (analysis_service.py's inline `"rate limit" in err_str`, etc.)
# already used this style, and every platform's own error text was written
# to match it.
_CHECKPOINT_TOKENS = ("checkpoint", "challenge", "verify your", "suspicious login")
_AUTH_TOKENS = ("login", "not authenticated", "credential", "api key")
_RATE_LIMIT_TOKENS = ("rate limit", "rate-limit", "too many requests", "floodwait", "quota")
_TRANSIENT_TOKENS = (
    "timeout", "timed out", "navigation failed", "econnreset", "econnrefused",
    "connection reset", "connection aborted", "net::err", "temporarily unavailable",
)

# HTTP STATUS CODES ARE MATCHED AS NUMBERS, NOT AS SUBSTRINGS.
#
# These used to sit in the plain token tuples above, so "401" and "403" were
# looked for with `in`. That matches any text that happens to contain those
# three digits anywhere -- and the text this runs over is error messages,
# which routinely carry entity ids:
#
#     "could not resolve entity 100403123456789"   -> classified "expired"
#     "profile 401234567890123 has no name"        -> classified "expired"
#
# Both are ordinary extraction failures on a perfectly good session, and
# both took that account OUT OF THE POOL with `mark_session_failed(...,
# "expired")` -- then told whoever looked that its cookies had expired, so
# the fix they would reach for (paste a fresh export) was for a problem that
# never existed. A Facebook id is 15-16 digits, so roughly one in eighty
# contains "403" by chance.
#
# The lookarounds are the whole fix: a status code is a number on its own,
# never a run of digits inside a longer one. "http-403", "HTTP 401
# Unauthorized" and "youtube search 403: ..." all still match, because what
# surrounds them is not a digit.
#
# This is the same class of mistake `_URL_RE` below already guards against
# (a URL carrying "login" in its path), applied to the numbers.
_AUTH_STATUS_RE = re.compile(r"(?<!\d)(401|403)(?!\d)")
_RATE_LIMIT_STATUS_RE = re.compile(r"(?<!\d)429(?!\d)")
_TRANSIENT_STATUS_RE = re.compile(r"(?<!\d)(502|503|504)(?!\d)")


# A URL IS NOT EVIDENCE. Every token below is matched as a plain substring
# against the whole error text, and a browser's error text routinely quotes
# the URL it was working on: Playwright's timeouts read `Timeout 30000ms
# exceeded. navigating to "https://www.facebook.com/login/?next=..."`. Left
# in, that URL alone matched `login` and turned a NETWORK TIMEOUT into a
# verdict of "expired" -- quarantining a perfectly good account into the
# graduated 15m/1h/6h/24h cooldown, after which the pool reported itself
# expired on credentials nothing was ever wrong with. Facebook redirecting
# an unauthenticated request to `/login` is a real signal, but it reaches
# us as the platform's own text ("not authenticated", "checkpoint"), never
# as a bare URL -- and the same stripping incidentally protects the numeric
# tokens, which used to be able to match an id inside a query string.
#
# Only the address is dropped, never the message around it, so an error
# that says BOTH what went wrong and where still classifies on what went
# wrong.
_URL_RE = re.compile(r"https?://\S+")


def _evidence(err: BaseException | str) -> str:
    """The part of an error that is actually a claim about the session:
    lowercased, with any URL it happens to quote removed."""
    return _URL_RE.sub(" ", str(err)).lower()


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
    text = _evidence(err)
    if any(tok in text for tok in _CHECKPOINT_TOKENS):
        return "checkpointed"
    if any(tok in text for tok in _AUTH_TOKENS) or _AUTH_STATUS_RE.search(text):
        return "expired"
    if (any(tok in text for tok in _RATE_LIMIT_TOKENS)
            or _RATE_LIMIT_STATUS_RE.search(text)):
        return "rate_limited"
    return None


def is_transient(err: BaseException | str) -> bool:
    """True for a failure worth a same-session immediate retry (a network
    blip, a slow page) as opposed to one that needs a different session
    (see `classify_failure`) or isn't network-shaped at all (a parser bug
    retrying would just hit again identically)."""
    text = str(err).lower()
    return (any(tok in text for tok in _TRANSIENT_TOKENS)
            or bool(_TRANSIENT_STATUS_RE.search(text)))


# ------------------------------------------------------- sweep outcomes

# WHAT A SWEEP'S ENDING MEANS, AS OPPOSED TO WHERE IT ENDED.
#
# Every discovery engine sets `Sweep.complete = True` on exactly one thing:
# it paged or scrolled until the platform ran out of results. That is a
# true and useful engine-level fact and it is left exactly as it is. What
# it is NOT is an answer to "should the analyst act on this" -- and it was
# being read as one. `discovery/runner.py` counted every `complete is
# False` sweep into a single `incomplete` tally and reported the lot as
# "N sweep(s) did not run to completion", with no reason attached.
#
# Most of those were `cap:results`: a sweep that stopped because it had
# collected exactly the number of profiles the analyst configured. A cap
# doing its job is not an incomplete sweep, and reporting it as one had
# two costs -- it trained analysts to scroll past the one warning that
# also carries real breakage (a stall, a geoblock, a dead parser), and it
# left every capped platform permanently `partial`, which the scheduler
# reads as "still owing" forever.
#
# So a stop code resolves to one of three outcomes instead:
#
#   satisfied -- we have what was asked for. Either the platform ran out
#                of results, or our own result cap was met. Nothing to say.
#   truncated -- a budget that is not about results ended it early. More
#                results exist; we chose not to spend the time on them.
#                Worth stating, not worth alarm.
#   broken    -- something went wrong that a human should look at.
#
# Anything unrecognised is BROKEN on purpose: a stop code this module has
# never been taught is either a new failure mode or a new engine, and both
# are better surfaced loudly once than swallowed silently forever.

SATISFIED = "satisfied"
TRUNCATED = "truncated"
BROKEN = "broken"

# `no-results` is here rather than in broken because a keyword nobody on
# the platform matches is a real, common, correct answer -- see
# instagram/discovery_engine.py's own note on not routing it through the
# extraction-fallback chain.
_SATISFIED_STOPS = frozenset({
    "exhausted", "end-of-serp", "no-results", "cap:results",
})

# `cancelled` sits here, not in broken: the analyst pressing Stop is a
# budget decision like any other, and the platform-level cancel branch in
# discovery/runner.py reports the cancel itself separately anyway.
_TRUNCATED_STOPS = frozenset({
    "cap:seconds", "cap:pages", "cancelled",
})


def sweep_outcome(stopped: str, complete: bool = False) -> str:
    """A `Sweep`'s (stopped, complete) pair -> SATISFIED | TRUNCATED |
    BROKEN.

    `complete` wins when it is set, because only the engine can know it
    reached the true end of a result set; the stop code decides everything
    else. Both are read rather than just the code so that an engine which
    sets `complete` without a code (or with one this module has not been
    taught) still reports cleanly.
    """
    if complete:
        return SATISFIED
    code = (stopped or "").strip().lower()
    if code in _SATISFIED_STOPS:
        return SATISFIED
    if code in _TRUNCATED_STOPS:
        return TRUNCATED
    return BROKEN


# Plain English for each stop code, written to read as the predicate of
# "N sweep(s) ___". Anything missing falls back to the raw code, which is
# still infinitely more use than the bare count this replaces.
_STOP_PHRASES = {
    "cap:seconds": "ran out of time budget",
    "cap:pages": "hit the page limit",
    "cancelled": "were cancelled",
    "stalled": "stalled with no new results",
    "error": "errored",
    "quota": "exhausted the platform's API quota",
    "checkpoint": "hit a login checkpoint",
    "checkpointed": "hit a login checkpoint",
    "geoblocked": "were geoblocked for this IP",
    "rate_limited": "were rate limited",
    "flood-wait": "were put on a flood wait",
    "session-failed": "lost every session that attempted them",
    # Instagram's private mobile API failed and the web endpoint stood in.
    # Results were still recovered, so this is not a total loss -- but a
    # fallback that fires on every sweep means the primary path is dead,
    # which is exactly the thing worth telling somebody about.
    "mobile-api-failed-web-recovered": "fell back to the web API",
}

_HTTP_RE = re.compile(r"^http-(\d{3})$")


def describe_stop(stopped: str) -> str:
    """One stop code -> the phrase an analyst reads."""
    code = (stopped or "").strip().lower()
    if not code:
        return "stopped for an unrecorded reason"
    if phrase := _STOP_PHRASES.get(code):
        return phrase
    if m := _HTTP_RE.match(code):
        return f"got HTTP {m.group(1)}"
    return f"stopped on {code!r}"


def summarise_stops(counts: dict[str, int]) -> str:
    """Stop code -> count, as the one sentence a platform's note carries.

    One reason reads as a plain statement ("3 sweep(s) ran out of time
    budget"); several are listed after a total, commonest first, so the
    dominant cause is the first thing read. Deterministic on ties (by
    code) because this string is asserted on in tests and diffed by eye
    across runs.
    """
    live = {code: n for code, n in counts.items() if n > 0}
    if not live:
        return ""
    total = sum(live.values())
    ranked = sorted(live.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) == 1:
        code, n = ranked[0]
        return f"{n} sweep(s) {describe_stop(code)}"
    detail = ", ".join(f"{n} {describe_stop(code)}" for code, n in ranked)
    return f"{total} sweep(s) stopped early -- {detail}"
