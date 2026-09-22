"""An Instagram account that has never posted must never be dated TODAY.

THE REPORT. Some Instagram profiles with no posts came back from analysis
carrying the current date as their last-post date. Not blank, not an error:
a confident, present-day date, which then scored the account as ACTIVE and
put it in an analyst's queue as a live impersonation. That is the worst
shape a defect can take here -- it looks exactly like a correct answer.

THE ROOT CAUSE, one sentence: a timestamp was read from something the
profile does not own.

Three independent paths could do it, and the fix closes all three, because
closing any one of them alone would have left the same symptom arriving by
a different route.

    1. THE PAYLOAD. `PROFILE_ENDPOINTS` matches the substrings
       "api/graphql" and "graphql/query". The modern web client answers a
       profile visit with several responses on those same paths -- among
       them the VIEWER'S OWN home feed and recommendations, which
       instagram/analysis_engine.py has documented as firing on every
       visit since the beginning. Their media carry fresh `taken_at`
       values, and their nested children (carousel items, clips metadata)
       carry the timestamp with NO `user` object beside them. So
       `timeline_latest_post`'s "no owner named, take it anyway" fallback
       read today's date off a stranger's post and handed it back as this
       profile's.

    2. THE WRITE. Even where Instagram's own record said media_count = 0,
       the assignment in `process()` carried no guard -- and it also
       overwrote `posts_seen` with "yes", destroying the one field that
       could afterwards have contradicted the date.

    3. THE DOM. `read_last_post_date`'s selectors ran over the whole
       document, and it was reached whenever the post count was merely
       UNKNOWN (`posts_seen != "no"`). A profile with no grid of its own
       still renders post tiles -- suggestions, an Explore rail -- and
       those tiles are recent by construction.

THE RULE THESE TESTS HOLD. Every date is attributed to this profile before
it is recorded, and a date that cannot be attributed is not recorded at
all. Blank is the honest outcome: shared/models/row.py::active_yes already
depends on it, because `last_post_date` is empty exactly when the date is
unknown -- which only works if nothing writes a date it cannot attribute.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.platforms.instagram.analysis_engine import Scraper
from backend.platforms.instagram.discovery_engine import (_latest_post,
                                                          timeline_latest_post,
                                                          user_from_node)

NOW = datetime.now(timezone.utc)
TODAY = int(NOW.timestamp())
TODAY_ISO = NOW.date().isoformat()
# A real, unambiguous past post.
OLD = int((NOW - timedelta(days=400)).timestamp())
OLD_ISO = (NOW - timedelta(days=400)).date().isoformat()


def _home_feed() -> dict:
    """The viewer's own feed, as it arrives on `api/graphql` during a
    profile visit: other people's posts, published today, with the
    carousel children carrying the timestamp and no owner of their own."""
    return {"data": {"xdt_api__v1__feed__connection": {"edges": [
        {"node": {
            "user": {"username": "someone_else"},
            "taken_at": TODAY,
            "carousel_media": [{"taken_at": TODAY}, {"taken_at": TODAY}],
        }},
    ]}}}


def _own_timeline(username: str, ts: int) -> dict:
    return {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
        "edges": [{"node": {"user": {"username": username}, "taken_at": ts}}],
    }}}


class TestTheTimelinePayload:
    def test_somebody_elses_post_is_not_this_profiles_last_post(self):
        """The reported bug, at its source. `blankaccount` has never
        posted; the only timestamps on the wire belong to the analyst's
        own feed."""
        assert timeline_latest_post(_home_feed(), "blankaccount") == ""

    def test_the_profiles_own_timeline_still_reads(self):
        """The fix must not cost the thing this function exists for -- it
        was added because the date was blank on 93 of 310 stored rows."""
        assert timeline_latest_post(_own_timeline("adaniparivar", OLD),
                                    "adaniparivar") == OLD_ISO

    def test_an_unowned_node_inside_a_named_user_timeline_still_counts(self):
        """The fallback is kept where it is safe: inside a container
        Instagram itself labels a user timeline, an unattributed post has
        nowhere else to have come from."""
        blob = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
            "edges": [{"node": {"taken_at": OLD}}]}}}
        assert timeline_latest_post(blob, "adaniparivar") == OLD_ISO

    def test_a_stranger_in_the_same_payload_cannot_win(self):
        """Live capture: the profile's own timeline response really does
        mention other accounts (tagged users, co-authors, suggestions).
        The newest timestamp in the payload is theirs; the answer is still
        the profile's own oldest-but-owned post."""
        blob = _own_timeline("adaniparivar", OLD)
        blob["data"]["suggested"] = {
            "user": {"username": "gautam.adani"}, "taken_at": TODAY}
        assert timeline_latest_post(blob, "adaniparivar") == OLD_ISO

    def test_asking_about_nobody_in_particular_is_unchanged(self):
        """A caller with no username to check against still gets the old
        behaviour -- the scoping is what the caller asked for, not a new
        blanket refusal."""
        assert timeline_latest_post(_home_feed()) == TODAY_ISO


