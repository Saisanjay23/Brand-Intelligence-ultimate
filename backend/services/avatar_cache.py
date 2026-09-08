"""Pulls a discovered profile's picture into our own store, off the sweep's
critical path.

THE ORDERING IS THE WHOLE DESIGN. A sweep saves its hits and reports them
immediately, exactly as before -- the analyst sees cards within seconds. The
pictures are fetched AFTERWARDS, by this module, and each profile's row is
updated in place as its bytes land. Discovery does not get slower to make
the pictures permanent; the two are simply decoupled.

That matters more than it sounds. Facebook's discovery engine goes out of
its way never to download an image during a sweep (a single profile visit
requests 74 of them), because image bytes are the difference between a sweep
that takes a minute and one that takes twenty. Fetching avatars inline would
have thrown that away. Fetching them behind the sweep costs the sweep
nothing.

WHY THE BYTES AND NOT THE URL: see avatar_repository's docstring -- the
signed CDN URLs expire within hours, so a card built on one is temporary by
construction.
"""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Iterable, Optional
from urllib.parse import urlparse

from backend.database.repositories import avatar_repository as avatars_db
from backend.database.repositories import profile_repository as profiles_db
from backend.database.repositories import logo_repository as logos_db
from backend.services import logo_match
from backend.shared.imageembedding import available as embedding_available
from backend.shared.imageembedding import embed
from backend.shared.imagefetch import ImageFetchError, allowed, fetch_image
from backend.shared.avatars import is_generated_avatar
from backend.shared.imagehashing import fingerprint
from backend.shared.logging import get_logger

log = get_logger("services.avatar_cache")

# Deliberately small. These run BEHIND a sweep that is itself talking to the
# same CDNs, and the point is to be invisible to it -- both in wall-clock
# cost and in how much traffic we add to a host that is already watching how
# much we ask of it. Avatars are a few KB each; this is not a throughput
# problem worth solving.
CONCURRENCY = 4

# One fetch that hangs must not hold a job's completion. Every avatar is
# best-effort: the card falls back to the live CDN URL and then to its
# initial-letter circle, both of which already work.
PER_IMAGE_TIMEOUT_SEC = 20.0
BATCH_TIMEOUT_SEC = 180.0
MAX_BYTES = 8 * 1024 * 1024  # Size cap matching imagefetch.MAX_BYTES


# Matches data:image/<type>;base64,<payload> -- the format Telegram's MTProto
# engine produces when it downloads a profile photo via download_profile_photo().
_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<b64>.+)$", re.DOTALL,
)


async def _cache_data_uri(
    url: str, *, want_embedding: bool = False,
) -> tuple[Optional[str], Optional[dict], Optional[list]]:
    """Decode a base64 `data:` URI and persist it directly to GridFS.

    Telegram's MTProto engine stores profile photos as inline base64 rather
    than a remote URL. Before this handler, these avatars were **skipped** by
    both `cache_one()` (rejected by the allowlist) and `cache_for_profiles()`
    (explicit `startswith("data:")` guard), so Telegram profiles never
    received an `avatar_sha`, their cards showed only the ephemeral base64
    blob, and logo matching was impossible.
    """
    m = _DATA_URI_RE.match(url)
    if not m:
        return None, None, None
    try:
        raw = base64.b64decode(m.group("b64"))
    except Exception:  # noqa: BLE001 - malformed base64
        log.warning("data: URI base64 decode failed")
        return None, None, None
    if not raw or len(raw) > MAX_BYTES:
        return None, None, None
    mime = m.group("mime").lower()
    try:
        sha = await avatars_db.store(raw, mime)
    except Exception as e:  # noqa: BLE001 - never fatal
        log.warning(f"data: URI store error: {type(e).__name__}: {e}")
        return None, None, None
    fp = None
    try:
        fp = await asyncio.to_thread(fingerprint, raw)
    except Exception as e:  # noqa: BLE001 - never fatal
        log.warning(f"avatar fingerprint error: {type(e).__name__}: {e}")
    vec = None
    if want_embedding:
        try:
            vec = await asyncio.to_thread(embed, raw)
        except Exception as e:  # noqa: BLE001 - never fatal
            log.warning(f"avatar embed error: {type(e).__name__}: {e}")
    return sha, (fp.to_dict() if fp else None), vec


