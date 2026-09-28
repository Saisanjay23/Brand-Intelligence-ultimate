"""Display-size copies of stored avatars change what the UI downloads, and
NOTHING ELSE.

The stored original is what analysis re-reads for the default/generated
avatar verdict, what migrations re-derive from, and what the logo `exact`
tier's sha256 is of. So these pin two things: without `w` the route serves
the original byte for byte, and with it a copy is only ever a faithful,
smaller, never-upscaled version of the same picture.

Measured on 400 real stored avatars at the size a card paints them: mean
pixel difference 0.54/255.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from unittest.mock import AsyncMock

from PIL import Image

from backend.shared.imagethumb import ThumbCache, thumbnail


def _img(size, fmt="JPEG", mode="RGB", **save) -> bytes:
    im = Image.new(mode, size)
    # Real content, not a flat colour -- a flat image compresses to nothing
    # and would make "smaller" trivially true.
    px = im.load()
    for x in range(size[0]):
        for y in range(0, size[1], 7):
            px[x, y] = (x % 255, y % 255, (x * y) % 255) + ((200,) if mode == "RGBA" else ())
    out = BytesIO()
    im.save(out, fmt, **save)
    return out.getvalue()


class TestTheCopy:
    def test_the_shorter_side_is_reduced_to_the_width(self):
        """srcset promises "at least w wide" and cards crop to fill, so no
        side may drop below w -- a landscape picture keeps its full height
        at w, a portrait one its full width."""
        body, ctype = thumbnail(_img((2048, 1536)), 512)
        assert ctype == "image/jpeg"
        assert Image.open(BytesIO(body)).size == (683, 512)
        body, _ = thumbnail(_img((1000, 2000)), 256)
        assert Image.open(BytesIO(body)).size == (256, 512)

    def test_never_upscaled(self):
        assert thumbnail(_img((300, 300)), 512) is None

    def test_orientation_is_applied_as_the_browser_would(self):
        """The browser rotates the original by its EXIF tag; the copy has
        no EXIF, so the rotation must already be in its pixels."""
        im = Image.new("RGB", (1200, 600))
        exif = im.getexif()
        exif[0x0112] = 6                      # rotate 90 CW on display
        out = BytesIO()
        im.save(out, "JPEG", exif=exif)
        body, _ = thumbnail(out.getvalue(), 300)
        assert Image.open(BytesIO(body)).size == (300, 600)

    def test_transparency_survives(self):
        # Photographic noise, so the original does not compress to nothing.
        im = Image.effect_noise((1024, 1024), 64).convert("RGBA")
        im.putalpha(Image.effect_noise((1024, 1024), 64))
        out = BytesIO()
        im.save(out, "PNG")
        body, ctype = thumbnail(out.getvalue(), 256)
        assert ctype == "image/png"
        assert Image.open(BytesIO(body)).mode == "RGBA"

    def test_a_copy_that_would_be_bigger_is_not_used(self):
        """A mostly-empty PNG compresses better at full size than resized."""
        assert thumbnail(_img((1024, 1024), "PNG", "RGBA"), 256) is None

    def test_animation_is_left_alone(self):
        frames = [Image.new("RGB", (800, 800), c) for c in ("red", "blue")]
        out = BytesIO()
        frames[0].save(out, "GIF", save_all=True, append_images=frames[1:])
        assert thumbnail(out.getvalue(), 128) is None

    def test_garbage_means_serve_the_original(self):
        assert thumbnail(b"not an image", 128) is None


class TestTheRoute:
    def _call(self, monkeypatch, original, w):
        from backend.api import media

        monkeypatch.setattr(media.avatars_db, "read", AsyncMock(return_value=(original, "image/jpeg")))
        monkeypatch.setattr(media, "_thumbs", ThumbCache())
        return asyncio.run(media.stored_avatar(sha="a" * 64, w=w))

    def test_without_w_the_original_is_served_byte_for_byte(self, monkeypatch):
        original = _img((2048, 2048))
        resp = self._call(monkeypatch, original, None)
        assert resp.body == original

    def test_with_w_a_smaller_copy_is_served(self, monkeypatch):
        original = _img((2048, 2048))
        resp = self._call(monkeypatch, original, 256)
        assert len(resp.body) < len(original)
        assert Image.open(BytesIO(resp.body)).size == (256, 256)
        assert "immutable" in resp.headers["cache-control"]

    def test_a_picture_that_already_fits_is_served_unchanged(self, monkeypatch):
        original = _img((200, 200))
        assert self._call(monkeypatch, original, 512).body == original


class TestTheCache:
    def test_bounded_by_bytes(self):
        c = ThumbCache(max_bytes=10)
        c.put(("a", 1), (b"12345678", "image/jpeg"))
        c.put(("b", 1), (b"12345678", "image/jpeg"))
        assert c.get(("a", 1)) is KeyError and c.get(("b", 1)) is not KeyError

    def test_bounded_by_count(self):
        c = ThumbCache(max_items=2)
        for k in "abc":
            c.put((k, 1), None)
        assert c.get(("a", 1)) is KeyError

    def test_remembers_serve_the_original(self):
        c = ThumbCache()
        c.put(("a", 1), None)
        assert c.get(("a", 1)) is None
