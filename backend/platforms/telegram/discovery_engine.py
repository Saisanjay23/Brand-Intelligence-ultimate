"""Telegram discovery engine: search and entity extraction, keywords in,
candidate users/channels/groups out, over MTProto (Telethon).

Also owns the MTProto connection itself (the `Telegram` class): it is
produced here first and re-used by analysis_engine.py, which imports it
rather than redefining it, so there is exactly one definition across the two
files.

Telegram publishes the real protocol, so there is no browser, no page to
render and nothing to intercept, this talks the same wire the official
clients do. That makes it the most accurate surface of the six and, like
YouTube, one with no fingerprint to detect.

WHAT LIMITS IT is FloodWait, not rate limiting: ask too fast and the server
replies "wait N seconds" rather than blocking you. That is a first-class
signal here, it is surfaced as a checkpoint so a run stops rather than
digging the account deeper into a limit.

AUTH is a saved session plus the account's api_id/api_hash. Logging in needs a
phone code, which is interactive, so this never attempts it: an unauthorised
session is reported, not repaired.

Telegram's global search returns a single capped page per keyword, there is
no cursor and no "load more", so a sweep is one request and is genuinely
complete when it returns. Unlike the browser platforms there is nothing to
scroll and nothing to miss. A sweep is only incomplete if Telegram asked us
to wait (FloodWait), which is reported rather than retried.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from backend.shared.logging import get_logger
from backend.shared.models.row import Row

log = get_logger("telegram")

try:
    from telethon import TelegramClient
    from telethon.errors import (AuthKeyUnregisteredError, ChannelPrivateError,
                                 FloodWaitError, SessionRevokedError,
                                 UserDeactivatedBanError, UserDeactivatedError,
                                 UsernameInvalidError,
                                 UsernameNotOccupiedError)
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.contacts import SearchRequest
    from telethon.tl.functions.users import GetFullUserRequest

    HAVE_TELETHON = True
except ImportError:  # pragma: no cover
    # Telethon is optional. Without it this platform cannot run, but the
    # rest of the tool must still import -- registry.py loads every engine
    # module at startup, so an ImportError here would take down all six
    # platforms over one missing dependency. These stubs exist purely so
    # the `except` clauses further down still name something catchable;
    # HAVE_TELETHON is what actually gates the platform being usable.
    HAVE_TELETHON = False
    TelegramClient = None  # type: ignore

    class FloodWaitError(Exception):  # type: ignore
        """Import-fallback stub, see the note above."""

        seconds = 0

    class AuthKeyUnregisteredError(Exception):  # type: ignore
        """Import-fallback stub, see the note above."""

        pass

    class SessionRevokedError(Exception):  # type: ignore
        """Import-fallback stub, see the note above."""

        pass

    class UserDeactivatedError(Exception):  # type: ignore
        """Import-fallback stub, see the note above."""

        pass

    class UserDeactivatedBanError(Exception):  # type: ignore
        """Import-fallback stub, see the note above."""

        pass

    class ChannelPrivateError(Exception):  # type: ignore
        """Import-fallback stub, see the note above."""

        pass

    class UsernameInvalidError(Exception):  # type: ignore
        """Import-fallback stub, see the note above."""

        pass

    class UsernameNotOccupiedError(Exception):  # type: ignore
        """Import-fallback stub, see the note above."""

        pass


# Connection / auth state


class NotAuthorised(RuntimeError):
    """The saved session is not logged in. Only an interactive login fixes it."""


class FloodWait(RuntimeError):
    """Telegram asked us to slow down. Treated like a checkpoint."""

    def __init__(self, seconds: int):
        """Carries the wait Telegram asked for, so the caller can report
        how long rather than just that it happened."""
        super().__init__(f"telegram asked for a {seconds}s pause")
        self.seconds = seconds


@dataclass
class TelegramEntity:
    """A user, channel or group, flattened to what the report needs."""

    entity_id: str = ""
    username: str = ""
    title: str = ""
    kind: str = "profile"  # profile | channel | group
    members: Optional[int] = None
    about: str = ""
    created_iso: str = ""  # channels carry a creation date; users do not
    verified: bool = False
    scam: bool = False
    restricted: bool = False
    fake: bool = False
    premium: bool = False
    avatar: str = ""
    has_photo: bool = False
    last_post_iso: str = ""

    @property
    def url(self) -> str:
        """The public t.me link, or a private-channel link by numeric id
        when there is no @username to link through."""
        if self.username:
            return f"https://t.me/{self.username}"
        return f"https://t.me/c/{self.entity_id}" if self.entity_id else ""


def entity_to_row(e: "TelegramEntity", keyword: str) -> Row:
    """A `TelegramEntity` -> the shared `Row` record `Sweep.hits` now
    carries. `entity_from()` above already reads title/members/creation
    date/verified-scam-restricted-fake-premium/photo straight off the
    plain `contacts.SearchRequest` result object -- the same fields, and
    the same avatar bytes (already downloaded via
    `download_profile_photo()`), analysis would otherwise fetch again from
    scratch. Only `about` and last-message date genuinely need the
    heavier `GetFullChannelRequest`/`GetFullUserRequest` + `get_messages()`
    calls analysis still makes. See the "One Pass or Two" research this is
    based on. `row.target` is seeded from the raw search term (`keyword`);
    a caller that resolves keyword-plan parents should overwrite it."""
    row = Row(
        url=e.url, target=keyword, entity_type=e.kind,
        profile_id=e.entity_id, profile_name=e.title,
        profile_pic_url=e.avatar, has_custom_pic=e.has_photo,
        verified=e.verified, followers=e.members, created_iso=e.created_iso,
    )
    for f in ("profile_name", "profile_pic_url", "has_custom_pic", "verified", "followers", "created_iso"):
        row.mark(f, "discovery-search:mtproto")
    if e.scam:
        row.note("Telegram flags this account as scam")
    if e.fake:
        row.note("Telegram flags this account as fake")
    if e.restricted:
        row.note("Telegram flags this account as restricted")
    return row


def _iso(value: Any) -> str:
    """A Telethon datetime -> 'YYYY-MM-DD' (UTC), "" for anything else."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date().isoformat()
    return ""