def is_same_avatar_asset(old_url: str, new_url: str) -> bool:
    """Returns True if two avatar URLs represent the exact same underlying image asset.
    Ignores ephemeral query parameters (e.g. Meta's oh= and oe= signatures), but detects
    genuine DP changes where the asset filename or path differs."""
    if old_url == new_url:
        return True
    if not old_url or not new_url:
        return False
    if old_url.startswith("data:") or new_url.startswith("data:"):
        return old_url == new_url
    try:
        p_old = urlparse(old_url).path
        p_new = urlparse(new_url).path
        return bool(p_old and p_old == p_new)
    except Exception:
        return False


async def cache_one(
    url: str, retries: int = 1, *, want_embedding: bool = False,
    platform: str = "",
) -> tuple[Optional[str], Optional[dict], Optional[list], Optional[bool]]:
    """Fetch, store and fingerprint one avatar
    -> (sha256, fingerprint, embedding, generated).

    `generated` is True when the bytes are a picture the PLATFORM drew
    rather than one the account holder chose -- Facebook's per-account
    letter avatar, YouTube's per-channel one. It needs the image, which is
    why it is answered here: this function has already paid for the
    download and the decode, so the verdict is free. Discovery's own sweep
    must never download an image (see this module's docstring), and
    analysis must not re-derive it (see analysis/runner.py) -- so this is
    the only place in the pipeline that can settle it at all.

    None means "not decided", not "real": an unreadable image, a platform
    with no rule, or a flat image on a ground the platform does not use.

    Either half can be None and that is an ordinary outcome, not an error:
    an expired signature, a CDN node refusing a connection, a file that is
    not decodable as an image. The caller keeps the live URL it had.

    Includes a bounded retry for transient network / CDN blips (502/503/timeout).

    Supports `data:` URIs (Telegram's inline base64 avatars) in addition to
    regular HTTPS URLs.

    THE FINGERPRINT IS COMPUTED IN A THREAD, NOT ON THE EVENT LOOP. Decoding
    is CPU-bound and does not yield, and this task shares its loop with the
    running sweep -- Playwright's response handlers, the poll loop,
    `arrived.wait()`. Measured over one sweep's avatars: computed inline it
    stalls that loop for a median of 1.7s and a maximum of 3.4s, which slows
    discovery itself; through `to_thread` the lag is ~5ms AND the work
    finishes 5.6x faster, because Pillow releases the GIL while decoding.
    """
    if not url:
        return None, None, None, None
    # Telegram's MTProto avatars arrive as data: URIs, not HTTP links.
    if url.startswith("data:"):
        return (*await _cache_data_uri(url, want_embedding=want_embedding), None)
    if not allowed(url):
        return None, None, None, None
    for attempt in range(retries + 1):
        try:
            img = await asyncio.wait_for(fetch_image(url), timeout=PER_IMAGE_TIMEOUT_SEC)
            sha = await avatars_db.store(img.data, img.content_type)
            try:
                fp = await asyncio.to_thread(fingerprint, img.data)
            except Exception as e:                   # noqa: BLE001 - never fatal
                # A picture we cannot fingerprint is still a picture worth
                # keeping: the cached bytes are what fixes the expiring-card
                # problem, and the logo signal is a bonus on top.
                log.warning(f"avatar fingerprint error: {type(e).__name__}: {e}")
                fp = None
            vec = None
            if want_embedding:
                # ~70ms, and only asked for when the client has reference
                # logos to compare against -- which is nobody until an
                # analyst opts in. Same thread rule as the fingerprint.
                try:
                    vec = await asyncio.to_thread(embed, img.data)
                except Exception as e:               # noqa: BLE001 - never fatal
                    log.warning(f"avatar embed error: {type(e).__name__}: {e}")
            # Same thread rule as the fingerprint: Pillow decoding is
            # CPU-bound and this task shares its loop with a running sweep.
            generated = None
            if platform:
                try:
                    generated = await asyncio.to_thread(
                        is_generated_avatar, platform, url, img.data,
                    )
                except Exception as e:               # noqa: BLE001 - never fatal
                    log.warning(f"generated-avatar check failed: {type(e).__name__}: {e}")
            return sha, (fp.to_dict() if fp else None), vec, generated
        except (ImageFetchError, asyncio.TimeoutError):
            if attempt < retries:
                await asyncio.sleep(1.2)
                continue
            return None, None, None, None
        except Exception as e:                       # noqa: BLE001 - never fatal
            log.warning(f"avatar fetch/store error: {type(e).__name__}: {e}")
            return None, None, None, None
    return None, None, None, None


