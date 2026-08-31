"""Telegram analysis engine: validation, t.me URL -> scored Row, over
MTProto.

The MTProto connection (`Telegram`) and its entity/error types live in
discovery_engine.py (imported below) since discovery produces/needs them
first; this file owns URL normalization for the analysis entry point and the
drive loop (Scraper).

Resolving a username gives the entity, one full-detail call gives the
description and member count, and the newest message gives the activity date.

WHAT TELEGRAM GIVES that the browser platforms do not: channels and groups
carry a real creation date, so the Created Date column is filled for them.
User accounts have no such field anywhere in the protocol, so it stays blank
for people, the honest answer rather than a guess.
"""

from __future__ import annotations

from urllib.parse import urlparse

from typing import Optional

from backend.shared.models.row import Row
from backend.shared.text import (name_score, normalized_host,
                                   parse_normalized_url)
from backend.platforms.telegram.discovery_engine import (FloodWait,
                                                          NotAuthorised,
                                                          Telegram,
                                                          TelegramEntity)

BAD_SEGMENTS = {"s", "c", "joinchat", "addstickers", "share", "proxy", "i"}


def normalize_url(url: str) -> str:
    """WHAT: one canonical `https://t.me/<slug>` form for any Telegram
    reference -- a t.me/telegram.me/telegram.dog URL, a bare `@handle`, or
    a `tg://` deep link. HOW: an `@handle` is a special case (Telegram
    renders it nowhere as a URL), everything else goes through
    shared/text.py::parse_normalized_url with `tg://` added to the
    accepted schemes. LINKED TO: the one normalizer this file uses;
    discovery_engine.py has none of its own since it never receives raw
    user-typed URLs, only entities it resolved itself."""
    stripped = (url or "").strip().strip("\"'")
    if stripped.startswith("@"):
        return f"https://t.me/{stripped[1:]}"
    p = parse_normalized_url(stripped, extra_schemes=("tg://",))
    if p is None:
        return ""
    host = normalized_host(p)
    if host in ("telegram.me", "telegram.dog", "t.me"):
        host = "t.me"
    return f"https://{host}{p.path.rstrip('/')}"


def username_of(url: str) -> str:
    """WHAT: the account's @username, out of a normalized t.me URL. HOW:
    the first non-BAD_SEGMENTS path segment, with one special case:
    `t.me/s/<name>` is the web-preview URL for the same channel `name`,
    so it resolves through to the real segment rather than being rejected
    outright. LINKED TO: Scraper.process()'s row.profile_id."""
    seg = [s for s in urlparse(normalize_url(url)).path.split("/") if s]
    if not seg:
        return ""
    name = seg[0].lstrip("@")
    if name.lower() in BAD_SEGMENTS:
        # https://t.me/s/<name> is the web preview of the same channel
        return seg[1] if name.lower() == "s" and len(seg) > 1 else ""
    return name