def entity_from(obj: Any) -> Optional[TelegramEntity]:
    """WHAT: one `TelegramEntity` out of a raw Telethon User/Channel/Chat
    object. HOW: distinguishes profile/channel/group by whether the
    object has a `title` (channels/groups) and its `broadcast` flag
    (channel vs group); reads every flag Telethon exposes directly (bot
    accounts read this too -- see the note on `is_bot` having been removed
    as dead code, 2026-08-22). LINKED TO: called by `Telegram.search()`/
    `resolve()` below, the shared builder both use."""
    if obj is None:
        return None
    username = getattr(obj, "username", "") or ""
    if title := getattr(obj, "title", ""):
        broadcast = bool(getattr(obj, "broadcast", False))
        kind = "channel" if broadcast else "group"
        name = title
    else:
        first = getattr(obj, "first_name", "") or ""
        last = getattr(obj, "last_name", "") or ""
        name = f"{first} {last}".strip()
        kind = "profile"
    if not username and not name:
        return None
    photo = getattr(obj, "photo", None)
    has_photo = photo is not None and "Empty" not in type(photo).__name__
    return TelegramEntity(
        entity_id=str(getattr(obj, "id", "") or ""),
        username=username,
        title=name,
        kind=kind,
        members=getattr(obj, "participants_count", None),
        created_iso=_iso(getattr(obj, "date", None)) if kind != "profile" else "",
        verified=bool(getattr(obj, "verified", False)),
        scam=bool(getattr(obj, "scam", False)),
        restricted=bool(getattr(obj, "restricted", False)),
        fake=bool(getattr(obj, "fake", False)),
        premium=bool(getattr(obj, "premium", False)),
        avatar=f"https://t.me/i/userpic/320/{username}.jpg" if username and has_photo else "",
        has_photo=has_photo,
    )


