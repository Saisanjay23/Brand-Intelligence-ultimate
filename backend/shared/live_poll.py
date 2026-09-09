"""Long-polling for the two job-status endpoints: a push, without a socket.

WHAT WAS SLOW, AND WHY IT WASN'T THE BACKEND. Discovery writes its profiles
to MongoDB per completed sweep, so a hit is readable within seconds of being
found. The UI then took up to two more seconds to notice, because the only
thing that told it anything had happened was a fixed 2s timer on
`GET /discovery/jobs/{id}`. Rows sat finished, saved and invisible for
longer than they took to save.

Polling faster is the obvious fix and the wrong one: the answer is almost
always "nothing changed", so most of the extra requests buy nothing, and the
ones that do still arrive up to a full interval late. The interval is a
floor on latency AND a multiplier on load, and lowering it trades one
against the other.

So the wait moves to the server. A client sends the revision it already has
and how long it is willing to wait; this holds the request until the job
actually changes, then answers immediately. Latency stops being a function
of the poll interval and becomes a function of `_TICK_S` -- a tenth of a
second -- while an idle job costs ONE request per `wait` window instead of
one every two seconds. Both numbers move the right way at once.

Deliberately not a WebSocket or SSE. This is the same request/response the
frontend already speaks, with the same error handling, the same proxy
behaviour and no connection lifecycle to manage; the only new idea is that
an answer may take a moment to arrive.

WHY A FINGERPRINT AND NOT THE PAYLOAD. Change is detected by comparing a
cheap tuple of the fields that represent progress, not by re-rendering the
snapshot. An analysis job's snapshot contains every analysed profile, and
building plus hashing that ten times a second -- to discover nothing had
changed -- would cost more than the polling this replaces. The fingerprint
reads a handful of attributes.

The cost of that choice is bounded and self-correcting: if something changes
that the fingerprint does not cover, the wait simply runs its course and the
client gets the full, current state on the next request. A field nobody
tracks is at worst `wait` seconds stale, never wrong.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Callable, Optional

# How often the held request looks for a change. This IS the delivery
# latency, so it is small; it is also a no-op attribute read against an
# in-memory object, so ten a second costs nothing worth measuring.
_TICK_S = 0.1

# The longest a request may be held. Kept well under the 30-60s idle
# timeouts that browsers, proxies and load balancers apply to a request
# that has sent no bytes -- a wait that gets killed by an intermediary
# looks to the client like a network error, which is a worse experience
# than the polling this replaces. The client simply asks again.
MAX_WAIT_S = 25.0


def revision(fingerprint: Any) -> str:
    """A short, stable id for one state of a job.

    Opaque on purpose: the client only ever echoes it back, so nothing
    outside this module should read meaning into it, and the shape of a
    fingerprint stays free to change without becoming an API contract.
    """
    return hashlib.sha1(repr(fingerprint).encode("utf-8", "replace")).hexdigest()[:16]


async def wait_for_change(
    fingerprint: Callable[[], Any],
    *,
    rev: str = "",
    wait_s: float = 0.0,
    is_final: Optional[Callable[[], bool]] = None,
) -> str:
    """Hold until the job's fingerprint stops matching `rev`, then return
    the revision it has now.

    Returns immediately -- so this stays exactly the old endpoint -- when
    `wait_s` is 0, when the caller sent no `rev` (its first request, which
    has nothing to wait for), or when what it holds is already out of date.

    `is_final` short-circuits a finished job: its state cannot change
    again, so holding the request open would waste the full window on every
    poll a client makes before it notices the job has ended.
    """
    current = revision(fingerprint())
    if wait_s <= 0 or not rev or rev != current:
        return current
    if is_final is not None and is_final():
        return current

    deadline = time.monotonic() + min(wait_s, MAX_WAIT_S)
    while time.monotonic() < deadline:
        await asyncio.sleep(_TICK_S)
        current = revision(fingerprint())
        if current != rev:
            return current
        if is_final is not None and is_final():
            return current
    return current
