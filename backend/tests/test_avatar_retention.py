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
