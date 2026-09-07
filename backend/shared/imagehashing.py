"""Perceptual fingerprints for a profile picture, and how close two of them are.

WHAT THIS IS FOR. An impersonator's giveaway is often the picture, not the
name: "Reliance Care Support" scores badly on `name_score` and sinks in
triage while wearing the real logo. Comparing a discovered avatar against a
client's reference logo is what surfaces it.

WHAT A PERCEPTUAL HASH CAN AND CANNOT DO -- read this before trusting it.
It answers "are these two IMAGES near-identical?", NOT "does this image
CONTAIN this logo?". Those are different questions. It reliably catches the
impersonator who downloaded the real profile picture and re-uploaded it
(re-encoded, resized, recompressed). It does NOT catch the same logo placed
on a different background, heavily cropped, recoloured, or occupying a
corner of a larger image -- published evaluations put pHash's tolerance for
crops, rotations and affine transforms as low, which is exactly why
visual-similarity phishing detectors (Phishpedia et al.) use a learned
embedding for that case instead.

So this module is deliberately the CHEAP, HIGH-PRECISION tier of a cascade:

    sha256 equal      -> the identical file, zero false positives
    this module       -> near-identical image, very high precision
    (embedding tier)  -> same logo re-presented        [not built yet]

Being wrong here is expensive in a way being silent is not: a false logo
match is a bogus impersonation claim in front of an analyst, and the rest of
this codebase already chose "blank beats wrong" for the same reason (see
discovery_engine._extract_entity). The thresholds below are therefore set
conservatively, and `MATCH_MAX_DISTANCE` requires BOTH hashes to agree.

PURE AND SYNCHRONOUS ON PURPOSE. Decoding an image is CPU-bound and does not
yield; running it directly on the event loop stalls every coroutine sharing
it -- measured at up to 3.4 SECONDS of stall for one sweep's avatars, which
would slow discovery itself. Callers must wrap these in
`asyncio.to_thread(...)`. Pillow releases the GIL during decode, so that is
not merely safe, it is ~5x faster as well.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional

import imagehash
from PIL import Image, ImageFile

from backend.shared.logging import get_logger

log = get_logger("imagehashing")

# A truncated download should still fingerprint rather than raise -- the
# bytes we have are the bytes an analyst would see rendered.
ImageFile.LOAD_TRUNCATED_IMAGES = True

# Both hashes are 8x8 = 64 bits.
HASH_BITS = 64

# Distance at or below which two images are called the same, required of
# BOTH hashes independently.
#
# WHY BOTH. dHash keys on horizontal gradients and pHash on low-frequency
# DCT structure; they fail differently, so agreement between them is a much
# stronger signal than either alone, and it kills the flat-colour and
# near-blank collisions a single hash produces.
#
# WHERE THE NUMBER COMES FROM. Measured, not guessed. The same logo encoded
# at 96/128/160/240/320/640/1024px across three styles (a solid mark, a
# wordmark, colour blocks) produced these worst-case distances:
#
#       solid mark      8        unrelated pairs, same test:
#       wordmark        4            vs a different logo   26
#       colour blocks  12            vs another logo       32
#                                    vs solid grey         31
#
# So same-logo tops out at 12 and unrelated starts at 26 -- a wide gap, and
# 12 sits at the safe end of it. The 12 came only from downscaling to 96px;
# at 128px and above the worst case is 8, and the live audit measured the
# smallest real Facebook avatar at 159px, so production sits in the easy
# part of that range.
#
# An earlier value of 6 was set by intuition and would have MISSED the solid
# mark at 128px and the colour blocks at 96px -- real matches, rejected.
#
# STILL NOT CALIBRATED ON PRODUCTION DATA. These are synthetic logos; real
# avatars carry photos, faces and gradients that may narrow the gap. Treat
# this as a defensible starting point and re-derive it from the analyst's
# own validate/reject decisions once there are enough of them.
MATCH_MAX_DISTANCE = 12

# Between the two thresholds a candidate is "near" -- ranked up for the
# analyst's eye, never labelled a match. Kept well clear of the 26 where
# unrelated images begin.
NEAR_MAX_DISTANCE = 18


@dataclass(frozen=True)
class ImageFingerprint:
    """The perceptual fingerprints of one image, as hex strings for storage."""

    phash: str
    dhash: str
    width: int
    height: int

    def to_dict(self) -> dict:
        return {"phash": self.phash, "dhash": self.dhash,
                "width": self.width, "height": self.height}


@dataclass(frozen=True)
class HashComparison:
    """How close two fingerprints are. `distance` is the WORSE of the two
    hashes, so a match requires both to agree -- see MATCH_MAX_DISTANCE."""

    phash_distance: int
    dhash_distance: int

    @property
    def distance(self) -> int:
        return max(self.phash_distance, self.dhash_distance)

    @property
    def is_match(self) -> bool:
        return self.distance <= MATCH_MAX_DISTANCE

    @property
    def is_near(self) -> bool:
        return MATCH_MAX_DISTANCE < self.distance <= NEAR_MAX_DISTANCE

    @property
    def similarity(self) -> int:
        """0-100, for ranking and for showing the analyst. Linear in the
        agreeing-bit fraction of the worse hash: 0 distance is 100, and half
        the bits differing (which is what two unrelated images average) is
        50, not something that reads like a partial match."""
        return max(0, min(100, round(100 * (1 - self.distance / HASH_BITS))))


def fingerprint(data: bytes) -> Optional[ImageFingerprint]:
    """Fingerprint raw image bytes, or None if they are not a usable image.

    ONE decode, both hashes: decoding dominates the cost (measured 53.7ms of
    a 71ms total on a 2048px image), so hashing twice off a single decoded
    frame is nearly free while decoding twice would almost double it.

    Converted to RGB first so a palettised PNG, a greyscale JPEG and a
    transparent PNG all fingerprint on the same footing -- otherwise the
    same logo saved two ways would score as two different images. A
    transparent background is composited onto white, which is what a
    platform does when it renders the avatar anyway.
    """
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
                im = flat
            else:
                im = im.convert("RGB")
            return ImageFingerprint(
                phash=str(imagehash.phash(im)),
                dhash=str(imagehash.dhash(im)),
                width=width, height=height,
            )
    except Exception as e:                      # noqa: BLE001 - never fatal
        # A picture we cannot fingerprint is a picture with no logo signal,
        # not an error worth failing a sweep over.
        log.warning(f"fingerprint failed: {type(e).__name__}: {e}")
        return None


def _hamming(a: str, b: str) -> Optional[int]:
    try:
        return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)
    except Exception:                           # noqa: BLE001
        return None


def compare(a: ImageFingerprint | dict, b: ImageFingerprint | dict) -> Optional[HashComparison]:
    """Distance between two stored fingerprints, or None if either is
    unusable. Pure arithmetic -- no decoding, no I/O. Measured at ~4
    microseconds, so comparing a sweep's avatars against a client's
    references costs about a millisecond in total."""
    pa = a.phash if isinstance(a, ImageFingerprint) else (a or {}).get("phash")
    da = a.dhash if isinstance(a, ImageFingerprint) else (a or {}).get("dhash")
    pb = b.phash if isinstance(b, ImageFingerprint) else (b or {}).get("phash")
    db = b.dhash if isinstance(b, ImageFingerprint) else (b or {}).get("dhash")
    if not (pa and da and pb and db):
        return None
    pd, dd = _hamming(pa, pb), _hamming(da, db)
    if pd is None or dd is None:
        return None
    return HashComparison(phash_distance=pd, dhash_distance=dd)
