"""Telling a platform's generic placeholder avatar apart from a picture the
account holder actually chose.

WHY IT MATTERS. `has_logo` is the heaviest single input to the risk rubric
(shared/models/scoring.py: a logo match alone outweighs location and
dormancy combined, and it sets priority outright). Reading a stock
silhouette as "this account is using the brand's photo" is therefore the
most expensive mistake this pipeline can make -- and it was making it on
172 of this repo's own rows before this module existed (168 Facebook, 4
Instagram), every one of them scored as though the account had chosen the
picture.

HOW EACH PLATFORM IS DECIDED, and how confident that is. Established by
fetching and hashing all 2325 stored avatars, then looking at every image
served to three or more unrelated accounts:

  telegram   AUTHORITATIVE. Telethon reports no photo object at all.
             Nothing to guess -- handled in that engine, not here.
  twitter    EXACT. One fixed asset, `default_profile*.png` under
             /sticky/default_profile. Already detected correctly.
  facebook   TWO KINDS, and they need different answers. The shared
             stock assets are EXACT and stable-id: the grey silhouette
             (104 of our rows) and the illustrated default GROUP avatar
             (63 rows, shared by 55 unrelated names -- a pastor's group,
             a real-estate group, a book page). But Facebook ALSO draws a
             per-account letter avatar, which has a per-account id and no
             URL tell at all -- see below, it needs the image.
  instagram  EXACT, and was being MISSED. The anonymous avatar has a
             stable id -- but Instagram ROTATED it. The id this codebase
             checked for (44884218_345707102882519_...) is the old one;
             the live asset is 573323465_1219825463302212_... Both are
             kept below, since old rows still carry the old URL.
  youtube    NOT decidable from the URL -- see below.
  tiktok     NOT HANDLED HERE, and deliberately so: that engine documents
             that TikTok omits the avatar entirely for a default-picture
             account rather than serving a stock URL, so presence of an
             avatar already IS the signal. No rows were available to
             verify a marker against, and an unverified marker is worse
             than none.

WHY YOUTUBE AND FACEBOOK NEED THE IMAGE ITSELF. Neither serves one shared
placeholder for this case. Both GENERATE a per-account avatar -- one letter
on a solid colour, varying by letter and colour, delivered from the same
host and the same URL shape as a real upload -- so no URL rule can see it,
and `looks_like_placeholder` returns False on every one of them.

Facebook's is decided by its PALETTE plus flatness (see
FACEBOOK_GENERATED_BG and `_facebook_generated`): the ground is one of
eight exact colours it draws, which no amount of URL inspection reveals but
which the pixels state outright. Audited over all 4095 distinct stored
Facebook avatars -- 122 flagged, every one of them genuinely generated, and
121 of those had been scored as real pictures.

YouTube's needs a different pair of signals. Measured over
960 stored YouTube avatars:

  * no URL discriminator exists. `/ytc/AIdro_` covers the generated ones
    but also 110 real photographs -- 60% precision, unusable alone.
  * a flat-colour test alone is not enough either: a real minimalist logo
    (black background, white monogram) is structurally identical to a
    generated letter avatar, and a dark blurry photograph also reads flat.

The two signals TOGETHER are precise: `/ytc/AIdro_` and a two-colour share
above FLAT_THRESHOLD flagged 164 of 960, and a random sample of 30 audited
by eye was 30/30 genuine generated avatars.

BOTH need the image bytes, so both live in `is_generated_avatar()` below,
and the caller is `services/avatar_cache.py` -- which has already fetched
and decoded the picture, making the verdict free -- never a discovery
sweep, which must not download images at all.

DELIBERATELY CONSERVATIVE. Every rule here is precision-first: it only
says "placeholder" where the evidence is a fixed asset or two agreeing
signals. Anything else returns None, meaning "unknown", NOT "real" -- the
13 YouTube avatars that read flat without the `AIdro_` marker are left
alone rather than risk calling someone's real monogram a placeholder.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from typing import Optional

# Substrings that identify a platform's own stock avatar asset. These are
# asset IDS, not CDN path tags: the tag `t1.30497-1` was tried and is wrong
# (facebook/discovery_engine.py documents why -- it is also a rendering
# context for genuine uploads), whereas the numeric asset id is the file
# itself and cannot collide with a real photo.
PLACEHOLDER_MARKERS: dict[str, tuple[str, ...]] = {
    "facebook": (
        # grey silhouette
        "453178253_471506465671661_2781666950760530985_n",
        # illustrated default GROUP avatar
        "116687302_959241714549285_318408173653384421_n",
        "730584813_122095914603376682_2911865549814502283_n",
        # older static-chrome silhouettes, kept from the original engine rule
        "rsrc.php",
        "/static.xx",
    ),
    "instagram": (
        # current anonymous avatar (live as of 2026-09)
        "573323465_1219825463302212_7278921664109726296_n",
        # the one this codebase used to check for, still on older rows
        "44884218_345707102882519_2446069589734326272_n",
        "anonymousUser",
        "default_profile",
    ),
    "twitter": (
        "default_profile_normal.png",
        "default_profile_400x400.png",
        "default_profile.png",
        "/sticky/default_profile",
    ),
}

# YouTube's generated avatars all sit under this path prefix. On its own it
# is only 60% precise (real photographs live there too), so it is never used
# without the flatness test as well.
YOUTUBE_GENERATED_PREFIX = "/ytc/AIdro_"

# Share of the image held by its two most common quantised colours. A
# generated letter avatar is one flat background plus one glyph and scores
# ~0.94-1.00; a photograph, even a dark one, sits far below.
FLAT_THRESHOLD = 0.93


def looks_like_placeholder(platform: str, url: str) -> bool:
    """Is this URL one of `platform`'s own stock avatars?

    URL-only, so it costs nothing and is safe to call from discovery's bulk
    path. False means "not a KNOWN placeholder", which is not the same as
    "definitely a real picture" -- see `is_generated_avatar` for YouTube,
    where no URL answer exists.
    """
    if not url:
        return False
    markers = PLACEHOLDER_MARKERS.get(platform, ())
    return any(m in url for m in markers)


# Facebook's GENERATED avatar backgrounds, sampled exactly. Like YouTube,
# Facebook draws a per-account letter avatar for an entity with no uploaded
# picture -- one glyph on a solid ground -- and serves it from the ordinary
# `t39.30808-1` profile-picture path with a per-account asset id, so no URL
# rule can see it. Unlike YouTube, the ground is drawn from a small FIXED
# palette, which is what makes this decidable rather than merely likely.
#
# Established by decoding all 4095 distinct stored Facebook avatars and
# taking each one's modal colour. Among the 275 that are flat, the modal
# colour collapses onto these eight and nothing else -- every remaining flat
# image is a real logo on white (77) or black (22) or some brand colour.
# Recorded at full precision because they are drawn, not photographed: the
# same eight RGB triples repeat exactly across hundreds of accounts.
FACEBOOK_GENERATED_BG: tuple[tuple[int, int, int], ...] = (
    (135, 214, 228),   # cyan
    (208, 148, 217),   # lilac
    (244, 131, 125),   # salmon
    (99, 163, 242),    # blue
    (255, 220, 135),   # amber
    (130, 132, 135),   # grey
    (255, 171, 111),   # orange
    (152, 214, 109),   # green
)

# Facebook's own flatness bar, NOT shared with YouTube's. YouTube's 0.93 was
# tuned against a different question -- there flatness is doing most of the
# discriminating, because `/ytc/AIdro_` is only 60% precise on its own. Here
# the palette is the precise signal and flatness only has to exclude
# photographs, so the bar sits where the data actually separates.
#
# Sorted by flatness, the palette-background images run: ... 0.9062, 0.9009,
# then a gap, then 0.8813 (still generated), then 0.8674 -- an "ellex"
# wordmark on a grey disc -- and 0.8386, a screenshot of a payment
# confirmation. Those last two are real pictures. 0.90 clears the nearest of
# them by 0.034 and matches exactly the 122-image set audited one by one.
#
# It costs four generated avatars sitting at 0.8813, and that is the trade
# taken deliberately: a miss leaves a stale Yes, which is the status quo,
# while a false positive calls a real logo a placeholder and under-scores a
# genuine impersonation. 0.88 would catch those four with only 0.013 of
# margin -- too thin to spend a real logo on.
FACEBOOK_FLAT_THRESHOLD = 0.90

# Room for JPEG re-encoding of a drawn flat colour, and nothing more. These
# are not photographed colours that need a wide catchment; a real photograph
# landing within 10/255 of a palette entry AND reading as two flat colours
# is the case the flatness test is there to exclude.
_BG_TOLERANCE = 10


def _dominant_pixels(image_bytes: bytes) -> Optional[tuple[tuple[int, int, int], float]]:
    """The image's modal colour and the share of the image it covers, or
    None if the bytes are not a readable image.

    NOT quantised, unlike `_two_colour_share`. That function is asking "is
    this image flat", where 4-bit quantisation usefully absorbs JPEG noise.
    This one is asking "is this ground one of eight exact colours Facebook
    drew", and quantising to 16-value buckets would merge palette entries
    with their neighbours and throw away the precision that makes the answer
    exact.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a hard dep of analysis
        return None
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        im.thumbnail((48, 48))
        data = list(im.get_flattened_data() if hasattr(im, "get_flattened_data")
                    else im.getdata())
    except Exception:
        return None
    if not data:
        return None
    colour, n = Counter(data).most_common(1)[0]
    return colour, n / len(data)


