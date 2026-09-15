"""Fetching a remote profile picture, safely. One implementation, two callers.

USED BY
    backend/api/media.py            the live proxy route (fetch and re-serve)
    backend/services/avatar_cache.py  the durable store (fetch once, keep)

WHY IT IS SHARED RATHER THAN COPIED. The host allowlist, the https-only
rule and the response size cap are load-bearing SECURITY controls, not
tidiness: without the allowlist anything the server can reach is one URL
away from any caller -- cloud metadata endpoints (169.254.169.254), Mongo's
own host, everything else inside the network perimeter. A second copy of
that logic is a second place for it to drift out of date, and the copy that
drifts is the one that becomes an SSRF relay. There is one copy.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import aiohttp

from backend.shared.logging import get_logger

log = get_logger("imagefetch")

# Suffix-matched against the PARSED hostname, never against the raw string:
# a substring test would let `notfbcdn.net.attacker.com` through, and a
# check on the URL text would be fooled by `https://evil.com/?x=.fbcdn.net`.
# Each entry matches the bare apex too (`fbcdn.net` as well as `*.fbcdn.net`).
ALLOWED_HOST_SUFFIXES = (
    ".fbcdn.net",           # facebook (scontent.*) and instagram (instagram.*)
    ".cdninstagram.com",    # instagram, when Meta routes it off the shared CDN
    ".twimg.com",           # twitter/X -- pbs.twimg.com, abs.twimg.com
    ".ggpht.com",           # youtube channel avatars -- yt3.ggpht.com
    ".googleusercontent.com",   # youtube's other avatar host
    ".ytimg.com",           # youtube -- i.ytimg.com, yt3.ytimg.com
    ".licdn.com",           # linkedin
    ".tiktokcdn.com",       # tiktok
    ".tiktokcdn-us.com",
    ".t.me",                # telegram web userpics (t.me/i/userpic/…)
    ".telegram.org",        # telegram CDN
    ".telesco.pe",          # telegram CDN mirror
)

# Avatars are thumbnails -- Instagram's measure 5-9 KB, and a full-resolution
# Facebook upload (what hd_picture_url asks for) is well under a megabyte.
# 8 MB is generous headroom that still refuses to let this be used to pull
# arbitrarily large files through the server.
MAX_BYTES = 8 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)


class ImageFetchError(Exception):
    """A fetch that did not produce usable image bytes. `status` is the
    status the API layer should answer with, not the upstream's own."""

    def __init__(self, detail: str, status: int = 502) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class FetchedImage:
    data: bytes
    content_type: str


_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()


IMAGE_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}


async def client() -> aiohttp.ClientSession:
    """One shared session for the process. A per-request session would mean
    a fresh TLS handshake for every avatar on a 25-card page."""
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession(timeout=_TIMEOUT, headers=IMAGE_FETCH_HEADERS)
    return _session


async def close() -> None:
    """Called from main.py's lifespan shutdown, alongside the Mongo close."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


# HOW LONG A SIGNED CDN URL HAS LEFT.
#
# Meta signs every picture URL and stamps the expiry into `oe=`, as a hex
# unix timestamp. Knowing it turns "this fetch failed" into two very
# different facts: a URL that is still valid and failed is a transient
# problem worth retrying, and one that has expired can never be fetched
# again from that URL by anybody, so retrying is pure waste and the picture
# has to be re-discovered instead.
#
# MEASURED, NOT ASSUMED. The comment this replaces said fbcdn signs "hours
# not days". Against the stored data the real window is 110-307 hours --
# four and a half to nearly thirteen days. That difference is the whole
# reason a background retry is a complete fix rather than a race: there is
# no realistic outage that outlasts a week of hourly retries.
_OE_RE = re.compile(r"[?&]oe=([0-9A-Fa-f]{1,16})")


def signed_expiry(raw: str) -> Optional[float]:
    """Unix seconds when this URL stops working, or None when it carries no
    expiry stamp (YouTube, Twitter and Telegram URLs do not sign, and a
    data: URI has no lifetime at all)."""
    m = _OE_RE.search(raw or "")
    if not m:
        return None
    try:
        return float(int(m.group(1), 16))
    except ValueError:
        return None


def url_is_live(raw: str, now: Optional[float] = None) -> bool:
    """Is this URL still fetchable, as far as its own signature says?

    Unsigned URLs answer True: they have no stated lifetime, so the only
    way to know is to try, and trying is what the caller wants. Only a
    signature that has demonstrably passed answers False.
    """
    exp = signed_expiry(raw)
    if exp is None:
        return True
    return exp > (now if now is not None else time.time())


def allowed(raw: str) -> bool:
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
    return any(host == s[1:] or host.endswith(s) for s in ALLOWED_HOST_SUFFIXES)


async def fetch_image(raw: str) -> FetchedImage:
    """The bytes at `raw`, or ImageFetchError. Never returns a partial body.

    No cookies and no referer: the CDNs need neither (verified against the
    live hosts), and sending either would hand a third party more than the
    fetch requires.
    """
    from urllib.parse import urljoin

    cur_url = raw
    max_redirects = 3
    session = await client()
    for _ in range(max_redirects + 1):
        if not allowed(cur_url):
            # 400, not 403: from the caller's side this is a malformed request.
            # The detail deliberately does not echo the URL back.
            raise ImageFetchError("url host is not an allowlisted image CDN", 400)

        host = urlparse(cur_url).hostname
        try:
            async with session.get(cur_url, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    if not loc:
                        raise ImageFetchError("upstream redirected without Location header")
                    cur_url = urljoin(cur_url, loc)
                    continue

                if resp.status != 200:
                    log.warning(f"avatar upstream {resp.status} from {host}")
                    raise ImageFetchError("upstream image fetch failed")

                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if not ctype.startswith("image/"):
                    # An allowlisted host answering with non-image content means
                    # the URL pointed at something that is not an avatar.
                    raise ImageFetchError("upstream did not return an image")

                # MUST loop. `resp.content.read(n)` returns UP TO n bytes -- in
                # practice whatever is in the first chunk off the socket -- NOT n
                # bytes. Using it directly served a 1-byte "image" for a 110 KB
                # Facebook avatar: a 200 with a valid image/jpeg content-type and
                # a truncated, undecodable body, so the browser fired onerror and
                # the card fell back to its letter circle exactly as if the fetch
                # had failed. Small avatars arrived in one chunk and hid it.
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(_CHUNK_BYTES):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ImageFetchError("upstream image too large")
                    chunks.append(chunk)
                body = b"".join(chunks)
                if not body:
                    raise ImageFetchError("upstream returned an empty image")
                return FetchedImage(data=body, content_type=ctype)
        except ImageFetchError:
            raise
        except asyncio.TimeoutError:
            raise ImageFetchError("upstream image fetch timed out", 504)
        except aiohttp.ClientError as e:
            log.warning(f"avatar fetch failed for {host}: {type(e).__name__}")
            raise ImageFetchError("upstream image fetch failed")

    raise ImageFetchError("too many redirects", 400)
