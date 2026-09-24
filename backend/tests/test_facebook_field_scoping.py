"""Facebook analysis reads a profile's fields from THAT profile only.

A Facebook profile page carries a great deal that is not the profile: the
notification flyout, suggested Pages and people, sponsored units, the
viewer's own chrome. Every tier that read "any payload on the page" could
hand the profile somebody else's number, city, picture or post date. The
shapes below are minimal copies of what a live capture (2026-09-24) showed.
"""

from __future__ import annotations

import json
import time

from backend.platforms.facebook.analysis_engine import (
    Harvest, _is_page_profile, _map_linked_address, read_counts,
    read_last_post, read_location, read_name, read_pic)
from backend.shared.models.row import Row

PID = "100057408731013"
STRANGER = "999000111222333"
NOW = int(time.time())
OLD = NOW - 400 * 86400


def _harvest(*blobs, text: str = "", gql=()) -> Harvest:
    h = Harvest()
    h.add_embedded([json.dumps(b) for b in blobs])
    h.gql = list(gql)
    h.text = {"main": text}
    return h.scoped(PID)


def _entity(**extra) -> dict:
    return {"id": PID, "name": "Cyfirma JP", **extra}


def _post(actor: str, ts: int) -> dict:
    return {"post_id": str(ts), "creation_time": ts, "actors": [{"id": actor}]}


class TestLastPostIsTheProfilesOwn:
    def test_a_strangers_fresh_post_on_the_page_is_not_this_profiles(self):
        h = _harvest({"user": _entity(timeline=[_post(PID, OLD)])},
                     {"notifications": [_post(STRANGER, NOW)]})
        row = Row(url="u", target="")
        read_last_post(row, h)
        assert row.last_post_iso == time.strftime("%Y-%m-%d", time.gmtime(OLD))

    def test_a_bare_timestamp_anywhere_on_the_page_is_never_a_post_date(self):
        """The removed ungated tier: a `publish_time` in a story rail or the
        HTML, with no post around it, used to become 'last post: today'."""
        h = _harvest({"user": _entity()}, {"stories": [{"publish_time": NOW}]})
        row = Row(url="u", target="")
        read_last_post(row, h)
        assert row.last_post_iso == ""

    def test_a_post_with_no_actors_is_not_attributed(self):
        h = _harvest({"user": _entity(timeline=[{"post_id": "1", "creation_time": NOW}])})
        row = Row(url="u", target="")
        read_last_post(row, h)
        assert row.last_post_iso == ""

    def test_an_unresolved_id_leaves_the_date_blank_and_says_so(self):
        h = Harvest()
        h.add_embedded([json.dumps({"x": _post(STRANGER, NOW)})])
        row = Row(url="u", target="")
        read_last_post(row, h.scoped(""))
        assert row.last_post_iso == "" and "not attributed" in row.notes


class TestCountsNameLocationPictureAreScoped:
    def test_a_suggested_pages_follower_count_is_not_this_profiles(self):
        h = _harvest({"user": _entity()},
                     gql=[{"suggested": {"id": STRANGER, "follower_count": 5_000_000}}],
                     text="Suggested for you\n5M people follow this")
        row = Row(url="u", target="")
        read_counts(row, h)
        assert row.followers is None

    def test_the_viewers_city_is_not_this_profiles_location(self):
        h = _harvest({"user": _entity()},
                     gql=[{"viewer": {"id": STRANGER, "current_city_name": "Mumbai, India"}}])
        row = Row(url="u", target="")
        read_location(row, h)
        assert row.location == ""

    def test_a_strangers_name_is_not_used_once_the_entity_is_known(self):
        h = _harvest({"user": {"id": PID, "is_viewer_friend": False}},
                     gql=[{"sponsored": {"page_name": "Some Advertiser"}}])
        row = Row(url="u", target="")
        read_name(row, h)
        assert row.profile_name != "Some Advertiser"

    def test_a_strangers_picture_is_not_this_profiles_logo(self):
        h = _harvest({"user": _entity()},
                     gql=[{"other": {"uri": "https://scontent.fbcdn.net/v/t39/1_2_3_n.jpg"}}])
        row = Row(url="u", target="")
        read_pic(row, h)
        assert row.has_custom_pic is None and row.profile_pic_url == ""


class TestPageAddressTile:
    def _tile(self, text: str, link: str) -> dict:
        return {"id": PID, "profile_tile_items": {"nodes": [{"tile_item": {
            "item_subtitle": {"text": {"text": text, "ranges": [
                {"entity": {"__typename": "ExternalUrl", "external_url": link}}]}}}}]}}

    def test_the_map_linked_tile_is_the_address(self):
        h = _harvest(self._tile("Chiyoda-ku, Tokyo, Japan",
                                "https://www.bing.com/maps/default.aspx?v=2&pc=FACEBK"))
        assert _map_linked_address(h) == "Chiyoda-ku, Tokyo, Japan"
        row = Row(url="u", target="")
        read_location(row, h)
        assert row.location == "Chiyoda-ku, Tokyo, Japan"

    def test_a_website_tile_is_not_an_address(self):
        h = _harvest(self._tile("cyfirma.jp", "https://cyfirma.jp/"))
        assert _map_linked_address(h) == ""

    def test_a_tile_in_a_payload_about_somebody_else_is_ignored(self):
        tile = self._tile("Somewhere Else", "https://www.bing.com/maps/x")
        tile["id"] = STRANGER
        h = Harvest()
        h.add_embedded([json.dumps(tile)])
        assert _map_linked_address(h.scoped(PID)) == ""


class TestNewStylePagesAreRecognised:
    def test_a_delegate_page_means_a_page(self):
        h = _harvest(_entity(delegate_page={"id": "254807731826340", "category_name": "Product/service"}))
        assert _is_page_profile(h) is True

    def test_a_person_has_no_delegate_page(self):
        assert _is_page_profile(_harvest(_entity(delegate_page=None))) is False
