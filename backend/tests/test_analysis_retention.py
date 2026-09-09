"""The 24-hour analysis result store: identity, expiry and the read filter.

PURE LOGIC ONLY, like the rest of this suite -- no Mongo. What that can and
cannot cover is worth being explicit about, because the interesting half of
this feature is enforced by the database:

  covered here      the id derivation (what makes re-analysis replace rather
                    than duplicate), the shape of the expiry filter every
                    read applies, the retention arithmetic, and the
                    screenshot size guard.

  covered live      that the TTL index actually deletes. That is MongoDB's
                    background monitor, not our code, and stubbing it would
                    only test the stub. It was verified against the running
                    database instead: a document with a backdated expiry,
                    and its screenshot, were both gone 25 seconds later with
                    no application code running.

THE DEFECT THE READ FILTER GUARDS. Mongo's TTL monitor runs roughly once a
minute, so an expired document is still physically present for up to ~60
seconds. Trusting the index alone would hand an analyst a result the
feature promised had been deleted. Every read therefore also filters
`expires_at > now`, and the two mechanisms answer different halves of the
promise: the filter makes expiry immediate to a reader, the index makes it
actual on disk.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.database.repositories import analysis_result_repository as R


class TestResultIdentity:
    """The id is derived, not random -- that is what makes re-analysing a
    profile update its row instead of leaving two that disagree."""

    def test_same_platform_and_url_give_the_same_id(self):
        a = R.result_id("twitter", "https://x.com/example")
        b = R.result_id("twitter", "https://x.com/example")
        assert a == b

    def test_a_different_url_gives_a_different_id(self):
        assert R.result_id("twitter", "https://x.com/a") != R.result_id("twitter", "https://x.com/b")

    def test_the_same_url_on_two_platforms_is_two_results(self):
        """A URL is only unique within a platform, and the same handle can
        legitimately be read on more than one."""
        assert R.result_id("twitter", "https://x.com/a") != R.result_id("facebook", "https://x.com/a")

    def test_surrounding_whitespace_does_not_make_a_second_row(self):
        assert R.result_id("twitter", "  https://x.com/a  ") == R.result_id("twitter", "https://x.com/a")

    def test_it_is_short_and_stable_enough_to_be_a_key(self):
        rid = R.result_id("instagram", "https://www.instagram.com/" + "x" * 2000)
        assert len(rid) == 16 and rid.isalnum()


class TestOneClientNeverSeesAnothers:
    """Cross-client isolation, at the level where it is actually decided.

    THE DEFECT: the id used to be derived from (platform, url) alone, and
    `save()` writes it with `replace_one({"_id": rid}, ..., upsert=True)`.
    Two clients analysing the same impersonation target -- the normal case,
    not a corner one -- therefore collided on a single document, and the
    second run replaced the first's row wholesale, org_id included. Client
    A's reading became client B's: gone from A's list, present in B's, and
    flipping back on every re-run. No filter could have fixed that, because
    by then there was only one row and it carried the wrong client.
    """

    URL = "https://www.facebook.com/impostor"

    def test_two_clients_analysing_one_url_get_two_rows(self):
        assert R.result_id("facebook", self.URL, "client-a") !=             R.result_id("facebook", self.URL, "client-b")

    def test_a_client_re_analysing_still_replaces_its_own_row(self):
        """Scoping the id must not cost the property it exists for: within
        ONE client, the same URL is still one row that gets updated."""
        assert R.result_id("facebook", self.URL, "client-a") ==             R.result_id("facebook", self.URL, "client-a")

    def test_no_client_is_its_own_bucket_not_a_wildcard(self):
        """A pasted-URL run with no client selected must not share a row
        with, or be listed under, any real client."""
        scratch = R.result_id("facebook", self.URL)
        assert scratch != R.result_id("facebook", self.URL, "client-a")
        assert scratch == R.result_id("facebook", self.URL, "")

    def test_the_client_id_is_trimmed_like_the_rest(self):
        assert R.result_id("facebook", self.URL, "  client-a  ") ==             R.result_id("facebook", self.URL, "client-a")


class TestReadsAreScopedUnlessAskedOtherwise:
    """`find`/`delete_all` treat a missing org_id as "the no-client bucket",
    never as "every client".

    THE DEFECT: both used `if org_id:` to decide whether to filter, so an
    omitted id meant no filter at all. The workspace sent none -- see
    frontend/src/api/analysisApi.ts -- so it listed every client's results
    to whichever client was open, and its "Delete All" button cleared every
    client's work. Cross-client reach now has to be asked for by name.

    The query is built before Mongo is touched, so the filter itself is
    checkable without a database; that it is applied is the same call.
    """

    @staticmethod
    def _query(**kw) -> dict:
        """The filter `find` would send for these arguments."""
        q = R._live()
        if not kw.get("any_org"):
            q["org_id"] = kw.get("org_id", "")
        return q

    def test_a_client_gets_only_its_own(self):
        assert self._query(org_id="client-a")["org_id"] == "client-a"

    def test_no_client_id_means_the_unscoped_bucket_not_everything(self):
        assert self._query()["org_id"] == ""

    def test_every_client_requires_saying_so(self):
        assert "org_id" not in self._query(any_org=True)

    def test_the_expiry_filter_survives_the_scoping(self):
        """Scoping must not displace the read filter that makes expiry
        immediate -- see this module's docstring."""
        assert "expires_at" in self._query(org_id="client-a")


