"""Every analysis engine reads the four fields an analyst triages on.

    display name, followers, last post date, and location where the
    platform publishes one.

For each platform this feeds the engine's OWN parser a payload in the shape
the platform serves, runs the engine's OWN `fill()` on what came out, and
checks the four fields landed on the Row -- the whole path from payload to
record, not one function in isolation. It then asks shared/completeness.py
for its verdict, so a field the engine claims to collect and a field it
honestly does not (location on TikTok, Telegram and Threads, none of which
publish one) are both pinned.

Facebook is not here: its readers work on a whole-page Harvest rather than
one payload, and they already have their own tests (test_facebook_*).

What this cannot prove is that the platforms still SERVE these shapes --
only a live run (backend/tools/live_probe.py) shows that.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from backend.shared import completeness
from backend.shared.models.row import Row

POSTED = 1_762_000_000                                    # 2025-11-01 UTC
POSTED_ISO = datetime.fromtimestamp(POSTED, timezone.utc).date().isoformat()


def _row(platform: str, handle: str = "brandco") -> Row:
    return Row(url=f"https://example.com/{handle}", target="Brand Co", original_feed="t")


def _verdicts(platform: str, row: Row) -> dict:
    return completeness.field_report(platform, row, want_screenshot=False)


# ------------------------------------------------------------------ instagram

def test_instagram():
    from backend.platforms.instagram.analysis_engine import Scraper
    from backend.platforms.instagram.discovery_engine import profile_from

    blob = {"data": {"user": {
        "username": "brandco", "full_name": "Brand Co", "pk": "1",
        "follower_count": 1234, "media_count": 3, "biography": "official",
        "city_name": "Mumbai", "has_anonymous_profile_picture": False,
        "edge_owner_to_timeline_media": {"count": 3, "edges": [
            {"node": {"taken_at_timestamp": POSTED, "owner": {"username": "brandco"}}},
        ]},
    }}}
    u = profile_from(blob, "brandco")
    row = _row("instagram")
    Scraper.fill(row, u)

    assert row.profile_name == "Brand Co"
    assert row.followers == 1234
    assert row.last_post_iso == POSTED_ISO
    assert row.location == "Mumbai"
    assert set(_verdicts("instagram", row).values()) >= {"read"}
    assert _verdicts("instagram", row)["location"] == "read"


def test_instagram_timeline_payload_gives_the_last_post():
    """The profile record often carries no posts; the timeline response
    does. That is the path most real visits take."""
    from backend.platforms.instagram.discovery_engine import timeline_latest_post
    blob = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": [
        {"node": {"taken_at": POSTED, "user": {"username": "brandco"}}},
        {"node": {"taken_at": POSTED + 999_999, "user": {"username": "someone_else"}}},
    ]}}}
    assert timeline_latest_post(blob, "brandco") == POSTED_ISO


# -------------------------------------------------------------------- threads

def test_threads():
    from backend.platforms.threads.analysis_engine import Scraper, profile_from

    blobs = [{"user": {"username": "brandco", "full_name": "Brand Co", "pk": "1",
                       "follower_count": 1234, "biography": "official"},
              "threads": [{"taken_at": POSTED, "user": {"username": "brandco"}},
                          {"taken_at": POSTED + 999_999, "user": {"username": "other"}}]}]
    u = profile_from(blobs, "brandco")
    row = _row("threads")
    Scraper.fill(row, u)

    assert row.profile_name == "Brand Co"
    assert row.followers == 1234
    assert row.last_post_iso == POSTED_ISO
    # Threads publishes no location: reported as not collected, never a miss.
    assert _verdicts("threads", row)["location"] == "not-collected"


# --------------------------------------------------------------------- tiktok

def test_tiktok():
    from backend.platforms.tiktok.analysis_engine import Scraper
    from backend.platforms.tiktok.discovery_engine import newest_post_iso, profile_from

    blob = {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo": {
        "user": {"uniqueId": "brandco", "nickname": "Brand Co", "signature": "official"},
        "stats": {"followerCount": 1234, "followingCount": 5, "videoCount": 3},
    }}}, "itemList": [{"id": str(POSTED << 32), "desc": "clip",
                       "author": {"uniqueId": "brandco"}}]}
    u = profile_from(blob, "brandco")
    row = _row("tiktok")
    Scraper.fill(row, u)
    # process() sets the date from this, after fill()
    row.last_post_iso = newest_post_iso(blob, "brandco")

    assert row.profile_name == "Brand Co"
    assert row.followers == 1234
    assert row.last_post_iso == POSTED_ISO
    assert _verdicts("tiktok", row)["location"] == "not-collected"


# -------------------------------------------------------------------------- X

@pytest.mark.parametrize("shape", ["legacy", "migrated"])
def test_x(shape):
    """Both payload shapes X has served: everything under `legacy`, and the
    newer one with name/handle in `core`, counts in `relationship_counts`
    and location in its own object."""
    from backend.platforms.twitter.analysis_engine import Scraper
    from backend.platforms.twitter.discovery_engine import _user_from_result, latest_post

    if shape == "legacy":
        res = {"rest_id": "1", "legacy": {
            "screen_name": "brandco", "name": "Brand Co", "followers_count": 1234,
            "location": "Mumbai", "statuses_count": 10}}
    else:
        res = {"rest_id": "1", "core": {"screen_name": "brandco", "name": "Brand Co"},
               "relationship_counts": {"followers": 1234}, "tweet_counts": {"tweets": 10},
               "location": {"location": "Mumbai"}}
    u = _user_from_result(res)
    row = _row("twitter")
    Scraper.fill(row, u)
    timeline = {"tweets": [
        {"rest_id": "9", "legacy": {"id_str": "9", "user_id_str": "1", "full_text": "hi",
                                    "created_at": "Sat Nov 01 10:00:00 +0000 2025"}},
        {"rest_id": "8", "legacy": {"id_str": "8", "user_id_str": "2", "full_text": "not ours",
                                    "created_at": "Mon Nov 10 10:00:00 +0000 2025"}},
    ]}
    row.last_post_iso = latest_post(timeline, "brandco", "1")

    assert row.profile_name == "Brand Co"
    assert row.followers == 1234
    assert row.last_post_iso == "2025-11-01"
    assert row.location == "Mumbai"
    assert _verdicts("twitter", row)["location"] == "read"


# -------------------------------------------------------------------- youtube

@pytest.mark.asyncio
async def test_youtube():
    from backend.platforms.youtube.analysis_engine import Scraper
    from backend.platforms.youtube.discovery_engine import YouTubeAPI

    ch = {"id": "UC1", "snippet": {"title": "Brand Co", "customUrl": "@brandco",
                                  "country": "IN"},
          "statistics": {"subscriberCount": "1234", "videoCount": "3"}}
    row = _row("youtube")
    Scraper.fill(row, ch)
    api = YouTubeAPI(key="test")
    api.get = AsyncMock(return_value={"items": [
        {"snippet": {"publishedAt": "2025-11-01T10:00:00Z"}}]})
    row.last_post_iso = await api.latest_upload("UU1")

    assert row.profile_name == "Brand Co"
    assert row.followers == 1234
    assert row.last_post_iso == "2025-11-01"
    assert row.location == "IN"
    assert _verdicts("youtube", row)["location"] == "read"


# ------------------------------------------------------------------- telegram

def test_telegram_channel():
    from backend.platforms.telegram.analysis_engine import Scraper
    from backend.platforms.telegram.discovery_engine import TelegramEntity

    e = TelegramEntity(entity_id="1", username="brandco", title="Brand Co",
                       kind="channel", members=1234, last_post_iso=POSTED_ISO)
    row = _row("telegram")
    Scraper.fill(row, e)

    assert row.profile_name == "Brand Co"
    assert row.followers == 1234          # a channel's members are its reach
    assert row.last_post_iso == POSTED_ISO
    assert _verdicts("telegram", row)["location"] == "not-collected"


# ----------------------------------------------------------------- the claims

def test_location_is_claimed_exactly_where_an_engine_reads_it():
    """If an engine starts or stops reading location, this list must change
    with it -- otherwise a blank is mis-explained to the analyst."""
    assert completeness.PLATFORMS_WITH_LOCATION == {
        "facebook", "twitter", "youtube", "instagram"}
