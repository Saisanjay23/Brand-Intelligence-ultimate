"""Threads analysis engine: validation and impersonation signal extraction --
profile URL -> scored Row.

Session/login-checking and the ThreadsUser type live in discovery_engine.py
(imported below) since discovery produces/needs them first; this file owns
everything specific to a validated profile visit: URL normalization, reading
the profile record, and the browser-drive loop (Scraper).

WHERE THE DATA COMES FROM. A profile page carries its own record as typed JSON
in its `<script type="application/json">` blocks, so nothing is scraped from
rendered text. Measured 2026-11-04 on a real profile, the record holds:

    pk / id, username, full_name, biography, follower_count (an exact
    integer, not "15.1M"), is_verified, text_post_app_is_private,
    profile_pic_url, hd_profile_pic_versions, bio_links

and the same blocks carry the profile's recent posts as objects with
`taken_at` (unix seconds) and `user.username`, so the last-post date needs no
extra request. The page answers the same record logged out for a public
account, but the SESSION still matters: signed out, Threads shows only the
first few posts and sends a missing account to the login wall, which looks
exactly like a blocked session. Signed in, a missing account says so.

EVERY DATE IS ATTRIBUTED BEFORE IT IS RECORDED (the rule instagram/ paid to
learn): a post counts only when it names THIS profile as its author. A repost
carries its original author, a suggestion rail carries other people's posts,
and a timestamp with no author at all is skipped rather than assumed -- so a
blank date means "could not establish one", never a date read off someone
else's post. A repost-only account therefore reads blank, which is the honest
side to err on.

NOT COLLECTED: account creation date and location. Threads publishes neither
on the profile record; they sit behind an interactive "About this profile"
panel, so those columns stay blank rather than guessed.

LOGO / PROFILE PICTURE: Threads serves avatars from Instagram's CDN, and its
record carries no `has_anonymous_profile_picture` flag, so the verdict falls
to the stock-avatar markers in shared/avatars.py (the same fallback tier
Instagram's DOM read uses). It is tri-state: an unread picture is None, never
"has one".
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from backend.platforms.scan_options import cancelled, captures_screenshot
from backend.platforms.threads.discovery_engine import (ThreadsSession,
                                                         ThreadsUser,
                                                         wall_kind)
from backend.shared import diagnostics, evidence
from backend.shared.avatars import extract_instagram_hd_avatar
from backend.shared.models.row import Row
from backend.shared.pushback import (CHALLENGED, EXPIRED, THROTTLED, Signal,
                                     governor_for, note_response)
from backend.shared.text import (iter_dicts, name_score, normalized_host,
                                 parse_normalized_url)

# Consecutive profile visits that returned no record, with no explicit
# pushback, before the governor is told Threads is probably soft-blocking.
NO_PAYLOAD_STREAK_LIMIT = 3

# What a signed-in Threads shows for a profile that does not exist. Consulted
# only after no profile record could be read, so a bio quoting it never
# matches.
_RE_GONE = re.compile(r"the link.s not working or the page is gone", re.I)

def pushback_verdict(sig) -> Optional[tuple[str, str]]:
    """What Threads' own answer says about the ACCOUNT, as (row status, note),
    or None when it says nothing about the account. A throttle is the
    governor's business (shared/pushback.py), not a verdict on the session."""
    if sig.kind == CHALLENGED:
        return "CHECKPOINT", f"threads challenge ({sig.reason})"
    if sig.kind == EXPIRED:
        return "LOGIN_REQUIRED", f"threads login rejected ({sig.reason})"
    return None


def register_no_payload(state, pushed: bool) -> str:
    """Count one profile visit that ended with no record and, when
    `NO_PAYLOAD_STREAK_LIMIT` of them come in a row WITHOUT an explicit
    pushback, tell the governor Threads is probably soft-blocking. Explicit
    pushback is excluded because the response handler already fed the governor
    with it. Returns the note for the row, "" when there is nothing to say."""
    state._no_payload_streak = getattr(state, "_no_payload_streak", 0) + 1
    if state._no_payload_streak >= NO_PAYLOAD_STREAK_LIMIT and not pushed:
        governor_for("threads").record(Signal(
            THROTTLED,
            f"{state._no_payload_streak} consecutive profiles returned no record"))
        return (f"{state._no_payload_streak} profiles in a row returned no record "
                "-- possible soft block, pacing slowed")
    return ""