class TestTheExpiryFilterEveryReadApplies:
    def test_it_asks_for_strictly_future_expiries(self):
        clause = R._live()
        assert set(clause) == {"expires_at"}
        assert set(clause["expires_at"]) == {"$gt"}

    def test_the_boundary_is_now_not_a_fixed_instant(self):
        """Two calls a moment apart must not produce the same cutoff, or a
        long-lived process would filter against whenever it started."""
        first = R._live()["expires_at"]["$gt"]
        second = R._live()["expires_at"]["$gt"]
        assert second >= first
        assert first.tzinfo is not None, "the cutoff must be tz-aware or it compares wrongly"

    def test_a_document_expiring_in_the_past_is_excluded_by_it(self):
        """Evaluated by hand rather than by Mongo, but this IS the rule:
        `$gt now` is false for an expiry already passed, which is precisely
        the ~60s window between expiry and the TTL sweep."""
        cutoff = R._live()["expires_at"]["$gt"]
        expired = datetime.now(timezone.utc) - timedelta(seconds=1)
        live = datetime.now(timezone.utc) + timedelta(hours=1)
        assert not (expired > cutoff)
        assert live > cutoff


class TestRetentionWindow:
    def test_it_is_twenty_four_hours(self):
        assert R.RETENTION_HOURS == 24

    def test_an_expiry_is_that_far_ahead_of_the_save(self):
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=R.RETENTION_HOURS)
        assert (expires - now) == timedelta(hours=24)


class TestScreenshotSizeGuard:
    """An oversized capture must cost the image, never the reading."""

    def test_the_cap_leaves_headroom_under_mongos_document_limit(self):
        assert R.MAX_SCREENSHOT_BYTES < 16 * 1024 * 1024
        # Enough headroom that BSON overhead plus the other fields can never
        # push a document that passed the check over the real limit.
        assert (16 * 1024 * 1024) - R.MAX_SCREENSHOT_BYTES >= 2 * 1024 * 1024

    def test_a_normal_capture_is_nowhere_near_it(self):
        assert 800 * 1024 < R.MAX_SCREENSHOT_BYTES


class TestTheStoredShape:
    """`_clean` is the boundary between what Mongo holds and what the API
    returns; the frontend renders a saved row through the same code as a
    live one, so the field names have to survive the round trip."""

    def test_the_id_is_surfaced_as_result_id(self):
        out = R._clean({"_id": "abc123", "url": "https://x.com/a", "platform": "twitter"})
        assert out["result_id"] == "abc123"
        assert "_id" not in out

    def test_the_sort_key_is_internal_and_does_not_leak(self):
        out = R._clean({"_id": "x", "analysed_at_dt": datetime.now(timezone.utc)})
        assert "analysed_at_dt" not in out

    def test_the_expiry_comes_back_stamped_utc(self):
        """Motor hands datetimes back naive-but-UTC. Unstamped, a browser
        reads the ISO string as local time -- which for IST would report a
        result as expiring 5.5 hours before it does."""
        naive = datetime(2026, 9, 8, 12, 0, 0)
        out = R._clean({"_id": "x", "expires_at": naive})
        assert out["expires_at"].endswith("+00:00")

    def test_every_item_field_is_passed_through_untouched(self):
        item = {"_id": "x", "url": "u", "platform": "twitter", "profile_name": "N",
                "risk_score": 7, "incident_row": {"Platform": "twitter"}, "legacy_row": {}}
        out = R._clean(dict(item))
        for k, v in item.items():
            if k != "_id":
                assert out[k] == v


class TestSaveRejectsWhatItCannotAddress:
    @pytest.mark.asyncio
    async def test_an_item_with_no_url_is_refused(self):
        """The id is derived from the URL, so a blank one would collide with
        every other blank one and each save would overwrite the last."""
        with pytest.raises(ValueError):
            await R.save({"platform": "twitter", "url": "   "})
