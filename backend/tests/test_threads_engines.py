"""Threads: search cards, walls, every way a sweep can end, and the profile reader.

The page shapes here were measured live on 2026-11-04 (a signed-in Chrome on
threads.com): the Profiles tab lists ~54 accounts and then simply ends; an
empty search prints "No results." and then a "Follow suggestions" rail of
strangers; a never-joined or signed-out session still renders post cards
behind a "Say more with Threads" dialog; a profile page embeds a typed record
with an exact follower count and owner-tagged posts.

The failure these tests exist to prevent is the one the project is built
around: a search that was never answered reading as a search that found
nothing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.platforms import registry
from backend.platforms.threads import analysis_engine as ae
from backend.platforms.threads import discovery_engine as de
from backend.shared import pushback, resilience


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pushback.reset_all()

    async def _instant(*_a, **_k):
        return None

    monkeypatch.setattr(de.asyncio, "sleep", _instant)
    monkeypatch.setattr(de, "_page_gap", AsyncMock())
    yield
    pushback.reset_all()


# ---------------------------------------------------------------- cards


def _card(handle, name="Some Name", followers="12 followers", bio=None, **kw):
    lines = [handle, name, "Follow"] + ([bio] if bio else []) + ([followers] if followers else [])
    return {"u": handle, "lines": lines, "img": "https://instagram.fx.fna.fbcdn.net/a.jpg",
            "ver": False, **kw}


class TestCards:
    def test_a_full_card_gives_name_bio_and_count(self):
        u = de.user_from_card(_card("nikendr", "Niken Ritami", "5,880 followers",
                                    bio="Photographer / Graphic Designer"))
        assert (u.username, u.full_name, u.followers) == ("nikendr", "Niken Ritami", 5880)
        assert u.followers_exact is True
        assert u.biography == "Photographer / Graphic Designer"
        assert u.url == "https://www.threads.com/@nikendr"

    def test_an_abbreviated_count_is_not_called_exact(self):
        u = de.user_from_card(_card("savithri", "Shiva", "164K followers"))
        assert u.followers == 164_000 and u.followers_exact is False

    def test_the_compact_top_block_has_a_name_and_no_count(self):
        u = de.user_from_card({"u": "nike", "lines": ["nike", "Nike"], "img": "", "ver": False})
        assert u.full_name == "Nike" and u.followers is None

    def test_the_button_label_is_never_taken_for_a_name_or_bio(self):
        u = de.user_from_card({"u": "ab", "lines": ["ab", "Follow", "43 followers"],
                               "img": "", "ver": False})
        assert u.full_name == "" and u.biography == "" and u.followers == 43

    @pytest.mark.parametrize("card", [
        {"u": "iguana84956", "lines": [], "img": "x", "ver": False},          # the nav icon
        {"u": "someone", "lines": ["somebody else", "Name"], "img": "", "ver": False},
        {"u": "", "lines": ["x"], "img": "", "ver": False},
        "not a card",
    ])
    def test_what_is_not_a_card_is_dropped(self, card):
        assert de.user_from_card(card) is None

    def test_a_picture_less_account_reads_as_unknown_not_as_having_one(self):
        u = de.user_from_card({"u": "ab", "lines": ["ab", "Name"], "img": "", "ver": False})
        assert u.has_custom_pic is None


class TestCardScript:
    """The in-page script cannot run here, so its two live-measured rules are
    pinned by name. Both came from the real page, not from guessing."""

    def test_the_rail_is_told_apart_by_its_heading(self):
        assert "Follow suggestions" in de._JS_CARDS

    def test_there_is_no_narrow_column_filter(self):
        # The first draft kept only cards near the leftmost one. The live page
        # lays its top block out as a grid of wide tiles (x = 249..703), so
        # that rule silently dropped 4 real accounts of 54.
        assert "minX" not in de._JS_CARDS

    def test_a_card_must_start_with_its_own_handle(self):
        # what keeps the viewer's own nav icon out of the results
        assert "cl[0].toLowerCase() !== u.toLowerCase()" in de._JS_CARDS


# ---------------------------------------------------------------- walls


class TestWalls:
    @pytest.mark.parametrize("url,want", [
        ("https://www.threads.com/login/?next=%2F", "login"),
        ("https://www.threads.com/accounts/login/", "login"),
        ("https://www.threads.com/challenge/abc", "checkpoint"),
        ("https://www.threads.com/search?q=nike", ""),
    ])
    def test_the_url_path_decides_first(self, url, want):
        assert de.wall_kind(url, []) == want

    def test_the_signed_out_dialog_is_a_login_wall(self):
        assert de.wall_kind("https://www.threads.com/search?q=a",
                            ["Top", "Say more with Threads"]) == "login"

    def test_a_never_joined_account_is_named_as_such(self):
        assert de.wall_kind("https://www.threads.com/search?q=a",
                            ["Join using your Meta account", "iguana84956"]) == "not-joined"

    def test_a_bio_quoting_the_wall_copy_is_not_a_wall(self):
        # whole lines only -- a longer line that merely contains the phrase is content
        assert de.wall_kind("https://www.threads.com/search?q=a",
                            ["Love it. Say more with Threads, they said"]) == ""


# ---------------------------------------------------------------- sweep


def _discovery(snaps, **opts):
    """A Discovery whose tab replays `snaps` (the last one repeats)."""
    args = SimpleNamespace(timeout=5, max_results=0, max_pages=0, cancel=None, **opts)
    seq = list(snaps)

    async def evaluate(_script, *_a):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate)
    page.mouse.wheel = AsyncMock()
    page.url = "https://www.threads.com/search?q=k"
    d = de.Discovery(args, MagicMock())
    d._tab = page
    return d


def _snap(cards=(), lines=("Top", "Recent", "Profiles"), url="https://www.threads.com/search?q=k"):
    return {"url": url, "lines": list(lines), "cards": list(cards)}


class TestSweepEndings:
    @pytest.mark.asyncio
    async def test_a_short_list_that_stops_growing_is_exhausted(self):
        cards = [_card(f"user{i}") for i in range(3)]
        out = await _discovery([_snap(cards)]).sweep("kw")
        assert (out.stopped, out.complete, len(out.hits)) == ("exhausted", True, 3)
        assert resilience.sweep_outcome(out.stopped, out.complete) == resilience.SATISFIED

    @pytest.mark.asyncio
    async def test_a_list_stopping_at_the_platform_ceiling_is_truncated_not_exhausted(self):
        cards = [_card(f"user{i}") for i in range(de.PLATFORM_RESULT_CEILING + 4)]
        out = await _discovery([_snap(cards)]).sweep("kw")
        assert out.stopped == "cap:platform" and out.complete is False
        assert resilience.sweep_outcome(out.stopped, out.complete) == resilience.TRUNCATED
        assert "ceiling" in resilience.describe_stop("cap:platform")

    @pytest.mark.asyncio
    async def test_the_analysts_own_cap_is_satisfied(self):
        cards = [_card(f"user{i}") for i in range(6)]
        d = _discovery([_snap(cards)])
        d.a.max_results = 2
        out = await d.sweep("kw")
        assert out.stopped == "cap:results" and len(out.hits) == 2
        assert resilience.sweep_outcome(out.stopped, out.complete) == resilience.SATISFIED

    @pytest.mark.asyncio
    async def test_a_confirmed_empty_search_is_a_real_zero(self):
        snap = _snap(lines=["Top", "Recent", "Profiles", "No results.", "Follow suggestions"])
        out = await _discovery([snap]).sweep("kw")
        assert (out.stopped, out.complete, out.hits) == ("no-results", True, [])
        assert resilience.sweep_outcome(out.stopped, out.complete) == resilience.SATISFIED

    @pytest.mark.asyncio
    async def test_a_search_nobody_answered_is_never_a_clean_zero(self):
        out = await _discovery([_snap(lines=["Top", "Recent", "Profiles"])]).sweep("kw")
        assert out.stopped == "empty-unconfirmed" and out.hits == []
        assert resilience.sweep_outcome(out.stopped, out.complete) == resilience.BROKEN

    @pytest.mark.asyncio
    async def test_cards_behind_the_signed_out_dialog_are_not_hits(self):
        # Measured: the dialog sits over real post cards, which parse fine.
        snap = _snap([_card("poster")], lines=["Top", "Say more with Threads"])
        out = await _discovery([snap]).sweep("kw")
        assert out.stopped == "login" and out.hits == []
        assert resilience.classify_failure(out.error) == "expired"

    @pytest.mark.asyncio
    async def test_a_redirect_to_login_is_a_wall(self):
        out = await _discovery([_snap(url="https://www.threads.com/login/?next=x")]).sweep("kw")
        assert out.stopped == "login"

    @pytest.mark.asyncio
    async def test_an_account_that_never_joined_threads_says_so(self):
        snap = _snap([_card("poster")], lines=["Join using your Meta account", "iguana84956"])
        out = await _discovery([snap]).sweep("kw")
        assert out.stopped == "login" and "not joined Threads" in out.error
        assert resilience.classify_failure(out.error) == "expired"

    @pytest.mark.asyncio
    async def test_a_challenge_is_a_checkpoint(self):
        out = await _discovery([_snap(url="https://www.threads.com/challenge/x")]).sweep("kw")
        assert out.stopped == "checkpoint"
        assert resilience.classify_failure(out.error) == "checkpointed"

    @pytest.mark.asyncio
    async def test_no_results_read_while_cards_exist_is_not_trusted(self):
        # A page still rendering printed the line once; the cards that follow win.
        first = _snap(lines=["No results."])
        later = _snap([_card("real1"), _card("real2")], lines=["Top"])
        out = await _discovery([first, later]).sweep("kw")
        assert out.stopped != "no-results" and len(out.hits) == 2

    @pytest.mark.asyncio
    async def test_progress_streams_new_users_as_the_scroll_goes(self):
        seen = []
        d = _discovery([_snap([_card("a1")]), _snap([_card("a1"), _card("a2")])])
        await d.sweep("kw", on_progress=lambda n, pages, rows: seen.append([r.username for r in rows]))
        assert seen[0] == ["a1"] and seen[1] == ["a2"]

    @pytest.mark.asyncio
    async def test_a_crash_names_itself(self):
        d = _discovery([_snap()])
        d._tab.goto = AsyncMock(side_effect=RuntimeError("boom"))
        out = await d.sweep("kw")
        assert out.stopped == "error" and "boom" in out.error

    @pytest.mark.asyncio
    async def test_a_hit_row_carries_what_the_card_showed(self):
        out = await _discovery([_snap([_card("nikendr", "Niken", "5,880 followers",
                                             bio="Photographer")])]).sweep("niken")
        row = out.hits[0]
        assert (row.url, row.target, row.profile_name) == (
            "https://www.threads.com/@nikendr", "niken", "Niken")
        assert row.followers == 5880 and row.bio == "Photographer"
        assert row.has_custom_pic is True


# ---------------------------------------------------------------- profile record


def _record(handle="nike", **over):
    rec = {"username": handle, "pk": "63454848350", "full_name": "Nike", "biography": "Just Do It.",
           "follower_count": 15_093_123, "is_verified": True, "text_post_app_is_private": False,
           "profile_pic_url": "https://instagram.fx.fna.fbcdn.net/v/a_n.jpg"}
    rec.update(over)
    return rec


def _post(owner, ts):
    return {"taken_at": ts, "user": {"username": owner}}


class TestProfileRecord:
    def test_the_record_gives_exact_typed_fields(self):
        u = ae.profile_from([{"data": _record()}], "nike")
        assert (u.entity_id, u.full_name, u.followers) == ("63454848350", "Nike", 15_093_123)
        assert u.followers_exact is True and u.verified is True and u.private is False

    def test_another_account_in_the_payload_is_not_this_profile(self):
        blob = {"a": _record("someoneelse"), "b": _record("nike", follower_count=5)}
        assert ae.profile_from([blob], "nike").followers == 5

    def test_a_bare_mention_is_not_a_profile_record(self):
        assert ae.profile_from([{"user": {"username": "nike"}}], "nike") is None

    def test_the_last_post_is_the_newest_one_this_account_wrote(self):
        blob = {"rec": _record(), "posts": [_post("nike", 1_780_834_529), _post("nike", 1_793_243_735)]}
        assert ae.profile_from([blob], "nike").last_post_iso == "2026-10-29"

    def test_a_repost_or_suggestion_never_becomes_this_accounts_post(self):
        blob = {"rec": _record(), "posts": [_post("stranger", 1_800_000_000)]}
        u = ae.profile_from([blob], "nike")
        assert u.last_post_iso == "" and u.posts_seen == ""

    def test_a_timestamp_naming_nobody_is_not_attributed(self):
        blob = {"rec": _record(), "posts": [{"taken_at": 1_800_000_000}]}
        assert ae.profile_from([blob], "nike").last_post_iso == ""

    def test_an_implausible_timestamp_is_ignored(self):
        blob = {"rec": _record(), "posts": [_post("nike", 99)]}
        assert ae.profile_from([blob], "nike").last_post_iso == ""

    def test_one_corrupt_block_does_not_cost_the_rest(self):
        got = ae.blobs_from_scripts(["{not json", json.dumps({"ok": 1}), None])
        assert got == [{"ok": 1}]


class TestUrls:
    @pytest.mark.parametrize("raw,want", [
        ("https://www.threads.com/@nike", "https://www.threads.com/@nike"),
        ("https://threads.net/@nike/post/ABC", "https://www.threads.com/@nike"),
        ("threads.com/@Nike/replies", "https://www.threads.com/@Nike"),
    ])
    def test_every_variant_folds_onto_the_account(self, raw, want):
        assert ae.normalize_url(raw) == want

    @pytest.mark.parametrize("raw", [
        "https://www.threads.com/", "https://www.threads.com/search?q=x",
        "https://www.instagram.com/@nike", "https://www.threads.com/@",
    ])
    def test_what_names_no_account_is_refused(self, raw):
        assert ae.normalize_url(raw) == "" and ae.username_of(raw) == ""

    def test_the_handle_is_read_without_the_at_sign(self):
        assert ae.username_of("https://www.threads.com/@nike/post/1") == "nike"


# ---------------------------------------------------------------- scraper


def _scraper():
    s = ae.Scraper.__new__(ae.Scraper)
    s.a = SimpleNamespace(timeout=5, settle=0.05, evidence=None, cancel=None)
    s.evidence = None
    s.session = MagicMock()
    s._no_payload_streak = 0
    return s


def _page(url, texts=(), lines=(), status=200, record=True):
    snap = {"url": url, "texts": list(texts), "lines": list(lines)}

    async def evaluate(script, *_a):
        return record if script is ae._JS_HAS_RECORD else snap

    p = MagicMock()
    p.url = url
    p.goto = AsyncMock(return_value=SimpleNamespace(status=status, text=AsyncMock(return_value="")))
    p.evaluate = AsyncMock(side_effect=evaluate)
    p.close = AsyncMock()
    s = _scraper()
    s.session.ctx.new_page = AsyncMock(return_value=p)
    s.screenshot = AsyncMock()
    return s


class TestScraper:
    @pytest.mark.asyncio
    async def test_a_readable_profile_is_ok_and_filled(self):
        texts = [json.dumps({"r": _record(), "p": [_post("nike", 1_793_243_735)]})]
        s = _page("https://www.threads.com/@nike", texts, ["nike", "15M followers"])
        row = await s.process("https://www.threads.com/@nike", "Nike", "feed")
        assert row.status == "OK"
        assert (row.profile_name, row.followers, row.followers_exact) == ("Nike", 15_093_123, "yes")
        assert row.last_post_iso == "2026-10-29" and row.verified is True
        assert row.bio == "Just Do It." and row.src.get("followers") == "api"

    @pytest.mark.asyncio
    async def test_a_profile_whose_bio_quotes_the_gone_text_is_still_read(self):
        texts = [json.dumps({"r": _record()})]
        s = _page("https://www.threads.com/@nike", texts,
                  ["The link's not working or the page is gone"])
        row = await s.process("https://www.threads.com/@nike", "Nike", "feed")
        assert row.status == "OK"

    @pytest.mark.asyncio
    async def test_a_missing_account_is_gone_when_signed_in(self):
        s = _page("https://www.threads.com/@nobody_zz", [], [
            "Not all who wander are lost, but this page is",
            "The link's not working or the page is gone. Go back to keep exploring.", "Back"],
            record=False)
        row = await s.process("https://www.threads.com/@nobody_zz", "t", "f")
        assert row.status == "GONE"

    @pytest.mark.asyncio
    async def test_a_login_redirect_is_the_session_not_the_profile(self):
        # Signed out, a missing account lands here too -- so it can never be GONE.
        s = _page("https://www.threads.com/login/?next=x", [], ["Log in with your Instagram account"],
                  record=False)
        row = await s.process("https://www.threads.com/@nobody_zz", "t", "f")
        assert row.status == "LOGIN_REQUIRED"

    @pytest.mark.asyncio
    async def test_a_never_joined_account_is_a_login_problem(self):
        s = _page("https://www.threads.com/@nike", [], ["Join using your Meta account"], record=False)
        row = await s.process("https://www.threads.com/@nike", "t", "f")
        assert row.status == "LOGIN_REQUIRED" and "not joined" in row.notes

    @pytest.mark.asyncio
    async def test_a_challenge_is_a_checkpoint(self):
        s = _page("https://www.threads.com/challenge/x", [], [], record=False)
        row = await s.process("https://www.threads.com/@nike", "t", "f")
        assert row.status == "CHECKPOINT"

    @pytest.mark.asyncio
    async def test_a_post_link_is_refused_without_opening_a_page(self):
        s = _page("https://www.threads.com/", [], [])
        row = await s.process("https://www.threads.com/search?q=x", "t", "f")
        assert row.status == "ERROR"
        s.session.ctx.new_page.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_three_unreadable_profiles_in_a_row_slow_the_platform_down(self):
        s = _page("https://www.threads.com/@nike", [], ["something odd"], record=False)
        for _ in range(ae.NO_PAYLOAD_STREAK_LIMIT):
            row = await s.process("https://www.threads.com/@nike", "t", "f")
        assert row.status == "PARTIAL" and "soft block" in row.notes
        assert pushback.governor_for("threads").gap_multiplier() > 1.0

    @pytest.mark.asyncio
    async def test_a_private_account_is_noted_not_dated(self):
        texts = [json.dumps({"r": _record(text_post_app_is_private=True)})]
        s = _page("https://www.threads.com/@nike", texts, [])
        row = await s.process("https://www.threads.com/@nike", "t", "f")
        assert row.status == "OK" and "private" in row.notes and row.last_post_iso == ""

    @pytest.mark.asyncio
    async def test_one_never_raises(self):
        s = _page("https://www.threads.com/@nike", [], [])
        s.ctx.new_page = AsyncMock(side_effect=RuntimeError("browser died"))
        s.session.ctx.new_page = AsyncMock(side_effect=RuntimeError("browser died"))
        row = await s.one("https://www.threads.com/@nike", "t", "f")
        assert row.status == "ERROR" and "browser died" in row.notes


# ---------------------------------------------------------------- registration


class TestRegistration:
    def test_threads_is_a_registered_cookie_platform(self):
        p = registry.get("threads")
        assert p.can_discover and p.uses_cookies and p.cookie_domain == "threads"
        assert p.required_cookies == ("sessionid",)

    def test_the_registry_classes_load(self):
        p = registry.get("threads")
        assert p.discoverer() is de.Discovery
        assert p.scraper() is ae.Scraper
        assert p.session_cls() is de.ThreadsSession

    def test_it_is_swept_on_one_tab_with_every_free_account(self):
        from backend.discovery import runner as dr
        from backend.stealth import levels

        assert dr.tabs_for("threads") == ["people"]
        assert levels.session_cap("threads", 3) == 3          # no level cap
        assert dr._sessions_wanted("threads", 5) == 5

    def test_a_threads_link_routes_to_the_threads_platform(self):
        from backend.analysis import runner as ar

        assert ar._PLATFORM_HOSTS["threads.com"] == "threads"
        assert ar._PLATFORM_HOSTS["threads.net"] == "threads"

    def test_the_api_accepts_threads(self):
        from backend.api.models import Platform

        assert Platform("threads").value == "threads"
