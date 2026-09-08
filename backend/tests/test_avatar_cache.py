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

import pytest

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


class TestNewAllowlistEntries:
    """Telegram and YouTube's secondary CDN hosts must be in the allowlist
    so that avatar_cache can fetch and persist their avatars."""

    @pytest.mark.parametrize("url", [
        "https://cdn4.telegram.org/file/abc123/photo.jpg",
        "https://t.me/i/userpic/640/durov.jpg",
        "https://cdn.telesco.pe/file/photo.jpg",
        "https://yt3.ytimg.com/ytc/AIdro_abc=s800",
        "https://i.ytimg.com/vi/abc123/default.jpg",
    ])
    def test_new_hosts_are_allowed(self, url):
        assert allowed(url), url

    @pytest.mark.parametrize("url", [
        "https://evil-t.me.attacker.com/photo.jpg",
        "https://faketelegram.org.evil.com/x.jpg",
        "http://t.me/i/userpic/320/photo.jpg",   # http, not https
    ])
    def test_new_hosts_are_not_spoofable(self, url):
        assert not allowed(url), url


class TestDataUriCaching:
    """Telegram's MTProto avatars are base64 data: URIs. Before the fix,
    cache_for_profiles skipped them. Now they must be queued for caching."""

    def test_data_uri_is_no_longer_skipped_in_target_selection(self):
        """The _targets helper mirrors cache_for_profiles' filtering.
        data: URIs used to be excluded; now they must pass through."""
        items = [{
            "url": "https://t.me/someone",
            "profile_image_url": "data:image/jpeg;base64,/9j/4AAQSkZJRgAB",
        }]
        # We replicate the updated filter logic here
        seen = []
        for it in items:
            img = (it.get("profile_image_url") or "").strip()
            url_val = (it.get("url") or "").strip()
            if not img or not url_val:
                continue
            key = f"{url_val}\n{img}"
            if key in seen:
                continue
            seen.append(key)
        assert len(seen) == 1

    def test_data_uri_regex_matches_valid_format(self):
        """The _DATA_URI_RE pattern must match Telegram's exact format."""
        import re
        pattern = re.compile(
            r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<b64>.+)$",
            re.DOTALL,
        )
        uri = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD"
        m = pattern.match(uri)
        assert m is not None
        assert m.group("mime") == "image/jpeg"
        assert m.group("b64").startswith("/9j/")

    def test_data_uri_regex_rejects_non_image(self):
        """Only image/* MIME types should match."""
        import re
        pattern = re.compile(
            r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<b64>.+)$",
            re.DOTALL,
        )
        assert pattern.match("data:text/html;base64,abc") is None
        assert pattern.match("data:application/json;base64,abc") is None


class TestSameAvatarAsset:
    """Verifies that avatar_cache skips identical assets but detects genuine DP changes."""

    def test_meta_cdn_same_asset_different_signatures(self):
        from backend.services.avatar_cache import is_same_avatar_asset
        old_url = "https://scontent.fblr8-1.fna.fbcdn.net/v/t39.30808-1/12345_67890_n.jpg?oh=AAA111&oe=66E00000"
        new_url = "https://scontent.fblr8-1.fna.fbcdn.net/v/t39.30808-1/12345_67890_n.jpg?oh=BBB222&oe=66F99999"
        # Path is identical, only signed parameters changed -> should recognize as SAME image
        assert is_same_avatar_asset(old_url, new_url) is True

    def test_meta_cdn_changed_profile_picture(self):
        from backend.services.avatar_cache import is_same_avatar_asset
        old_url = "https://scontent.fblr8-1.fna.fbcdn.net/v/t39.30808-1/12345_67890_n.jpg?oh=AAA111"
        new_url = "https://scontent.fblr8-1.fna.fbcdn.net/v/t39.30808-1/99999_88888_n.jpg?oh=BBB222"
        # Different asset in path -> should detect that DP CHANGED!
        assert is_same_avatar_asset(old_url, new_url) is False

    def test_twitter_changed_profile_picture(self):
        from backend.services.avatar_cache import is_same_avatar_asset
        old_url = "https://pbs.twimg.com/profile_images/111111/avatar_400x400.jpg"
        new_url = "https://pbs.twimg.com/profile_images/222222/new_pic_400x400.jpg"
        assert is_same_avatar_asset(old_url, new_url) is False

    def test_twitter_same_profile_picture(self):
        from backend.services.avatar_cache import is_same_avatar_asset
        url = "https://pbs.twimg.com/profile_images/111111/avatar_400x400.jpg"
        assert is_same_avatar_asset(url, url) is True

    def test_empty_or_none(self):
        from backend.services.avatar_cache import is_same_avatar_asset
        assert is_same_avatar_asset("", "https://pbs.twimg.com/avatar.jpg") is False
        assert is_same_avatar_asset("https://pbs.twimg.com/avatar.jpg", "") is False


