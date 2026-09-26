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
So discovery costs ~101 units per 50 results, and analysis is ~2 units per
channel. Reading the newest upload through playlistItems instead of a dated
search is a 100x saving, which is why it is done that way. No browser, so no
session, no pacing and no detection surface, a sweep stops on an explicit
end of results, a cap, or quota exhaustion, and says which.

THE 101st UNIT is one `channels.list` call per search page, and it buys the
URL. `search.list` returns a channel id and a snippet and no handle at all,
so the only URL a sweep could build from it was
`https://www.youtube.com/channel/UCAOnNAx9wF8tkPtKH1a8VUw` -- correct, and
unreadable. YouTube's public identity for that same channel is
`https://www.youtube.com/@NewGautamAdani-q3o`: it is what the channel page
displays, what the share button copies, and the only one of the two an
analyst can hold against a brand name at a glance. The handle lives on
`snippet.customUrl`, which only `channels.list` returns.

Worth spending because it is not a per-profile cost. One call covers 50
ids for a single unit, against the 100 units the page that produced those
ids already cost -- a 1% surcharge, not a multiplier, which is why the
"One Pass or Two" objection that keeps per-profile enrichment out of the
other engines' sweeps does not apply here. When the call fails or quota
refuses it, the affected rows keep the id-shaped URL and the reason is
logged rather than left to look like a design choice.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from backend.shared.schema_probe import SchemaProbe
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.shared.avatars import hd_picture_url
from backend.shared.logging import get_logger
from backend.shared.models.hit import Hit, hit_to_row
from backend.shared.models.row import Row

log = get_logger("youtube.api")

BASE = "https://www.googleapis.com/youtube/v3"
CHANNEL_URL = "https://www.youtube.com/channel/{cid}"
# The form a human recognises and can paste into a browser. YouTube has
# made the handle the public identity of a channel -- it is what the
# channel page shows, what the platform's own share sheet copies, and what
# an analyst sees when they open the account -- while `/channel/UC...` is
# the internal id, correct but unreadable and impossible to eyeball against
# a brand name.
HANDLE_URL = "https://www.youtube.com/@{handle}"


def channel_url(cid: str, custom_url: str = "") -> str:
    """The public URL for a channel: the @handle form when YouTube gives
    us one, the `/channel/<id>` form when it does not.

    `custom_url` is `snippet.customUrl` from channels.list, which is the
    handle (usually already carrying its "@"). Older channels that never
    claimed a handle have no customUrl at all, and legacy ones can still
    carry a `/c/`-era vanity string; both are handled by normalising the
    leading "@" off and putting exactly one back.

    NEVER RETURNS AN EMPTY STRING when it has an id to work with. A URL is
    this row's identity everywhere downstream, so falling back to the id
    form is mandatory -- a channel whose handle could not be read must
    still be reachable, just less readably.

    CASE IS PASSED THROUGH UNTOUCHED. The API is the only thing here that
    knows the handle, so whatever case it reports is what gets stored.
    YouTube routes handles case-insensitively, so a link built this way
    resolves either way.
    """
    handle = (custom_url or "").strip().lstrip("@").strip()
    if handle:
        return HANDLE_URL.format(handle=handle)
    return CHANNEL_URL.format(cid=cid) if cid else ""


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

    async def handles(self, ids: list[str]) -> dict[str, str]:
        """`{channel id: @handle}` for a batch of ids, skipping any the API
        does not give a handle for.

        WHY DISCOVERY PAYS FOR THIS AT ALL, when the rule everywhere else
        in this codebase is that a sweep must not spend a per-profile call:
        this is not a per-profile call. `channels.list` costs ONE unit for
        up to 50 ids, against the 100 units the `search.list` page that
        produced those ids already cost -- so a whole page of handles is a
        1% surcharge on a page of results, not a multiplier.

        `search.list` is the reason it is needed: its result carries the
        channel id and the snippet, and no handle at all. Without this the
        only URL a sweep can build is `/channel/UC...`, which is the id
        form -- correct, but not what a channel calls itself and not what
        an analyst can recognise.

        NEVER RAISES. A quota refusal or an API error returns what was
        resolved so far (possibly nothing), and the caller falls back to
        the id form per channel. Losing the readable URL must not be able
        to lose the sweep.
        """
        out: dict[str, str] = {}
        if not ids:
            return out
        try:
            for ch in await self.channels(ids):
                cid = ch.get("id") or ""
                custom = str(((ch.get("snippet") or {}).get("customUrl")) or "").strip()
                if cid and custom:
                    out[cid] = custom
        except (QuotaExceeded, RuntimeError) as e:
            # Named, not swallowed: a sweep quietly reverting to id-shaped
            # URLs looks like a product decision rather than a failure.
            log.warning(
                f"youtube: channel handles unresolved for {len(ids)} id(s) "
                f"({type(e).__name__}: {e}) -- those rows keep the "
                f"/channel/<id> URL form")
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


