"""TikTok's discovery sweep and analysis visit, driven end to end.

WHY THIS FILE EXISTS. TikTok is region-blocked from the network this tool
runs on -- every tiktok.com URL redirects to /<region>/about with the June
2020 India ban notice -- so nobody here can point the engine at the real
site and watch it work. That left TikTok as the one platform whose engines
had never been run past their own parsers: `test_schema_probes.py` proves
`iter_users` reads a node, and nothing proved `sweep()` or `process()`
assemble anything out of those nodes.

That gap is exactly where the `probe` NameError lived (see
test_tiktok_user_search.py) -- a whole code path that only runs against a
real payload, in an engine nothing could run.

So this drives the REAL `Discovery.sweep()` and `Scraper.process()` with
only the network faked: navigation, the hydration read, the XHR absorb, the
scroll loop, the extraction chain, the geoblock check and the row build are
all the engine's own code. What it cannot prove is that TikTok still serves
these shapes -- only a live sweep does that, and that needs a route out of
the blocked region. What it does prove is that when a route exists, nothing
between the payload and the stored row is broken.

The payload shapes are the ones the engine itself documents reading:
  search XHR   {"user_list": [{"user_info": {...}}]}   -> match_kind account
  profile page __UNIVERSAL_DATA_FOR_REHYDRATION__ with
               webapp.user-detail.userInfo.{user,stats}
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.platforms.scan_options import DiscoveryOptions, ScanOptions
from backend.platforms.tiktok import analysis_engine as A
from backend.platforms.tiktok import discovery_engine as D

# --------------------------------------------------------------- payloads

SEARCH_XHR = json.dumps({
    "user_list": [
        {"user_info": {
            "unique_id": "adanigroup", "nickname": "Adani Group",
            "uid": "6812", "follower_count": 120400, "total_favorited": 998,
            "signature": "Official", "custom_verify": "verified account",
            "avatar_larger": {"url_list": ["https://cdn.tiktok/a_large.jpg"]},
        }},
        {"user_info": {
            "unique_id": "adani.fanpage", "nickname": "Adani Fan Page",
            "uid": "9931", "follower_count": 540,
            "avatar_larger": {"url_list": ["https://cdn.tiktok/b_large.jpg"]},
        }},
    ]
})

PROFILE_HYDRATION = json.dumps({
    "__DEFAULT_SCOPE__": {
        "webapp.app-context": {"user": {"uniqueId": "our_scraper_account"}},
        "webapp.user-detail": {
            "userInfo": {
                "user": {
                    "id": "6812", "uniqueId": "adanigroup",
                    "nickname": "Adani Group", "signature": "Official account",
                    "verified": True, "privateAccount": False,
                    "avatarLarger": "https://cdn.tiktok/a_large.jpg",
                },
                "stats": {
                    "followerCount": 120400, "followingCount": 12,
                    "heartCount": 998, "videoCount": 87,
                },
            },
            "itemList": [
                # a real video node: an id that decodes to a date, plus the
                # `desc`/`stats` siblings newest_post_iso requires
                {"id": "7240000000000000000", "desc": "a post",
                 "author": {"uniqueId": "adanigroup"}, "stats": {"diggCount": 4}},
            ],
        },
    }
})

GEOBLOCK_BODY = (
    "Watch now\n\nDear Users,\n\nOn June 29, 2020 the Govt. of India decided "
    "to block 59 apps, including TikTok."
)


# ------------------------------------------------------------- the fakes


class _Resp:
    def __init__(self, url: str, body: str):
        self.url = url
        self._body = body

    async def text(self) -> str:
        return self._body


class _Page:
    """Only what the engine actually drives. Everything else is real code."""

    def __init__(self, *, body: str, hydration: str = "",
                 xhr: str = "", header: dict | None = None):
        self.url = "https://www.tiktok.com/search?q=adani"
        self._body = body
        self._hydration = hydration
        self._xhr = xhr
        self._header = header or {}
        self._handler = None
        self.closed = False

    def on(self, _event, handler):
        self._handler = handler

    async def goto(self, url, **_kw):
        self.url = url
        if self._xhr and self._handler:
            self._handler(_Resp(
                "https://www.tiktok.com/api/search/general/full/?q=adani",
                self._xhr))
            await asyncio.sleep(0)

    async def inner_text(self, _sel):
        return self._body

    async def wait_for_selector(self, *_a, **_kw):
        return None

    async def wait_for_timeout(self, _ms):
        await asyncio.sleep(0)

    async def evaluate(self, js, *args):
        if "REHYDRATION" in js:
            return self._hydration
        if "scrollTop" in js or "scrollHeight" in js:
            return True
        if "following" in js or "followers" in js:      # JS_HEADER
            return self._header
        return []

    async def close(self):
        self.closed = True


class _Ctx:
    def __init__(self, page: _Page):
        self._page = page
        self.pages_opened = 0

    async def new_page(self):
        self.pages_opened += 1
        return self._page


def _disc_options(**kw) -> DiscoveryOptions:
    base = dict(timeout=5, settle=0.05, page_wait=0.05, patience=1,
                progress_every=99, max_results=0, max_pages=0, max_seconds=0)
    base.update(kw)
    return DiscoveryOptions(**base)


# ------------------------------------------------------- discovery sweep


@pytest.mark.asyncio
async def test_a_sweep_turns_a_real_search_payload_into_rows(monkeypatch):
    """The whole discovery path: navigate, absorb the XHR, run the scroll
    loop out, run the extraction chain, build rows."""
    page = _Page(body="Accounts\nVideos\n", xhr=SEARCH_XHR)
    disc = D.Discovery(_disc_options(), _Ctx(page))

    # _merge_user_accounts opens a second, anonymous browser; that is a
    # separate live path and not what this test is about.
    async def _no_merge(_out, _probe=None):
        return None

    monkeypatch.setattr(disc, "_merge_user_accounts", _no_merge)

    out = await disc.sweep("adani")

    assert [h.username for h in out.hits] == ["adanigroup", "adani.fanpage"]
    assert out.hits[0].profile_name == "Adani Group"
    assert out.hits[0].followers == 120400
    assert out.hits[0].verified is True
    assert out.hits[0].url == "https://www.tiktok.com/@adanigroup"
    assert out.error == ""
    assert page.closed, "the sweep leaked its page"


@pytest.mark.asyncio
async def test_a_sweep_reports_the_geoblock_instead_of_finding_nothing(
        monkeypatch):
    """THE STATE THIS DEPLOYMENT IS ACTUALLY IN. A blocked region serves the
    ban notice for every URL. That has to read as "blocked", never as "this
    keyword matched nobody" -- the second sends whoever investigates at the
    parsers, which is what `geoblocked()` exists to prevent."""
    page = _Page(body=GEOBLOCK_BODY)
    page.url = "https://www.tiktok.com/in/about"
    disc = D.Discovery(_disc_options(), _Ctx(page))

    out = await disc.sweep("adani")

    assert out.stopped == "geoblocked"
    assert out.complete is False
    assert "proxy" in out.error.lower(), "the fix was not named"
    assert out.hits == []


@pytest.mark.asyncio
async def test_a_capped_sweep_stops_at_the_cap(monkeypatch):
    page = _Page(body="Accounts\n", xhr=SEARCH_XHR)
    disc = D.Discovery(_disc_options(max_results=1), _Ctx(page))

    async def _no_merge(_out, _probe=None):
        return None

    monkeypatch.setattr(disc, "_merge_user_accounts", _no_merge)
    out = await disc.sweep("adani")

    assert len(out.hits) == 1
    assert out.stopped == "cap:results"


@pytest.mark.asyncio
async def test_the_sweep_carries_a_schema_tally_out(monkeypatch):
    """The drift detector reads this. A sweep that finds results and reports
    an empty tally means the probe is not wired to the parser any more."""
    page = _Page(body="Accounts\n", xhr=SEARCH_XHR)
    disc = D.Discovery(_disc_options(), _Ctx(page))

    async def _no_merge(_out, _probe=None):
        return None

    monkeypatch.setattr(disc, "_merge_user_accounts", _no_merge)
    out = await disc.sweep("adani")

    assert out.schema, "no attributes were tallied"
    assert D.K_TT_USERNAME in out.schema
    assert out.schema[D.K_TT_USERNAME][0] > 0, "the handle never registered a hit"


# ------------------------------------------------------ analysis visit


@pytest.mark.asyncio
async def test_a_profile_visit_fills_every_field_the_report_promises(
        monkeypatch):
    """The whole analysis path: navigate, read the hydration, scope it to
    this profile, fill the Row, derive the score."""
    page = _Page(body="Adani Group\n120.4K Followers\n", hydration=PROFILE_HYDRATION)
    scraper = A.Scraper(ScanOptions(timeout=5, settle=0.05), [], session_id="")
    scraper.session = None
    monkeypatch.setattr(A.Scraper, "ctx", property(lambda self: _Ctx(page)))

    async def _no_search(*_a, **_kw):
        return ""

    monkeypatch.setattr(A, "newest_post_via_search", _no_search)

    row = await scraper.process(
        "https://www.tiktok.com/@adanigroup", "Adani", "adani.com")

    assert row.status == "OK"
    assert row.profile_name == "Adani Group"
    assert row.followers == 120400
    assert row.friends == 12
    assert row.has_custom_pic is True
    assert row.verified is True
    assert row.posts_seen == "yes"
    assert row.last_post_iso, "the video node never produced a date"
    assert row.name_score > 0
    assert "creation date not exposed" in row.notes


@pytest.mark.asyncio
async def test_a_profile_visit_in_a_blocked_region_says_so(monkeypatch):
    page = _Page(body=GEOBLOCK_BODY)
    page.url = "https://www.tiktok.com/in/about"
    scraper = A.Scraper(ScanOptions(timeout=5, settle=0.05), [], session_id="")
    scraper.session = None
    monkeypatch.setattr(A.Scraper, "ctx", property(lambda self: _Ctx(page)))

    row = await scraper.process(
        "https://www.tiktok.com/@adanigroup", "Adani", "adani.com")

    assert row.status == "ERROR"
    assert "blocked" in row.notes.lower() and "proxy" in row.notes.lower()


@pytest.mark.asyncio
async def test_a_removed_account_is_gone_not_an_error(monkeypatch):
    """GONE is terminal in shared/completeness.py, so it must mean the
    account really is not there -- not that the read failed."""
    page = _Page(body="Couldn't find this account\n", hydration="")
    scraper = A.Scraper(ScanOptions(timeout=5, settle=0.05), [], session_id="")
    scraper.session = None
    monkeypatch.setattr(A.Scraper, "ctx", property(lambda self: _Ctx(page)))

    row = await scraper.process(
        "https://www.tiktok.com/@nobody", "Adani", "adani.com")

    assert row.status == "GONE"


def test_a_video_url_is_refused_rather_than_analysed_as_a_profile():
    """/@user/video/123 is a post, not an account. username_of() rejects the
    reserved segments so a video link cannot become a profile row."""
    assert A.username_of("https://www.tiktok.com/@adanigroup") == "adanigroup"
    assert A.username_of("https://www.tiktok.com/video/7240000000") == ""
    assert A.username_of("https://www.tiktok.com/explore") == ""


# ------------------------------------------- the claim follows the egress


class TestBrowserTimezoneFollowsTheExit:
    """A claimed timezone that disagrees with the real egress IP is, in
    stealth/timezone.py's own words, "a worse tell than every session
    sharing one".

    That was safe to hardcode while this host always left from India. It
    stops being safe the moment the host runs behind a VPN: every context
    then announces Asia/Kolkata from a foreign exit, and the platforms can
    see both halves. Only this half is ours to keep honest.
    """

    def test_it_still_defaults_to_india_when_nothing_is_set(self):
        """The unconfigured answer must not change -- this host leaves from
        India unless someone says otherwise."""
        from backend.stealth import timezone as TZ

        assert TZ._FALLBACK_TIMEZONE_ID == "Asia/Kolkata"
        assert TZ._configured() == "Asia/Kolkata"

    def test_an_operator_behind_a_vpn_can_make_the_claim_match(self, monkeypatch):
        from backend.config.settings import settings
        from backend.stealth import timezone as TZ

        monkeypatch.setattr(settings, "browser_timezone_id", "Asia/Singapore")
        assert TZ._configured() == "Asia/Singapore"

    def test_a_blank_or_whitespace_value_is_not_a_timezone(self, monkeypatch):
        """An empty string would reach Playwright as a real timezone_id and
        is not one."""
        from backend.config.settings import settings
        from backend.stealth import timezone as TZ

        for blank in ("", "   "):
            monkeypatch.setattr(settings, "browser_timezone_id", blank)
            assert TZ._configured() == "Asia/Kolkata"

    def test_a_broken_settings_import_never_stops_a_browser_starting(
            self, monkeypatch):
        """A session with a slightly wrong timezone still works. One that
        cannot launch does not."""
        import builtins

        from backend.stealth import timezone as TZ

        real_import = builtins.__import__

        def _boom(name, *a, **kw):
            if name == "backend.config.settings":
                raise RuntimeError("settings exploded")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _boom)
        assert TZ._configured() == "Asia/Kolkata"

    def test_every_browser_context_reads_the_same_one(self):
        """One host has one egress, so one timezone is the truth for every
        context on it. A pooled session and TikTok's anonymous context are
        the two ways a browser gets built here; both have to land on the
        same value or half the fleet contradicts the other half."""
        import inspect
        from types import SimpleNamespace

        from backend.platforms.tiktok import discovery_engine as K
        from backend.stealth import browser as B
        from backend.stealth.timezone import DEFAULT_TIMEZONE_ID

        # a pooled session, built the way session_for_job builds one
        session = B.Session(
            SimpleNamespace(headful=False, timeout=30, delay=0,
                            warmup=False, cancel=None),
            [], session_id="s1", platform="tiktok",
        )
        assert session.timezone_id == DEFAULT_TIMEZONE_ID

        # and TikTok's anonymous context, which builds its own
        assert "DEFAULT_TIMEZONE_ID" in inspect.getsource(K.anonymous_context)
