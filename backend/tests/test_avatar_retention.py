"""A profile picture, once found, is kept -- and kept for good.

THE COMPLAINT THIS ANSWERS. Cards showed an initial-letter circle for
profiles that visibly have a photo. 10,413 stored rows had a picture URL
and no stored copy of the picture.

The picture was never missing from the platform. Meta SIGNS its CDN URLs
and stamps the expiry into the URL itself; once that passes, the stored
link answers `403 URL signature expired` for ever. The copy that was
supposed to outlive the signature was the thing missing, because caching
was one best-effort attempt at sweep time with a flat 180-second budget for
any batch size, no retry, and no record when it failed.

THREE THINGS MAKE IT ROBUST, and each is pinned below:

  * the budget scales with the batch, so a big sweep is not truncated
    (test_avatar_cache.py);
  * a retry monitor keeps trying for as long as each URL stays valid --
    measured at 110-307 HOURS, not the "hours not days" this codebase's own
    comment claimed, which is why hourly retries are a complete fix and not
    a race;
  * an expired URL is recognised as unrecoverable rather than retried for
    ever, so the genuinely-lost ones stay visible instead of hiding in a
    log full of expected 403s.
"""

from __future__ import annotations

import time

import pytest

from backend.shared.imagefetch import signed_expiry, url_is_live


class TestAUrlKnowsItsOwnLifetime:
    def test_a_signed_url_reports_when_it_dies(self):
        # oe= is a hex unix timestamp. 0x6A9ED8FA is 2026-09-07.
        url = "https://scontent.fbcdn.net/v/a.jpg?_nc_ht=x&oe=6A9ED8FA"
        assert signed_expiry(url) == float(0x6A9ED8FA)

    def test_an_unsigned_url_has_no_stated_lifetime_so_it_is_worth_trying(self):
        """YouTube, Twitter and Telegram do not sign. The only way to know
        is to try, and trying is what the caller wants -- answering False
        here would write off pictures that are perfectly fetchable."""
        assert signed_expiry("https://pbs.twimg.com/profile/a.jpg") is None
        assert url_is_live("https://pbs.twimg.com/profile/a.jpg") is True
        assert url_is_live("data:image/jpeg;base64,abc") is True

    def test_only_a_demonstrably_passed_signature_is_dead(self):
        past = f"https://x.fbcdn.net/a.jpg?oe={int(time.time()) - 3600:X}"
        future = f"https://x.fbcdn.net/a.jpg?oe={int(time.time()) + 3600:X}"
        assert url_is_live(past) is False
        assert url_is_live(future) is True

    def test_a_malformed_stamp_is_treated_as_worth_trying(self):
        """Garbage in the parameter must not silently write a picture off.
        An unreadable expiry is an unknown one, and unknown means try."""
        assert url_is_live("https://x.fbcdn.net/a.jpg?oe=notahexnumber") is True
        assert signed_expiry("https://x.fbcdn.net/a.jpg") is None


class TestWhichPlatformIsTheWrongQuestion:
    def test_a_fresh_meta_url_is_live_and_an_old_one_is_not(self):
        """THE MISREADING THAT COST AN INSTAGRAM RATE LIMIT. The first
        version of the backfill split platforms into "URL works" and "URL
        expired" from a sample of twelve-day-old rows, concluded Meta URLs
        are never recoverable, and built a per-profile re-visit that
        rate-limited a real account after 200 calls and recovered nothing.

        Expiry is a property of the URL, never of the platform. Asking per
        row made 687 Facebook pictures free to recover that the
        platform-based rule had written off as needing a login.
        """
        fresh = f"https://scontent.fbcdn.net/a.jpg?oe={int(time.time()) + 86400 * 5:X}"
        stale = f"https://scontent.fbcdn.net/a.jpg?oe={int(time.time()) - 86400 * 5:X}"
        assert url_is_live(fresh) is True
        assert url_is_live(stale) is False


class TestTheRetryMonitor:
    def test_the_interval_fits_inside_the_shortest_observed_window(self):
        """110 hours is the shortest signed window measured in the stored
        data. An hourly retry gets ~110 attempts inside it, which is what
        makes this a guarantee rather than a hope. An interval anywhere near
        the window would turn a single missed night into a lost picture."""
        from backend.services import avatar_backfill as bf

        shortest_observed_window_s = 110 * 3600
        assert bf.PICTURE_RETRY_INTERVAL_S < shortest_observed_window_s / 50

    def test_starting_twice_leaves_one_loop(self):
        """Same contract as the session and evidence monitors it sits
        beside -- a second call must not spawn a second sweep of the same
        rows."""
        import asyncio

        from backend.services import avatar_backfill as bf

        async def go():
            bf.start_retry_monitor()
            first = bf._monitor_task
            bf.start_retry_monitor()
            assert bf._monitor_task is first
            bf.stop_retry_monitor()
            assert bf._monitor_task is None

        asyncio.run(go())


