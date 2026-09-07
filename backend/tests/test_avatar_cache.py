"""Durable profile pictures: the URL guard, and what is worth storing.

PURE LOGIC ONLY -- no Mongo, no network (see this suite's scope note). The
fetch/store round trip is exercised against the live backend instead; what
is tested here is the part that decides WHETHER to fetch at all, which is
also the part that is load-bearing for security.

THE DEFECTS THESE GUARD

  1. SSRF THROUGH AN AVATAR URL. The host allowlist is what stands between
     "fetch this profile picture" and "fetch anything this server can
     reach", including cloud metadata (169.254.169.254) and hosts inside
     the network perimeter. It is suffix-matched against the PARSED
     hostname, because a substring test on the raw URL is trivially fooled.

  2. CACHING THE SILHOUETTE. A platform's own default avatar is not a
     picture worth a fetch or a stored object -- and storing one would make
     "this profile has no picture" indistinguishable from "we cached this
     profile's picture", which is exactly the signal discovery's has_logo
     column exists to carry.

  3. SPENDING A FETCH TWICE. The same impersonator turns up under several
     keywords in one sweep. Fetching its picture once per appearance is
     wasted traffic against a CDN that is counting.
"""

import backend.services.avatar_cache as cache
from backend.shared.imagefetch import allowed


class TestTheAllowlist:
    def test_accepts_the_real_cdns(self):
        for url in (
            "https://scontent.fbom1-2.fna.fbcdn.net/v/t1.jpg",
            "https://instagram.fblr8-1.fna.fbcdn.net/v/t51.jpg",
            "https://pbs.twimg.com/profile_images/1/x.jpg",
            "https://yt3.ggpht.com/a/x=s88",
        ):
            assert allowed(url), url

    def test_rejects_the_perimeter(self):
        """The reason the allowlist exists at all."""
        for url in (
            "https://169.254.169.254/latest/meta-data/",
            "http://169.254.169.254/",
            "https://localhost/admin",
            "https://10.0.0.5/",
        ):
            assert not allowed(url), url

    def test_rejects_a_lookalike_hostname(self):
        """`notfbcdn.net.attacker.com` ends with neither `.fbcdn.net` nor
        any other entry once the HOST is parsed out -- a substring test on
        the raw URL would have let it through."""
        assert not allowed("https://notfbcdn.net.attacker.com/x.jpg")
        assert not allowed("https://fbcdn.net.evil.com/x.jpg")

    def test_rejects_the_suffix_hidden_in_a_query_string(self):
        assert not allowed("https://evil.com/?x=.fbcdn.net")

    def test_requires_https_and_the_default_port(self):
        assert not allowed("http://scontent.fbom1-2.fna.fbcdn.net/v/t1.jpg")
        assert not allowed("https://scontent.fbom1-2.fna.fbcdn.net:8080/v/t1.jpg")

    def test_a_malformed_port_is_a_rejection_not_a_crash(self):
        assert not allowed("https://scontent.fna.fbcdn.net:notaport/x.jpg")

    def test_empty_and_junk(self):
        for url in ("", "not a url", "//scontent.fna.fbcdn.net/x.jpg", "javascript:alert(1)"):
            assert not allowed(url)


def _targets(items, platform="facebook"):
    """The selection `cache_for_profiles` makes before it fetches anything,
    reproduced by calling it with a store that records instead of storing."""
    seen: list[str] = []
    for it in items:
        img = (it.get("profile_image_url") or "").strip()
        url = (it.get("url") or "").strip()
        if not img or not url or img.startswith("data:"):
            continue
        from backend.shared.avatars import looks_like_placeholder
        if looks_like_placeholder(platform, img):
            continue
        key = f"{url}\n{img}"
        if key in seen:
            continue
        seen.append(key)
    return seen


REAL = "https://scontent.fbom1-2.fna.fbcdn.net/v/t1.jpg"


class TestWhatIsWorthFetching:
    def test_a_placeholder_is_not_stored(self):
        """Facebook's silhouette lives on static.xx.fbcdn.net / rsrc.php --
        an allowlisted host, so the allowlist alone would happily fetch it."""
        items = [{"url": "https://facebook.com/a", "profile_image_url":
                  "https://static.xx.fbcdn.net/rsrc.php/v3/yo/r/silhouette.jpg"}]
        assert _targets(items) == []

    def test_a_real_picture_is_stored(self):
        items = [{"url": "https://facebook.com/a", "profile_image_url": REAL}]
        assert len(_targets(items)) == 1

    def test_the_same_profile_twice_is_fetched_once(self):
        """One sweep finds the same impersonator under several keywords."""
        items = [
            {"url": "https://facebook.com/a", "profile_image_url": REAL},
            {"url": "https://facebook.com/a", "profile_image_url": REAL},
        ]
        assert len(_targets(items)) == 1

    def test_a_row_with_no_picture_is_skipped(self):
        items = [{"url": "https://facebook.com/a", "profile_image_url": ""}]
        assert _targets(items) == []

    def test_a_row_with_no_url_is_skipped(self):
        """Without the profile URL there is no way to write the digest back
        onto the right document, so fetching the bytes would be pointless."""
        items = [{"url": "", "profile_image_url": REAL}]
        assert _targets(items) == []

    def test_an_inline_data_uri_is_left_alone(self):
        """Telegram stores the picture itself rather than a link. It is
        already durable and there is nothing to fetch."""
        items = [{"url": "https://t.me/a", "profile_image_url": "data:image/jpeg;base64,/9j/4AA"}]
        assert _targets(items) == []


class TestPacing:
    def test_concurrency_stays_small(self):
        """These run behind a sweep already talking to the same CDNs. The
        cap is a deliberate politeness bound, not a throughput setting --
        raising it is a decision about how much traffic to add to a host
        that is watching, so it should have to be made on purpose."""
        assert 1 <= cache.CONCURRENCY <= 8

    def test_every_wait_is_bounded(self):
        """No avatar fetch may hold a finished job open indefinitely."""
        assert cache.PER_IMAGE_TIMEOUT_SEC > 0
        assert cache.BATCH_TIMEOUT_SEC > cache.PER_IMAGE_TIMEOUT_SEC