def normalize_url(url: str) -> str:
    """One canonical `https://www.threads.com/@<handle>` form for any Threads
    URL variant (threads.net, threads.com, a post or replies link all fold onto
    the account). Empty when the URL names no account."""
    p = parse_normalized_url(url)
    if p is None:
        return ""
    host = normalized_host(p)
    if "threads" not in host:
        return ""
    seg = [s for s in p.path.split("/") if s]
    if not seg or not seg[0].startswith("@") or len(seg[0]) < 2:
        return ""
    return f"https://www.threads.com/{seg[0]}"


def username_of(url: str) -> str:
    """The account's handle, out of a normalized profile URL ("" if none)."""
    seg = [s for s in urlparse(normalize_url(url)).path.split("/") if s]
    return seg[0].lstrip("@") if seg else ""


# --------------------------------------------------------- profile record


_PROFILE_KEYS = ("follower_count", "biography")


def is_profile_record(node: Any) -> bool:
    """A FULL profile record carries the account's own statistics; a bare
    mention of an account (a post's author stub) does not."""
    return isinstance(node, dict) and any(k in node for k in _PROFILE_KEYS)


def _timestamp(d: dict) -> int:
    ts = d.get("taken_at")
    if isinstance(ts, bool) or not isinstance(ts, int):
        return 0
    return ts if 1_000_000_000 < ts < 4_000_000_000 else 0


def latest_post(blobs: list[Any], owner: str) -> str:
    """Newest post date (ISO, UTC) attributable to `owner`, or "".

    A media object counts only when it NAMES `owner` as its author. One that
    names someone else (a repost, a suggestion) or nobody is skipped: an
    unattributed timestamp is how a stranger's post becomes this account's."""
    want = owner.lower().strip("@/")
    best = 0
    for blob in blobs:
        for d in iter_dicts(blob):
            ts = _timestamp(d)
            if not ts:
                continue
            user = d.get("user")
            name = str(user.get("username") or "").strip().lower() if isinstance(user, dict) else ""
            if name and name == want:
                best = max(best, ts)
    if not best:
        return ""
    return datetime.fromtimestamp(best, timezone.utc).date().isoformat()


def profile_from(blobs: list[Any], username: str = "") -> Optional[ThreadsUser]:
    """The profile this page is about, not whoever else the payload mentions."""
    want = username.lower().strip("@/")
    best: Optional[dict] = None
    for blob in blobs:
        for d in iter_dicts(blob):
            name = d.get("username")
            if not isinstance(name, str) or not name or not is_profile_record(d):
                continue
            if want and name.lower() != want:
                continue
            if best is None or ("follower_count" in d and "follower_count" not in best):
                best = d
    if best is None:
        return None

    fc = best.get("follower_count")
    followers = fc if isinstance(fc, int) and not isinstance(fc, bool) else None
    owner = str(best["username"])
    last = latest_post(blobs, owner)
    return ThreadsUser(
        entity_id=str(best.get("pk") or best.get("id") or ""),
        username=owner,
        full_name=str(best.get("full_name") or "").strip(),
        followers=followers,
        followers_exact=followers is not None,
        avatar=extract_instagram_hd_avatar(best),
        biography=str(best.get("biography") or "").strip(),
        verified=bool(best.get("is_verified")),
        private=bool(best.get("text_post_app_is_private") or best.get("is_private")),
        last_post_iso=last,
        posts_seen="yes" if last else "",
    )


def blobs_from_scripts(texts: list[str]) -> list[Any]:
    """The JSON the page embeds, parsed. A block that is not valid JSON is
    skipped -- it is one of dozens, and one bad one must not cost the rest."""
    out: list[Any] = []
    for t in texts:
        try:
            out.append(json.loads(t))
        except (TypeError, ValueError):
            continue
    return out


# Runs in the page: the embedded JSON blocks that can hold a profile or a post
# (the rest of the ~45 are layout and feature flags), plus the visible text
# lines the wall and gone checks read.
_JS_PAGE = r"""
() => {
  const texts = [];
  for (const s of document.querySelectorAll('script[type="application/json"]')) {
    const t = s.textContent || '';
    if (t.includes('"follower_count"') || t.includes('"taken_at"')) texts.push(t);
  }
  const lines = (document.body.innerText || '').split('\n')
    .map(s => s.trim()).filter(Boolean).slice(0, 300);
  return {url: location.href, texts, lines};
}
"""

_JS_HAS_RECORD = r"""
() => [...document.querySelectorAll('script[type="application/json"]')]
  .some(s => (s.textContent || '').includes('"follower_count"'))
"""


