"""Re-serves remote profile avatars from our own origin.

WHY THIS EXISTS. Instagram serves profile pictures with the response header
`Cross-Origin-Resource-Policy: same-origin`. CORP is enforced by the
BROWSER, not the server, which is what makes this failure so misleading to
diagnose: curl, httpx and the scrapers all fetch the very same URL and get a
clean `200` with valid JPEG bytes, so the URL looks perfectly healthy from
the server side and is stored happily. Chrome fetches it, sees CORP, and
throws the bytes away before they ever reach the `<img>` -- firing `onerror`
with no status and no console entry. The discovery card then falls back to
its initial-letter circle, so every Instagram profile reads as "has no
picture". Pasting that identical URL into a tab renders it fine, because a
top-level navigation is not a cross-origin subresource embed and CORP does
not apply to it.

Nothing on the client side can lift CORP -- not `referrerPolicy`, not
`crossOrigin`, not a CSS `background-image`, not a `fetch` of any mode (even
`no-cors` fails). The only fix is to stop the browser making the
cross-origin request at all: fetch the image server-side, where CORP has no
meaning, and hand it back from an origin the page is allowed to embed.

MEASURED, per platform, against the live CDNs (see the allowlist below):

    instagram  instagram.*.fna.fbcdn.net   CORP: same-origin   <- blocked
    facebook   scontent.*.fna.fbcdn.net    CORP: cross-origin  ok
    twitter    pbs.twimg.com               CORP: cross-origin  ok
    youtube    yt3.ggpht.com               CORP: cross-origin  ok
    telegram   (stores data: URIs)         never hits network  ok

Only Instagram needs this today, and the client does NOT route everything
through here: it sends known-blocked hosts straight to this route and tries
every other CDN directly first, falling back here only if that fails (see
frontend/src/utils/avatar.ts). That ordering matters -- an earlier version
proxied all of Meta unconditionally and put Facebook's 1300+ working avatars
behind this one code path, where a single bug in it broke every one of them
at once.

The allowlist below is therefore wider than the set of hosts that are
actually blocked: it is the set this route is WILLING to fetch, so that any
platform which starts sending `same-origin` recovers through the client's
fallback with no code change. Which header Meta, Google or Twitter attach is
their decision, not ours.

NOT A GENERAL-PURPOSE PROXY. `url` is matched against a host allowlist
before a single byte is fetched. Without that check this route would be an
open SSRF relay -- anything the server can reach, including cloud metadata
endpoints (169.254.169.254), Mongo's own host and everything else inside the
network perimeter, would be one query parameter away from any caller. The
allowlist, the https-only rule and the response size cap are load-bearing
security controls, not tidiness.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Path, Query, Response


from backend.database.repositories import avatar_repository as avatars_db
from backend.database.repositories import profile_repository as profiles_db
from backend.shared.tasks import spawn
from backend.shared.imagefetch import ImageFetchError, allowed as _allowed
from backend.shared.imagefetch import close as _close_fetcher
from backend.shared.imagefetch import fetch_image
from backend.shared.fast_http import close as _close_fast_http
from backend.shared.logging import get_logger

router = APIRouter(tags=["media"])
log = get_logger("media")

# The allowlist, size cap and fetch loop all live in shared/imagefetch.py --
# this route and the durable avatar store (services/avatar_cache.py) use the
# same one, because a second copy of an SSRF guard is a second place for it
# to drift.

# A Meta CDN node briefly refusing connections is routine (one of the
# `*.fna.*` nodes did exactly that while this was being diagnosed), so the
# browser cache is what keeps a card's picture stable across re-renders and
# tab switches rather than re-fetching on every paint. Six hours is well
# inside the signed URL's own lifetime.
_CACHE_CONTROL = "public, max-age=21600"

# The stored copy is addressed by the sha256 of its own bytes, so it can
# never change under a given URL. That is what makes `immutable` correct
# here and not merely optimistic: a year is the max-age ceiling browsers
# honour, and no revalidation is possible or needed.
_STORED_CACHE_CONTROL = "public, max-age=31536000, immutable"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# THE BYTES ARE A THIRD PARTY'S, SERVED FROM OUR ORIGIN. An allowlisted CDN
# can still answer with an SVG, and an SVG opened directly in a tab runs its
# own scripts with this API's origin. `nosniff` stops a browser guessing a
# different type, and a sandboxing CSP makes any document these bytes turn
# out to be inert. Neither changes how an <img> renders them.
_IMAGE_SAFETY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}


async def close() -> None:
    """Called from main.py's lifespan shutdown. Delegates: the sessions it
    closes belong to shared/imagefetch.py (aiohttp) and shared/fast_http.py
    (curl_cffi), the two clients an avatar fetch can go out through."""
    await _close_fetcher()
    await _close_fast_http()


def _err(status: int, detail: str) -> Response:
    """Errors answer with a status, never a placeholder image: the caller is
    an `<img>` whose `onerror` already falls back to the profile's
    initial-letter circle, and a real status keeps that fallback honest
    instead of painting a broken-image graphic over it."""
    return Response(content=json.dumps({"detail": detail}).encode(),
                    status_code=status, media_type="application/json")


@router.get("/media/avatar",
            summary="Proxy a profile picture past its CDN's CORP header")
async def avatar(
    url: str = Query(..., description="Absolute https URL of the image, on an allowlisted CDN host."),
) -> Response:
    """Fetch `url` server-side, return the bytes -- and KEEP them.

    SERVING A PICTURE IS ALSO THE LAST CHANCE TO SAVE IT. This used to be
    live-only: fetch on demand, serve, discard. That made it the final way
    a picture could still be lost. The proxy downloaded the bytes, the
    analyst looked at the card, and a week later the signed CDN URL expired
    and the same card went blank -- holding nothing, having had the whole
    image in memory.

    So anything this route fetches is stored on the way out and attached to
    whichever profiles are pointing at that URL without a picture of their
    own. The bytes are already paid for; keeping them costs one GridFS
    write, off the response path.

    `/media/avatar/{sha}` is still the route that serves a kept picture.
    This one is how a picture becomes kept.
    """
    if not _allowed(url):
        # 400, not 403: from the browser's side this is a malformed request
        # for this endpoint.
        return _err(400, "url host is not an allowlisted image CDN")
    try:
        img = await fetch_image(url)
    except ImageFetchError as e:
        return _err(e.status, e.detail)

    # BEHIND THE RESPONSE, never in front of it. The analyst is waiting for
    # this image; keeping a copy must not add a database round trip to the
    # time it takes to appear. A failure here costs the copy and never the
    # picture on screen.
    spawn(_keep(url, img.data, img.content_type))

    return Response(
        content=img.data,
        media_type=img.content_type,
        headers={
            "Cache-Control": _CACHE_CONTROL,
            # The whole point of this route. Set explicitly rather than left
            # to the default, because the UI is not always same-origin with
            # this API -- see VITE_API_BASE_URL in frontend/src/api/httpClient.ts.
            "Cross-Origin-Resource-Policy": "cross-origin",
            **_IMAGE_SAFETY_HEADERS,
        },
    )


async def _keep(url: str, data: bytes, content_type: str) -> None:
    """Store these bytes and attach them to any profile still pointing at
    `url` with no picture of its own.

    Content-addressed, so a picture thousands of profiles share is stored
    once and this is a no-op for every one after the first.
    """
    try:
        sha = await avatars_db.store(data, content_type)
        if not sha:
            return
        kept = await profiles_db.keep_avatar_for_image_url(url, sha)
        if kept:
            log.info(
                f"kept a profile picture served through the proxy -- {kept} card(s) "
                f"will now render it from our own store, after its CDN link expires")
    except Exception as e:                           # noqa: BLE001 - never fatal
        log.warning(f"could not keep a proxied picture: {type(e).__name__}: {e}")


@router.get("/media/avatar/{sha}",
            summary="Serve a profile picture from our own store")
async def stored_avatar(
    sha: str = Path(..., description="sha256 of the image bytes, from a profile's `avatar_sha`."),
) -> Response:
    """The cached copy: bytes we pulled from the CDN once and kept.

    WHY THIS EXISTS. The CDN URL a profile was discovered with is signed and
    expires within hours, so a card built on it goes blank overnight -- and
    it cannot be refreshed, because the signature is the very part that
    died. This route answers from our own store and keeps answering.

    404 is a normal answer: caching runs behind the sweep and is best-effort,
    so a very recently discovered profile may not have its bytes yet. The
    client falls back to the live URL, then to the initial-letter circle.
    """
    if not _SHA256.match(sha or ""):
        return _err(400, "not a sha256 digest")
    found = await avatars_db.read(sha)
    if not found:
        return _err(404, "no stored image for that digest")
    data, ctype = found
    return Response(
        content=data,
        media_type=ctype,
        headers={
            "Cache-Control": _STORED_CACHE_CONTROL,
            "Cross-Origin-Resource-Policy": "cross-origin",
            **_IMAGE_SAFETY_HEADERS,
        },
    )
