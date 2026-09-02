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
  facebook   EXACT, and was being MISSED. Two stock assets, both stable
             ids: the grey silhouette (104 of our rows) and the
             illustrated default GROUP avatar (63 rows, shared by 55
             unrelated names -- a pastor's group, a real-estate group,
             a book page).
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

WHY YOUTUBE IS DIFFERENT, and why it needs the image itself. YouTube does
not serve one shared placeholder. It GENERATES a per-channel avatar: one
letter on a solid colour, varying by both letter and colour, delivered
from the same host and the same URL shape as a real upload. Measured over
960 stored YouTube avatars:

  * no URL discriminator exists. `/ytc/AIdro_` covers the generated ones
    but also 110 real photographs -- 60% precision, unusable alone.
  * a flat-colour test alone is not enough either: a real minimalist logo
    (black background, white monogram) is structurally identical to a
    generated letter avatar, and a dark blurry photograph also reads flat.

The two signals TOGETHER are precise: `/ytc/AIdro_` and a two-colour share
above FLAT_THRESHOLD flagged 164 of 960, and a random sample of 30 audited
by eye was 30/30 genuine generated avatars. That needs the image bytes, so
it is `is_generated_avatar()` below and belongs in analysis, which already
pays a per-profile cost -- never in discovery, which is bulk.

DELIBERATELY CONSERVATIVE. Every rule here is precision-first: it only
says "placeholder" where the evidence is a fixed asset or two agreeing
signals. Anything else returns None, meaning "unknown", NOT "real" -- the
13 YouTube avatars that read flat without the `AIdro_` marker are left
alone rather than risk calling someone's real monogram a placeholder.
"""

from __future__ import annotations

import io
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

    Only YouTube needs this -- every other platform is answered by
    `looks_like_placeholder` from the URL alone. Requires the image, so call
    it from analysis, never from a discovery sweep.

    Returns None rather than False on an unreadable image or an unhandled
    platform, so a caller can leave `has_custom_pic` unset instead of
    recording a guess as a fact.
    """
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