class Scraper:
    """Same surface as the browser scanners; MTProto behind it.

    LINKED TO: `analysis_path` in backend/platforms/registry.py names
    this class (loaded dynamically), and backend/services/
    analysis_service.py is the actual caller -- it constructs and drives
    every platform's Scraper identically, which is why this class's
    `__init__` accepts the same (args, cookies, session_id, proxy) shape
    even though MTProto uses none of the last three.
    """

    normalize_url = staticmethod(normalize_url)

    def __init__(self, args, cookies=None, session_id: str = "", proxy=None):
        """MTProto, not a browser. `cookies`, `session_id` and `proxy` are
        accepted and unused so services/analysis_service.py can construct
        every platform's Scraper with one signature."""
        self.a = args
        self.tg = Telegram(args)

    async def start(self):
        """Connects the MTProto client (delegates to Telegram.start())."""
        await self.tg.start()

    async def stop(self):
        """Disconnects the MTProto client, releasing the session file's
        lock (see discovery_engine.py's Discovery.stop() for the "database
        is locked" incident this half of the pairing exists to prevent)."""
        await self.tg.stop()

    async def pause(self, mult: float = 1.0):
        """Between-profile pacing -- a flat delay (MTProto has no
        page/scroll rhythm to wait on, see Telegram.pause())."""
        await self.tg.pause(getattr(self.a, "delay", 2.0) * mult)

    async def check_session(self) -> bool:
        """Is this MTProto session still authenticated? Delegates to
        Telegram.check_session(), catching NotAuthorised (missing
        credentials) as a plain False rather than letting it propagate."""
        from backend.shared.logging import get_logger as _gl
        try:
            return await self.tg.check_session()
        except NotAuthorised as e:
            _gl("platforms.telegram.analysis").warning(f"SESSION: {e}")
            return False

    # ───────────────────────────── per URL ────────────────────────────── #

    async def process(
        self, raw_url: str, target: str, feed: str, known: Optional[dict] = None,
    ) -> Row:
        """WHAT: one t.me URL -> a scored Row. HOW: normalize the URL, pull
        the @username out of it, resolve that username to an entity over
        MTProto, then hand the entity to fill() for the field-by-field
        mapping. Three outcomes, deliberately distinguished: no username
        (ERROR -- a private t.me/joinchat link carries no resolvable
        identity, so retrying cannot help), no such entity (GONE -- the
        terminal state, the account is deleted or banned), or a reading.

        `known` (whatever discovery already read for this URL, see
        analysis/runner.py's `seed_by_url`) is accepted for interface
        consistency with the other platforms' `one()`/`process()`, but
        unlike Twitter's About-panel there is nothing here to skip:
        `resolve()` below is ONE MTProto call that returns every field this
        engine reads in one shot (members, created date, avatar, last post,
        verified/scam flags) -- there is no separate per-field fetch a
        known value could let this profile skip. Runner.py's own
        `_populate` fallback already covers "discovery had it, this call
        somehow didn't" for whichever fields apply.

        LINKED TO: called by one() below, which wraps it in the error
        handling; resolve() is discovery_engine.py::Telegram.resolve."""
        url = normalize_url(raw_url)
        row = Row(url=url, target=target, original_feed=feed)
        username = username_of(url)
        row.profile_id = username

        if not username:
            row.status = "ERROR"
            row.note("no @username in the URL -- private links cannot be resolved")
            return row

        ent = await self.tg.resolve(username)
        if ent is None:
            row.status = "GONE"
            row.note("no such user or channel -- may already be taken down")
            return row

        self.fill(row, ent)
        row.status = "OK" if row.profile_name else "PARTIAL"
        return row

    @staticmethod
    def fill(row: Row, e: TelegramEntity) -> None:
        """WHAT: copies one resolved TelegramEntity onto the Row, field by
        field. HOW: every value comes from the protocol rather than a
        rendered page, so each one is marked `mtproto` (see
        shared/models/row.py::mark, which records WHERE a field came from
        so shared/completeness.py can tell "read it" from "never saw it").
        Absences are recorded as findings rather than left blank and
        unexplained: a user account genuinely has no creation date
        anywhere in the protocol, so it gets a note saying so instead of a
        guess. LINKED TO: called by process() above; the entity is built
        in discovery_engine.py::TelegramEntity."""
        row.profile_id = e.username or e.entity_id
        row.entity_type = e.kind
        row.profile_name = e.title
        row.mark("name", "mtproto")
        row.name_score = name_score(e.title, row.target)

        if e.members is not None:
            # members are this platform's reach figure; exact, from the protocol
            row.followers = e.members
            row.followers_exact = "yes"
            row.mark("followers", "mtproto")
        if e.created_iso:
            row.created_iso = e.created_iso
            row.mark("created", "mtproto")
        elif e.kind == "profile":
            row.note("telegram exposes no creation date for user accounts")
        if e.avatar:
            row.profile_pic_url = e.avatar
        # has_photo comes straight from Telethon's own PhotoEmpty check, not
        # a guess from whether the avatar URL happened to resolve
        row.has_custom_pic = e.has_photo
        row.mark("logo", "mtproto")
        if e.last_post_iso:
            row.last_post_iso = e.last_post_iso
            row.posts_seen = "yes"
            row.mark("last_post", "mtproto")
        if e.about:
            row.note(f"bio: {e.about[:120]}")
        if e.verified:
            row.verified = True
            row.note("verified by telegram")
        if e.scam:
            row.note("FLAGGED BY TELEGRAM AS SCAM")
        if e.restricted:
            row.note("restricted by telegram")

    # ─────────────────────────── orchestration ────────────────────────── #

    async def one(self, u: str, tgt: str, feed: str, known: Optional[dict] = None) -> Row:
        """WHAT: process() that never raises -- always a Row, whatever
        happened. HOW: FloodWait becomes CHECKPOINT, which is the status
        the run loop treats as "stop the wave, the account is at risk",
        the same handling a browser platform's login challenge gets.
        Anything else becomes ERROR with the exception on the row, so one
        unreachable channel cannot end a job."""
        try:
            return await self.process(u, tgt, feed, known)
        except FloodWait as e:
            row = Row(url=normalize_url(u), target=tgt, original_feed=feed)
            row.profile_id = username_of(row.url)
            row.status = "CHECKPOINT"  # stop the run, same as a challenge
            row.note(str(e))
            return row
        except Exception as e:
            row = Row(url=normalize_url(u), target=tgt, original_feed=feed)
            row.profile_id = username_of(row.url)
            row.status = "ERROR"
            row.note(f"{type(e).__name__}: {e}")
            return row

    @staticmethod
    def report(i: int, total: int, u: str, row: Row) -> None:
        """WHAT: one progress line per profile. HOW: prints the fields an
        operator watching a run needs to spot a silent extraction failure
        early -- a column that is "-" on every line means that field
        stopped being read. LINKED TO: called by run() after each row; the
        equivalent of the browser engines' own report()."""
        from backend.shared.logging import get_logger as _gl
        _gl("platforms.telegram.analysis").info(
            f"[{i}/{total}] {u} | {row.status} name={row.profile_name[:22]} "
            f"members={row.followers if row.followers is not None else '-'} "
            f"active={row.active_yes or '-'} risk={row.risk} {row.priority}"
        )

    async def run(self, jobs: list[tuple[str, str, str]]) -> list[Row]:
        """WHAT: drives a whole batch of (url, target, feed) jobs. HOW:
        sequentially, pausing between profiles, and stopping the moment a
        row comes back CHECKPOINT -- a flood wait means Telegram is
        already rate-limiting this account, and continuing to hammer it is
        what turns a temporary wait into a ban. Rows gathered before the
        stop are still returned, so the work already done is never thrown
        away. LINKED TO: the standalone entry point; the API path drives
        one() directly through services/analysis_service.py instead."""
        rows: list[Row] = []
        for i, (u, tgt, feed) in enumerate(jobs, 1):
            row = await self.one(u, tgt, feed)
            rows.append(row)
            self.report(i, len(jobs), u, row)
            if row.status == "CHECKPOINT":
                from backend.shared.logging import get_logger as _gl
                _gl("platforms.telegram.analysis").warning(
                    "FLOOD WAIT -- stopping to protect the account."
                )
                break
            if i < len(jobs):
                await self.pause()
        return rows