# EVERY ATTRIBUTE THIS ENGINE TARGETS IN YOUTUBE'S SEARCH RESPONSE.
# A documented public API drifts far less than a scraped payload -- but
# "less" is not "never", and a platform with no probe is one whose rename
# arrives as a silent zero. See shared/schema_probe.py.
K_YT_CHANNEL_ID = "item.id.channelId"
K_YT_SNIPPET = "item.snippet"
K_YT_TITLE = "snippet.{channelTitle|title}"


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
    # WHICH TARGETED ATTRIBUTES THE PLATFORM STILL SERVES: {key: [hits,
    # misses]}, from shared/schema_probe.py. Carried on the Sweep so the
    # runner can fold it into the rolling telemetry, which is what lets an
    # alert name the exact renamed key instead of reporting a mystery drop.
    schema: dict = field(default_factory=dict)

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
        # YouTube is a documented public API rather than a scraped payload,
        # so it drifts far less than the others -- but "less" is not "never"
        # (the v3 search response has changed shape before), and a platform
        # with no probe is a platform whose rename arrives as a silent zero.
        probe = SchemaProbe()
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
                # ONE extra unit for the whole page (see api.handles), spent
                # before the Hits are built so each one is born with the URL
                # it will keep. Doing it here rather than after the loop also
                # means the `on_progress` stream below carries the readable
                # URL, so a card never appears as /channel/UC... and then
                # silently changes shape underneath the analyst.
                page_ids = [
                    cid for it in items
                    if (cid := (it.get("id") or {}).get("channelId", ""))
                    and cid not in by_id
                ]
                handle_by_id = await self.api.handles(page_ids)

                page_hits: list[Hit] = []
                for i, it in enumerate(items):
                    if self.a.max_results and len(by_id) >= self.a.max_results:
                        out.stopped = "cap:results"
                        break
                    
                    cid = (it.get("id") or {}).get("channelId", "")
                    snip = it.get("snippet") or {}
                    # Probed inside a result the API did return, so a search
                    # that genuinely matched no channels records nothing.
                    (probe.hit if cid else probe.miss)(K_YT_CHANNEL_ID)
                    (probe.hit if snip else probe.miss)(K_YT_SNIPPET)
                    if not cid or cid in by_id:
                        continue
                    probe.first_of(snip, ("channelTitle", "title"), K_YT_TITLE)
                    
                    thumbs = snip.get("thumbnails") or {}
                    avatar = hd_picture_url(
                        (thumbs.get("high") or thumbs.get("medium")
                         or thumbs.get("default") or {}).get("url", "")
                    )
                    hit = Hit(
                        entity_id=cid,
                        name=(
                            snip.get("channelTitle") or snip.get("title") or ""
                        ).strip(),
                        url=channel_url(cid, handle_by_id.get(cid, "")),
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
                        # ROWS, not Hits -- the same shape the finished
                        # Sweep returns, which is the contract every
                        # streaming engine here shares (see
                        # facebook/discovery_engine.py::_notify). Handing
                        # back raw Hits made the runner's save raise, and
                        # because this callback swallows exceptions by
                        # design, the whole sweep's results vanished
                        # silently on the first live run.
                        res = on_progress(
                            len(by_id), out.pages,
                            [hit_to_row(h) for h in page_hits])
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
            out.schema = probe.report()
            if broken := probe.broken_keys():
                from backend.shared.logging import get_logger as _gl
                _gl("youtube").warning(
                    f"youtube: {keyword!r} -- attribute(s) no longer served where we "
                    f"look for them: {', '.join(broken)}. The search API response "
                    f"shape has changed; see sweep()")
        return out