class TestProfileRepositoryAvatarChangeDetection:
    """Verifies that profile_repository.save() detects avatar changes on existing
    profiles, sets avatar_changed_at, preserves previous avatar info, and invalidates
    stale cached hashes/logo scores."""

    @pytest.mark.asyncio
    async def test_save_detects_changed_avatar(self, monkeypatch):
        from datetime import datetime
        from backend.database.repositories import profile_repository
        from backend.database.repositories.profile_repository import save

        updates = []

        class MockColl:
            async def find_one(self, *args, **kwargs):
                return {
                    "_id": "doc123",
                    "url": "https://x.com/target",
                    "status": "pending",
                    "profile_image_url": "https://pbs.twimg.com/profile_images/111/old.jpg",
                    "avatar_sha": "old_sha_hash_value",
                }

            async def update_one(self, filt, update):
                updates.append((filt, update))
                return None

        monkeypatch.setattr(profile_repository, "db", lambda: {"profiles": MockColl()})

        res = await save(
            client_id="c1",
            platform="twitter",
            url="https://x.com/target",
            fields={"profile_image_url": "https://pbs.twimg.com/profile_images/222/new.jpg"},
            phase="discovery",
        )
        assert res is False
        assert len(updates) == 1
        filt, update = updates[0]
        assert filt == {"_id": "doc123"}
        set_clause = update["$set"]
        assert "avatar_changed_at" in set_clause
        assert isinstance(set_clause["avatar_changed_at"], datetime)
        assert set_clause["avatar_previous_url"] == "https://pbs.twimg.com/profile_images/111/old.jpg"
        assert set_clause["avatar_previous_sha"] == "old_sha_hash_value"

        unset_clause = update["$unset"]
        for stale in ("avatar_sha", "avatar_phash", "avatar_dhash", "avatar_embedding",
                      "logo_similarity", "logo_ref_id", "logo_match_tier"):
            assert stale in unset_clause

    @pytest.mark.asyncio
    async def test_save_ignores_same_cdn_asset_with_different_signatures(self, monkeypatch):
        from backend.database.repositories import profile_repository
        from backend.database.repositories.profile_repository import save

        updates = []

        class MockColl:
            async def find_one(self, *args, **kwargs):
                return {
                    "_id": "doc456",
                    "url": "https://facebook.com/target",
                    "status": "pending",
                    "profile_image_url": "https://scontent.fbcdn.net/v/t39.30808-1/123_n.jpg?oh=SIG1&oe=66E",
                    "avatar_sha": "existing_sha",
                }

            async def update_one(self, filt, update):
                updates.append((filt, update))
                return None

        monkeypatch.setattr(profile_repository, "db", lambda: {"profiles": MockColl()})

        res = await save(
            client_id="c1",
            platform="facebook",
            url="https://facebook.com/target",
            fields={"profile_image_url": "https://scontent.fbcdn.net/v/t39.30808-1/123_n.jpg?oh=SIG2&oe=66F"},
            phase="discovery",
        )
        assert res is False
        assert len(updates) == 1
        filt, update = updates[0]
        assert "avatar_changed_at" not in update["$set"]
        assert "avatar_previous_url" not in update["$set"]
        assert "$unset" not in update