class Telegram:
    """A connected MTProto session. One at a time, the session file is a
    lock. LINKED TO: constructed by Discovery/Scraper in this file and
    analysis_engine.py alike -- this is Telegram's counterpart to the
    browser platforms' `stealth/browser.py::Session`, but talking raw
    MTProto instead of driving a browser (see this module's own top
    docstring for why that makes it the most accurate of the six
    platforms)."""

    def __init__(self, options=None):
        """`options` is a ScanOptions/DiscoveryOptions-shaped object
        (scan_options.py); the actual credentials come from
        TELEGRAM_API_ID/TELEGRAM_API_HASH in the environment (set by
        sessions/manager.py::session_for_job before this is constructed),
        not from `options` itself."""
        if not HAVE_TELETHON:
            raise RuntimeError("pip install telethon")
        self.o = options
        self.api_id = int(os.environ.get("TELEGRAM_API_ID", "0") or 0)
        self.api_hash = os.environ.get("TELEGRAM_API_HASH", "")
        from backend.config.settings import settings

        self.session_file = str(settings.session_blob_path / "telegram")
        self.client: Any = None

    async def start(self) -> None:
        """WHAT: connects the MTProto client against the saved session
        file, raising `NotAuthorised` if credentials are missing or the
        session isn't logged in. HOW: `connect()`, deliberately never
        `client.start()` -- Telethon's start() prompts for a phone code on
        stdin, which would hang a headless job forever the first time a
        session needs (re-)authenticating; see this module's own top
        docstring on why login is never attempted here."""
        if not (self.api_id and self.api_hash):
            raise NotAuthorised("TELEGRAM_API_ID / TELEGRAM_API_HASH not set -- not authenticated")
        self.client = TelegramClient(self.session_file, self.api_id, self.api_hash)
        # connect(), never start(): start() prompts for a phone code on stdin
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.stop()
            # "not authenticated" is deliberate phrasing, not just English.
            # It is one of shared/resilience.py::classify_failure's matched
            # auth tokens, so this correctly quarantines the session/fires
            # the SessionInvalid alert instead of reading as an
            # unclassified, left-alone error.
            raise NotAuthorised(
                "telegram session is not authenticated -- run an interactive login "
                "once to create session/telegram.session"
            )

    async def stop(self) -> None:
        """Disconnects and releases the MTProto client, freeing the local
        session file's lock (see Discovery.stop() below for the real
        "database is locked" incident this matters for)."""
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def check_session(self) -> bool:
        """False means CONCLUSIVELY dead. Telegram itself says this
        auth key/account no longer works, the MTProto equivalent of a
        browser landing on a login wall. Anything else (a network drop,
        a connection reset, FloodWait) is not evidence the session itself
        is bad and is left to propagate as a raised exception, exactly
        like every browser-based platform's check_session already does
        (see stealth/browser.py's docstring), the caller
        (sessions/manager.py::verify_session_item) treats a raised
        exception as inconclusive and leaves the session's status
        untouched, instead of a transient blip getting recorded as "this
        session is now expired" and quarantining a perfectly good account.

        This used to catch bare `Exception`, which made a dropped
        connection during the health check indistinguishable from Telegram
        actually revoking the session, the single most common false
        positive this file could produce.
        """
        try:
            me = await self.client.get_me()
        except (AuthKeyUnregisteredError, SessionRevokedError,
                 UserDeactivatedError, UserDeactivatedBanError) as e:
            log.error(f"session INVALID -- {type(e).__name__}: {e}")
            return False
        if me is None:
            log.error("session INVALID -- get_me() returned nothing")
            return False
        log.info(f"session valid -> @{me.username or me.id}")
        return True

    async def _call(self, request):
        """Every raw Telethon RPC request goes through here so FloodWait
        is caught and re-raised as this module's own `FloodWait` in one
        place, rather than every call site duplicating the same
        try/except. LINKED TO: used by `search()` and (for the "full"
        detail calls) `resolve()` below."""
        try:
            return await self.client(request)
        except FloodWaitError as e:
            raise FloodWait(int(getattr(e, "seconds", 0))) from e

    async def search(self, keyword: str, limit: int = 50) -> list[TelegramEntity]:
        """Global search. Telegram returns one capped page, there is no cursor."""
        res = await self._call(SearchRequest(q=keyword, limit=limit))
        out: list[TelegramEntity] = []
        for obj in list(getattr(res, "users", [])) + list(getattr(res, "chats", [])):
            if ent := entity_from(obj):
                if ent.has_photo:
                    try:
                        photo_bytes = await self.client.download_profile_photo(obj, file=bytes)
                        if photo_bytes:
                            ent.avatar = f"data:image/jpeg;base64,{base64.b64encode(photo_bytes).decode('utf-8')}"
                    except FloodWaitError as e:
                        # NOT the generic except below -- every other RPC in
                        # this file converts this to the module's own
                        # FloodWait and lets it propagate so the run stops;
                        # swallowing it here at debug level let a flood
                        # triggered by a photo download go completely
                        # unnoticed, and every remaining photo download in
                        # this same search() call (and every resolve() call
                        # after it) would re-hit the same limit with no
                        # backoff, silently.
                        raise FloodWait(int(getattr(e, "seconds", 0))) from e
                    except Exception as e:
                        log.debug(f"could not download photo for {ent.username or ent.entity_id}: {e}")
                out.append(ent)
        return out

    async def resolve(self, username: str) -> Optional[TelegramEntity]:
        """@name -> entity, with the detail that needs a second call filled
        in. Used only by the analysis pass, which is independent of
        discovery by design: everything here is re-read over MTProto for
        this run, including the profile photo, rather than reusing
        anything a discovery sweep may already have fetched."""
        try:
            obj = await self.client.get_entity(username)
        except (UsernameInvalidError, UsernameNotOccupiedError, ValueError):
            return None
        except FloodWaitError as e:
            raise FloodWait(int(getattr(e, "seconds", 0))) from e
        except ChannelPrivateError:
            return None

        ent = entity_from(obj)
        if ent is None:
            return None

        if ent.has_photo:
            try:
                photo_bytes = await self.client.download_profile_photo(obj, file=bytes)
                if photo_bytes:
                    ent.avatar = f"data:image/jpeg;base64,{base64.b64encode(photo_bytes).decode('utf-8')}"
            except FloodWaitError as e:
                # See search()'s identical fix -- this used to be silently
                # swallowed by the generic except below, so a flood-wait
                # hit here came back as an ordinary OK/PARTIAL row instead
                # of CHECKPOINT, and Scraper.run() never stopped the batch,
                # driving the account deeper into the limit on every
                # subsequent resolve() call.
                raise FloodWait(int(getattr(e, "seconds", 0))) from e
            except Exception as e:
                log.debug(f"could not download photo for @{username}: {e}")

        try:
            if ent.kind == "profile":
                full = await self._call(GetFullUserRequest(obj))
                ent.about = (getattr(full.full_user, "about", "") or "").strip()
            else:
                full = await self._call(GetFullChannelRequest(obj))
                chat = full.full_chat
                ent.about = (getattr(chat, "about", "") or "").strip()
                if ent.members is None:
                    ent.members = getattr(chat, "participants_count", None)
        except FloodWait:
            raise
        except Exception as e:
            log.debug(f"no full detail for @{username}: {type(e).__name__}")

        ent.last_post_iso = await self.last_post(obj)
        return ent

    async def last_post(self, entity: Any) -> str:
        """Newest message date. Users' own messages are not readable; channels' are."""
        try:
            msgs = await self.client.get_messages(entity, limit=1)
        except FloodWaitError as e:
            raise FloodWait(int(getattr(e, "seconds", 0))) from e
        except Exception:
            return ""
        if not msgs:
            return ""
        return _iso(getattr(msgs[0], "date", None))

    async def pause(self, seconds: float) -> None:
        """Plain pacing sleep -- MTProto has no per-page/scroll rhythm to
        wait on, so unlike the browser platforms this is a flat delay,
        not a wait-for-a-real-signal poll."""
        if seconds > 0:
            await asyncio.sleep(seconds)


