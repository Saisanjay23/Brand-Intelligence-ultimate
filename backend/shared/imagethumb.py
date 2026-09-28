"""Display-size copies of stored profile pictures.

WHY. The avatar store keeps the bytes exactly as the platform served them --
and it must: analysis re-reads them for the default/generated-avatar
verdict, migrations re-derive from them, and the logo `exact` tier matches
on their sha256. But a quarter of them are 1024-2048px originals (610MB of
the 970MB store sits in files over 100KB) painted into a 190px card or a
26px table cell, so the UI downloaded and decoded megapixels it threw away.

So the ORIGINAL IS NEVER TOUCHED. This produces a smaller copy for display
only, on request, and says "use the original" whenever a copy would not be
a strict improvement:

  * the original already fits the requested width -- never upscaled;
  * animated images (a resize would keep one frame);
  * formats other than JPEG, PNG and WebP;
  * a copy that would not actually be smaller in bytes;
  * anything that fails to decode.

What a viewer sees is kept: EXIF orientation is applied before resizing
(the browser applies it to the original, and the copy carries no EXIF), the
ICC colour profile is carried over, and transparency survives in PNG/WebP.
"""

from __future__ import annotations

from collections import OrderedDict
from io import BytesIO
from threading import Lock
from typing import Optional

_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

# JPEG 90 with 4:4:4 chroma: visually lossless at display size, and still a
# small fraction of a 2048px original.
_JPEG_QUALITY = 90


def thumbnail(data: bytes, width: int) -> Optional[tuple[bytes, str]]:
    """(bytes, content type) of a copy whose SHORTER side is `width`, or
    None meaning "serve the original".

    The shorter side, not the longer: the UI asks for `w` through `srcset`,
    where it means "this copy is at least w wide", and the cards crop to
    fill their box (object-fit: cover). Sizing the shorter side keeps BOTH
    dimensions at or above w for any aspect ratio, so the browser never
    receives fewer pixels than it was told it would get."""
    try:
        from PIL import Image, ImageOps

        im = Image.open(BytesIO(data))
        fmt = im.format
        if fmt not in _FORMATS:
            return None
        if getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1:
            return None
        if min(im.size) <= width:
            return None
        icc = im.info.get("icc_profile")
        scale = width / min(im.size)
        target = (max(width, round(im.size[0] * scale)), max(width, round(im.size[1] * scale)))
        if fmt == "JPEG":
            # Decode at a reduced DCT scale straight away -- a 2048px JPEG
            # never has to be fully unpacked. draft() only ever picks a scale
            # that stays at or above the requested size.
            im.draft("RGB", target)
        im = ImageOps.exif_transpose(im)
        scale = width / min(im.size)
        im = im.resize((max(width, round(im.size[0] * scale)),
                        max(width, round(im.size[1] * scale))), Image.LANCZOS)

        out = BytesIO()
        kw = {"icc_profile": icc} if icc else {}
        if fmt == "JPEG":
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(out, "JPEG", quality=_JPEG_QUALITY, subsampling=0,
                    optimize=True, progressive=True, **kw)
        elif fmt == "PNG":
            im.save(out, "PNG", optimize=True, **kw)
        else:
            im.save(out, "WEBP", quality=_JPEG_QUALITY, method=4, **kw)
        body = out.getvalue()
        if len(body) >= len(data):
            return None
        return body, _FORMATS[fmt]
    except Exception:                                   # noqa: BLE001
        return None


class ThumbCache:
    """A small in-process LRU of produced copies, bounded by BYTES.

    Browsers already cache these for a year (the URL is immutable), so this
    only saves repeat work across browsers and page loads within one
    process. Losing it on restart costs one resize per picture, again.
    """

    def __init__(self, max_bytes: int = 64 * 1024 * 1024, max_items: int = 50_000) -> None:
        self._max = max_bytes
        self._max_items = max_items
        self._used = 0
        self._items: OrderedDict[tuple[str, int], Optional[tuple[bytes, str]]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: tuple[str, int]):
        with self._lock:
            if key not in self._items:
                return KeyError
            self._items.move_to_end(key)
            return self._items[key]

    def put(self, key: tuple[str, int], value: Optional[tuple[bytes, str]]) -> None:
        size = len(value[0]) if value else 0
        if size > self._max:
            return
        with self._lock:
            if key in self._items:
                old = self._items.pop(key)
                self._used -= len(old[0]) if old else 0
            self._items[key] = value
            self._used += size
            while (self._used > self._max or len(self._items) > self._max_items) and self._items:
                _, gone = self._items.popitem(last=False)
                self._used -= len(gone[0]) if gone else 0
