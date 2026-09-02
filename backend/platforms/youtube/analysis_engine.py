"""YouTube analysis engine: validation, channel URL -> scored Row, via the
official API.

The API client (`YouTubeAPI`) and the default-picture check live in
discovery_engine.py (imported below) since discovery produces/needs them
first; this file owns URL normalization for the analysis entry point and the
drive loop (Scraper).

Two cheap calls per channel (detail + newest upload) and every field arrives
typed: subscriber counts as integers, a real creation date, and an upload date
that makes the activity check meaningful. No browser, so `start`/`stop` are
no-ops kept only to satisfy the same interface as the browser platforms.
"""

from __future__ import annotations

import asyncio
import urllib.request
from typing import Optional
from urllib.parse import unquote, urlparse

from backend.shared.avatars import (YOUTUBE_GENERATED_PREFIX,
                                    is_generated_avatar)
from backend.shared.models.row import Row
from backend.shared.text import name_score, normalized_host, parse_normalized_url
from backend.platforms.youtube.discovery_engine import (RE_DEFAULT_PIC,
                                                         QuotaExceeded,
                                                         YouTubeAPI)


def normalize_url(url: str) -> str:
    """WHAT: one canonical `https://www.youtube.com/<path>` form for any
    YouTube reference. HOW: shared/text.py::parse_normalized_url does the
    scheme/host parsing, then every youtu-something host (youtu.be,
    m.youtube.com, music.youtube.com) collapses to www.youtube.com so the
    same channel reached by two different hosts dedups to one row. LINKED
    TO: exposed as Scraper.normalize_url, which services/analysis_service
    .py calls before storing any URL."""
    p = parse_normalized_url(url)
    if p is None:
        return ""
    host = normalized_host(p)
    if "youtu" in host:
        host = "www.youtube.com"
    return f"https://{host}{p.path.rstrip('/')}"


def channel_ref(url: str) -> tuple[str, str]:
    """WHAT: -> (kind, value) where kind is 'id' or 'handle', naming which
    API lookup can resolve this URL. HOW: YouTube has four surviving
    channel URL shapes and they need different endpoints -- /channel/UC...
    is the canonical id (a direct `channels` lookup), while /@handle, /c/
    and the legacy /user/ are all vanity forms that only `forHandle` can
    resolve. A bare first segment that looks like a UC id is treated as
    one even without the /channel/ prefix. LINKED TO: process() below
    branches on the kind to pick between api.channels() and
    api.channel_by_handle()."""
    p = urlparse(normalize_url(url))
    seg = [unquote(s) for s in p.path.split("/") if s]
    if not seg:
        return "", ""
    if seg[0] == "channel" and len(seg) > 1:
        return "id", seg[1]
    if seg[0].startswith("@"):
        return "handle", seg[0]
    if seg[0] in ("c", "user") and len(seg) > 1:
        return "handle", seg[1]
    if seg[0].startswith("UC") and len(seg[0]) >= 20:
        return "id", seg[0]
    return "handle", seg[0]