# Crawling / search

# global search caps well below this; asking for more costs nothing
SEARCH_LIMIT = 100


@dataclass
class Sweep:
    """One keyword's search sweep, and how it ended. Simpler than the
    browser platforms' Sweep: Telegram's global search returns a single
    capped page with no cursor (see this module's own top docstring), so
    there is no pagination state to carry -- `stopped` is always
    "exhausted" (a clean single-page result) or "flood-wait"/"error"."""

    keyword: str
    tab: str = "all"
    hits: list[Row] = field(default_factory=list)
    pages: int = 0
    stopped: str = ""
    complete: bool = False
    seconds: float = 0.0
    error: str = ""

    def summary(self) -> str:
        """One-line log form: how many, and why the sweep stopped. No page
        count -- MTProto search returns a single result set rather than
        paginating the way the browser platforms do."""
        return f"{len(self.hits)} hits, {self.stopped}"


class Discovery:
    """`ctx` is accepted and unused. MTProto needs no browser.

    `sweep()` connects `self.tg` itself, lazily, the first time it's called:
    discovery_service.py's shared harness (_run_incremental) drives every
    platform by calling `sweep(keyword, tab)` per keyword directly, it never
    calls `run()` below. `run()` predates that harness and is dead code from
    the caller's perspective (nothing imports/calls it), but every keyword
    it once looped over now arrives as its own `sweep()` call instead, so
    the connect step it used to own has to happen inside `sweep()`.
    A lock guards the connect because `_run_incremental` fires multiple
    keywords concurrently (semaphore-limited), and Telethon's `connect()`
    is not safe to race.
    """

    def __init__(self, args, ctx=None):
        """`ctx` is accepted and ignored (no browser). The MTProto client
        is left unbuilt until the first sweep, so constructing a Discovery
        never opens a connection."""
        self.a = args
        self.tg: Telegram | None = None
        self._connect_lock = asyncio.Lock()

    async def _ensure_connected(self) -> None:
        """WHAT: connects `self.tg` on first use, idempotent after that.
        HOW: double-checked locking against `self._connect_lock`, since
        discovery_service.py's shared harness fires several keyword
        sweeps concurrently and Telethon's own connect() is not safe to
        race (see this class's own docstring). LINKED TO: called at the
        top of `sweep()` below; every keyword sweep for one Discovery
        instance shares the same connection once established."""
        if self.tg is not None:
            return
        async with self._connect_lock:
            if self.tg is not None:
                return
            tg = Telegram(self.a)
            await tg.start()
            try:
                if not await tg.check_session():
                    raise NotAuthorised("telegram session rejected -- not authenticated")
            except Exception:
                # Not just the explicit "not authenticated" case above --
                # ANY exception past this point must not leave `tg`
                # connected with nothing able to reach it. `self.tg` is
                # only assigned below, after check_session() succeeds, so
                # Discovery.stop() (which only acts `if self.tg is not
                # None`) can never find and close a connection that died
                # here. check_session()'s own docstring says it
                # deliberately lets non-auth exceptions (a FloodWaitError
                # from get_me(), a transient RPC/network error) propagate
                # rather than returning False for them -- so this is not a
                # rare edge case, it's the documented normal behavior for
                # that path. Without this, the session file stays locked
                # and the NEXT sweep for this account reproduces the exact
                # "database is locked" bug `stop()`'s own docstring below
                # says was already fixed -- just from a connect path that
                # fix didn't cover.
                await tg.stop()
                raise
            self.tg = tg

    async def stop(self) -> None:
        """Release the MTProto connection `sweep()` opened.

        Confirmed live: without this, discovery_service.py's caller had
        nothing to call it FROM either, every discovery sweep for this
        platform left `self.tg` connected, holding the local SQLite
        `.session` file locked. Reproduced the real symptom directly: a
        discovery sweep followed immediately by an analysis run for the
        same client (exactly what the round-robin engine's per-client turn
        does) failed analysis with "database is locked", because
        Telethon's own client only supports one open connection to that
        file at a time. Analysis' own Scraper always closes its own
        connection via its `stop()`; this was the missing other half.
        """
        if self.tg is not None:
            await self.tg.stop()
            self.tg = None

    async def sweep(self, keyword: str, tab: str = "all") -> Sweep:
        """One keyword, one request -- the simplest of the six platforms'
        sweep() methods, since Telegram's global search has no pagination
        to loop over (see this module's top docstring). WHAT IT RETURNS:
        a `Sweep` of every Hit `Telegram.search()` found, "exhausted" the
        moment that one request returns, or "flood-wait"/"error" if it
        didn't. LINKED TO: called by discovery_service.py's shared
        harness directly per keyword (not via `run()` below, which
        predates that harness -- see this class's own docstring)."""
        out = Sweep(keyword=keyword, tab=tab)
        started = time.time()
        try:
            await self._ensure_connected()
            found = await self.tg.search(keyword, SEARCH_LIMIT)
            out.pages = 1
            out.hits = [entity_to_row(e, keyword) for e in found if e.url]
            if self.a.max_results:
                out.hits = out.hits[: self.a.max_results]
            out.stopped, out.complete = "exhausted", True
        except FloodWait as e:
            out.stopped, out.error = "flood-wait", str(e)
        except Exception as e:
            out.stopped, out.error = "error", f"{type(e).__name__}: {e}"
            log.error(f"[telegram] {keyword!r} sweep failed: {out.error}")
        finally:
            out.seconds = time.time() - started
        return out

    async def run(self, keywords: list[str], tabs=None) -> list[Sweep]:
        """Sequential on purpose: one session, and search is what gets limited."""
        self.tg = Telegram(self.a)
        sweeps: list[Sweep] = []
        try:
            await self.tg.start()
            if not await self.tg.check_session():
                raise NotAuthorised("telegram session rejected -- not authenticated")
            for i, keyword in enumerate(keywords):
                s = await self.sweep(keyword)
                sweeps.append(s)
                print(
                    f"  [telegram] {keyword!r}: {s.summary()} ({s.seconds:.1f}s)",
                    file=sys.stderr,
                )
                if s.stopped == "flood-wait":
                    print(
                        "  telegram asked us to slow down -- stopping the sweep",
                        file=sys.stderr,
                    )
                    break
                if i < len(keywords) - 1:
                    await asyncio.sleep(2.0)  # unhurried between searches
        finally:
            await self.tg.stop()
        return sweeps

