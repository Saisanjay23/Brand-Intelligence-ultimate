"""A stored logo verdict can only be replaced by evidence at least as strong.

THE BUG. Measured on this deployment's own data (2026-09-24): 187 Facebook
profiles wear one of 48 generated letter avatars ("A" / "G" on a flat
palette colour), the pixel check recognises all 48, and 180 of those
profiles were still stored as "Logo: Yes" -- because every re-save by a
later sweep wrote the URL rule's answer back over the pixel verdict.
"""

from __future__ import annotations

from backend.database.repositories.profile_repository import (
    PHASE_DISCOVERY, _keep_stronger_logo)
from backend.platforms.facebook.discovery_engine import iter_results
from backend.platforms.twitter.discovery_engine import TwitterUser, _user_from_result, user_to_row
from backend.shared import logo_verdict as lv
from backend.shared.avatars import DEFAULT_AVATAR_FINGERPRINTS, default_avatar_match
from backend.shared.models.scoring import compute_score, resolve_match


def _update(has_logo, strength=None, source=None, **extra):
    sets = {"has_logo": has_logo, "display_name": "x", **extra}
    if strength is not None:
        sets["logo_strength"] = strength
    if source is not None:
        sets["logo_source"] = source
    return {"$set": sets}


class TestSaveKeepsTheStrongerVerdict:
    def test_a_url_yes_cannot_overwrite_a_pixel_no(self):
        existing = {"has_logo": False, "logo_strength": lv.PIXELS, "logo_source": "generated-avatar"}
        upd = _update(True, lv.OBSERVED, "")
        _keep_stronger_logo(existing, {}, upd, PHASE_DISCOVERY)
        assert "has_logo" not in upd["$set"]
        assert upd["$set"]["display_name"] == "x"   # everything else still written

    def test_a_manual_verdict_survives_every_sweep(self):
        existing = {"has_logo": True, "sources": {"logo": "manual"}}
        upd = _update(False, lv.FLAG, "api-anonymous-flag")
        _keep_stronger_logo(existing, {}, upd, PHASE_DISCOVERY)
        assert "has_logo" not in upd["$set"]

    def test_stronger_incoming_evidence_replaces_a_weaker_stored_one(self):
        existing = {"has_logo": True}                     # legacy, OBSERVED
        upd = _update(False, lv.FLAG, "api-anonymous-flag")
        _keep_stronger_logo(existing, {}, upd, PHASE_DISCOVERY)
        assert upd["$set"]["has_logo"] is False

    def test_a_new_picture_is_new_evidence_whatever_its_strength(self):
        existing = {"has_logo": False, "logo_strength": lv.PIXELS}
        upd = _update(True, lv.OBSERVED, "", avatar_changed_at="now")
        _keep_stronger_logo(existing, {}, upd, PHASE_DISCOVERY)
        assert upd["$set"]["has_logo"] is True

    def test_a_document_with_no_verdict_takes_the_incoming_one(self):
        upd = _update(True, lv.OBSERVED, "")
        _keep_stronger_logo({}, {}, upd, PHASE_DISCOVERY)
        assert upd["$set"]["has_logo"] is True


class TestDefaultAvatarReferences:
    def test_every_reference_matches_itself(self):
        for platform, refs in DEFAULT_AVATAR_FINGERPRINTS.items():
            for p, d, name in refs:
                assert default_avatar_match(platform, p, d) == name

    def test_a_reference_only_applies_to_its_own_platform(self):
        p, d, _ = DEFAULT_AVATAR_FINGERPRINTS["instagram"][0]
        assert default_avatar_match("tiktok", p, d) == ""

    def test_a_distant_image_does_not_match(self):
        # the nearest REAL avatar measured on stored data sat 16 bits away
        p, d, _ = DEFAULT_AVATAR_FINGERPRINTS["facebook"][0]
        flipped = format(int(p, 16) ^ 0xFFFF, "016x")      # 16 bits differ
        assert default_avatar_match("facebook", flipped, d) == ""

    def test_missing_hashes_are_no_match_not_an_error(self):
        assert default_avatar_match("facebook", "", "") == ""


class TestValidationDoesNotMakeAStockAvatarALogo:
    def test_a_validated_profile_with_a_confirmed_stock_avatar_has_no_logo(self):
        assert resolve_match(False, None, True, automated_veto=True) is False

    def test_a_validated_profile_nobody_judged_the_picture_of_keeps_the_default(self):
        assert resolve_match(None, None, True, automated_veto=True) is True

    def test_the_name_default_is_untouched(self):
        assert resolve_match(False, None, True) is True

    def test_an_analysts_call_still_wins(self):
        assert resolve_match(False, True, True, automated_veto=True) is True

    def test_the_score_follows(self):
        stock = compute_score(False, True, True, "", validated=True)
        real = compute_score(True, True, True, "", validated=True)
        assert stock < real


class TestMissingPictureIsUnknownNotNo:
    def test_x_with_no_avatar_is_unknown(self):
        assert TwitterUser(handle="a").has_custom_pic is None

    def test_x_default_flag_is_authoritative(self):
        u = _user_from_result({"rest_id": "1", "core": {"screen_name": "a", "name": "A"},
                               "legacy": {"default_profile_image": True},
                               "avatar": {"image_url": "https://pbs.twimg.com/profile_images/1/x_normal.jpg"}})
        assert u.default_pic is True and u.has_custom_pic is False
        row = user_to_row(u, "a")
        assert lv.row_evidence(row).strength == lv.FLAG

    def test_x_default_asset_url_is_still_caught_without_the_flag(self):
        u = TwitterUser(handle="a", avatar="https://abs.twimg.com/sticky/default_profile_images/default_profile_400x400.png")
        assert u.has_custom_pic is False

    def test_facebook_search_edge_with_no_picture_is_unknown(self):
        blob = {"edges": [{"rendering_strategy": {"view_model": {
            "__typename": "SearchProfileViewModel",
            "profile": {"__typename": "User", "id": "123", "name": "A",
                        "profile_url": "https://www.facebook.com/a"}}}}]}
        (hit,) = list(iter_results(blob))
        assert hit.has_custom_pic is None
