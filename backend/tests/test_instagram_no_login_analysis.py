"""Instagram analysis, no login first: read the public page, fall back to an
account only when Instagram refuses a logged-out visitor.

WHY THIS EXISTS. The pool's Instagram accounts kept being suspended, and a
public profile page still loads for a visitor who is not signed in, with the
profile's own data embedded in it. So analysis now reads that page FIRST with
no account at all, and only hands a profile to an account when the logged-out
read is refused. What these tests pin down is the part that can go silently
wrong:

  * the PARSER reads the page Instagram really serves (the fixture below is a
    trimmed copy of the public `instagram` account's logged-out page, taken
    live on 2026-11-17 -- same nesting, same keys, a few values cut down);
  * a REFUSED page (login wall, 429, a 200 with no data, a challenge) is never
    filed as GONE or as an empty profile -- it goes to an account;
  * the RUNNER claims no account when the page answers, hands everything from
    the first refusal on to the ORIGINAL account path, remembers the refusal
    for the next batch, and -- with no account to fall back on -- ends those
    profiles with a stated, owed reason, never a fake result;
  * no-login visits have their OWN hourly ceiling and never touch an
    account's counts;
  * DISCOVERY is unchanged: Instagram still has no anonymous context, so a
    keyword sweep is never attempted logged out (it would find nothing).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.analysis import runner as R
from backend.config.settings import settings
from backend.platforms import registry
from backend.platforms.instagram import analysis_engine as E
from backend.platforms.scan_options import ScanOptions
from backend.shared import account_budget as AB
from backend.shared.models.row import Row
from backend.shared.pushback import governor_for

PLATFORM = "instagram"

# --------------------------------------------------------------- the fixture
#
# The public `instagram` account's logged-out page, trimmed. Instagram embeds
# the profile as TWO query results inside `<script type="application/json"
# data-sjs>` blocks (the same split instaloader documents): the details, and
# the first posts. Both sit at
#   require[0][3][0].__bbox.require[0][3][1].__bbox.result.data.xig_user_by_username
# Captions, links and highlights are dropped; the post nodes keep only what
# the engine reads (author and Instagram's own accessibility caption). The
# post nodes are in a NON-chronological order on purpose: a pinned post sits
# first on a real grid, and the newest date must still win.

DETAILS = {
    "account_badges": [],
    # Live: the exact post count is withheld from a logged-out visitor.
    "all_media_count": None,
    "biography": "Discover what's new on Instagram \U0001f50e✨",
    "follower_count": 686368166,
    "following_count": 174,
    "full_name": "Instagram",
    # The page's own id. NOT the user id -- that is `pk`.
    "id": "17841400039600391",
    "is_private": False,
    "is_verified": True,
    "latest_reel_media": 0,
    "pk": "25025320",
    "profile_pic_url": ("https://scontent.cdninstagram.com/v/t51.82787-19/"
                        "550891366_18667771684001321_1383210656577177067_n.jpg"
                        "?stp=dst-jpg_s150x150_tt6"),
    "username": "instagram",
}


def _post(code: str, caption: str, author: str = "instagram") -> dict:
    return {"node": {
        "__typename": "XDTMediaDict", "code": code, "media_type": 2,
        "product_type": "clips", "user": {"username": author, "id": "17841400039600391"},
        "accessibility_caption": caption,
    }, "cursor": ""}


POSTS = {
    "id": "17841400039600391",
    "pk": "25025320",
    "polaris_ordered_timeline_connection": {
        "edges": [
            _post("DfxQeYNtW-_", "Video by Instagram on November 15, 2026. May be a "
                                 "Twitter screenshot of phone, screen and text."),
            _post("Df2aGgPtPZQ", "Video by Instagram on November 17, 2026. May be an "
                                 "image of headdress and text."),
            _post("Dfz_ew4hKVX", "Video by Instagram on November 16, 2026. May be art "
                                 "of skull and text."),
        ],
        "page_info": {"has_next_page": True},
    },
}

META = ('<meta property="og:description" content="686M Followers, 174 Following, '
        '8,649 Posts - See Instagram photos and videos from Instagram (&#064;instagram)" />')


def _block(query: str, xig: dict) -> str:
    """One embedded query result, nested exactly as the live page nests it."""
    doc = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [[
        "RelayPrefetchedStreamCache", "next", [],
        [f"adp_{query}RelayPreloader_6afc9be0", {"__bbox": {
            "complete": True,
            "result": {"data": {"xig_user_by_username": xig}, "extensions": {}},
            "sequence_number": 0}}]]]}}]]]}
    body = json.dumps(doc)
    return (f'<script type="application/json" nonce="x" '
            f'data-content-len="{len(body)}" data-sjs>{body}</script>')


def page_html(details=DETAILS, posts=POSTS, meta=META, extra: str = "") -> str:
    return ("<!DOCTYPE html><html><head>" + meta
            + '<script id="envjson" type="application/json">{"x":1}</script></head><body>'
            + _block("PolarisLoggedOutDesktopWWWProfileRootContentQuery", details)
            + _block("PolarisLoggedOutDesktopWWWProfilePostsTabContentQuery", posts)
            + extra + "</body></html>")


# A login wall, a "page isn't available" error page, and a bare 200: what
# Instagram serves when it will not show the profile.
LOGIN_HTML = ('<html><head><meta property="og:description" content="Create an account '
              'or log in to Instagram" /></head><body>login</body></html>')
LOGIN_BODY = "Log in to Instagram\nPhone number, username, or email\nPassword"
ERROR_HTML = ('<html><head></head><body><script type="application/json" data-sjs>'
              '{"require":[["PolarisErrorRoot","render",null,[]]]}</script></body></html>')
GONE_BODY = ("Sorry, this page isn't available.\nThe link you followed may be broken, "
             "or the page may have been removed. Go back to Instagram.")
BARE_HTML = "<html><head></head><body><div id='root'></div></body></html>"


@pytest.fixture(autouse=True)
def _no_login_on_and_unpaused(monkeypatch):
    """Each test starts with no-login switched on (the shipped default) and
    no pause left over from a refusal in an earlier test."""
    monkeypatch.setattr(settings, "instagram_analysis_no_login", True)
    R.reset_no_login_pause()
    yield
    R.reset_no_login_pause()


# ------------------------------------------------------------------ parser


class TestParsingTheRealPageShape:
    def test_the_profile_comes_out_of_the_embedded_json(self):
        raw = E.logged_out_user(E.json_scripts(page_html()), "instagram")
        assert raw is not None
        u = E.user_from_logged_out(raw)
        assert u.username == "instagram"
        assert u.full_name == "Instagram"
        assert u.followers == 686368166
        assert u.following == 174
        assert u.verified is True and u.private is False
        assert u.biography.startswith("Discover what's new")
        assert u.avatar.startswith("https://scontent.cdninstagram.com/")

    def test_the_user_id_is_pk_not_the_pages_own_id(self):
        u = E.user_from_logged_out(E.logged_out_user(E.json_scripts(page_html()), "instagram"))
        assert u.entity_id == "25025320"

    def test_both_query_results_are_merged(self):
        raw = E.logged_out_user(E.json_scripts(page_html()), "instagram")
        assert raw["follower_count"] == 686368166                    # details block
        assert E.timeline_edge_count(raw) == 3                       # posts block

    def test_a_withheld_post_count_stays_unknown_not_zero(self):
        u = E.user_from_logged_out(E.logged_out_user(E.json_scripts(page_html()), "instagram"))
        assert u.posts is None

    def test_all_media_count_is_the_post_count_when_the_page_gives_it(self):
        raw = E.logged_out_user(
            E.json_scripts(page_html(details={**DETAILS, "all_media_count": 42})), "instagram")
        assert E.user_from_logged_out(raw).posts == 42

    def test_a_block_naming_another_account_is_not_merged_in(self):
        other = {**DETAILS, "username": "someone_else", "pk": "1", "follower_count": 5}
        html = page_html(extra=_block("PolarisSomethingElse", other))
        raw = E.logged_out_user(E.json_scripts(html), "instagram")
        assert raw["pk"] == "25025320" and raw["follower_count"] == 686368166

    @pytest.mark.parametrize("html", [BARE_HTML, LOGIN_HTML, ERROR_HTML,
                                      page_html(details={**DETAILS, "pk": None},
                                                posts={"id": "x"})])
    def test_no_pk_means_no_profile_on_the_page(self, html):
        assert E.logged_out_user(E.json_scripts(html), "instagram") is None

    def test_unparseable_json_is_skipped_not_raised(self):
        html = page_html(extra='<script type="application/json" data-sjs>'
                               '{"xig_user_by_username": oops</script>')
        assert E.logged_out_user(E.json_scripts(html), "instagram")["pk"] == "25025320"

    def test_the_description_line_counts(self):
        c = E.meta_counts(page_html())
        assert c["followers"] == (686_000_000, False)                 # rounded, said so
        assert c["following"] == (174, True)
        assert c["posts"][0] == 8649

    def test_newest_post_date_wins_over_page_order(self):
        raw = E.logged_out_user(E.json_scripts(page_html()), "instagram")
        dated = E.Scraper._timeline_alt_dates(raw, ("instagram", "Instagram"))
        assert dated == ["2026-11-15", "2026-11-17", "2026-11-16"]
        assert max(d for d in dated if d) == "2026-11-17"

    def test_a_post_by_another_account_is_not_this_profiles_date(self):
        posts = {**POSTS, "polaris_ordered_timeline_connection": {"edges": [
            _post("A", "Photo by Someone on November 17, 2026.", author="someone"),
            _post("B", "Photo by Someone Else on November 16, 2026."),
            _post("C", "Video by Instagram on November 10, 2026."),
        ]}}
        raw = E.logged_out_user(E.json_scripts(page_html(posts=posts)), "instagram")
        assert E.Scraper._timeline_alt_dates(raw, ("instagram", "Instagram")) == [
            "", "", "2026-11-10"]


# ------------------------------------------------------------- the engine


def _resp(status: int, text: str):
    r = MagicMock()
    r.status = status
    r.text = AsyncMock(return_value=text)
    return r


def _page(url: str, html: str = "", body: str = "", status: int = 200, goto_exc=None):
    p = MagicMock()
    p.url = url
    p.goto = (AsyncMock(side_effect=goto_exc) if goto_exc
              else AsyncMock(return_value=_resp(status, html)))
    p.on = MagicMock()
    p.close = AsyncMock()
    p.inner_text = AsyncMock(return_value=body)
    p.evaluate = AsyncMock(return_value=[])
    p.content = AsyncMock(return_value=html)
    p.wait_for_timeout = AsyncMock()
    return p


def _anon(page) -> E.Scraper:
    s = E.Scraper.__new__(E.Scraper)
    s.a = ScanOptions(timeout=5, settle=0.01, warmup=False)
    s.evidence = None
    s.anonymous = True
    s.session = MagicMock()
    s.session.ctx.new_page = AsyncMock(return_value=page)
    s.screenshot = AsyncMock()
    return s


async def _visit(page, handle="instagram") -> Row:
    return await _anon(page).process(f"https://www.instagram.com/{handle}/", "", "", None)


class TestALoggedOutReadThatWorks:
    @pytest.mark.asyncio
    async def test_every_field_is_read_from_one_page(self):
        page = _page("https://www.instagram.com/instagram/", page_html(), "Instagram")
        row = await _visit(page)
        assert row.status == "OK"
        assert row.profile_id == "25025320"
        assert row.profile_name == "Instagram"
        assert (row.followers, row.followers_exact) == (686368166, "yes")
        assert row.friends == 174
        assert row.verified is True
        assert row.bio.startswith("Discover")
        assert row.has_custom_pic is True
        assert row.posts_seen == "yes"
        assert row.last_post_iso == "2026-11-17"
        page.goto.assert_awaited_once()                     # ONE page load

    @pytest.mark.asyncio
    async def test_provenance_says_logged_out(self):
        row = await _visit(_page("https://www.instagram.com/instagram/", page_html(), "x"))
        assert row.src["access"] == "logged-out-page"
        assert row.src["name"] == "logged-out-page"
        assert row.src["followers"] == "logged-out-page"
        assert row.src["last_post"] == "logged-out-page-alt-text"
        assert row.src["posts"] == "logged-out-page-description"
        assert "read without login" in row.notes

    @pytest.mark.asyncio
    async def test_no_dated_post_leaves_the_date_blank_and_says_so(self):
        posts = {**POSTS, "polaris_ordered_timeline_connection": {"edges": [
            _post("A", "a custom caption with no date")]}}
        row = await _visit(_page("https://www.instagram.com/instagram/",
                                 page_html(posts=posts), "x"))
        assert row.status == "OK"
        assert row.last_post_iso == ""
        assert "left blank, not guessed" in row.notes

    @pytest.mark.asyncio
    async def test_a_private_profile_is_read_without_a_date(self):
        details = {**DETAILS, "is_private": True}
        posts = {"id": "1", "pk": "25025320"}
        row = await _visit(_page("https://www.instagram.com/instagram/",
                                 page_html(details=details, posts=posts), "x"))
        assert row.status == "OK"
        assert row.last_post_iso == ""
        assert "private account" in row.notes

    @pytest.mark.asyncio
    async def test_zero_posts_on_the_description_line_means_no_posts(self):
        meta = META.replace("8,649 Posts", "0 Posts")
        row = await _visit(_page("https://www.instagram.com/instagram/",
                                 page_html(meta=meta, posts={"id": "1", "pk": "25025320"}), "x"))
        assert row.posts_seen == "no"
        assert row.last_post_iso == ""

    @pytest.mark.asyncio
    async def test_the_dom_copy_is_used_when_the_body_could_not_be_read(self):
        page = _page("https://www.instagram.com/instagram/", "", "x")
        page.content = AsyncMock(return_value=BARE_HTML)
        page.evaluate = AsyncMock(return_value=E.json_scripts(page_html()))
        row = await _visit(page)
        assert row.status == "OK" and row.followers == 686368166


class TestARefusedReadIsNeverAFinding:
    @pytest.mark.asyncio
    async def test_a_login_wall_is_blocked_not_gone(self):
        page = _page("https://www.instagram.com/accounts/login/?next=%2Fbrand%2F",
                     LOGIN_HTML, LOGIN_BODY)
        row = await _visit(page, "brand")
        assert row.status == E.BLOCKED_LOGGED_OUT
        assert E.no_login_block_kind(row) == E.NL_LOGIN_WALL
        assert row.followers is None and row.profile_name == ""

    @pytest.mark.asyncio
    async def test_a_200_without_the_profile_data_is_blocked_not_gone(self):
        row = await _visit(_page("https://www.instagram.com/brand/", BARE_HTML, "Instagram"),
                           "brand")
        assert row.status == E.BLOCKED_LOGGED_OUT
        assert E.no_login_block_kind(row) == E.NL_NO_DATA
        assert "not a verdict" in row.notes

    @pytest.mark.asyncio
    async def test_a_429_is_blocked_and_slows_the_governor(self):
        row = await _visit(_page("https://www.instagram.com/brand/", "Please wait a few minutes",
                                 "Please wait a few minutes before you try again.",
                                 status=429), "brand")
        assert row.status == E.BLOCKED_LOGGED_OUT
        assert E.no_login_block_kind(row) == E.NL_THROTTLED
        assert governor_for("instagram").pushbacks >= 1

    @pytest.mark.asyncio
    async def test_a_challenge_page_is_blocked(self):
        row = await _visit(_page("https://www.instagram.com/challenge/?next=/brand/",
                                 BARE_HTML, ""), "brand")
        assert row.status == E.BLOCKED_LOGGED_OUT
        assert E.no_login_block_kind(row) == E.NL_CHALLENGE

    @pytest.mark.asyncio
    async def test_a_401_is_a_refusal_but_not_an_expired_login(self):
        """There is no login to expire. Feeding EXPIRED to the governor would
        slow the ACCOUNTS down for a refusal that was about this visit."""
        row = await _visit(_page("https://www.instagram.com/brand/", "", "", status=401), "brand")
        assert row.status == E.BLOCKED_LOGGED_OUT
        assert governor_for("instagram").pushbacks == 0

    @pytest.mark.asyncio
    async def test_a_page_that_did_not_load_is_blocked_not_an_error(self):
        row = await _visit(_page("about:blank", goto_exc=TimeoutError("Timeout 45000ms")),
                           "brand")
        assert row.status == E.BLOCKED_LOGGED_OUT
        assert E.no_login_block_kind(row) == E.NL_UNREACHABLE
        assert E.NL_UNREACHABLE not in E.NO_LOGIN_IP_BLOCKS

    @pytest.mark.asyncio
    async def test_a_url_that_is_not_a_profile_is_refused_before_any_page(self):
        page = _page("https://www.instagram.com/p/ABC/")
        s = _anon(page)
        row = await s.process("https://www.instagram.com/p/ABC/", "", "", None)
        assert row.status == "ERROR"
        s.session.ctx.new_page.assert_not_awaited()


class TestGoneNeedsInstagramsOwnWords:
    @pytest.mark.asyncio
    async def test_the_error_page_that_says_so_is_gone(self):
        row = await _visit(_page("https://www.instagram.com/brand/", ERROR_HTML, GONE_BODY),
                           "brand")
        assert row.status == "GONE"
        assert "without login" in row.notes

    @pytest.mark.asyncio
    async def test_instagrams_current_wording_on_its_error_page_is_gone(self):
        """Seen live (2026-11-17) for a handle that cannot exist: the error
        page (PolarisErrorRoot), no data, no description line, and this
        text. RE_GONE learned the wording on 2026-11-18 -- the same error
        root the logged-out pre-flight (shared/fast_http.py) was measured to
        mean "missing" on, 3/3 -- so it settles GONE without spending an
        account visit."""
        live_text = ("Log in\nProfile isn't available\nThe link may be broken, or the "
                     "profile may have been removed.\nSign up for Instagram")
        row = await _visit(_page("https://www.instagram.com/brand/", ERROR_HTML, live_text),
                           "brand")
        assert row.status == "GONE"

    @pytest.mark.asyncio
    async def test_the_error_page_in_words_we_do_not_know_goes_to_an_account(self):
        """Instagram's error page, but saying something RE_GONE has never
        seen: removed, hidden from logged-out visitors, or a new wording --
        an account decides, not this read, and no-login carries on."""
        odd_text = "Log in\nSomething is off with this page.\nSign up for Instagram"
        row = await _visit(_page("https://www.instagram.com/brand/", ERROR_HTML, odd_text),
                           "brand")
        assert row.status == E.BLOCKED_LOGGED_OUT
        assert E.no_login_block_kind(row) == E.NL_ERROR_PAGE
        assert E.NL_ERROR_PAGE not in E.NO_LOGIN_IP_BLOCKS

    @pytest.mark.asyncio
    async def test_gone_words_on_the_login_page_are_not_gone(self):
        row = await _visit(_page("https://www.instagram.com/accounts/login/?next=/brand/",
                                 ERROR_HTML, GONE_BODY), "brand")
        assert row.status == E.BLOCKED_LOGGED_OUT

    @pytest.mark.asyncio
    async def test_gone_words_without_instagrams_error_page_are_not_gone(self):
        row = await _visit(_page("https://www.instagram.com/brand/", BARE_HTML, GONE_BODY),
                           "brand")
        assert row.status == E.BLOCKED_LOGGED_OUT

    @pytest.mark.asyncio
    async def test_a_live_profile_page_is_never_gone_even_quoting_the_words(self):
        html = page_html(extra='<script type="application/json">"PolarisErrorRoot"</script>')
        row = await _visit(_page("https://www.instagram.com/instagram/", html, GONE_BODY))
        assert row.status == "OK"


class TestTheNoLoginBrowserCarriesNoAccount:
    def test_cookies_handed_in_are_never_used(self):
        s = E.Scraper(ScanOptions(), [{"name": "sessionid", "value": "secret"}],
                      session_id="acct-1", anonymous=True)
        assert isinstance(s.session, E.LoggedOutInstagramSession)
        assert s.session.cookies == []
        assert s.session.session_id == E.NO_LOGIN_PROFILE_ID

    def test_it_has_its_own_persistent_profile_never_an_accounts(self, monkeypatch):
        monkeypatch.setattr(settings, "browser_persistent_profiles", True)
        s = E.Scraper(ScanOptions(), [], anonymous=True)
        assert s.session._wanted_profile().name == "instagram_no-login"

    @pytest.mark.asyncio
    async def test_no_login_probe_no_warm_up_no_cookie_save(self):
        s = E.Scraper(ScanOptions(), [], anonymous=True)
        s.session.ctx = MagicMock()
        s.session.ctx.new_page = AsyncMock()
        s.session.on_cookies = AsyncMock()
        assert await s.check_session() is True
        await s.session._warmup()
        await s.session.sync_cookies()
        s.session.ctx.new_page.assert_not_awaited()
        s.session.on_cookies.assert_not_awaited()

    def test_a_logged_in_scraper_is_unchanged(self):
        s = E.Scraper(ScanOptions(), [{"name": "sessionid", "value": "v"}], session_id="a")
        assert type(s.session).__name__ == "InstagramSession"
        assert s.anonymous is False


# ---------------------------------------------------------------- the runner


class FakePool:
    """Pooled accounts: claims are exclusive; none at all raises the same
    ConflictError-style message the real pool does."""

    def __init__(self, sessions, plat):
        self.sessions = sessions
        self.plat = plat
        self.claimed: set[str] = set()
        self.calls = 0
        self.marked_failed: list = []
        self.marked_ok: list = []

    async def session_for_job(self, platform_id, *, wait_s=0.0):
        self.calls += 1
        for s in self.sessions:
            if s["id"] not in self.claimed:
                self.claimed.add(s["id"])
                return self.plat, dict(s)
        raise RuntimeError(f"{platform_id}: no healthy sessions available -- "
                           f"please add more cookies")

    def release_claim(self, platform_id, session_id):
        self.claimed.discard(session_id)

    async def mark_session_failed(self, platform_id, session_id, reason="expired", detail=""):
        self.marked_failed.append((session_id, reason))

    async def mark_session_ok(self, platform_id, session_id):
        self.marked_ok.append(session_id)

    def cookie_saver(self, platform_id, session_id):
        return None

    def proven_fresh(self, session_item):
        return True


def _wire(monkeypatch, sessions, anon: dict[str, str] | None = None):
    """A fake Instagram: the anonymous scraper answers each URL with the
    outcome in `anon` (default OK) built by the real engine's own helpers;
    an account scraper reads everything."""
    anon = anon or {}

    class FakeScraper:
        instances: list["FakeScraper"] = []

        def __init__(self, options, cookies, session_id="", anonymous=False):
            self.anonymous = anonymous
            self.session_id = session_id
            self.cookies = cookies
            self.session = None
            self.seen: list[str] = []
            self.started = self.stopped = False
            FakeScraper.instances.append(self)

        async def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

        async def check_session(self):
            return True

        async def pause(self, mult=1.0):
            pass

        async def one(self, url, target, feed, known=None):
            self.seen.append(url)
            await asyncio.sleep(0)
            row = Row(url=url, target=target)
            row.profile_id = url.rstrip("/").rsplit("/", 1)[-1]
            if not self.anonymous:
                row.status, row.profile_name = "OK", "Read via account"
                return row
            outcome = anon.get(url, "OK")
            if outcome == "OK":
                row.status, row.profile_name = "OK", "Read without login"
                row.mark("access", E.ACCESS_LOGGED_OUT)
            elif outcome == "GONE":
                row.status = "GONE"
            else:
                E.Scraper._refused(row, outcome, "test refusal")
            return row

    class FakePlatform:
        id = PLATFORM
        name = "Instagram"
        enabled = True
        uses_api_key = False
        env_keys = ()

        def scraper(self):
            return FakeScraper

    plat = FakePlatform()
    pool = FakePool(sessions, plat)
    for name in ("session_for_job", "release_claim", "mark_session_failed",
                 "mark_session_ok", "cookie_saver", "proven_fresh"):
        monkeypatch.setattr(R.sessions_engine, name, getattr(pool, name))
    monkeypatch.setitem(R.registry.PLATFORMS, PLATFORM, plat)
    saved: list[str] = []

    async def _save(doc, **kw):
        saved.append(doc["url"])

    monkeypatch.setattr(R.results_db, "save", _save)
    monkeypatch.setitem(R._PLATFORM_INTER_BATCH_DELAY, PLATFORM, 0.0)
    return pool, FakeScraper, saved


def _session(sid):
    return {"id": sid, "identifier": f"acct-{sid}", "cookies": [{"name": "sessionid"}],
            "last_ok": 1.0}


def _job(n: int):
    urls = [f"https://www.instagram.com/u{i}/" for i in range(n)]
    items = [R.AnalysisItem(id=f"i{i}", raw_url=u, url=u, platform=PLATFORM, entity_id=f"u{i}")
             for i, u in enumerate(urls)]
    job = R.AnalysisJob(id="job1", items=items, total=len(items))
    job.platform_progress[PLATFORM] = {"status": "pending", "total": len(items),
                                       "completed": 0, "display_name": "Instagram"}
    return job, items, urls


async def _scrape(job, items):
    await R.AnalysisRunner()._scrape_platform(job, PLATFORM, items)


def _anon_instances(cls):
    return [s for s in cls.instances if s.anonymous]


def _account_instances(cls):
    return [s for s in cls.instances if not s.anonymous]


class TestNoLoginFirst:
    @pytest.mark.asyncio
    async def test_when_every_page_answers_no_account_is_claimed(self, monkeypatch):
        pool, cls, _ = _wire(monkeypatch, [_session("a")])
        job, items, urls = _job(3)
        await _scrape(job, items)
        assert pool.calls == 0, "an account was claimed for work the public pages did"
        assert _account_instances(cls) == []
        assert _anon_instances(cls)[0].seen == urls
        assert _anon_instances(cls)[0].cookies == []
        assert [i.status for i in items] == ["done"] * 3
        assert job.completed == 3
        assert job.platform_progress[PLATFORM]["status"] == "done"
        assert job.platform_progress[PLATFORM]["note"] == "3 read without login, 0 via account"

    @pytest.mark.asyncio
    async def test_the_first_refusal_hands_it_and_the_rest_to_the_account_path(self, monkeypatch):
        pool, cls, saved = _wire(monkeypatch, [_session("a")],
                                 {"https://www.instagram.com/u1/": E.NL_LOGIN_WALL})
        job, items, urls = _job(4)
        await _scrape(job, items)
        assert _anon_instances(cls)[0].seen == urls[:2], "no-login kept going after a refusal"
        assert _account_instances(cls)[0].seen == urls[1:]
        assert [i.status for i in items] == ["done"] * 4
        assert items[0].profile_name == "Read without login"
        assert [i.profile_name for i in items[1:]] == ["Read via account"] * 3
        # the refused read was never saved or counted as a result
        assert saved.count(urls[1]) == 1
        assert job.completed == job.total == 4
        # and its account attempt is its FIRST: failover budget untouched
        assert items[1].attempts == 1
        # no session verdict for a read that had no session
        assert pool.marked_failed == []
        assert job.platform_progress[PLATFORM]["note"] == "1 read without login, 3 via account"

    @pytest.mark.asyncio
    async def test_a_refusal_pauses_no_login_for_the_next_batch(self, monkeypatch):
        _, cls, _ = _wire(monkeypatch, [_session("a")],
                          {"https://www.instagram.com/u0/": E.NL_THROTTLED})
        job, items, _ = _job(2)
        await _scrape(job, items)
        assert R.no_login_paused()
        before = len(_anon_instances(cls))

        job2, items2, urls2 = _job(2)
        await _scrape(job2, items2)
        assert len(_anon_instances(cls)) == before, "a paused no-login phase opened a browser"
        assert _account_instances(cls)[-1].seen == urls2
        assert [i.status for i in items2] == ["done"] * 2

    @pytest.mark.asyncio
    async def test_a_page_that_did_not_load_hands_over_without_pausing(self, monkeypatch):
        _, cls, _ = _wire(monkeypatch, [_session("a")],
                          {"https://www.instagram.com/u0/": E.NL_UNREACHABLE})
        job, items, _ = _job(2)
        await _scrape(job, items)
        assert R.no_login_paused() == ""
        assert [i.status for i in items] == ["done"] * 2

    @pytest.mark.asyncio
    async def test_one_error_page_is_set_aside_and_no_login_carries_on(self, monkeypatch):
        _, cls, _ = _wire(monkeypatch, [_session("a")],
                          {"https://www.instagram.com/u1/": E.NL_ERROR_PAGE})
        job, items, urls = _job(4)
        await _scrape(job, items)
        assert _anon_instances(cls)[0].seen == urls
        assert _account_instances(cls)[0].seen == [urls[1]]
        assert R.no_login_paused() == ""
        assert [i.status for i in items] == ["done"] * 4

    @pytest.mark.asyncio
    async def test_error_pages_in_a_row_are_a_refusal_after_all(self, monkeypatch):
        bad = {f"https://www.instagram.com/u{i}/": E.NL_ERROR_PAGE for i in range(4)}
        _, cls, _ = _wire(monkeypatch, [_session("a")], bad)
        job, items, urls = _job(5)
        await _scrape(job, items)
        assert _anon_instances(cls)[0].seen == urls[:R._NO_LOGIN_ERROR_PAGE_STREAK]
        assert R.no_login_paused()
        # every URL still read, in batch order, by the account
        assert _account_instances(cls)[0].seen == urls
        assert [i.status for i in items] == ["done"] * 5

    @pytest.mark.asyncio
    async def test_a_gone_verdict_from_instagrams_own_page_is_kept(self, monkeypatch):
        pool, cls, _ = _wire(monkeypatch, [_session("a")],
                             {"https://www.instagram.com/u0/": "GONE"})
        job, items, _ = _job(1)
        await _scrape(job, items)
        assert pool.calls == 0
        assert items[0].row_status == "GONE"


class TestNoAccountToFallBackOn:
    @pytest.mark.asyncio
    async def test_refused_profiles_end_owed_with_both_reasons_never_a_result(self, monkeypatch):
        _, cls, _ = _wire(monkeypatch, [],  # the pool is empty
                          {"https://www.instagram.com/u1/": E.NL_LOGIN_WALL})
        job, items, _ = _job(3)
        await _scrape(job, items)
        assert items[0].status == "done"
        for it in items[1:]:
            assert it.status == "error"
            assert it.row_status not in ("GONE", "OK", "PARTIAL")
            assert "could not be read without login" in it.error
            assert "no Instagram account could take it over" in it.error
            assert "no healthy sessions available" in it.error
            assert "Owed" in it.error
            assert it.profile_name == "" and it.followers is None
        assert "test refusal" in items[1].error            # its own reason
        assert "earlier profile" in items[2].error          # never tried logged out
        assert job.platform_progress[PLATFORM]["status"] == "failed"
        assert job.completed == job.total == 3

    @pytest.mark.asyncio
    async def test_a_paused_no_login_with_no_account_says_so(self, monkeypatch):
        _wire(monkeypatch, [])
        R.pause_no_login("Instagram refused a logged-out visit (throttled) at 10:00 UTC")
        job, items, _ = _job(2)
        await _scrape(job, items)
        for it in items:
            assert it.status == "error"
            assert "no-login reading is paused" in it.error
            assert "Owed" in it.error


class TestSwitchedOff:
    @pytest.mark.asyncio
    async def test_off_means_the_account_path_only(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_analysis_no_login", False)
        # One account at a time, so exactly one claim. At "standard" (the
        # default since 2026-11-18) the runner asks again for more accounts.
        monkeypatch.setattr(settings, "instagram_stealth", "high")
        pool, cls, _ = _wire(monkeypatch, [_session("a")])
        job, items, urls = _job(3)
        await _scrape(job, items)
        assert _anon_instances(cls) == []
        assert _account_instances(cls)[0].seen == urls
        assert pool.calls == 1
        assert "note" not in job.platform_progress[PLATFORM]

    @pytest.mark.asyncio
    async def test_other_platforms_never_get_a_no_login_phase(self, monkeypatch):
        assert R._no_login_wanted("instagram") is True
        for pid in ("threads", "facebook", "twitter", "tiktok", "youtube", "telegram"):
            assert R._no_login_wanted(pid) is False


class TestTheJobMessageSaysWhereReadingsCameFrom:
    @pytest.mark.asyncio
    async def test_the_note_reaches_the_job_message(self, monkeypatch):
        _wire(monkeypatch, [_session("a")], {"https://www.instagram.com/u2/": E.NL_NO_DATA})
        job, items, _ = _job(3)
        monkeypatch.setattr(R.AnalysisRunner, "_preflight", AsyncMock(return_value=0))
        await R.AnalysisRunner()._run(job)
        assert "Instagram: 2 read without login, 1 via account" in job.message


# ----------------------------------------------------------------- budgets


class TestNoLoginBudget:
    def test_the_defaults(self):
        from backend.config.settings import Settings
        d = Settings(_env_file=None)
        assert d.instagram_analysis_no_login is True
        assert d.instagram_no_login_profiles_per_hour == 60

    def test_an_anonymous_visit_gets_the_no_login_ceiling_not_an_accounts(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_no_login_profiles_per_hour", 7)
        monkeypatch.setattr(settings, "instagram_profile_visits_per_hour", 3)
        assert AB.limit_for(PLATFORM, AB.PROFILE, {"anonymous": True}) == 7
        assert AB.limit_for(PLATFORM, AB.PROFILE, {"id": "a"}) == 3
        assert AB.limit_for(PLATFORM, AB.PROFILE) == 3

    def test_no_daily_ceiling_for_an_anonymous_visit(self):
        assert AB.daily_limit_for(PLATFORM, AB.PROFILE, {"anonymous": True}) == 0

    @pytest.mark.asyncio
    async def test_no_login_visits_never_touch_an_accounts_counts(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_no_login_profiles_per_hour", 2)
        anon = dict(R.NO_LOGIN_SESSION_ITEM)
        acct = _session("a")
        for _ in range(2):
            assert await AB.hold(PLATFORM, anon, AB.PROFILE, lambda: False)
        b = AB.budget()
        no_login_key = AB.NO_LOGIN_KEY.format(platform=PLATFORM)
        assert b.used(no_login_key, AB.PROFILE) == 2
        # the account's hour and day are untouched
        assert b.used("instagram:a", AB.PROFILE) == 0
        assert await AB.daily_used(PLATFORM, acct, AB.PROFILE) == 0
        # and the no-login ceiling is now full while the account's is not
        assert b.wait_s(no_login_key, AB.PROFILE, 2) > 0
        assert b.wait_s("instagram:a", AB.PROFILE, AB.limit_for(PLATFORM, AB.PROFILE, acct)) == 0

    @pytest.mark.asyncio
    async def test_the_runner_counts_each_no_login_visit_under_the_no_login_key(self, monkeypatch):
        _wire(monkeypatch, [_session("a")])
        job, items, _ = _job(3)
        await _scrape(job, items)
        assert AB.budget().used(AB.NO_LOGIN_KEY.format(platform=PLATFORM), AB.PROFILE) == 3
        assert AB.budget().used("instagram:a", AB.PROFILE) == 0


# --------------------------------------------------------------- discovery


class TestDiscoveryIsUnchanged:
    def test_instagram_still_has_no_anonymous_context(self):
        """The no-login mode is ANALYSIS ONLY. This flag is what discovery
        reads as "runnable without an account"; set on Instagram it would run
        keyword sweeps against a login page and report them as finding
        nothing."""
        plat = registry.PLATFORMS["instagram"]
        assert plat.anonymous_context_path == ""
        assert plat.can_run_anonymously is False

    def test_the_setting_is_not_read_by_discovery(self):
        from pathlib import Path
        src = Path(R.__file__).resolve().parents[1] / "discovery" / "runner.py"
        assert "instagram_analysis_no_login" not in src.read_text(encoding="utf-8")
