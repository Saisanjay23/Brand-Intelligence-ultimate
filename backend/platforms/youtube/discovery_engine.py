"""YouTube discovery engine: search, pagination, and channel extraction,
keywords in, candidate channels out, via the official Data API v3.

Also owns the API client (`YouTubeAPI`) and the default-picture check: both
are produced here first and re-used by analysis_engine.py, which imports
them rather than redefining them, so there is exactly one definition of each
across the two files.

YouTube publishes an official API that returns exactly what the report needs,
so this platform uses no browser at all: nothing to fingerprint, nothing to
detect, and no session to burn. It is the fastest and safest of the six.

QUOTA is the real constraint, not rate limiting. Default allowance is 10,000
units/day:
    search.list        100 units   -- expensive, used once per keyword page
    channels.list        1 unit    -- cheap, batched 50 ids at a time
    playlistItems.list   1 unit    -- cheap, how last-upload is read
So discovery costs ~100 units per 50 results, and analysis is ~2 units per
channel. Reading the newest upload through playlistItems instead of a dated
search is a 100x saving, which is why it is done that way. No browser, so no
session, no pacing and no detection surface, a sweep stops on an explicit
end of results, a cap, or quota exhaustion, and says which.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.shared.logging import get_logger
from backend.shared.models.hit import Hit, hit_to_row
from backend.shared.models.row import Row

log = get_logger("youtube.api")

BASE = "https://www.googleapis.com/youtube/v3"
CHANNEL_URL = "https://www.youtube.com/channel/{cid}"
# YouTube's stock avatars come from this host; a real upload does not
RE_DEFAULT_PIC = re.compile(r"/(default|no_avatar|blank)", re.I)


class QuotaExceeded(RuntimeError):
    """The daily allowance is gone, retrying today will not help."""


class YouTubeAPI:
    """WHAT: the thin Data API v3 client both engines share. HOW: plain
    urllib in a worker thread -- no browser, no session, no dependency
    beyond the stdlib -- with HTTP error bodies inspected so the two
    failures that mean different things stay apart: an exhausted daily
    QUOTA (normal, self-healing at midnight PT) versus a rejected KEY
    (needs a human). LINKED TO: defined here because discovery needs it
    first; analysis_engine.py imports this class rather than defining a
    second one."""

    def __init__(self, key: str = ""):
        """Takes the key explicitly or from YOUTUBE_API_KEY. Raises
        immediately when neither is set: failing at construction is far
        easier to diagnose than every call failing with a 403 that looks
        like a quota problem."""
        self.key = key or os.environ.get("YOUTUBE_API_KEY", "")
        if not self.key:
            raise RuntimeError("YOUTUBE_API_KEY is not set")

    def _get_sync(self, endpoint: str, params: dict) -> dict:
        """WHAT: one blocking GET against the API. HOW: reads the error
        BODY, not just the status code, because YouTube returns 403 for
        both "quota gone" and "key invalid" and only the body says which.
        Getting that wrong is what used to quarantine a perfectly good key
        every day the quota ran out -- see the check_session docstring in
        analysis_engine.py. LINKED TO: wrapped by get() below; raises
        QuotaExceeded, which sweep() turns into a clean stop rather than
        an error."""
        q = urllib.parse.urlencode({**params, "key": self.key}, doseq=True)
        url = f"{BASE}/{endpoint}?{q}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            body_lower = body.lower()
            if e.code in (403, 429) and ("quota" in body_lower or "quotaexceeded" in body_lower):
                raise QuotaExceeded("YouTube daily quota exhausted") from e
            if e.code == 403 and any(tok in body_lower for tok in ("key", "api key", "invalid", "badrequest")):
                raise RuntimeError(f"youtube {endpoint} 403: API key invalid - {body[:200]}") from e
            raise RuntimeError(f"youtube {endpoint} {e.code}: {body[:200]}") from e

    async def get(self, endpoint: str, **params) -> dict:
        """WHAT: the async face of _get_sync. HOW: urllib in a thread --
        one dependency fewer than an async HTTP client, identical
        behaviour, and the call is I/O-bound so the thread costs nothing.
        LINKED TO: every read method below goes through this."""
        return await asyncio.to_thread(self._get_sync, endpoint, params)

    # ---------- reads ----------

    async def search_channels(
        self, keyword: str, page_token: str = "", per_page: int = 50
    ) -> tuple[list[dict], str]:
        """WHAT: one page of channel search results -> (items,
        next_page_token). The expensive call: 100 quota units, against a
        daily allowance of 10,000.

        HOW: up to 3 attempts with increasing backoff. Two distinct
        transients are handled, and the second is the subtle one: the API
        sometimes returns HTTP 200 with an EMPTY items list on the first
        page of a keyword that really does have results. That is
        indistinguishable from "no such channel" to any caller, so a sweep
        would report a clean zero-hit result and nothing would look wrong.
        It is only retried when there is no page_token -- mid-pagination an
        empty page is a genuine end-of-results, and retrying it would loop.

        QuotaExceeded is re-raised immediately rather than retried: the
        allowance does not come back within a backoff window, and three
        more attempts would only cost time. LINKED TO: driven by the
        pagination loop in Discovery.sweep()."""
        params: dict[str, Any] = {
            "part": "snippet",
            "type": "channel",
            "q": keyword,
            "maxResults": min(per_page, 50),
        }
        if page_token:
            params["pageToken"] = page_token
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                data = await self.get("search", **params)
                items = data.get("items", [])
                token = data.get("nextPageToken", "")
                # YouTube occasionally returns an empty items list on
                # transient hiccups even with a 200 status. Retry once
                # if this is the first page (no page_token) and we got
                # nothing back.
                if not items and not page_token and attempt < 2:
                    log.info(
                        f"youtube search {keyword!r}: empty response on "
                        f"attempt {attempt + 1}, retrying"
                    )
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                return items, token
            except QuotaExceeded:
                raise
            except Exception as e:
                last_exc = e
                log.info(
                    f"youtube search {keyword!r}: {type(e).__name__} on "
                    f"attempt {attempt + 1}, retrying"
                )
                await asyncio.sleep(1.5 * (attempt + 1))
        raise last_exc or RuntimeError("youtube search_channels failed after retries")

    async def channels(self, ids: list[str]) -> list[dict]:
        """WHAT: full detail for a list of channel ids. HOW: batched 50 at
        a time, because channels.list costs ONE unit per call regardless
        of how many ids it carries -- batching is a 50x quota saving over
        per-channel lookups. LINKED TO: analysis_engine.py process() for
        an id-shaped URL, and the verification step in
        channel_by_handle()."""
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            data = await self.get(
                "channels",
                part="snippet,statistics,contentDetails,brandingSettings",
                id=",".join(ids[i : i + 50]),
                maxResults=50,
            )
            out += data.get("items", [])
        return out

    async def channel_by_handle(self, handle: str) -> Optional[dict]:
        """WHAT: resolve a vanity reference (@handle, legacy /c/ or /user/)
        to a channel, or None.

        HOW: three exact lookups first -- forHandle with and without the
        @, then the legacy forUsername -- since each covers a different
        generation of YouTube URL and all three are exact. Only if every
        one misses does it fall back to SEARCH, and a search result is
        accepted only when the channel own customUrl or title equals what
        was asked for.

        That verification is the whole point: search happily returns a
        similarly-named channel for a handle that does not exist, and this
        answer becomes an impersonation report about a named account.
        Reporting the wrong channel is worse than reporting nothing, so an
        unverifiable match returns None. LINKED TO: analysis_engine.py
        process() calls this for every non-id URL shape (see channel_ref
        there)."""
        want = handle.lstrip("@").strip().lower()
        if not want:
            return None

        for params in (
            {"forHandle": f"@{want}"},
            {"forHandle": want},
            {"forUsername": want},
        ):
            try:
                data = await self.get(
                    "channels",
                    part="snippet,statistics,contentDetails,brandingSettings",
                    **params,
                )
                if items := data.get("items"):
                    return items[0]
            except (QuotaExceeded, RuntimeError):
                continue

        items, _ = await self.search_channels(handle, per_page=5)
        for it in items:
            cid = (it.get("id") or {}).get("channelId", "")
            if not cid:
                continue
            found = await self.channels([cid])
            if not found:
                continue
            snip = found[0].get("snippet") or {}
            branding = (found[0].get("brandingSettings") or {}).get("channel") or {}
            identifiers = {
                str(snip.get("customUrl") or "").lstrip("@").lower(),
                str(snip.get("title") or "").lower(),
                str(snip.get("channelTitle") or "").lower(),
                str(branding.get("title") or "").lower(),
            }
            if want in identifiers:
                return found[0]
        log.info(f"no channel exactly matches handle {handle!r}")
        return None

    async def latest_upload(self, uploads_playlist: str) -> str:
        """WHAT: ISO date of the newest upload, or "" when there is none.
        HOW: reads one item from the channel uploads playlist -- 1 quota
        unit, against 100 for the dated search that would otherwise be
        needed. That 100x saving is why activity is read this way. LINKED
        TO: analysis_engine.py process() sets last_post_iso from this.

        THE 404 THAT IS NOT AN ERROR
        `channels()` always synthesizes an "uploads" playlist id for a
        channel (the `UC` -> `UU` prefix swap) even when that channel has
        never actually uploaded anything, the playlist is never
        materialized server-side in that case, and `playlistItems` 404s on
        it. Confirmed live: a channel with `statistics.videoCount == "0"`
        404s here every time. That is a normal, expected shape (a channel
        genuinely has zero videos), not a real error, analysis_engine.py's
        `process()` already has an `elif videoCount == "0"` fallback for
        exactly this case, but it never got a chance to run before this was
        fixed, because the 404 propagated as an exception straight past it
        and `one()`'s broad except handler discarded the whole `Row`,
        including the profile name/subscriber count `fill()` had already
        successfully populated moments earlier.
        """
        if not uploads_playlist:
            return ""
        try:
            data = await self.get(
                "playlistItems", part="snippet", playlistId=uploads_playlist, maxResults=1
            )
        except RuntimeError as e:
            if "404" in str(e):
                return ""
            raise
        items = data.get("items") or []
        if not items:
            return ""
        published = (items[0].get("snippet") or {}).get("publishedAt", "")
        return published[:10]


# Crawling / pagination


@dataclass
class Sweep:
    """WHAT: the result of sweeping one keyword -- the hits, plus WHY the
    sweep ended. HOW: `stopped` carries the reason as a short tag
    (cap:results, cap:seconds, exhausted, quota, error) and `complete` is
    True only for `exhausted`, so a caller can tell "there was no more to
    find" apart from "we stopped early". Returning a short list without
    that distinction would make a quota failure look like a clean result.
    LINKED TO: services/discovery_service.py reads these fields to decide
    whether a keyword still has pages left."""

    keyword: str
    tab: str = "channels"
    hits: list[Row] = field(default_factory=list)
    pages: int = 0
    stopped: str = ""
    complete: bool = False
    seconds: float = 0.0
    error: str = ""

    def summary(self) -> str:
        """One-line log form: how many, over how many pages, and why it
        stopped."""
        return f"{len(self.hits)} hits, {self.pages} pages, {self.stopped}"


class Discovery:
    """WHAT: keywords in, candidate channels out. HOW: the official search
    endpoint, paginated, with no browser anywhere -- nothing to
    fingerprint, no session to burn, no detection surface -- which makes
    this the fastest and safest of the six platforms.

    LINKED TO: `discovery_path` in backend/platforms/registry.py names
    this class, and services/discovery_service.py drives it. `ctx` is
    accepted and ignored so that driver can construct every platform
    Discovery with the identical (args, ctx) signature."""

    def __init__(self, args, ctx=None):
        """`ctx` is accepted and ignored -- there is no browser on this
        platform -- so discovery_service.py can construct every platform's
        Discovery identically."""
        self.a = args
        self.api = YouTubeAPI()

    async def stop(self) -> None:
        """No persistent connection to release (plain HTTP calls per
        request), exists so discovery_service.py can call every
        no-session platform's discoverer.stop() uniformly, the same way it
        already calls session.stop() for the browser-based ones. See
        telegram/discovery_engine.py's Discovery.stop() for the platform
        where this actually matters."""
        return None

    async def sweep(self, keyword: str, tab: str = "channels", on_progress: Any = None) -> Sweep:
        """WHAT: one keyword -> a Sweep of candidate channels. HOW: pages
        through search_channels until a cap, the end of results, or quota;
        dedups by channel id as it goes, so the same channel appearing on
        two pages is one Hit; and streams each page to `on_progress` so
        the UI fills in during a long sweep instead of all at the end.

        A failure is recorded ON the Sweep rather than raised, and the
        `finally` block means hits already gathered survive it -- quota
        running out halfway through a keyword still returns what was found
        before it did. LINKED TO: Hit is the dataclass from
        facebook/discovery_engine.py, shared by every platform so
        discovery_service.py has one shape to handle; on_progress is that
        service page callback."""
        out = Sweep(keyword=keyword, tab=tab)
        started = time.time()
        by_id: dict[str, Hit] = {}
        token = ""
        try:
            while True:
                if self.a.max_results and len(by_id) >= self.a.max_results:
                    out.stopped = "cap:results"
                    break
                if self.a.max_seconds and time.time() - started >= self.a.max_seconds:
                    out.stopped = "cap:seconds"
                    break

                items, token = await self.api.search_channels(keyword, token)
                out.pages += 1
                # Small delay between paginated API calls to avoid
                # transient rate limiting from YouTube's backend
                if token and out.pages > 1:
                    await asyncio.sleep(0.5)
                page_hits: list[Hit] = []
                for i, it in enumerate(items):
                    if self.a.max_results and len(by_id) >= self.a.max_results:
                        out.stopped = "cap:results"
                        break
                    
                    cid = (it.get("id") or {}).get("channelId", "")
                    snip = it.get("snippet") or {}
                    if not cid or cid in by_id:
                        continue
                    
                    thumbs = snip.get("thumbnails") or {}
                    avatar = (thumbs.get("high") or thumbs.get("medium")
                             or thumbs.get("default") or {}).get("url", "")
                    hit = Hit(
                        entity_id=cid,
                        name=(
                            snip.get("channelTitle") or snip.get("title") or ""
                        ).strip(),
                        url=CHANNEL_URL.format(cid=cid),
                        avatar=avatar,
                        has_custom_pic=bool(avatar) and not RE_DEFAULT_PIC.search(avatar),
                        entity_type="channel",
                        keyword=keyword,
                        tab=tab,
                        rank=len(by_id) + i,
                        source="api",
                    )
                    by_id[cid] = hit
                    page_hits.append(hit)

                if page_hits and on_progress and callable(on_progress):
                    try:
                        res = on_progress(len(by_id), out.pages, page_hits)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        pass

                if out.stopped == "cap:results":
                    break

                if not token:
                    # the API stopped offering pages: genuinely the end
                    out.stopped, out.complete = "exhausted", True
                    break
        except QuotaExceeded as e:
            out.stopped, out.error = "quota", str(e)
        except Exception as e:
            out.stopped, out.error = "error", f"{type(e).__name__}: {e}"
        finally:
            # search.list's snippet has no statistics/contentDetails part at
            # all (that's channels.list, a different API resource analysis
            # calls per approved channel) -- so there is nothing free to
            # carry forward beyond name/avatar, already on Hit. See
            # hit_to_row's docstring (facebook/discovery_engine.py) and the
            # "One Pass or Two" research this redesign is based on.
            out.hits = [hit_to_row(h) for h in by_id.values()]
            if self.a.max_results:
                # Same bug class confirmed live on Twitter's identical
                # pattern: the loop-break check above (`len(by_id) >=
                # max_results`) only fires at the top of the NEXT
                # iteration, after a whole search.list page has already
                # been absorbed into by_id, so a configured cap of 5
                # still returned however many channels came back in that
                # page (YouTube's API commonly pages 50 at a time), with
                # nothing here to trim it back down.
                out.hits = out.hits[: self.a.max_results]
            out.seconds = time.time() - started
        return out

    async def run(self, keywords: list[str], tabs=None) -> list[Sweep]:
        """WHAT: sweeps a whole list of keywords. HOW: concurrently, but
        capped at 4 -- API calls parallelise cheaply, yet the quota they
        spend comes from a single shared daily pool, so more concurrency
        only exhausts it faster and in a less predictable order. Results
        are re-sorted back into the caller keyword order, which `gather`
        does not guarantee. LINKED TO: the standalone entry point; the API
        path drives sweep() per keyword through
        services/discovery_service.py instead."""
        sem = asyncio.Semaphore(max(1, min(self.a.concurrency, 4)))

        async def one(i: int, keyword: str) -> tuple[int, Sweep]:
            """One keyword, holding a quota/concurrency slot. Returns its
            index alongside the Sweep so the caller can restore order."""
            async with sem:
                s = await self.sweep(keyword)
                print(
                    f"  [youtube] {keyword!r}: {s.summary()} ({s.seconds:.1f}s)",
                    file=sys.stderr,
                )
                return i, s

        pairs = await asyncio.gather(*(one(i, k) for i, k in enumerate(keywords)))
        return [s for _, s in sorted(pairs, key=lambda p: p[0])]

