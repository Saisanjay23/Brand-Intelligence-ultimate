"""Placeholder-avatar detection.

WHY THIS IS WORTH PROTECTING. `has_logo` is the heaviest input to the risk
rubric -- a logo match alone outweighs location and dormancy combined and
forces High priority outright (shared/models/scoring.py). So reading a
platform's own stock silhouette as "this account is using the brand's
photo" is the most expensive misread in the pipeline, and it is silent:
the row looks fully populated and confidently wrong.

The asset ids below are the actual live assets, established by fetching and
hashing all 2325 stored avatars. Two of them are the reason this exists:

  * Facebook's grey silhouette and its illustrated default GROUP avatar
    were both being read as real uploads (168 rows).
  * Instagram ROTATED its anonymous avatar. The engine checked only the old
    id, so detection silently stopped and every picture-less account was
    recorded as having a real one (4 rows).

If a test here starts failing because a platform rotated an asset again,
that is the point -- add the new id, keep the old one (older rows still
carry it), and do not delete either.
"""

from __future__ import annotations

import io

import pytest

from backend.shared.avatars import (FLAT_THRESHOLD, YOUTUBE_GENERATED_PREFIX,
                                    is_generated_avatar,
                                    looks_like_placeholder)

FB_SILHOUETTE = ("https://scontent.fblr8-1.fna.fbcdn.net/v/t1.30497-1/"
                 "453178253_471506465671661_2781666950760530985_n.png?stp=cp0_dst-png")
FB_GROUP_DEFAULT = ("https://scontent.fblr8-1.fna.fbcdn.net/v/t1.30497-1/"
                    "116687302_959241714549285_318408173653384421_n.jpg")
IG_ANON_CURRENT = ("https://instagram.fluh3-1.fna.fbcdn.net/v/t51.2885-19/"
                   "573323465_1219825463302212_7278921664109726296_n.png?stp=dst-jpg")
IG_ANON_LEGACY = ("https://instagram.fblr8-1.fna.fbcdn.net/v/t51.2885-19/"
                  "44884218_345707102882519_2446069589734326272_n.jpg")
TW_EGG = "https://abs.twimg.com/sticky/default_profile_images/default_profile_400x400.png"


class TestKnownPlaceholders:
    @pytest.mark.parametrize("platform,url", [
        ("facebook", FB_SILHOUETTE),
        ("facebook", FB_GROUP_DEFAULT),
        ("instagram", IG_ANON_CURRENT),
        ("instagram", IG_ANON_LEGACY),
        ("twitter", TW_EGG),
    ])
    def test_stock_avatars_are_recognised(self, platform, url):
        assert looks_like_placeholder(platform, url) is True

    def test_instagram_still_knows_the_asset_it_rotated_away_from(self):
        """Rows stored before the rotation still carry the old URL, so
        dropping the old id would un-detect every one of them."""
        assert looks_like_placeholder("instagram", IG_ANON_LEGACY) is True


class TestRealPicturesSurvive:
    """Precision matters more than recall here: calling a real photo a
    placeholder silently DROPS a genuine impersonation signal."""

    @pytest.mark.parametrize("platform,url", [
        ("facebook", "https://scontent.fblr8-1.fna.fbcdn.net/v/t39.30808-1/"
                     "481234567_122095914603376682_1111111111111111111_n.jpg"),
        ("instagram", "https://instagram.fblr8-1.fna.fbcdn.net/v/t51.2885-19/"
                      "271440417_173258961675115_5208959205795977905_n.jpg"),
        ("twitter", "https://pbs.twimg.com/profile_images/1234567890/photo_400x400.jpg"),
        ("youtube", "https://yt3.ggpht.com/yyw4SeYvifkWuXqwxN6DyUmFVrgTUhIqR06Gz=s800"),
        ("telegram", "https://t.me/i/userpic/320/someone.jpg"),
    ])
    def test_a_real_upload_is_not_flagged(self, platform, url):
        assert looks_like_placeholder(platform, url) is False

    def test_the_facebook_cdn_TAG_alone_never_flags(self):
        """`t1.30497-1` is also a rendering context for genuine uploads --
        facebook/discovery_engine.py documents this and it was verified
        wrong against live data. Only the ASSET ID may decide."""
        real_under_same_tag = ("https://scontent.fblr8-1.fna.fbcdn.net/v/t1.30497-1/"
                               "999888777_123456789012345_9999999999999999999_n.jpg")
        assert looks_like_placeholder("facebook", real_under_same_tag) is False

    def test_empty_and_unknown_platform(self):
        assert looks_like_placeholder("facebook", "") is False
        assert looks_like_placeholder("mastodon", FB_SILHOUETTE) is False


def _solid_png(colour=(90, 70, 200), size=(64, 64), noise=False):
    from PIL import Image
    im = Image.new("RGB", size, colour)
    if noise:
        px = im.load()
        for x in range(size[0]):
            for y in range(size[1]):
                px[x, y] = ((x * 7 + y * 13) % 256, (x * 3) % 256, (y * 5) % 256)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class TestYouTubeGeneratedAvatars:
    """YouTube generates a per-channel letter avatar from the same host and
    URL shape as a real upload, so only the image itself can decide -- and
    only together with the URL marker, which alone is 60% precise."""

    GEN_URL = "https://yt3.ggpht.com/ytc/AIdro_kVfxBemKTXVOvYzuHj3r_aqB=s800"
    REAL_URL = "https://yt3.ggpht.com/yyw4SeYvifkWuXqwxN6DyUmFVrgTUhIqR06Gz=s800"

    def test_a_flat_image_under_the_marker_is_generated(self):
        assert is_generated_avatar("youtube", self.GEN_URL, _solid_png()) is True

    def test_a_busy_image_under_the_marker_is_not(self):
        """A real photograph served from the same prefix -- 110 of the 274
        rows carrying this marker were exactly that."""
        assert is_generated_avatar("youtube", self.GEN_URL, _solid_png(noise=True)) is False

    def test_without_the_marker_the_answer_is_unknown_not_false(self):
        """A flat image with no marker may be someone's real monogram logo
        (a black square with a white W was in the live data). Unknown, so
        the caller leaves has_custom_pic alone rather than guessing."""
        assert is_generated_avatar("youtube", self.REAL_URL, _solid_png()) is None

    def test_unreadable_bytes_are_unknown(self):
        assert is_generated_avatar("youtube", self.GEN_URL, b"not an image") is None

    def test_other_platforms_are_not_this_function_s_job(self):
        assert is_generated_avatar("facebook", FB_SILHOUETTE, _solid_png()) is None

    def test_the_threshold_is_a_share_not_a_count(self):
        assert 0.5 < FLAT_THRESHOLD < 1.0
        assert YOUTUBE_GENERATED_PREFIX in self.GEN_URL


class TestEngineProperties:
    """The per-platform `has_custom_pic` properties must go through the same
    registry, so there is one definition rather than one per engine."""

    def test_instagram(self):
        from backend.platforms.instagram.discovery_engine import InstagramUser
        assert InstagramUser(avatar=IG_ANON_CURRENT).has_custom_pic is False
        assert InstagramUser(avatar="https://cdn/real.jpg").has_custom_pic is True
        assert InstagramUser(avatar="").has_custom_pic is False

    def test_twitter(self):
        from backend.platforms.twitter.discovery_engine import TwitterUser
        assert TwitterUser(avatar=TW_EGG).has_custom_pic is False
        assert TwitterUser(avatar="https://pbs.twimg.com/profile_images/1/r.jpg").has_custom_pic is True
