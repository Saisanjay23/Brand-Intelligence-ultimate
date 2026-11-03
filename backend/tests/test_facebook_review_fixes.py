"""Regression tests for the Facebook review fixes (analysis + discovery).

The governing rule: a blocked / throttled / half-loaded / misparsed page must
never read as a clean result, and "unknown" must never be reported as a
definite value. Each class below pins one numbered finding.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.platforms.facebook import analysis_engine as ae
from backend.platforms.facebook.analysis_engine import (
    Harvest, Scraper, name_is_trusted, no_posts_notice, read_counts,
    read_last_post, read_name, read_profile_adaptive, settle_status, take_chip,
    _jit)
from backend.platforms.facebook.discovery_engine import (
    RE_CHECKPOINT, RE_TRY_LATER, _in_platform_order, _worth_visiting,
    dedupe_dom_hits)
from backend.shared import pushback
from backend.shared.models.hit import Hit
from backend.shared.models.row import Row

PID = "100057408731013"
OTHER = "999000111222333"
URL = f"https://www.facebook.com/profile.php?id={PID}"


@pytest.fixture(autouse=True)
def _fresh_governor():
    pushback.reset_all()
    yield
    pushback.reset_all()


# --------------------------------------------------------------- fake browser

class _Resp:
    def __init__(self, status):
        self.status = status


class FakePage:
    """Just enough of a Playwright page for Scraper.process()."""

    def __init__(self, routes):
        self.routes = routes
        self.url = ""
        self.cur: dict = {}
        self.visited: list[str] = []
        self.waits: list = []
        self.mouse = SimpleNamespace(wheel=self._noop)

    async def _noop(self, *a, **k):
        return None

    def on(self, *a, **k):
        return None

    async def goto(self, url, **kw):
        self.visited.append(url)
        r = self.routes.get(url) or self.routes["*"]
        if r.get("nav_error"):
            raise TimeoutError("Page.goto: Timeout exceeded")
        self.cur = r
        self.url = r.get("landed", url)
        return _Resp(r.get("status", 200))

    async def wait_for_function(self, *a, **k):
        raise TimeoutError()

    async def wait_for_selector(self, *a, **k):
        raise TimeoutError()

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)

    async def content(self):
        return self.cur.get("html", "<html></html>")

    async def inner_text(self, sel):
        if self.cur.get("text_fails"):
            raise RuntimeError("detached")
        return self.cur.get("text", "")

    async def evaluate(self, js, *args):
        if js == Scraper.JS_EMBEDDED:
            return self.cur.get("embedded", [])
        if js == Scraper.JS_HEADER:
            return self.cur.get("dom", {})
        return None if not isinstance(js, str) or "post" not in js.lower() else []

    async def close(self):
        pass


def _scraper(routes, cancel=None):
    sc = Scraper.__new__(Scraper)
    sc.a = SimpleNamespace(timeout=5, settle=1, scrolls=0, cancel=cancel)
    sc.evidence = None
    page = FakePage(routes)

    async def new_page():
        return page

    async def pause(mult=1.0):
        return None

    async def no_shot(p, row):
        return None

    sc.session = SimpleNamespace(ctx=SimpleNamespace(new_page=new_page), pause=pause)
    sc.pause = pause
    sc.screenshot = no_shot
    return sc, page


def _live_main(**over):
    ent = {"id": PID, "name": "Acme Corp", "profile_social_context": {
        "content": [{"text": {"text": "120 followers"}}]}}
    r = {
        "html": "<html><title>Acme Corp | Facebook</title></html>",
        "text": "Acme Corp\n120 followers\nPosts",
        "embedded": [json.dumps({"user": ent})],
        "dom": {"name": "Acme Corp", "counter": "120 followers"},
    }
    r.update(over)
    return r


def _harvest(*blobs, text="", html="", gql=(), dom=None) -> Harvest:
    h = Harvest()
    h.add_embedded([json.dumps(b) for b in blobs])
    h.gql = list(gql)
    h.text = {"main": text}
    h.html = {"main": html}
    h.dom = dom or {}
    return h


# ------------------------------------------------------------------ 1. names

class TestNameTiers:
    def test_a_removed_page_never_gets_a_strangers_name(self):
        h = _harvest(gql=[{"viewer": {"profile_name": "Somebody Else"}}],
                     html="<title>Facebook</title>")
        row = Row(url="u", target="")
        read_name(row, h, strict=True)
        assert row.profile_name == ""

    def test_strict_mode_still_reads_og_title(self):
        h = _harvest(gql=[{"profile_name": "Somebody Else"}],
                     html='<meta property="og:title" content="Real Co | Facebook">')
        row = Row(url="u", target="")
        read_name(row, h, strict=True)
        assert row.profile_name == "Real Co"
        assert row.src["name"] == "og:title"

    def test_strict_mode_rejects_an_error_page_title(self):
        h = _harvest(html="<title>Page Not Found | Facebook</title>")
        row = Row(url="u", target="")
        read_name(row, h, strict=True)
        assert row.profile_name == ""

    def test_the_loose_tier_fills_but_cannot_make_the_row_ok(self):
        h = _harvest(gql=[{"viewer": {"profile_name": "Somebody Else"}}])
        row = Row(url="u", target="")
        read_name(row, h)
        assert (row.profile_name, row.src["name"]) == ("Somebody Else", "graphql-loose")
        settle_status(row)
        assert row.status == "PARTIAL"
        assert "graphql-loose" in row.notes


# ------------------------------------------------------------------ 2. status

class TestStatusNeedsATrustedName:
    def test_post_label_name_is_partial_and_names_the_tier(self):
        row = Row(url="u", target="")
        row.profile_name = "X"
        row.mark("name", "dom-post-label")
        settle_status(row)
        assert row.status == "PARTIAL"
        assert "dom-post-label" in row.notes

    @pytest.mark.parametrize("tier", ["graphql", "dom-header", "og:title", "title-tag"])
    def test_trusted_tiers_are_ok(self, tier):
        row = Row(url="u", target="")
        row.profile_name = "X"
        row.mark("name", tier)
        settle_status(row)
        assert row.status == "OK"
        assert name_is_trusted(row)

    def test_a_degraded_read_is_never_ok(self):
        row = Row(url="u", target="")
        row.profile_name = "X"
        row.mark("name", "graphql")
        settle_status(row, ["landed on a different profile"])
        assert row.status == "PARTIAL"
        assert "landed on a different profile" in row.notes

    def test_landing_on_another_profile_is_detected(self):
        h = _harvest()
        assert not Scraper.landed_on("acme", "", "https://www.facebook.com/stranger", h)
        assert not Scraper.landed_on(PID, PID, f"https://www.facebook.com/profile.php?id={OTHER}", h)

    def test_a_vanity_to_numeric_redirect_is_the_same_profile(self):
        h = _harvest()
        assert Scraper.landed_on(
            "acme", PID, f"https://www.facebook.com/profile.php?id={PID}", h)
        assert Scraper.landed_on("acme", PID, "https://www.facebook.com/acme/", h)

    @pytest.mark.asyncio
    async def test_process_does_not_return_ok_after_landing_elsewhere(self):
        landed = f"https://www.facebook.com/profile.php?id={OTHER}"
        sc, _ = _scraper({"*": _live_main(landed=landed)})
        row = await sc.process(URL, "t", "f", known={"location": "Pune"})
        assert row.status == "PARTIAL"
        assert "different profile" in row.notes

    @pytest.mark.asyncio
    async def test_process_ok_on_a_clean_profile(self):
        sc, _ = _scraper({"*": _live_main()})
        row = await sc.process(URL, "t", "f", known={"location": "Pune"})
        assert row.status == "OK", row.notes
        assert row.followers == 120


# ------------------------------------------------------------------ 3. chips

def _chips_row(*chips, ents=()):
    ent = {"id": PID, "name": "N", "profile_social_context": {
        "content": [{"text": {"text": c}} for c in chips]}}
    ent.update(dict(ents))
    h = _harvest({"u": ent}).scoped(PID)
    row = Row(url="u", target="")
    read_counts(row, h)
    return row


class TestFollowersBeatLikes:
    def test_a_likes_chip_first_does_not_suppress_the_followers_chip(self):
        row = _chips_row("5.4K likes", "1.2K followers")
        assert row.followers == 1200
        assert "likes" not in row.notes

    def test_likes_are_used_when_nothing_else_exists(self):
        row = _chips_row("5.4K likes")
        assert row.followers == 5400
        assert "likes, not followers" in row.notes

    def test_fan_count_cannot_win_the_max_over_followers(self):
        ent = {"id": PID, "name": "N", "follower_count": 10, "fan_count": 5000}
        h = _harvest({"u": ent}).scoped(PID)
        row = Row(url="u", target="")
        read_counts(row, h)
        assert row.followers == 10

    def test_fan_count_is_the_last_resort_and_says_so(self):
        h = _harvest({"u": {"id": PID, "name": "N", "fan_count": 5000}}).scoped(PID)
        row = Row(url="u", target="")
        read_counts(row, h)
        assert row.followers == 5000
        assert "likes, not followers" in row.notes

    def test_take_chip_modes(self):
        row = Row(url="u", target="")
        take_chip(row, "9 likes", "x", "no-likes")
        assert row.followers is None
        take_chip(row, "9 followers", "x", "likes-only")
        assert row.followers is None
        take_chip(row, "9 likes", "x", "likes-only")
        assert row.followers == 9


# ------------------------------------------------------ 4. not-published claim

class TestNotPublishedOnlyWhenNothingWasThere:
    def test_no_chip_at_all_is_not_published(self):
        row = _chips_row()
        assert row.src.get("followers") == "not-published"
        assert ae.NO_AUDIENCE_NOTE in row.notes

    def test_an_unparsed_chip_is_not_called_not_published(self):
        row = _chips_row("5 abonnés")
        assert row.followers is None
        assert row.src.get("followers") != "not-published"
        assert ae.NO_AUDIENCE_NOTE not in row.notes
        assert "audience chip unparsed: 5 abonn" in row.notes

    def test_unparsed_dom_counter_text_is_not_called_not_published(self):
        h = _harvest({"u": {"id": PID, "name": "N"}},
                     dom={"counter": "1 234 567 seguidores"}).scoped(PID)
        row = Row(url="u", target="")
        read_counts(row, h)
        assert row.src.get("followers") != "not-published"
        assert "audience chip unparsed" in row.notes

    @pytest.mark.parametrize("chip,value", [
        ("1.2 million followers", 1_200_000),
        ("1,2 Mio. Follower", 1_200_000),
        ("1,2 mil seguidores", None),          # not an English word: stays unparsed
        ("3 million people follow this", 3_000_000),
        ("154M followers", 154_000_000),
    ])
    def test_word_suffixes(self, chip, value):
        row = _chips_row(chip)
        assert row.followers == value
        if value is None:
            assert "audience chip unparsed" in row.notes

    def test_mil_normalizes_to_thousands(self):
        assert ae._count_token("1,2 mil") == "1,2K"
        assert ae._count_token("1.2 million") == "1.2M"


# --------------------------------------------------------- 5. About-tab walls

def _about_routes(**about):
    routes = {"*": _live_main()}
    routes[URL] = _live_main()
    routes[f"https://www.facebook.com/profile.php?id={PID}&sk=about"] = {
        "html": "<html></html>", "text": "nothing here", **about}
    routes[f"https://www.facebook.com/profile.php?id={PID}&sk=about_profile_transparency"] = {
        "html": "<html></html>", "text": "nothing here"}
    return routes


class TestAboutTabs:
    @pytest.mark.asyncio
    async def test_a_login_wall_on_about_is_a_session_verdict(self):
        routes = _about_routes(landed="https://www.facebook.com/login/?next=x")
        sc, page = _scraper(routes)
        row = await sc.process(URL, "t", "f")
        assert row.status == "LOGIN_REQUIRED"
        assert "About tab" in row.notes
        assert len(page.visited) == 2, "kept loading tabs after a wall"

    @pytest.mark.asyncio
    async def test_a_checkpoint_on_about_stops_further_tabs(self):
        routes = _about_routes(landed="https://www.facebook.com/checkpoint/123/")
        sc, page = _scraper(routes)
        row = await sc.process(URL, "t", "f")
        assert row.status == "CHECKPOINT"
        assert len(page.visited) == 2

    @pytest.mark.asyncio
    async def test_a_nav_failure_is_noted_not_read_as_no_location(self):
        routes = _about_routes(nav_error=True)
        sc, page = _scraper(routes)
        row = await sc.process(URL, "t", "f")
        assert "about tab nav failed" in row.notes
        assert row.location == ""
        assert len(page.visited) == 2, "loaded the second tab after the first failed"

    @pytest.mark.asyncio
    async def test_no_about_tabs_when_the_main_page_was_not_a_profile(self):
        # no entity, no name, no title
        routes = {"*": {"html": "<html></html>", "text": "hello", "embedded": [], "dom": {}}}
        sc, page = _scraper(routes)
        row = await sc.process(URL, "t", "f")
        assert page.visited == [URL]
        assert row.status == "PARTIAL"
        assert "About tabs skipped" in row.notes

    @pytest.mark.asyncio
    async def test_a_cancelled_run_loads_no_about_tab(self):
        sc, page = _scraper(_about_routes(), cancel=True)
        row = await sc.process(URL, "t", "f")
        assert page.visited == [URL]
        assert row.status == "PARTIAL"
        assert "cancelled" in row.notes

    def test_tab_wall_is_not_fooled_by_the_about_refusal_or_a_long_page(self):
        refusal = "This content isn't available at the moment. The owner only shared it..."
        assert Scraper.tab_wall("https://www.facebook.com/x/about", refusal) is None
        long_bio = "Log In or Sign Up " + "x" * 3000
        assert Scraper.tab_wall("https://www.facebook.com/x/about", long_bio) is None
        assert Scraper.tab_wall("https://www.facebook.com/loginradius/about", "") is None
        assert Scraper.tab_wall("https://www.facebook.com/x/about",
                                "You're Temporarily Blocked")[0] == "CHECKPOINT"


# ------------------------------------------------------ 6. HTTP status / walls

class TestPushback:
    @pytest.mark.asyncio
    async def test_http_429_on_the_main_visit_is_a_throttle_not_partial(self):
        routes = {"*": {"html": "<html></html>", "text": "Something went wrong",
                        "status": 429}}
        sc, _ = _scraper(routes)
        row = await sc.process(URL, "t", "f")
        assert row.status == "CHECKPOINT"
        assert "pushed back" in row.notes
        assert pushback.governor_for("facebook").pushbacks == 1

    @pytest.mark.asyncio
    async def test_a_small_try_again_later_body_is_a_wall(self):
        routes = {"*": {"html": "<html></html>",
                        "text": "You’re Temporarily Blocked. Try again later."}}
        sc, _ = _scraper(routes)
        row = await sc.process(URL, "t", "f")
        assert row.status == "CHECKPOINT"

    @pytest.mark.asyncio
    async def test_403_is_a_rejected_login(self):
        routes = {"*": {"html": "<html></html>", "text": "Forbidden", "status": 403}}
        sc, _ = _scraper(routes)
        row = await sc.process(URL, "t", "f")
        assert row.status == "LOGIN_REQUIRED"

    @pytest.mark.asyncio
    async def test_a_loaded_profile_whose_posts_say_try_again_later_is_not_a_wall(self):
        sc, _ = _scraper({"*": _live_main(
            text="Acme Corp\n120 followers\nTry again later, said the post. Temporarily Blocked?")},
        )
        row = await sc.process(URL, "t", "f", known={"location": "Pune"})
        assert row.status == "OK", row.notes

    def test_wall_wording_survives_curly_apostrophes(self):
        for s in ("We’ve temporarily", "We've temporarily", "You’re Temporarily Blocked",
                  "you're temporarily blocked"):
            assert RE_CHECKPOINT.search(s), s
        assert RE_TRY_LATER.search("Please try again later")
        # ordinary words alone are not a checkpoint
        assert not RE_CHECKPOINT.search("a checkpoint on the border")


# --------------------------------------------------- 7. GONE vs live detection

class TestGoneDetection:
    def _blocked(self, h, text, row=None):
        row = row or Row(url=URL, target="")
        row.profile_id = PID
        return row, Scraper.blocked_status(row, URL, text, h)

    def test_a_resolved_entity_with_a_name_is_not_gone_over_a_restricted_post(self):
        h = _harvest({"u": {"id": PID, "name": "Acme Corp"}})
        row, blocked = self._blocked(h, "This content isn't available at the moment")
        assert not blocked and row.status != "GONE"

    def test_a_resolved_entity_with_social_context_is_not_gone(self):
        ent = {"id": PID, "profile_social_context": {"content": [{"text": {"text": "5 friends"}}]}}
        h = _harvest({"u": ent})
        row, blocked = self._blocked(h, "Content isn't available")
        assert not blocked

    def test_a_suggested_pages_count_does_not_keep_a_removed_profile_alive(self):
        h = _harvest(gql=[{"suggested": {"id": OTHER, "follower_count": 90000}}])
        row, blocked = self._blocked(h, "This content isn't available at the moment")
        assert blocked and row.status == "GONE"

    def test_a_removed_page_is_still_gone(self):
        h = _harvest()
        row, blocked = self._blocked(h, "This content isn't available right now. Facebook")
        assert blocked and row.status == "GONE"

    def test_the_rendered_audience_line_still_proves_a_live_profile(self):
        h = _harvest()
        row, blocked = self._blocked(h, "Content isn't available\n915 followers")
        assert not blocked

    @pytest.mark.asyncio
    async def test_a_gone_row_gets_no_stranger_name(self):
        routes = {"*": {"html": "<html><title>Facebook</title></html>",
                        "text": "This content isn't available at the moment",
                        "embedded": [json.dumps({"viewer": {"profile_name": "Somebody Else",
                                                            "id": OTHER}})]}}
        sc, _ = _scraper(routes)
        row = await sc.process(URL, "t", "f")
        assert row.status == "GONE"
        assert row.profile_name == ""


# ------------------------------------------------------------- 8. no-posts text

class TestNoPostsNotice:
    def test_a_post_or_comment_containing_the_words_is_not_a_notice(self):
        text = "Acme\nSomeone wrote: there is nothing to show for this quarter, honestly\n"
        assert not no_posts_notice(text, "<html></html>")

    def test_a_standalone_line_with_no_post_links_is_a_notice(self):
        assert no_posts_notice("Acme\nNo posts available\n", "<html></html>")

    def test_a_standalone_comment_line_under_a_real_post_is_not(self):
        html = '<a href="https://www.facebook.com/acme/posts/pfbid0abc">3d</a>'
        assert not no_posts_notice("Nothing to show\n", html)

    def test_unreadable_markup_is_unknown_not_no(self):
        assert not no_posts_notice("No posts yet", None)

    def test_curly_apostrophe_is_matched(self):
        assert no_posts_notice("Acme hasn’t shared any posts", "")

    def test_read_last_post_honours_it(self):
        h = _harvest({"u": {"id": PID, "name": "N"}},
                     text="a very long comment that says nothing to show at all, really, yes\n",
                     html="").scoped(PID)
        row = Row(url="u", target="")
        read_last_post(row, h)
        assert row.posts_seen != "no"

    @pytest.mark.asyncio
    async def test_the_dom_fallback_still_runs_after_a_false_no(self):
        """The guard used to skip dom_last_post once posts_seen was 'no'."""
        sc, page = _scraper({"*": _live_main(
            text="Acme Corp\n120 followers\nsomeone said nothing to show here today ok\n")})
        seen = {}
        real = ae.dom_last_post

        async def spy(p, owners=()):
            seen["called"] = True
            return "2026-08-07"

        ae.dom_last_post = spy
        try:
            row = await sc.process(URL, "t", "f", known={"location": "Pune"})
        finally:
            ae.dom_last_post = real
        assert seen.get("called")
        assert row.last_post_iso == "2026-08-07"


# ------------------------------------------------------ 9. comment timestamps

def test_comment_permalinks_are_skipped_in_the_post_time_script():
    js = ae.JS_POST_TIMES
    assert "comment_id" in js
    assert js.index("comment_id") < js.index("authorOf(a)")


# --------------------------------------------------------------- 10. minor

class TestMinor:
    @pytest.mark.asyncio
    async def test_script_json_is_not_scanned_when_inner_text_fails(self):
        html = ('<html><body><p>Hello</p><script type="application/json">'
                '{"m":"This content isn\'t available"}</script>'
                '<style>.x{}</style></body></html>')
        routes = {"*": {"html": html, "text_fails": True, "embedded": []}}
        sc, page = _scraper(routes)
        h = Harvest()
        await sc.visit(page, URL, h, "main")
        assert "isn't available" not in h.text["main"]
        assert "Hello" in h.text["main"]

    @pytest.mark.asyncio
    async def test_an_adaptive_healer_crash_keeps_the_fields_read(self, monkeypatch):
        async def boom(row, h):
            raise RuntimeError("healer exploded")

        monkeypatch.setattr(ae, "_read_profile_adaptive", boom)
        row = Row(url="u", target="")
        row.followers = 12
        row.profile_name = "Acme"
        await read_profile_adaptive(row, Harvest())
        assert (row.followers, row.profile_name) == (12, "Acme")
        assert ae.ADAPTIVE_FAILED_NOTE in row.notes

    @pytest.mark.asyncio
    async def test_process_is_partial_not_error_when_the_healer_crashes(self, monkeypatch):
        async def boom(row, h):
            raise RuntimeError("healer exploded")

        monkeypatch.setattr(ae, "_read_profile_adaptive", boom)
        sc, _ = _scraper({"*": _live_main()})
        row = await sc.process(URL, "t", "f", known={"location": "Pune"})
        assert row.status == "PARTIAL"
        assert row.followers == 120 and row.profile_name == "Acme Corp"

    @pytest.mark.asyncio
    async def test_waits_are_jittered(self):
        sc, page = _scraper({"*": _live_main()})
        seen = set()
        for _ in range(12):
            h = Harvest()
            await sc.visit(page, URL, h, "main")
        seen = set(page.waits)
        assert len(seen) > 1
        assert all(1125 <= w <= 1875 for w in seen)

    def test_jit_bounds(self):
        vals = {_jit(1000) for _ in range(200)}
        assert min(vals) >= 750 and max(vals) <= 1250 and len(vals) > 1


# ------------------------------------------------- discovery: dupes and order

def _hit(eid, name="", url="", avatar="", rank=0):
    return Hit(entity_id=eid, name=name, url=url or f"https://www.facebook.com/{eid}",
               avatar=avatar, rank=rank, source="dom")


class TestDomDuplicates:
    def test_vanity_case_variants_collapse(self):
        out = dedupe_dom_hits([_hit("Acme"), _hit("acme"), _hit("Other")])
        assert [h.entity_id for h in out] == ["Acme", "Other"]

    def test_numeric_and_vanity_forms_of_one_person_collapse(self):
        pic = "https://scontent.x/v/t39/abc123_n.jpg?oh=1"
        a = _hit("acme", "Acme Corp", avatar=pic)
        b = _hit("1000555", "Acme Corp",
                 url="https://www.facebook.com/profile.php?id=1000555",
                 avatar="https://scontent.y/v/t39/abc123_n.jpg?oh=2")
        out = dedupe_dom_hits([a, b])
        assert len(out) == 1
        assert out[0].entity_id == "1000555"
        assert out[0].url.endswith("id=1000555")

    def test_two_different_people_with_the_same_name_are_kept(self):
        out = dedupe_dom_hits([_hit("a1", "Sam Lee"), _hit("b2", "Sam Lee")])
        assert len(out) == 2

    def test_platform_order_is_preserved(self):
        out = dedupe_dom_hits([_hit("z"), _hit("a"), _hit("z"), _hit("m")])
        assert [h.entity_id for h in out] == ["z", "a", "m"]


class TestCapKeepsPlatformOrder:
    def test_worth_visiting_keeps_the_top_ranked_not_the_smallest_ids(self):
        order = ["900", "300", "100", "700"]
        keep = _worth_visiting({"100", "300", "700", "900"}, cap=3, parsed=1, order=order)
        assert keep == {"900", "300"}

    def test_without_an_order_it_is_the_old_behaviour(self):
        assert _worth_visiting({"3", "1", "2"}, cap=2, parsed=0) == {"1", "2"}

    def test_in_platform_order(self):
        assert _in_platform_order({"b", "c", "a", "z"}, ["c", "a"]) == ["c", "a", "b", "z"]