def _is_facebook_palette_bg(colour: tuple[int, int, int]) -> bool:
    return any(
        all(abs(a - b) <= _BG_TOLERANCE for a, b in zip(colour, entry))
        for entry in FACEBOOK_GENERATED_BG
    )


def _two_colour_share(image_bytes: bytes) -> Optional[float]:
    """How much of the image its two commonest colours account for, or None
    if the bytes are not a readable image. Quantised to 4 bits per channel
    so JPEG noise around a glyph does not read as detail."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a hard dep of analysis
        return None
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        im.thumbnail((64, 64))
        data = (im.get_flattened_data() if hasattr(im, "get_flattened_data")
                else im.getdata())
        quantised = [(r // 16, g // 16, b // 16) for r, g, b in data]
    except Exception:
        return None
    if not quantised:
        return None
    counts = Counter(quantised)
    return sum(n for _, n in counts.most_common(2)) / len(quantised)


def is_generated_avatar(platform: str, url: str, image_bytes: bytes) -> Optional[bool]:
    """True when this is a platform-GENERATED placeholder rather than a
    picture anyone chose; None when the evidence does not settle it.

    Facebook and YouTube need this -- every other platform is answered by
    `looks_like_placeholder` from the URL alone. Requires the image, so call
    it from `avatar_cache`, which has the bytes already, and never from a
    discovery sweep.

    Returns None rather than False on an unreadable image or an unhandled
    platform, so a caller can leave `has_custom_pic` unset instead of
    recording a guess as a fact.
    """
    if platform == "facebook":
        return _facebook_generated(image_bytes)
    if platform != "youtube":
        return None
    if YOUTUBE_GENERATED_PREFIX not in (url or ""):
        # A real upload, or an older generated avatar we cannot confirm.
        # Deliberately not False: absence of the marker is not evidence of a
        # real picture.
        return None
    share = _two_colour_share(image_bytes)
    if share is None:
        return None
    return share >= FLAT_THRESHOLD


def _facebook_generated(image_bytes: bytes) -> Optional[bool]:
    """TWO AGREEING SIGNALS, the same shape as the YouTube rule and for the
    same reason: neither one alone is safe.

    Flatness alone would condemn real logos. 153 of this repo's stored
    Facebook avatars read as two flat colours and are genuine brand marks --
    a white monogram on black, a navy swoosh on white, a wordmark on a solid
    brand colour. Those are pictures the account chose and must stay Yes.

    The palette alone would condemn photographs. 22 stored avatars have a
    modal colour within tolerance of a palette entry and are ordinary
    photographs that happen to be mostly sky or mostly skin.

    Together they were exact on the whole stored population: 122 flagged,
    audited by eye one by one, 122 of them genuine generated letter avatars
    (121 were being scored as real pictures before this). No genuine logo
    was flagged, and no photograph.

    Returns None, never False, when the evidence does not settle it -- so a
    caller leaves `has_custom_pic` at whatever the URL rule decided rather
    than recording a guess. A palette Facebook adds later is therefore a
    MISS (a stale Yes), never a false No, which is the direction this module
    is built to fail in.
    """
    share = _two_colour_share(image_bytes)
    if share is None or share < FACEBOOK_FLAT_THRESHOLD:
        return None
    dominant = _dominant_pixels(image_bytes)
    if dominant is None:
        return None
    colour, _ = dominant
    if not _is_facebook_palette_bg(colour):
        # Flat, but on a ground Facebook does not draw. A real logo.
        return None
    return True


# fbcdn signs the whole crop range up to `cstp`'s bound, not the specific
# `ctp` size actually requested. Meta profile-picture URLs (Facebook and Instagram alike)
# default to tiny crops (e.g. 50x50 or 150x150) while cstp carries the real uploaded
# photo's native size. Raising ctp to match cstp, with signature tokens untouched,
# yields the full-resolution upload rather than the thumbnail.
_CTP = re.compile(r"ctp=s\d+x\d+")
_CSTP = re.compile(r"cstp=mx(\d+)x(\d+)")
_STP_SIZE = re.compile(r"([sp])\d+x\d+")


_TWITTER_THUMB = re.compile(r"_(normal|mini|bigger)\.([a-zA-Z0-9]+)")
_YOUTUBE_SIZE = re.compile(r"=s\d+")
_TELEGRAM_USERPIC = re.compile(r"/userpic/(?:160|320)/")

# Hostname fragments for quick matching without a full URL parse.
_YOUTUBE_HOSTS = ("ggpht.com", "googleusercontent.com", "ytimg.com")
_TELEGRAM_HOSTS = ("t.me", "telegram.org", "telesco.pe")


def hd_picture_url(url: str) -> str:
    """Rewrites a platform CDN photo URL to request the highest available
    resolution instead of the default low-res thumbnail.

    Supports Meta (Facebook / Instagram), Twitter / X, YouTube, and Telegram.
    Each platform's rewrite is independent: Meta uses the signed crop
    parameters already in the URL, the others are simple suffix / path
    substitutions that the CDN accepts without a new signature.
    """
    if not url:
        return url

    # -- Meta (Facebook / Instagram) --
    cstp = _CSTP.search(url)
    if cstp:
        w, h = cstp.group(1), cstp.group(2)
        if _CTP.search(url):
            url = _CTP.sub(f"ctp=s{w}x{h}", url)
        url = _STP_SIZE.sub(lambda m: f"{m.group(1)}{w}x{h}", url)
        return url

    # -- Twitter / X  (pbs.twimg.com) --
    # Default search payloads and DOM images carry `_normal.jpg` (48×48).
    # The CDN honours `_400x400` for the same asset id.
    if _TWITTER_THUMB.search(url):
        url = _TWITTER_THUMB.sub(r"_400x400.\2", url)
        return url

    # -- YouTube (yt3.ggpht.com / *.googleusercontent.com / *.ytimg.com) --
    # API search thumbnails are =s88 (88×88); =s800 is the max the CDN serves.
    if any(h in url for h in _YOUTUBE_HOSTS) and _YOUTUBE_SIZE.search(url):
        url = _YOUTUBE_SIZE.sub("=s800", url)
        return url

    # -- Telegram (t.me/i/userpic/320/…) --
    # Web avatar URLs default to 320×320; 640 is the largest available.
    if any(h in url for h in _TELEGRAM_HOSTS) and _TELEGRAM_USERPIC.search(url):
        url = _TELEGRAM_USERPIC.sub("/userpic/640/", url)
        return url

    return url


def extract_instagram_hd_avatar(node: dict) -> str:
    """Extracts the highest-resolution avatar URL available from an Instagram user node."""
    if not isinstance(node, dict):
        return ""

    candidates: list[str] = []

    # 1. Mobile API v1 hd_profile_pic_url_info dict (primary for mobile search)
    hd_info = node.get("hd_profile_pic_url_info")
    if isinstance(hd_info, dict) and hd_info.get("url"):
        candidates.append(str(hd_info["url"]).strip())

    # 2. hd_profile_pic_versions list (sorted by width descending)
    versions = node.get("hd_profile_pic_versions")
    if isinstance(versions, list) and versions:
        sorted_versions = sorted(
            [v for v in versions if isinstance(v, dict) and v.get("url")],
            key=lambda v: int(v.get("width") or 0),
            reverse=True,
        )
        if sorted_versions:
            candidates.append(str(sorted_versions[0]["url"]).strip())

    # 3. Direct hd field or standard field (GraphQL / web-search topsearch)
    if node.get("profile_pic_url_hd"):
        candidates.append(str(node["profile_pic_url_hd"]).strip())
    if node.get("profile_pic_url"):
        candidates.append(str(node["profile_pic_url"]).strip())

    for c in candidates:
        if c:
            return hd_picture_url(c)
    return ""