class TestTheProfileRecord:
    def test_a_zero_post_account_yields_no_date_from_its_own_record(self):
        """The profile payload carries timestamps that are not posts -- a
        highlight cover, a tagged-media preview, a suggestion rail. When
        Instagram says the media count is zero, none of them can be a post
        of this account's."""
        node = {"username": "blankaccount", "media_count": 0,
                "highlights": [{"taken_at": TODAY}]}
        assert _latest_post(node, posts=0) == ""
        assert user_from_node(node).last_post_iso == ""

    def test_a_story_timestamp_is_not_a_post_date(self):
        """A guard on the narrowing, not a reproduced defect: this key was
        never read, and it must stay that way. `latest_reel_media` is the
        account's newest STORY, and a story expires within 24 hours -- so
        it ALWAYS reads as today or yesterday. It is the most dangerous
        near-miss in this payload for anyone widening the key list later.
        """
        node = {"username": "someone", "media_count": 7,
                "latest_reel_media": TODAY}
        assert _latest_post(node, posts=7) == ""

    def test_an_unknown_post_count_is_not_treated_as_zero(self):
        """Unknown is not "none". A record with no media_count must still
        be allowed to yield its own post date."""
        node = {"username": "someone", "taken_at": OLD}
        assert _latest_post(node, posts=None) == OLD_ISO

    def test_a_real_post_still_reads(self):
        node = {"username": "someone", "media_count": 3,
                "edges": [{"node": {"taken_at": OLD}}]}
        assert _latest_post(node, posts=3) == OLD_ISO


class TestTheGridAltText:
    """`Scraper._parse_alt_date` -- the free DOM tier, which reads a post
    tile's own accessibility alt text ("Photo by X on August 19, 2026.").

    The owner check here REJECTS ONLY ON POSITIVE EVIDENCE. An unreadable
    author, an alt text in another language, a display name that changed
    since the profile record was read -- each of those keeps the date. The
    check can only ever remove a date belonging to a different account; it
    cannot blank out a real one because the markup moved.
    """

    OWNERS = ("adaniparivar", "Adani Group")

    def test_a_tile_belonging_to_someone_else_is_dropped(self):
        alt = f"Photo by randomstranger on {NOW.strftime('%B %d, %Y')}."
        assert Scraper._parse_alt_date(alt, self.OWNERS) == ""

    def test_the_profiles_own_handle_is_accepted(self):
        alt = "Photo by adaniparivar on August 19, 2026."
        assert Scraper._parse_alt_date(alt, self.OWNERS) == "2026-08-19"

    def test_the_profiles_display_name_is_accepted(self):
        """Instagram writes whichever of the two it feels like."""
        alt = "Photo shared by Adani Group on August 08, 2026 tagging @x."
        assert Scraper._parse_alt_date(alt, self.OWNERS) == "2026-08-08"

    def test_separators_in_the_name_do_not_matter(self):
        alt = "Photo by adani.parivar on August 19, 2026."
        assert Scraper._parse_alt_date(alt, self.OWNERS) == "2026-08-19"

    def test_an_unreadable_author_keeps_the_date(self):
        """Fail open: no author parsed means no rejection, which is the
        behaviour this tier had before the check existed."""
        alt = "on August 19, 2026."
        assert Scraper._parse_alt_date(alt, self.OWNERS) == "2026-08-19"

    def test_with_no_owners_to_check_against_nothing_is_rejected(self):
        alt = "Photo by randomstranger on August 19, 2026."
        assert Scraper._parse_alt_date(alt, ()) == "2026-08-19"

    def test_a_future_date_is_still_refused(self):
        ahead = NOW + timedelta(days=30)
        alt = f"Photo by adaniparivar on {ahead.strftime('%B %d, %Y')}."
        assert Scraper._parse_alt_date(alt, self.OWNERS) == ""


class TestTheWriteSite:
    """`Scraper.timeline_verdict` -- the last gate before a date is stored.

    Defence in depth on purpose. `timeline_latest_post` is what stops a
    stranger's timestamp arriving; this is what refuses to write one even
    if a future payload shape gets past it.
    """

    def test_a_known_postless_account_is_never_dated(self):
        date, note = Scraper.timeline_verdict("no", False, [TODAY_ISO])
        assert date == ""
        assert "no posts" in note

    def test_the_disagreement_is_put_on_the_row_not_swallowed(self):
        """Two sources contradicting each other is a fact about this
        profile. Resolving it silently is how it stops being visible."""
        _, note = Scraper.timeline_verdict("no", False, [TODAY_ISO])
        assert note

    def test_a_confirmed_posting_account_keeps_its_date(self):
        assert Scraper.timeline_verdict("yes", False, [OLD_ISO]) == (OLD_ISO, "")

    def test_an_unknown_post_count_still_accepts_an_attributed_date(self):
        """By the time a date reaches here it has already been attributed
        to this profile, so an unreadable post count is no reason to
        discard it."""
        assert Scraper.timeline_verdict("", False, [OLD_ISO]) == (OLD_ISO, "")

    def test_an_earlier_tier_wins(self):
        """The profile payload's own date is cheaper and more direct; this
        tier only ever fills a gap."""
        assert Scraper.timeline_verdict("yes", True, [TODAY_ISO]) == ("", "")

    def test_the_newest_of_several_is_taken(self):
        """Instagram pins up to 3 posts to the top of a grid, so position
        is not recency -- max() is."""
        assert Scraper.timeline_verdict(
            "yes", False, ["2025-01-01", OLD_ISO, "2024-06-06"]) == (OLD_ISO, "")

    def test_nothing_in_means_nothing_out(self):
        assert Scraper.timeline_verdict("yes", False, []) == ("", "")
