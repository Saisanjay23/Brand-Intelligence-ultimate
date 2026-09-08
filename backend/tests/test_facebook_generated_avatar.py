"""Facebook's own letter avatar must not score as a picture the account chose.

THE DEFECT THIS GUARDS. `has_logo` is the heaviest input to the risk rubric
-- a logo alone outweighs location and dormancy together and forces High --
so reading a placeholder as a real picture is the most expensive misread in
the pipeline. shared/avatars.py already caught Facebook's SHARED stock
assets by their fixed ids. It could not catch the other kind: Facebook DRAWS
a per-account avatar, one letter on a solid ground, and serves it from the
ordinary `t39.30808-1` profile-picture path with a per-account asset id.
There is no URL tell whatsoever, so `looks_like_placeholder` returns False
on every one of them.

Measured over all 4095 distinct stored Facebook avatars: 122 are generated,
and 121 of those were stored as `has_logo=True`.

WHY TWO SIGNALS, AND WHY NEITHER ALONE. Both failure directions are real and
both were measured, which is what these tests fix in place:

  * flatness alone condemns real logos. 153 stored avatars read as two flat
    colours and are genuine brand marks -- a white monogram on black, a navy
    swoosh on white. Those are pictures the account chose.
  * the palette alone condemns photographs. 22 stored avatars have a modal
    colour within tolerance of a palette entry and are ordinary photos that
    happen to be mostly sky or mostly skin.

THE FAILURE DIRECTION IS DELIBERATE. Every negative answer is None, never
False. A ground Facebook adds tomorrow is therefore a MISS -- the row keeps
the Yes it already had -- and never a wrong No that would under-score a
genuine impersonation. TestNeverClaimsRealness is that rule stated directly.
"""

from __future__ import annotations

import io

import pytest

from backend.shared.avatars import (
    FACEBOOK_FLAT_THRESHOLD,
    FACEBOOK_GENERATED_BG,
    is_generated_avatar,
    looks_like_placeholder,
)

Image = pytest.importorskip("PIL.Image", reason="Pillow is an analysis dependency")


def _png(pixels_fn, size: int = 64) -> bytes:
    im = Image.new("RGB", (size, size))
    im.putdata([pixels_fn(x, y, size) for y in range(size) for x in range(size)])
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _generated(bg: tuple[int, int, int], glyph_frac: float = 0.18) -> bytes:
    """A stand-in for Facebook's letter avatar: one solid palette ground with
    a darker block of the same hue where the glyph would be."""
    fg = tuple(max(0, c - 90) for c in bg)
    def px(x, y, n):
        lo, hi = n * 0.4, n * 0.4 + n * glyph_frac
        return fg if lo <= x < hi and n * 0.2 <= y < n * 0.8 else bg
    return _png(px)


def _real_flat_logo() -> bytes:
    """A genuine minimalist brand mark: white ground, black glyph. Flat, but
    on a ground Facebook does not draw."""
    def px(x, y, n):
        return (0, 0, 0) if n * 0.3 <= x < n * 0.5 else (255, 255, 255)
    return _png(px)


def _photograph_on_palette_colour() -> bytes:
    """Detailed image whose modal colour IS a palette entry -- the "mostly
    sky" case. Must survive on the flatness test alone."""
    bg = FACEBOOK_GENERATED_BG[0]
    def px(x, y, n):
        return ((x * 7 + y * 13) % 256, (x * 3 + y * 5) % 256, (x * 11) % 256) \
            if (x + y) % 2 else bg
    return _png(px)


# The URL a generated avatar actually arrives on -- an ordinary profile
# picture path with a per-account asset id, which is the whole problem.
GENERATED_URL = (
    "https://scontent.fblr8-1.fna.fbcdn.net/v/t39.30808-1/"
    "602468512_122098666545182433_8563136015116181211_n.png?stp=cp0_dst-png"
)


class TestTheGeneratedAvatarIsCaught:
    @pytest.mark.parametrize("bg", FACEBOOK_GENERATED_BG)
    def test_every_palette_ground_is_recognised(self, bg):
        assert is_generated_avatar("facebook", GENERATED_URL, _generated(bg)) is True

    def test_the_url_alone_could_never_have_told(self):
        """States the reason this needs the image at all: the same URL that
        carries a generated avatar is a perfectly ordinary picture URL."""
        assert looks_like_placeholder("facebook", GENERATED_URL) is False

    def test_a_slightly_recompressed_ground_still_matches(self):
        """These are drawn, not photographed, but they are re-encoded. A few
        levels of drift must not lose the match."""
        bg = tuple(c + 4 for c in FACEBOOK_GENERATED_BG[3])
        assert is_generated_avatar("facebook", GENERATED_URL, _generated(bg)) is True


class TestRealPicturesAreLeftAlone:
    def test_a_flat_brand_logo_is_not_flagged(self):
        """The 153-image case: flat, and genuinely the account's own mark."""
        assert is_generated_avatar("facebook", GENERATED_URL, _real_flat_logo()) is None

    def test_a_photograph_on_a_palette_colour_is_not_flagged(self):
        """The 22-image case: palette-ish modal colour, real photo."""
        got = is_generated_avatar("facebook", GENERATED_URL, _photograph_on_palette_colour())
        assert got is None

    def test_a_ground_facebook_does_not_draw_is_not_flagged(self):
        """Flat, single glyph, same shape as a generated avatar -- but the
        ground is not one Facebook uses, so it is somebody's real logo."""
        assert is_generated_avatar("facebook", GENERATED_URL, _generated((17, 90, 200))) is None


class TestNeverClaimsRealness:
    """Every non-detection is None. False would be a claim this module has
    not earned, and True/False are the only values that reach the database."""

    @pytest.mark.parametrize("data", [b"", b"not an image at all", b"\x89PNG\r\n\x1a\n trunc"])
    def test_undecodable_bytes_are_unknown(self, data):
        assert is_generated_avatar("facebook", GENERATED_URL, data) is None

    def test_it_never_returns_false_for_facebook(self):
        for data in (_real_flat_logo(), _photograph_on_palette_colour(), b"junk"):
            assert is_generated_avatar("facebook", GENERATED_URL, data) is not False

    @pytest.mark.parametrize("platform", ["instagram", "twitter", "telegram", "tiktok", ""])
    def test_other_platforms_are_untouched(self, platform):
        """Only Facebook and YouTube are answered here; everything else is
        settled from the URL and must not be second-guessed by pixels."""
        assert is_generated_avatar(platform, GENERATED_URL, _generated(FACEBOOK_GENERATED_BG[0])) is None


class TestTheThresholdIsFacebooksOwn:
    def test_it_is_not_youtubes(self):
        """Set from where Facebook's data separates (lowest generated 0.9009,
        highest real picture 0.8674), not inherited from a bar tuned for a
        different platform's signals."""
        from backend.shared.avatars import FLAT_THRESHOLD
        assert FACEBOOK_FLAT_THRESHOLD == 0.90
        assert FACEBOOK_FLAT_THRESHOLD < FLAT_THRESHOLD

    def test_a_busy_image_on_a_palette_ground_falls_below_it(self):
        """The bar has to actually bite: a ground-coloured image with enough
        detail is not a letter avatar."""
        assert is_generated_avatar(
            "facebook", GENERATED_URL, _photograph_on_palette_colour(),
        ) is None
