"""A YouTube channel is reported by its @handle, not its internal id.

THE REPORT. Every YouTube row came out of the tool as
`https://www.youtube.com/channel/UCAOnNAx9wF8tkPtKH1a8VUw`. That URL is
correct and it is useless: an analyst cannot hold it against a brand name,
cannot recognise it in an export, and cannot tell two of them apart. The
channel's public identity -- what its own page displays and what YouTube's
share button copies -- is `https://www.youtube.com/@NewGautamAdani-q3o`.

WHY IT WAS THE ID. `search.list`, the one call discovery makes, returns a
channel id and a snippet and no handle at all. The handle lives on
`snippet.customUrl`, which only `channels.list` returns -- so discovery had
nothing else it could build a URL from. That call is now made once per
search page: one quota unit for up to 50 ids, against the 100 units the
page already cost.

THE TWO THINGS THAT MADE THIS SAFE TO CHANGE, because a profile's URL is
not just a label:

    IDENTITY. `entity_id` stays the UC id, and that is what
    profile_repository deduplicates on -- it matches on entity_id first,
    and separately keeps every URL shape a profile has been seen under in
    a `urls` array (see its `canonical_yt`). So the same channel stored
    under the id URL and re-read under the handle URL is one profile, not
    two.

    RESOLUTION. A handle belongs to its owner and can be changed; the UC
    id cannot. So analysis tries the stored channel id before it trusts a
    handle. Without that, a channel that renamed itself between the sweep
    that found it and the analysis that scored it would resolve to nothing
    and be recorded GONE -- "may already be taken down" -- about an
    account that is still up and still impersonating.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from backend.analysis.runner import AnalysisItem, AnalysisJob, AnalysisRunner
from backend.platforms.youtube import analysis_engine as youtube_analysis
from backend.platforms.youtube import discovery_engine as youtube
from backend.platforms.youtube.analysis_engine import Scraper, channel_ref
from backend.platforms.youtube.discovery_engine import CHANNEL_URL, channel_url
from backend.shared.models.row import Row
from backend.shared.text import handle_from_url

CID = "UCAOnNAx9wF8tkPtKH1a8VUw"
HANDLE_URL = "https://www.youtube.com/@NewGautamAdani-q3o"
ID_URL = f"https://www.youtube.com/channel/{CID}"

# One `channels.list` resource, as the API returns it.
CHANNEL = {
    "id": CID,
    "snippet": {"title": "New Gautam Adani", "customUrl": "@NewGautamAdani-q3o"},
    "statistics": {},
}


class TestTheUrlBuilder:
    def test_a_handle_becomes_the_handle_url(self):
        assert channel_url(CID, "@NewGautamAdani-q3o") == HANDLE_URL

    def test_a_handle_without_its_at_sign_still_does(self):
        """`customUrl` has been served both ways across API revisions."""
        assert channel_url(CID, "NewGautamAdani-q3o") == HANDLE_URL

    def test_padding_and_a_doubled_at_sign_do_not_leak_into_the_url(self):
        assert channel_url(CID, "  @NewGautamAdani-q3o ") == HANDLE_URL

    def test_no_handle_falls_back_to_the_id_form(self):
        """Older channels never claimed a handle, and the `channels.list`
        call can be refused by quota. Either way the row must still carry a
        working URL -- less readable is acceptable, unreachable is not."""
        assert channel_url(CID, "") == ID_URL
        assert channel_url(CID) == CHANNEL_URL.format(cid=CID)

    def test_nothing_to_build_from_yields_nothing(self):
        assert channel_url("", "") == ""

    def test_the_apis_capitalisation_is_passed_through(self):
        """The API is the only thing here that knows the handle, so its
        answer is stored as given. YouTube routes handles
        case-insensitively, so the link resolves either way."""
        assert channel_url(CID, "@newgautamadani-q3o").endswith("@newgautamadani-q3o")


class TestTheNewUrlStillWorksEverywhere:
    def test_analysis_can_resolve_a_handle_url(self):
        """`channel_ref` is what picks the API lookup, and discovery now
        hands it the handle shape on every YouTube row."""
        assert channel_ref(HANDLE_URL) == ("handle", "@NewGautamAdani-q3o")

    def test_the_id_url_still_resolves_too(self):
        """Pasted links and every row stored before this change."""
        assert channel_ref(ID_URL) == ("id", CID)

    def test_the_username_column_fills_itself_from_the_new_url(self):
        """`hit_to_row` recovers the handle from the URL, so discovery's
        `username` populates with no extra plumbing. The id form carries
        no handle and correctly yields nothing."""
        assert handle_from_url(HANDLE_URL) == "NewGautamAdani-q3o"
        assert handle_from_url(ID_URL) == ""


class TestAnalysisAdoptsTheHandle:
    def test_a_pasted_id_url_still_reports_the_handle(self):
        """The export prints this. A link pasted as `/channel/UC...` is the
        one case discovery cannot fix, so analysis has to."""
        row = Row(url=ID_URL, target="Gautam Adani")
        Scraper.fill(row, CHANNEL)
        assert row.canonical_url == HANDLE_URL

    def test_the_handle_lands_in_the_username_column(self):
        row = Row(url=ID_URL, target="Gautam Adani")
        Scraper.fill(row, CHANNEL)
        assert row.username == "NewGautamAdani-q3o"
        assert row.src.get("username") == "api"

    def test_the_url_the_caller_gave_is_not_rewritten_underneath_them(self):
        """`canonical_url` is a separate, opt-in field precisely so the key
        a caller is still holding does not move. The runner adopts it; the
        engine does not force it."""
        row = Row(url=ID_URL, target="Gautam Adani")
        Scraper.fill(row, CHANNEL)
        assert row.url == ID_URL

    def test_identity_is_still_the_channel_id(self):
        """The whole safety argument rests on this: dedup keys on
        `entity_id`, which is unaffected by the URL shape."""
        row = Row(url=HANDLE_URL, target="Gautam Adani")
        Scraper.fill(row, CHANNEL)
        assert row.profile_id == CID

    def test_a_channel_with_no_handle_keeps_the_id_url(self):
        row = Row(url=ID_URL, target="Gautam Adani")
        Scraper.fill(row, {"id": CID, "snippet": {"title": "Old Channel"},
                           "statistics": {}})
        assert row.canonical_url == ID_URL
        assert row.username == ""


class TestEveryOtherPlatformIsUntouched:
    def test_canonical_url_is_blank_by_default(self):
        """The runner only adopts a URL an engine explicitly set, so a
        blank here is what keeps this change YouTube-only."""
        assert Row(url="https://x.com/someone", target="t").canonical_url == ""


class _FakeAPI:
    """One search page, one channel. No network and no API key -- the
    handle lookup is the only thing under test."""

    def __init__(self, handles: dict[str, str]):
        self._handles = handles
        self.handle_calls = 0

    async def search_channels(self, keyword, token="", per_page=50):
        return ([{
            "id": {"channelId": CID},
            "snippet": {
                "channelTitle": "New Gautam Adani",
                "thumbnails": {"high": {"url": "https://yt3.ggpht.com/x=s800"}},
            },
        }], "")

    async def handles(self, ids):
        self.handle_calls += 1
        return dict(self._handles)


def _sweep(handles: dict[str, str]):
    # __new__ rather than the constructor: YouTubeAPI() demands an API key
    # at construction, and this test has no business having one.
    d = youtube.Discovery.__new__(youtube.Discovery)
    d.a = types.SimpleNamespace(max_results=10, max_seconds=0)
    d.api = _FakeAPI(handles)
    return d


class TestTheSweepProducesHandleUrls:
    @pytest.mark.asyncio
    async def test_a_swept_channel_carries_its_handle_url(self):
        d = _sweep({CID: "@NewGautamAdani-q3o"})
        row = (await d.sweep("gautam adani")).hits[0]
        assert row.url == HANDLE_URL
        assert row.username == "NewGautamAdani-q3o"

    @pytest.mark.asyncio
    async def test_identity_is_untouched_by_the_new_shape(self):
        d = _sweep({CID: "@NewGautamAdani-q3o"})
        assert (await d.sweep("gautam adani")).hits[0].profile_id == CID

    @pytest.mark.asyncio
    async def test_one_lookup_serves_the_whole_page(self):
        """The cost argument this change rests on: one unit per PAGE, not
        one per channel."""
        d = _sweep({CID: "@NewGautamAdani-q3o"})
        await d.sweep("gautam adani")
        assert d.api.handle_calls == 1

    @pytest.mark.asyncio
    async def test_a_sweep_survives_the_handle_lookup_coming_back_empty(self):
        """Quota refusal, an API error, or a channel that never claimed a
        handle. The row degrades to the id URL; it does not vanish."""
        d = _sweep({})
        row = (await d.sweep("gautam adani")).hits[0]
        assert row.url == ID_URL
        assert (await d.sweep("gautam adani")).stopped == "exhausted"


class _FakeChannelAPI:
    """`channels.list`/`forHandle` answering, or not answering at all."""

    def __init__(self, resolves: bool = True):
        self.resolves = resolves

    async def channels(self, ids):
        return [CHANNEL] if self.resolves else []

    async def channel_by_handle(self, handle):
        return CHANNEL if self.resolves else None

    async def latest_upload(self, playlist):
        return "2026-05-04"


def _export(url: str, known: dict | None, *, resolves: bool = True) -> dict:
    """One URL all the way through analysis to the two export layouts,
    with no network and no API key."""
    s = youtube_analysis.Scraper.__new__(youtube_analysis.Scraper)
    s.a = types.SimpleNamespace(timeout=10)
    s.api = _FakeChannelAPI(resolves)
    # The avatar confirmation downloads an image; it has nothing to do
    # with the URL and must not be reached from a unit test.
    s._confirm_avatar = lambda row: asyncio.sleep(0)

    row = asyncio.run(s.process(url, "Gautam Adani", "", known))
    it = AnalysisItem(id="i", raw_url=url, url=url, platform="youtube",
                      entity_id=CID)
    asyncio.run(AnalysisRunner()._populate(AnalysisJob(id="j"), it, row, known))
    return {"status": row.status, "url": it.url,
            "impersonated": it.legacy_row["IMPERSONATED"],
            "source": it.incident_row["Source"],
            "description": it.incident_row["Description"]}


class TestTheExportCarriesTheHandle:
    """The sheet an analyst actually sends. Every column that prints a URL
    prints the same one, and it is the handle wherever a handle is known."""

    def test_a_pasted_channel_id_url_exports_as_the_handle(self):
        out = _export(ID_URL, None)
        assert out["impersonated"] == HANDLE_URL
        assert out["source"] == HANDLE_URL
        assert HANDLE_URL in out["description"]

    def test_a_row_that_arrived_as_a_handle_stays_one(self):
        out = _export(HANDLE_URL, {"entity_id": CID})
        assert out["impersonated"] == HANDLE_URL

    def test_a_taken_down_channel_still_exports_readably(self):
        """The row most likely to matter. The API cannot answer for a
        channel that is gone, so without discovery's stored handle this is
        exactly where the export would fall back to the id."""
        out = _export(ID_URL, {"entity_id": CID, "username": "NewGautamAdani-q3o"},
                      resolves=False)
        assert out["status"] == "GONE"
        assert out["impersonated"] == HANDLE_URL

    def test_with_no_handle_known_anywhere_the_id_url_stands(self):
        """The honest floor. Nothing -- not the URL, not discovery, not the
        API -- ever saw a handle for this channel, so there is no other
        true value to print. A takedown report still needs an identifier."""
        out = _export(ID_URL, None, resolves=False)
        assert out["impersonated"] == ID_URL

    def test_every_url_column_agrees(self):
        """Three columns across two layouts, one value. A sheet where they
        disagreed would be worse than one that used the id everywhere."""
        out = _export(ID_URL, None)
        assert out["url"] == out["impersonated"] == out["source"]