async def cache_for_profiles(
    client_id: str, platform: str, items: Iterable[dict],
) -> int:
    """Cache the pictures for a batch of just-saved discovery rows and write
    each resulting digest back onto its profile. Returns how many landed.

    `items` are the same dicts handed to `profile_repository.save_many`, so
    the caller passes what it already built rather than re-deriving it.

    Never raises. A failure here must not fail a sweep that has already
    found and saved its profiles -- the pictures are an enhancement to rows
    that are correct without them.
    """
    targets: list[tuple[str, str, str]] = []       # (url, entity_id, image_url)
    seen: set[str] = set()
    for it in items:
        img = (it.get("profile_image_url") or "").strip()
        url = (it.get("url") or "").strip()
        if not img or not url:
            continue
        # STOCK AVATARS ARE CACHED TOO, deliberately -- reversing an earlier
        # decision, for two reasons that have both changed.
        #
        # The old reason not to was that storing one would make "has no
        # picture" indistinguishable from "has a picture we cached". That is
        # no longer how absence is expressed: `has_custom_pic` is tri-state
        # now (True real / False stock / None never looked, see
        # shared/models/hit.py), so the verdict lives in its own field and
        # does not have to be inferred from whether bytes exist.
        #
        # The cost objection is also weaker than it looks. The avatar store
        # is content-addressed by sha256 (avatar_repository), so every
        # profile wearing the same silhouette shares ONE stored object --
        # thousands of rows, a single blob.
        #
        # And it buys the thing that matters: a card renders what Facebook
        # renders. A stock avatar served from a signed CDN URL expires like
        # any other, so without the cached bytes those cards go blank
        # overnight and show the initial-letter fallback instead of the
        # silhouette the platform actually shows.
        key = f"{url}\n{img}"
        if key in seen:
            continue
        seen.add(key)
        targets.append((url, (it.get("entity_id") or "").strip(), img))

    if not targets:
        return 0

    # Avoid re-fetching avatars for profiles that already have this EXACT picture
    # cached in MongoDB (e.g. from an earlier keyword or previous sweep). If the
    # account changed its profile picture, the asset path differs, so we re-fetch,
    # re-hash, and re-evaluate logo similarity.
    try:
        cached_map = await profiles_db.existing_avatar_urls(
            client_id, platform, [t[0] for t in targets],
        )
        if cached_map:
            targets = [t for t in targets if not is_same_avatar_asset(cached_map.get(t[0], ""), t[2])]
    except Exception as e:
        log.warning(f"avatar cache lookup error: {type(e).__name__}: {e}")

    if not targets:
        return 0

    # Asked ONCE per batch, before any CPU is spent: a client with no
    # reference logos never pays for the embedding tier.
    try:
        want_embedding = await logos_db.has_any(client_id) and embedding_available()
    except Exception:                            # noqa: BLE001 - never fatal
        want_embedding = False

    sem = asyncio.Semaphore(CONCURRENCY)
    stored = 0
    # What landed, keyed by profile URL, so the logo pass below runs over
    # this batch without re-reading anything from Mongo.
    landed: dict[str, tuple[str, Optional[dict], Optional[list]]] = {}

    async def one(url: str, entity_id: str, image_url: str) -> None:
        nonlocal stored
        async with sem:
            sha, fp, vec, generated = await cache_one(
                image_url, want_embedding=want_embedding, platform=platform,
            )
        if not sha:
            return
        try:
            ok = await profiles_db.set_avatar_sha(
                client_id, platform, sha, url=url, entity_id=entity_id,
            )
            # WHAT THE SWEEP COULD NOT KNOW. Discovery decides `has_logo`
            # from the URL alone, because it must never download an image.
            # That is right for a stock asset with a fixed id, and blind to
            # a picture the platform DREW for this one account: Facebook's
            # letter avatar is served from the ordinary profile-picture
            # path with a per-account id and is indistinguishable from a
            # real upload until you look at the pixels. 121 of this repo's
            # Facebook rows were scored as real pictures on that basis.
            #
            # Correcting it here costs the sweep nothing -- these bytes were
            # already fetched and decoded a few lines up -- and it corrects
            # the ONE record that matters, because analysis now inherits
            # this verdict rather than re-deriving it (analysis/runner.py).
            #
            # Only ever writes False, never True: `generated is False` from
            # a platform with no rule would be a guess, and this must not be
            # able to promote an unknown into a claim.
            if generated is True:
                await profiles_db.set_has_logo(
                    client_id, platform, False, url=url, entity_id=entity_id,
                )
            if fp:
                await profiles_db.set_avatar_fingerprint(
                    client_id, platform, fp["phash"], fp["dhash"],
                    url=url, entity_id=entity_id, embedding=vec,
                )
        except Exception as e:                   # noqa: BLE001 - never fatal
            log.warning(f"avatar sha write failed: {type(e).__name__}: {e}")
            return
        landed[url] = (sha, fp, vec)
        if ok:
            stored += 1

    try:
        await asyncio.wait_for(
            asyncio.gather(*(one(u, e, i) for u, e, i in targets), return_exceptions=True),
            timeout=BATCH_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        # Whatever finished has already been written -- each task persists
        # its own result as it lands rather than at the end of the batch --
        # so a timeout costs the stragglers, not the batch.
        log.warning(f"avatar cache batch timed out after {BATCH_TIMEOUT_SEC}s")

    # Compare what just landed against the client's reference logos. Pure
    # arithmetic over stored hashes (~4us a comparison), and a no-op for a
    # client that has uploaded none -- which is every client until an
    # analyst opts in. Isolated: a fault here must not lose the avatars this
    # function has already cached.
    try:
        scored = []
        for it in items:
            u = (it.get("url") or "").strip()
            if u in landed:
                scored.append({**it, "avatar_sha": landed[u][0],
                               "avatar_fingerprint": landed[u][1],
                               "avatar_embedding": landed[u][2]})
        if scored:
            hits = await logo_match.match_profiles(client_id, platform, scored)
            if hits:
                log.info(f"logo match: {hits} profile(s) matched a reference logo")
    except Exception as e:                       # noqa: BLE001 - never fatal
        log.warning(f"logo matching failed: {type(e).__name__}: {e}")

    return stored


def spawn(client_id: str, platform: str, items: list[dict]) -> Optional[asyncio.Task]:
    """Fire the batch off behind the caller and hand back the task.

    The task is RETURNED rather than discarded so a job can keep hold of it
    and let it finish; a bare `create_task` with no reference can be garbage
    collected mid-flight, which would silently cache nothing under exactly
    the load where it matters most.
    """
    if not items:
        return None
    return asyncio.create_task(cache_for_profiles(client_id, platform, items))
