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

Only Instagram needs this today. The allowlist deliberately covers the whole
Meta CDN family anyway: which host serves a given avatar is Meta's routing
decision rather than ours (the same picture can come back on `fbcdn.net` or
`cdninstagram.com`, on a shared host or an ISP-local `*.fna.*` cache node),
and the header they attach is theirs to change. Proxying a Facebook avatar
that would have loaded directly costs one extra hop; NOT proxying an
Instagram one costs a broken card.

NOT A GENERAL-PURPOSE PROXY. `url` is matched against a host allowlist
before a single byte is fetched. Without that check this route would be an
open SSRF relay -- anything the server can reach, including cloud metadata
endpoints (169.254.169.254), Mongo's own host and everything else inside the
network perimeter, would be one query parameter away from any caller. The
allowlist, the https-only rule and the response size cap are load-bearing
security controls, not tidiness.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

import aiohttp
from fastapi import APIRouter, Query, Response

from backend.shared.logging import get_logger

router = APIRouter(tags=["media"])
log = get_logger("media")

# Suffix-matched against the parsed hostname, never against the raw string:
# a substring test would let `notfbcdn.net.attacker.com` through, and a
# check on the URL text would be fooled by `https://evil.com/?x=.fbcdn.net`.
# Each entry matches the bare apex too (`fbcdn.net` as well as `*.fbcdn.net`).
_ALLOWED_HOST_SUFFIXES = (".fbcdn.net", ".cdninstagram.com")

# Avatars are thumbnails -- the Instagram ones measured here are 5-9 KB, and
# the largest `profile_pic_url_hd` variant is well under a megabyte. 8 MB is
# generous headroom that still refuses to let this endpoint be used to pull
# arbitrarily large files through the server.
_MAX_BYTES = 8 * 1024 * 1024
_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)

# A Meta CDN node briefly refusing connections is routine (one of the
# `*.fna.*` nodes did exactly that while this was being diagnosed), so the
# browser cache is what keeps a card's picture stable across re-renders and
# tab switches rather than re-fetching on every paint. Six hours is well
# inside the signed URL's own lifetime -- Instagram's `oe=` parameter is
# typically ~3 days out -- so a cached copy never outlives the signature
# that would let us refresh it.
_CACHE_CONTROL = "public, max-age=21600"

_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()


async def _client() -> aiohttp.ClientSession:
    """One shared session for the process. A per-request session would mean
    a fresh TLS handshake for every avatar on a 25-card page."""
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession(timeout=_TIMEOUT)
    return _session


async def close() -> None:
    """Called from main.py's lifespan shutdown, alongside the Mongo close."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _allowed(raw: str) -> bool:
    try:
        p = urlparse(raw)
    except ValueError:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    # Anything but the default TLS port is a sign of a hand-built URL aimed
    # at something other than a CDN.
    try:
        if p.port not in (None, 443):
            return False
    except ValueError:      # malformed port, e.g. "https://host:notaport/"
        return False
    host = p.hostname.lower()
    return any(host == s[1:] or host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES)


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
    """Fetch `url` server-side and return the bytes from this origin."""
    if not _allowed(url):
        # 400, not 403: from the browser's side this is a malformed request
        # for this endpoint. The detail deliberately does not echo the URL
        # back into the response.
        return _err(400, "url host is not an allowlisted image CDN")

    host = urlparse(url).hostname
    try:
        session = await _client()
        # No cookies, no referer -- the CDN needs neither (verified against
        # the live hosts), and sending either would hand a third party more
        # than the fetch requires.
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                log.warning(f"avatar upstream {resp.status} from {host}")
                return _err(502, "upstream image fetch failed")

            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                # An allowlisted host answering with non-image content means
                # the URL pointed at something that is not an avatar.
                return _err(502, "upstream did not return an image")

            # Read one byte past the cap so an oversized body is rejected
            # rather than silently truncated into a corrupt image.
            body = await resp.content.read(_MAX_BYTES + 1)
            if len(body) > _MAX_BYTES:
                return _err(502, "upstream image too large")
    except asyncio.TimeoutError:
        return _err(504, "upstream image fetch timed out")
    except aiohttp.ClientError as e:
        log.warning(f"avatar fetch failed for {host}: {type(e).__name__}")
        return _err(502, "upstream image fetch failed")

    return Response(
        content=body,
        media_type=ctype,
        headers={
            "Cache-Control": _CACHE_CONTROL,
            # The whole point of this route. Set explicitly rather than left
            # to the default, because the UI is not always same-origin with
            # this API -- see VITE_API_BASE_URL in frontend/src/api/httpClient.ts.
            "Cross-Origin-Resource-Policy": "cross-origin",
        },
    )