class Scraper:
    """Same surface as the browser scanners; no browser behind it."""

    normalize_url = staticmethod(normalize_url)

    def __init__(self, args, cookies=None, session_id: str = "", proxy=None):
        """API-key authed, no browser. `cookies`, `session_id` and `proxy`
        are accepted and unused so services/analysis_service.py can
        construct every platform's Scraper with one signature."""
        self.a = args
        self.api = YouTubeAPI()

    async def start(self):
        """No-op: there is no browser or connection to open, the API is
        stateless and key-authed. Kept so analysis_service.py can drive
        every platform's Scraper through the identical lifecycle."""
        return None

    async def stop(self):
        """No-op, the other half of start()'s interface obligation."""
        return None

    async def pause(self, mult: float = 1.0):
        """No-op: this platform is quota-bound, not rate-bound. Pacing
        would not save quota, it would only make jobs slower."""
        return None

    async def check_session(self) -> bool:
        """False means the KEY ITSELF is conclusively rejected, the
        YouTube-API equivalent of a browser landing on a login wall.

        Daily quota exhaustion is NOT that: it is a normal, expected state
        (see discovery_engine.py's module docstring, 10,000 units/day,
        resets on its own) that says nothing about whether the key is
        valid. This used to return False for it too, which wrongly
        quarantined a perfectly good key and fired a SessionInvalid alert
        telling someone to replace credentials that were never the
        problem, every single day the quota happened to run out first.

        A network/timeout error during the check is likewise not evidence
        the key is bad, and is left to propagate as a raised exception
        (via classify_failure's `None` result below) rather than being
        swallowed into "rejected", same conclusive-vs-inconclusive
        contract every other platform's check_session follows (see
        stealth/browser.py's docstring); the caller
        (sessions/manager.py::verify_session_item) treats a raised
        exception as inconclusive and leaves the session's status
        untouched instead of recording a blip as "this key is now dead."
        """
        from backend.shared.logging import get_logger as _gl
        from backend.shared.resilience import classify_failure
        _log = _gl("platforms.youtube.analysis")
        try:
            await self.api.get("channels", part="id", forHandle="@youtube")
            _log.info("SESSION: API key valid")
            return True
        except QuotaExceeded as e:
            _log.info(f"SESSION: quota exhausted for today -- key itself still valid ({e})")
            return True
        except Exception as e:
            reason = classify_failure(e)
            if reason in ("expired", "checkpointed"):
                _log.warning(f"SESSION: API key rejected -- {e}")
                return False
            # rate_limited (a 429/"too many requests" distinct from daily
            # quota), or unclassified/transient, not conclusive evidence
            # the key itself is bad
            raise

    # ───────────────────────────── per URL ────────────────────────────── #

    async def process(
        self, raw_url: str, target: str, feed: str, known: Optional[dict] = None,
    ) -> Row:
        """WHAT: one channel URL -> a scored Row. HOW: resolve the URL to a
        channel through whichever lookup its shape allows (trying the
        direct id first and falling back to the handle lookup, so a
        /channel/UC... URL whose id has since changed still resolves),
        fill the typed fields, then spend one more call on the uploads
        playlist for the newest video date.

        A channel with zero videos is recorded as `posts_seen = "no"` --
        genuinely postless, which is a real finding about an impersonator
        account -- rather than left blank, which shared/completeness.py
        would otherwise have to report as a field we failed to read.

        `known` (whatever discovery already read for this URL, see
        analysis/runner.py's `seed_by_url`) is accepted for interface
        consistency with the other platforms, but there's nothing to skip
        here: the `channels.list` call below is the ONLY source for the
        uploads-playlist id the following `latest_upload()` call needs, so
        it stays mandatory even when every other field it would return
        (followers/created/location/pic) is already known. Runner.py's own
        `_populate` fallback covers those from `known` if this call ever
        genuinely comes back without one.

        LINKED TO: fill() below for the mapping; api.latest_upload is in
        discovery_engine.py::YouTubeAPI."""
        url = normalize_url(raw_url)
        row = Row(url=url, target=target, original_feed=feed)
        kind, ref = channel_ref(url)
        row.entity_type = "channel"

        if not ref:
            row.status = "ERROR"
            row.note("could not read a channel reference from the URL")
            return row

        ch = None
        if kind == "id":
            found = await self.api.channels([ref])
            ch = found[0] if found else None
        if ch is None:
            ch = await self.api.channel_by_handle(ref)

        if ch is None:
            row.status = "GONE"
            row.note("no such channel -- may already be taken down")
            return row

        self.fill(row, ch)
        await self._confirm_avatar(row)
        uploads = ((ch.get("contentDetails") or {}).get("relatedPlaylists") or {}).get(
            "uploads", ""
        )
        if iso := await self.api.latest_upload(uploads):
            row.last_post_iso = iso
            row.posts_seen = "yes"
            row.mark("last_post", "api")
        elif (ch.get("statistics") or {}).get("videoCount") == "0":
            row.posts_seen = "no"
            row.mark("last_post", "api-no-videos")

        row.status = "OK" if row.profile_name else "PARTIAL"
        return row

    @staticmethod
    def fill(row: Row, ch: dict) -> None:
        """WHAT: copies one API channel resource onto the Row. HOW: every
        value is already typed by the API, so each is marked `api` (see
        shared/models/row.py::mark). Two YouTube-specific truths are
        recorded rather than papered over: subscriber counts are rounded
        to three significant figures unless hidden entirely, so
        `followers_exact` says which of those happened; and the display
        name falls through title -> channelTitle -> branding title ->
        customUrl, because a channel that has never been renamed carries
        its name in only some of those. LINKED TO: called by process()
        above; RE_DEFAULT_PIC is discovery_engine.py's shared check for
        the stock avatar."""
        snip = ch.get("snippet") or {}
        stats = ch.get("statistics") or {}

        row.profile_id = ch.get("id", "")
        row.profile_name = (
            snip.get("title")
            or snip.get("channelTitle")
            or (ch.get("brandingSettings") or {}).get("channel", {}).get("title")
            or snip.get("customUrl")
            or ""
        ).strip()
        row.mark("name", "api")
        row.name_score = name_score(row.profile_name, row.target)

        if (subs := stats.get("subscriberCount")) is not None:
            row.followers = int(subs)
            # YouTube rounds public subscriber counts to 3 significant figures
            row.followers_exact = "no" if stats.get("hiddenSubscriberCount") else "yes"
            row.mark("followers", "api")
        if stats.get("hiddenSubscriberCount"):
            row.note("subscriber count hidden by the channel")

        if published := snip.get("publishedAt"):
            row.created_iso = published[:10]
            row.mark("created", "api")
        if country := snip.get("country"):
            row.location = country
            row.mark("location", "api")

        thumbs = snip.get("thumbnails") or {}
        best = thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}
        if uri := best.get("url"):
            row.profile_pic_url = uri
            row.has_custom_pic = not bool(RE_DEFAULT_PIC.search(uri))
            row.mark("logo", "api")

        if (videos := stats.get("videoCount")) is not None:
            row.note(f"{int(videos):,} videos")

    # ─────────────────────────── orchestration ────────────────────────── #

    _AVATAR_TIMEOUT = 8
    _AVATAR_MAX_BYTES = 2 * 1024 * 1024

    @classmethod
    def _read_avatar(cls, uri: str) -> bytes:
        """The avatar image itself. urllib in a thread, matching how this
        platform's API client already does its I/O rather than pulling in an
        async HTTP client for one call."""
        req = urllib.request.Request(uri, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=cls._AVATAR_TIMEOUT) as resp:
            return resp.read(cls._AVATAR_MAX_BYTES)

    async def _confirm_avatar(self, row: Row) -> None:
        """Settle has_custom_pic from the IMAGE, which for YouTube is the
        only thing that can settle it.

        YouTube does not serve one shared placeholder the way Facebook,
        Instagram and X do. It GENERATES a per-channel avatar -- one letter
        on a solid colour, varying by letter and by colour -- from the same
        host and the same URL shape as a real upload, so `fill()` above
        cannot tell the two apart and marked every channel as having a real
        picture. Measured across 960 stored avatars, 164 were generated.

        Only runs for URLs carrying the generated-avatar prefix (that alone
        is 60% precise, so shared/avatars.py also requires the image to be
        two flat colours), which keeps this to a fraction of channels rather
        than one extra fetch each.

        NON-FATAL by construction: on any failure the row keeps whatever
        `fill()` decided. A missed placeholder is a far smaller error than a
        broken analysis run.
        """
        uri = row.profile_pic_url
        if not uri or YOUTUBE_GENERATED_PREFIX not in uri:
            return
        try:
            data = await asyncio.to_thread(self._read_avatar, uri)
        except Exception:
            return
        if not data:
            return
        generated = is_generated_avatar("youtube", uri, data)
        if generated is not None:
            row.has_custom_pic = not generated
            row.mark("logo", "api+image")

    async def one(self, u: str, tgt: str, feed: str, known: Optional[dict] = None) -> Row:
        """WHAT: process() that never raises -- always a Row. HOW: quota
        exhaustion becomes CHECKPOINT, the status that stops the wave,
        because every further call today would fail identically and burn
        job time for nothing. Anything else becomes ERROR carrying the
        exception, so one dead channel cannot end a job. LINKED TO: called
        by run(); note that check_session() deliberately does NOT treat
        quota as a bad key -- see its docstring."""
        try:
            return await self.process(u, tgt, feed, known)
        except QuotaExceeded as e:
            row = Row(url=normalize_url(u), target=tgt, original_feed=feed)
            row.status = "CHECKPOINT"  # stops the run, same as a challenge
            row.note(str(e))
            return row
        except Exception as e:
            row = Row(url=normalize_url(u), target=tgt, original_feed=feed)
            row.status = "ERROR"
            row.note(f"{type(e).__name__}: {e}")
            return row

    @staticmethod
    def report(i: int, total: int, u: str, row: Row) -> None:
        """WHAT: one progress line per channel. HOW: prints the fields an
        operator needs to spot a silent extraction failure early -- a
        column reading "-" on every line means that field stopped being
        read. LINKED TO: called by run() after each row."""
        from backend.shared.logging import get_logger as _gl
        _gl("platforms.youtube.analysis").info(
            f"[{i}/{total}] {u} | {row.status} name={row.profile_name[:22]} "
            f"subs={row.followers if row.followers is not None else '-'} "
            f"active={row.active_yes or '-'} risk={row.risk} {row.priority}"
        )

    async def run(self, jobs: list[tuple[str, str, str]]) -> list[Row]:
        """WHAT: drives a whole batch of (url, target, feed) jobs. HOW:
        resolves every job's channel reference up front, then fetches every
        id-shaped channel through ONE (or few, chunked-50 -- see
        YouTubeAPI.channels) `channels.list` call instead of one call per
        channel. `channels.list` costs 1 quota unit regardless of how many
        ids ride along, so a run of 200 approved channels drops from 200
        units to ~4 -- previously every channel paid for its own call
        because `process()` (still used for a single ad-hoc lookup, see
        one()) always called `api.channels([ref])` with a length-1 list.

        Handle-shaped URLs (@handle, legacy /c/ or /user/) still resolve
        one at a time: the Data API v3 has no batch handle-lookup endpoint.
        The per-channel `latest_upload()` call (1 unit each) is likewise
        unavoidable and unchanged -- see that method's own docstring for
        why it's already the cheap way to get an activity date.

        Stops the moment a row comes back CHECKPOINT (daily quota gone,
        every remaining job would fail the same way); rows gathered before
        the stop are still returned. LINKED TO: the standalone entry
        point; the API path drives this directly."""
        from backend.shared.logging import get_logger as _gl
        log = _gl("platforms.youtube.analysis")

        resolved = [(normalize_url(u), tgt, feed, *channel_ref(normalize_url(u))) for u, tgt, feed in jobs]

        id_refs = list(dict.fromkeys(ref for _, _, _, kind, ref in resolved if kind == "id" and ref))
        by_id: dict[str, dict] = {}
        if id_refs:
            try:
                by_id = {c.get("id"): c for c in await self.api.channels(id_refs) if c.get("id")}
            except QuotaExceeded as e:
                log.warning(f"QUOTA EXHAUSTED before the batch channel lookup -- stopping. {e}")
                return [
                    Row(url=url, target=tgt, original_feed=feed, entity_type="channel",
                        status="CHECKPOINT", notes=str(e))
                    for url, tgt, feed, _, _ in resolved
                ]

        rows: list[Row] = []
        for i, (url, tgt, feed, kind, ref) in enumerate(resolved, 1):
            row = Row(url=url, target=tgt, original_feed=feed, entity_type="channel")
            if not ref:
                row.status = "ERROR"
                row.note("could not read a channel reference from the URL")
                rows.append(row)
                self.report(i, len(resolved), url, row)
                continue
            try:
                ch = by_id.get(ref) if kind == "id" else None
                if ch is None:
                    ch = await self.api.channel_by_handle(ref)
                if ch is None:
                    row.status = "GONE"
                    row.note("no such channel -- may already be taken down")
                else:
                    self.fill(row, ch)
                    await self._confirm_avatar(row)
                    uploads = ((ch.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads", "")
                    if iso := await self.api.latest_upload(uploads):
                        row.last_post_iso = iso
                        row.posts_seen = "yes"
                        row.mark("last_post", "api")
                    elif (ch.get("statistics") or {}).get("videoCount") == "0":
                        row.posts_seen = "no"
                        row.mark("last_post", "api-no-videos")
                    row.status = "OK" if row.profile_name else "PARTIAL"
            except QuotaExceeded as e:
                row.status = "CHECKPOINT"
                row.note(str(e))
                rows.append(row)
                self.report(i, len(resolved), url, row)
                log.warning("QUOTA EXHAUSTED -- stopping.")
                break
            except Exception as e:
                row.status = "ERROR"
                row.note(f"{type(e).__name__}: {e}")
            rows.append(row)
            self.report(i, len(resolved), url, row)
        return rows
