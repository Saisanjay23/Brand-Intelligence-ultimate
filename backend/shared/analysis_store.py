"""In-memory store for analysis results. RAM only -- nothing here is ever
written to MongoDB.

THE STORAGE SPLIT THIS ENFORCES
    discovery -> MongoDB  (database/repositories/profile_repository.py,
                           phase=PHASE_DISCOVERY, DISCOVERY_FIELDS)
    analysis  -> here     (this module, process memory, never persisted)

Discovery and analysis are independent passes by explicit design: analysis
reads nothing discovery produced (see any platform's
`analysis_engine.py::Scraper.process`, which re-derives every field from
its own visit), and its results never touch the profile document discovery
owns. The two can run in either order, on different schedules, without
sharing state in either direction.

WHAT "MEMORY ONLY" COSTS, stated plainly so nobody is surprised by it:
every scored row, risk score and evidence screenshot in this store is gone
on process restart, and gone again when its TTL lapses or the store evicts
it under pressure. Analysis output cannot be queried after the fact,
exported later, or published to a client from here -- whatever consumes a
result has to read it out of this store while the process that produced it
is still alive.

ROBUSTNESS -- what this guards against, and why each guard exists:

  * UNBOUNDED GROWTH. A long-running process analysing continuously would
    otherwise hold every row it ever scored. Two independent ceilings:
    `max_entries` and `max_bytes`.

  * SCREENSHOTS, specifically. `Row.screenshot_bytes` is a full PNG --
    hundreds of KB to low MB each. Entry COUNT alone is a useless ceiling
    when one entry can be 2 MB and another 400 bytes, so this store
    budgets real bytes (`_sizeof`) and evicts against that too. This is
    the single most likely way an in-memory store of this shape runs a box
    out of RAM.

  * STALE READS. Entries expire (`ttl_seconds`); an expired entry is never
    returned, even before the sweeper reaches it.

  * CONCURRENT MUTATION. Every read and write holds one `asyncio.Lock`.
    Nothing here acquires a second lock while holding it, so it cannot
    deadlock against itself.

  * ALIASING. Rows are copied on the way in and on the way out, so a
    caller that keeps mutating the Row object it just stored (every
    platform engine mutates rows field-by-field as it scrapes) cannot
    retroactively change what the store holds, and a caller that mutates
    what it reads back cannot corrupt the store.

  * SILENT MISSES. `get`/`find` return None/[] for absent or expired
    entries rather than raising, so a consumer that lost a race with the
    TTL degrades instead of crashing.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from backend.shared.logging import get_logger
from backend.shared.models.row import Row

log = get_logger("shared.analysis_store")

# Defaults sized for a single-worker process doing analyst-driven runs.
# An entry is one scored profile; the byte ceiling is what actually binds
# in practice once evidence capture is on (see the module docstring).
DEFAULT_MAX_ENTRIES = 5_000
DEFAULT_TTL_SECONDS = 6 * 3600
DEFAULT_MAX_BYTES = 512 * 1024 * 1024  # 512 MB

# Rough per-entry overhead for the Row's own strings/dataclass machinery,
# counted on top of the screenshot bytes so the byte budget is not wildly
# optimistic for rows that carry no screenshot at all.
_ROW_OVERHEAD_BYTES = 2_048


def _key(client_id: str, platform: str, url: str) -> str:
    """One stable id per analysed profile, so re-analysing the same URL
    REPLACES its result instead of accumulating a second copy of it.

    Hashed rather than concatenated: a raw `client|platform|url` key is
    unbounded in length (URLs get long) and would be held twice, once as
    the dict key and once inside the entry.
    """
    raw = f"{client_id}\x00{platform}\x00{url}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def _sizeof(row: Row) -> int:
    """Approximate bytes this row costs in memory. Exact accounting is not
    the point -- the screenshot dominates by orders of magnitude, and
    getting THAT into the budget is what stops the store from filling a
    box while reporting a comfortable entry count."""
    n = _ROW_OVERHEAD_BYTES
    if row.screenshot_bytes:
        n += len(row.screenshot_bytes)
    for text in (row.bio, row.notes, row.profile_pic_url, row.url):
        if text:
            n += len(text)
    return n


@dataclass
class StoredAnalysis:
    """One analysis result, plus the bookkeeping the store needs to age
    and evict it. `row` is the scored `Row` exactly as the platform's
    analysis engine produced it."""

    key: str
    client_id: str
    platform: str
    url: str
    row: Row
    stored_at: float = field(default_factory=time.time)
    size_bytes: int = 0

    def age_seconds(self) -> float:
        return time.time() - self.stored_at

    def is_expired(self, ttl_seconds: float) -> bool:
        return ttl_seconds > 0 and self.age_seconds() >= ttl_seconds


class AnalysisStore:
    """Process-wide, in-memory, bounded, TTL'd store of analysis results.

    Not a singleton by construction -- `analysis_store` at the bottom of
    this module is the shared instance every caller should use, but the
    class stays independently constructible so a test (or a second,
    differently-bounded store) never has to reach around a global.
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes
        # insertion-ordered: Python dicts preserve it, which is what makes
        # "evict oldest" a cheap iteration rather than a sort
        self._items: dict[str, StoredAnalysis] = {}
        self._bytes = 0
        self._lock = asyncio.Lock()
        # cumulative counters, never reset -- an operator reading stats()
        # needs to see that eviction has been happening, not just the
        # current occupancy that eviction is keeping healthy
        self._evicted_expired = 0
        self._evicted_pressure = 0
        self._stored_total = 0

    # ---------------------------------------------------------------- write

    async def put(self, client_id: str, platform: str, row: Row) -> str:
        """Store one scored row; returns its key. Replaces any existing
        result for the same (client_id, platform, url)."""
        async with self._lock:
            return self._put_locked(client_id, platform, row)

    async def put_many(self, client_id: str, platform: str, rows: Iterable[Row]) -> list[str]:
        """Store a whole batch under one lock acquisition rather than one
        per row -- an analysis run finishing 200 profiles should not
        contend 200 times."""
        async with self._lock:
            return [self._put_locked(client_id, platform, r) for r in rows]

    def _put_locked(self, client_id: str, platform: str, row: Row) -> str:
        url = (row.url or "").strip()
        key = _key(client_id, platform, url)
        # Copy on the way in: every platform engine mutates its Row
        # field-by-field as it scrapes, and some reuse a row object across
        # fallback tiers. Without this, storing a row and then continuing
        # to touch it would silently rewrite what was stored.
        stored_row = copy.copy(row)
        stored_row.src = dict(row.src)

        if (old := self._items.pop(key, None)) is not None:
            self._bytes -= old.size_bytes

        entry = StoredAnalysis(
            key=key, client_id=client_id, platform=platform, url=url,
            row=stored_row, size_bytes=_sizeof(row),
        )
        self._items[key] = entry
        self._bytes += entry.size_bytes
        self._stored_total += 1
        self._evict_locked()
        return key

    # ----------------------------------------------------------------- read

    async def get(self, key: str) -> Optional[StoredAnalysis]:
        """One result by key, or None if it is absent or expired."""
        async with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if entry.is_expired(self.ttl_seconds):
                # drop it now rather than hand back a stale read and wait
                # for the next put() to notice
                self._drop_locked(key)
                self._evicted_expired += 1
                return None
            return self._copy_out(entry)

    async def get_row(self, client_id: str, platform: str, url: str) -> Optional[Row]:
        """The scored Row for one analysed profile, addressed the way a
        caller actually has it (client + platform + url) rather than by an
        opaque key."""
        entry = await self.get(_key(client_id, platform, url))
        return entry.row if entry else None

    async def find(
        self, client_id: str = "", platform: str = "", *, limit: int = 0,
    ) -> list[StoredAnalysis]:
        """Every live result, newest first, optionally scoped to one client
        and/or platform. Expired entries are skipped (and reaped)."""
        async with self._lock:
            self._reap_expired_locked()
            out = [
                self._copy_out(e) for e in self._items.values()
                if (not client_id or e.client_id == client_id)
                and (not platform or e.platform == platform)
            ]
        out.sort(key=lambda e: e.stored_at, reverse=True)
        return out[:limit] if limit else out

    @staticmethod
    def _copy_out(entry: StoredAnalysis) -> StoredAnalysis:
        """A caller that mutates what it read must not be able to change
        what the store holds -- the mirror of the copy in `_put_locked`."""
        row = copy.copy(entry.row)
        row.src = dict(entry.row.src)
        return StoredAnalysis(
            key=entry.key, client_id=entry.client_id, platform=entry.platform,
            url=entry.url, row=row, stored_at=entry.stored_at,
            size_bytes=entry.size_bytes,
        )

    # --------------------------------------------------------------- delete

    async def delete(self, key: str) -> bool:
        async with self._lock:
            return self._drop_locked(key)

    async def clear(self, client_id: str = "", platform: str = "") -> int:
        """Drop everything, or everything for one client/platform. Returns
        how many entries went."""
        async with self._lock:
            if not client_id and not platform:
                n = len(self._items)
                self._items.clear()
                self._bytes = 0
                return n
            doomed = [
                k for k, e in self._items.items()
                if (not client_id or e.client_id == client_id)
                and (not platform or e.platform == platform)
            ]
            for k in doomed:
                self._drop_locked(k)
            return len(doomed)

    def _drop_locked(self, key: str) -> bool:
        entry = self._items.pop(key, None)
        if entry is None:
            return False
        self._bytes -= entry.size_bytes
        return True

    # ------------------------------------------------------------- eviction

    def _reap_expired_locked(self) -> int:
        if self.ttl_seconds <= 0:
            return 0
        doomed = [k for k, e in self._items.items() if e.is_expired(self.ttl_seconds)]
        for k in doomed:
            self._drop_locked(k)
        self._evicted_expired += len(doomed)
        return len(doomed)

    def _evict_locked(self) -> None:
        """Expired entries first (they are free to lose), then oldest-first
        under either ceiling. Insertion order IS age order here, so the
        oldest live entry is simply the first key still in the dict."""
        self._reap_expired_locked()
        pressured = 0
        while self._items and (
            len(self._items) > self.max_entries or self._bytes > self.max_bytes
        ):
            oldest = next(iter(self._items))
            self._drop_locked(oldest)
            pressured += 1
        if pressured:
            self._evicted_pressure += pressured
            log.warning(
                f"analysis store over budget -- evicted {pressured} oldest result(s) "
                f"(now {len(self._items)}/{self.max_entries} entries, "
                f"{self._bytes / 1024 / 1024:.1f}/{self.max_bytes / 1024 / 1024:.0f} MB). "
                "Analysis results are memory-only and are lost when evicted."
            )

    # ---------------------------------------------------------- observability

    async def stats(self) -> dict:
        """What an operator needs to answer "is this store about to start
        losing results, and has it already?"."""
        async with self._lock:
            by_platform: dict[str, int] = {}
            for e in self._items.values():
                by_platform[e.platform] = by_platform.get(e.platform, 0) + 1
            return {
                "entries": len(self._items),
                "max_entries": self.max_entries,
                "bytes": self._bytes,
                "max_bytes": self.max_bytes,
                "mb": round(self._bytes / 1024 / 1024, 2),
                "ttl_seconds": self.ttl_seconds,
                "by_platform": by_platform,
                "stored_total": self._stored_total,
                "evicted_expired": self._evicted_expired,
                "evicted_pressure": self._evicted_pressure,
            }


# The shared instance. Analysis results go here and nowhere else; see this
# module's docstring for the discovery-to-Mongo / analysis-to-memory split
# this is one half of.
analysis_store = AnalysisStore()