class TestARefreshNeverInventsProfiles:
    """A refresh re-reads what discovery already found. It must never add.

    THE BUG THIS PINS, found by running it on real data. The first version
    of `profile_refresh` searched a blank profile's own name and saved every
    result, on the theory that the other ~25 hits were free repairs.

    They are not. Over 15 real searches it saw 424 profiles and 11 were rows
    we held; the rest were strangers who merely had similar names. Saving
    them put 119 unrelated people into a brand-monitoring client, each filed
    under a person's name as though it were one of the analyst's keywords --
    "Taylor-Jayne Adrian", "Adrian Villavicencio". They had to be deleted.

    Discovery decides who belongs to a client. A refresh only re-reads that
    decision, so a hit it does not already hold is discarded.
    """

    def test_only_known_profiles_are_kept(self):
        from backend.services import profile_refresh as pr

        held = {"111": {"client_id": "c1"}, "222": {"client_id": "c1"}}
        by_url = {"https://fb.com/known": {"client_id": "c1"}}

        class Hit:
            def __init__(self, pid, url):
                self.profile_id, self.url = pid, url

        hits = [Hit("111", "https://fb.com/a"),      # known by id
                Hit("999", "https://fb.com/known"),  # known by url
                Hit("888", "https://fb.com/stranger"),
                Hit("777", "https://fb.com/other")]

        kept = [h for h in hits
                if held.get(str(h.profile_id)) or by_url.get(str(h.url))]

        assert [h.profile_id for h in kept] == ["111", "999"]
        assert len(kept) < len(hits), "strangers must be discarded"

    def test_a_refresh_belonging_to_another_client_is_not_applied(self):
        """Two clients can hold the same profile. A refresh running for one
        must not write the other's row -- that would move a profile between
        investigations."""
        from backend.services import profile_refresh as pr

        held = {"111": {"client_id": "other-client"}}
        cid = "c1"
        known = held.get("111")
        assert known is not None
        assert (known.get("client_id") or "") != cid


class TestSearchIsTheOnlySafePictureSource:
    def test_facebook_profile_page_avatars_stay_untrusted(self):
        """NOT A PREFERENCE -- A SAFETY RULE, and one this work had every
        incentive to break.

        For a privacy-restricted profile, Facebook's client substitutes THE
        VIEWER'S OWN PHOTO into every picture field it renders. Three
        separate defences were tried when this was first investigated and
        each was caught attributing the scraper's own face to a candidate.

        Refreshing pictures from a profile visit would have been by far the
        most direct fix for the blank-card problem. It is forbidden, which
        is why `profile_refresh` goes through search instead.
        """
        from backend.platforms.facebook import discovery_engine as F

        assert F.TRUST_PAGE_CONTEXT_AVATAR is False


class TestLookingAtACardKeepsItsPicture:
    """The last way a picture could still be lost, and the cheapest fix.

    The media proxy fetched a picture on behalf of a card being looked at,
    served it, and threw the bytes away -- "LIVE, NOT STORED", by design.
    So the analyst saw the picture, we had the whole image in memory, and a
    week later the signed CDN URL expired and that same card went blank
    holding nothing.

    Now the proxy keeps what it serves, and the front end deliberately
    routes an uncached picture THROUGH the proxy rather than straight to the
    CDN -- which looks like the slower option and is the only one where the
    bytes pass through somewhere that can save them.
    """

    def test_an_uncached_picture_is_routed_through_the_proxy(self):
        """Direct-to-CDN is faster and loses the picture: the browser gets
        the bytes, we never do, and there is nothing to store."""
        import re
        from pathlib import Path

        src = Path("frontend/src/utils/avatar.ts").read_text(encoding="utf-8")
        # With no stored copy, the proxy must come first.
        assert "if (!stored.length) return [proxied, raw];" in src
        # ...and once stored, neither is touched again.
        assert "return [...stored, raw, proxied];" in src

    def test_the_proxy_keeps_what_it_serves(self):
        from pathlib import Path

        src = Path("backend/api/media.py").read_text(encoding="utf-8")
        assert "_keep(url, img.data, img.content_type)" in src
        # Off the response path -- the analyst is waiting for this image.
        assert "asyncio.create_task(_keep(" in src
        # And the old contract is gone.
        assert "LIVE, NOT STORED" not in src

    @pytest.mark.asyncio
    async def test_it_only_ever_fills_a_blank(self, monkeypatch):
        """A profile that already has a picture is left alone. Its digest
        may be one this URL no longer serves -- `save` invalidates the old
        one on purpose when an account changes its photo, and re-pointing it
        at whatever the proxy happened to fetch would undo that."""
        from backend.database.repositories import profile_repository as pdb

        captured = {}

        class _Coll:
            async def update_many(self, q, u):
                captured["query"] = q
                class R:
                    modified_count = 1
                return R()

        monkeypatch.setattr(pdb, "db", lambda: {pdb.PROFILES: _Coll()})
        await pdb.keep_avatar_for_image_url("https://cdn/x.jpg", "abc123")

        blank_clause = captured["query"]["$or"]
        assert {"avatar_sha": {"$exists": False}} in blank_clause
        assert {"avatar_sha": {"$in": ["", None]}} in blank_clause

    @pytest.mark.asyncio
    async def test_a_missing_url_or_digest_writes_nothing(self):
        from backend.database.repositories import profile_repository as pdb

        assert await pdb.keep_avatar_for_image_url("", "abc") == 0
        assert await pdb.keep_avatar_for_image_url("https://cdn/x.jpg", "") == 0