class Scraper:
    """One logged-in Threads session, driven over a list of profiles.

    LINKED TO: `analysis_path` in backend/platforms/registry.py names this
    class (loaded dynamically); analysis/runner.py is the caller, one Scraper
    per analysis run."""

    normalize_url = staticmethod(normalize_url)

    def __init__(self, args, cookies: list[dict], session_id: str = ""):
        """Binds this Scraper to one ThreadsSession built from `cookies`.
        Images load only when evidence capture is on."""
        self.a = args
        self.evidence = args.evidence or None  # GridFS key prefix, not a path
        self.session = ThreadsSession(
            args,
            cookies,
            load_images=captures_screenshot(args),
            session_id=session_id,
        )

    @property
    def ctx(self):
        """The live Playwright BrowserContext, once `start()` has run."""
        return self.session.ctx

    async def start(self):
        await self.session.start()

    async def stop(self):
        await self.session.stop()

    async def pause(self, mult: float = 1.0):
        """Between-profile pacing (jittered, fatigue-aware) -- stealth/human.py."""
        await self.session.pause(mult)

    async def check_session(self) -> bool:
        """Is this cookie set still signed in to Threads and unchallenged?"""
        return await self.session.check_session()

    async def sync_cookies(self) -> None:
        if hasattr(self.session, "sync_cookies"):
            await self.session.sync_cookies()

    async def process(
        self, raw_url: str, target: str, feed: str, known: Optional[dict] = None,
    ) -> Row:
        """One profile URL, start to finish -> a scored `Row`, status
        OK / PARTIAL / GONE / ERROR / CHECKPOINT / LOGIN_REQUIRED, every field
        tagged with `row.mark()`.

        HOW: open the profile, wait for its embedded record, parse it. With no
        record, decide WHY in this order and never the other way round: a
        wall (login / not-joined / checkpoint), then a confirmed "page is
        gone", then a bare PARTIAL that feeds the soft-block streak."""
        url = normalize_url(raw_url)
        row = Row(url=url, target=target, original_feed=feed)
        row.profile_id = username_of(url)
        if not row.profile_id:
            row.status = "ERROR"
            row.note("not a Threads profile URL (no @handle) -- not visited")
            return row

        # Pinned once: fill() reassigns row.profile_id to the numeric pk.
        wanted = row.profile_id
        page = await self.ctx.new_page()
        pushed: list = []
        try:
            doc_status: Optional[int] = None
            try:
                resp = await page.goto(
                    url, wait_until="domcontentloaded", timeout=self.a.timeout * 1000)
                st = getattr(resp, "status", None)
                if isinstance(st, int) and not isinstance(st, bool):
                    doc_status = st
                    err_body = None
                    if st >= 400:
                        try:
                            err_body = (await resp.text())[:4000]
                        except Exception:
                            err_body = None
                    sig = note_response("threads", st, err_body)
                    if sig.is_pushback:
                        pushed.append(sig)
            except Exception as e:
                first = (str(e).splitlines() or [""])[0][:200]
                row.status = "ERROR"
                row.note(f"navigation failed: {type(e).__name__}: {first}")
                return row

            # Wait for the record rather than a fixed sleep: it is in the
            # document's own script blocks, so it lands with the page.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + float(getattr(self.a, "settle", 6.0))
            while loop.time() < deadline and not cancelled(self.a):
                try:
                    if await page.evaluate(_JS_HAS_RECORD):
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)

            try:
                snap = await page.evaluate(_JS_PAGE)
            except Exception:
                snap = {}
            snap = snap if isinstance(snap, dict) else {}
            lines = snap.get("lines") or []
            landed = snap.get("url") or page.url
            user = profile_from(blobs_from_scripts(snap.get("texts") or []), wanted)

            if user is None:
                wall = wall_kind(landed, lines)
                if wall == "checkpoint":
                    row.status = "CHECKPOINT"
                    row.note("session checkpointed")
                    return row
                if wall in ("login", "not-joined"):
                    row.status = "LOGIN_REQUIRED"
                    row.note("this Instagram account has not joined Threads"
                             if wall == "not-joined" else "cookies rejected/expired")
                    return row
                if pushed:
                    sig = pushed[-1]
                    if verdict := pushback_verdict(sig):
                        row.status, note = verdict
                        row.note(note)
                        return row
                    row.note(f"threads throttled this visit ({sig.reason})")
                if _RE_GONE.search("\n".join(lines)):
                    row.status = "GONE"
                    row.note("removed or unavailable -- may already be down"
                             + (" (HTTP 404)" if doc_status == 404 else ""))
                    return row
                row.status = "PARTIAL"
                row.note("profile record not seen"
                         + (f" (page answered HTTP {doc_status})"
                            if doc_status and doc_status != 200 else ""))
                if streak_note := register_no_payload(self, bool(pushed)):
                    row.note(streak_note)
                await self.screenshot(page, row)
                return row

            self._no_payload_streak = 0
            self.fill(row, user)
            await self.screenshot(page, row)
            if user.private:
                row.note("private account -- posts not visible")
            elif not user.last_post_iso:
                row.note("no post attributable to this account was read -- "
                         "last-post date left blank")
            row.status = "OK" if row.profile_name or row.profile_id else "PARTIAL"
            return row
        finally:
            try:
                await page.close()
            except Exception:
                pass

    @staticmethod
    def fill(row: Row, u: ThreadsUser) -> None:
        """Every field a ThreadsUser carries, written onto `row` and tagged
        "api" (the embedded record is Threads' own typed data, not a DOM
        read). Each guarded so a field the record did not carry is left
        untouched rather than blanked."""
        row.profile_id = u.entity_id or u.username
        # the display name is what an impersonator copies; fall back to handle
        row.profile_name = u.full_name or u.username
        row.mark("name", "api")
        row.name_score = name_score(row.profile_name, row.target)

        if u.followers is not None:
            row.followers = u.followers
            row.followers_exact = "yes" if u.followers_exact else "no"
            row.mark("followers", "api")
        if u.avatar:
            row.profile_pic_url = u.avatar
        # None is left alone: unknown must not overwrite an earlier verdict.
        if u.has_custom_pic is not None:
            row.has_custom_pic = u.has_custom_pic
            row.mark("logo", "api")
        if u.last_post_iso:
            row.last_post_iso = u.last_post_iso
            row.posts_seen = "yes"
            row.mark("last_post", "api")
        if u.biography:
            row.bio = u.biography
            row.mark("bio", "api")
        if u.verified:
            row.verified = True
            row.note("verified account")

    async def screenshot(self, page, row: Row) -> None:
        """Evidence capture (a GridFS-stored PNG) for `row`, when enabled.
        Waits for a real post to paint, so the capture proves the account is
        in use and not just that its header exists. Mechanics live in
        shared/evidence.py."""
        await evidence.capture(
            self.session, page, row, evidence=self.evidence,
            ephemeral=getattr(self.a, "ephemeral_screenshot", False),
            content_selector='a[href*="/post/"]', platform="threads")

    # ─────────────────────────── orchestration ────────────────────────── #

    async def one(self, u: str, tgt: str, feed: str, known: Optional[dict] = None) -> Row:
        """process() with a failed profile turned into a reportable ERROR row
        instead of raising, so one bad URL never crashes the batch."""
        try:
            return await self.process(u, tgt, feed, known)
        except Exception as e:
            row = Row(url=normalize_url(u), target=tgt, original_feed=feed)
            row.profile_id = username_of(row.url)
            row.status = "ERROR"
            row.note(f"{type(e).__name__}: {e}")
            row.where = diagnostics.where(e)
            return row

    @staticmethod
    def report(i: int, total: int, u: str, row: Row) -> None:
        """One progress line to the platform's own logger after each profile."""
        from backend.shared.logging import get_logger as _gl
        _gl("platforms.threads.analysis").info(
            f"[{i}/{total}] {u} | {row.status} name={row.profile_name[:22]} "
            f"followers={row.followers if row.followers is not None else '-'} "
            f"active={row.active_yes or '-'} risk={row.risk} {row.priority}"
        )

    async def run(self, jobs: list[tuple[str, str, str]]) -> list[Row]:
        """Every (url, target, feed) job, sequentially, pausing between
        profiles and aborting the batch on a CHECKPOINT (unless
        `self.a.keep_going`) to protect the session."""
        rows: list[Row] = []
        for i, (u, tgt, feed) in enumerate(jobs, 1):
            row = await self.one(u, tgt, feed)
            rows.append(row)
            self.report(i, len(jobs), u, row)
            if row.status == "CHECKPOINT" and not getattr(self.a, "keep_going", False):
                from backend.shared.logging import get_logger as _gl
                _gl("platforms.threads.analysis").warning(
                    "CHECKPOINT -- aborting to protect the session.")
                break
            if i < len(jobs):
                await self.pause()
        return rows
