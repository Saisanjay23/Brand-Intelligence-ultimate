"""The two backfills that rewrite stored rows, tested on their decisions.

A migration is the one kind of code in this repo that cannot be re-run to
fix itself: it edits the durable record an analyst's queue is built from.
So the interesting part of each is not that it writes, but WHICH rows it
declines to touch -- and that part is pure logic, which is what is pinned
here.

    migrate_instagram_false_last_post   retracts a last-post date that was
                                        read off somebody else's post
    migrate_youtube_handle_urls         rewrites `/channel/UC...` to the
                                        channel's own @handle

Neither test reaches MongoDB or the network: the first calls the selection
predicate directly, the second drives the migration against an in-memory
collection and a stub API.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.database.migrations.migrate_instagram_false_last_post import (_day,
                                                                           _suspect)
from backend.database.migrations import migrate_youtube_handle_urls as yt

NOW = datetime.now(timezone.utc)
TODAY = NOW.date().isoformat()
OLD = (NOW - timedelta(days=400)).date().isoformat()


def _doc(**over) -> dict:
    base = {"last_post_date": OLD, "analysed_at": NOW, "posts_seen": "yes"}
    base.update(over)
    return base


class TestWhichInstagramDatesAreRetracted:
    def test_a_date_equal_to_the_scan_day_is_retracted(self):
        """The defect's signature. Every stray timestamp came from content
        published the same day the profile was visited."""
        assert _suspect(_doc(last_post_date=TODAY)) == "dated the day it was scanned"

    def test_a_date_on_an_account_with_no_posts_is_retracted(self):
        """Self-contradictory whatever the date says: Instagram stated a
        media count of zero and a date was stored anyway."""
        assert _suspect(_doc(posts_seen="no")) == "contradicts posts_seen=no"

    def test_an_ordinary_old_date_is_left_alone(self):
        assert _suspect(_doc()) == ""

    def test_todays_date_from_a_scan_a_week_ago_is_left_alone(self):
        """A real post, on a row the bug could not have produced -- the
        stray timestamps were always same-day. Retracting this one would
        destroy a good date for nothing."""
        assert _suspect(_doc(last_post_date=TODAY,
                             analysed_at=NOW - timedelta(days=7))) == ""

    def test_an_analysts_own_correction_is_never_touched(self):
        """`patch()` relabels provenance so a hand edit cannot be mistaken
        for scraped evidence. This is the other half of that promise: the
        analyst's answer outranks anything this script can work out."""
        assert _suspect(_doc(last_post_date=TODAY,
                             sources={"last_post": "analyst"})) == ""

    def test_a_row_with_no_date_is_nothing_to_do(self):
        """Which is also what makes the migration idempotent: a retracted
        row has a blank date and is skipped on the next run."""
        assert _suspect(_doc(last_post_date="")) == ""
        assert _suspect(_doc(last_post_date=None)) == ""

    @pytest.mark.parametrize("stamp,expected", [
        (NOW, TODAY),
        (NOW.isoformat(), TODAY),          # older writers stored a string
        ("2026-09-21T05:00:00+00:00", "2026-09-21"),
        (None, ""),
        ("", ""),
        (12345, ""),
    ])
    def test_the_scan_day_is_read_from_either_stored_shape(self, stamp, expected):
        assert _day(stamp) == expected


CID, CID_NO_HANDLE, CID_CLASH = "UCaaa", "UCbbb", "UCccc"
HANDLE_URL = "https://www.youtube.com/@NewGautamAdani-q3o"


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


class _Collection:
    """Just enough of a Mongo collection for the migration to run against."""

    def __init__(self, docs):
        self.docs = docs
        self.ops = []

    def find(self, query, projection=None):
        return _Cursor([d for d in self.docs if "/channel/UC" in d["url"]])

    async def find_one(self, query, projection=None):
        exclude = (query.get("_id") or {}).get("$ne")
        return next((d for d in self.docs
                     if d["url"] == query.get("url") and d["_id"] != exclude), None)

    async def bulk_write(self, ops, ordered=True):
        self.ops.extend(ops)


def _run_youtube(monkeypatch, *, handles, docs, dry_run=False) -> _Collection:
    coll = _Collection(docs)
    monkeypatch.setattr(yt, "AsyncIOMotorClient", lambda *a, **k: type(
        "_Client", (), {"__getitem__": lambda s, n: {"profiles": coll},
                        "close": lambda s: None})())

    async def channels(ids):
        return [{"id": cid, "snippet": {"customUrl": h}}
                for cid, h in handles.items() if cid in ids]

    monkeypatch.setattr(yt, "YouTubeAPI", lambda *a, **k: type(
        "_API", (), {"channels": staticmethod(channels)})())
    asyncio.run(yt.migrate(dry_run))
    return coll


class TestWhichYoutubeUrlsAreRewritten:
    DOCS = [
        {"_id": 1, "client_id": "c1", "entity_id": CID,
         "url": f"https://www.youtube.com/channel/{CID}"},
        {"_id": 2, "client_id": "c1", "entity_id": CID_NO_HANDLE,
         "url": f"https://www.youtube.com/channel/{CID_NO_HANDLE}"},
        {"_id": 3, "client_id": "c1", "entity_id": CID_CLASH,
         "url": f"https://www.youtube.com/channel/{CID_CLASH}"},
        # The document already sitting on the URL doc 3 would move to.
        {"_id": 4, "client_id": "c1", "entity_id": "UCddd",
         "url": "https://www.youtube.com/@Taken"},
    ]
    HANDLES = {CID: "@NewGautamAdani-q3o", CID_CLASH: "@Taken"}

    def test_a_resolvable_channel_gets_its_handle_url(self, monkeypatch):
        coll = _run_youtube(monkeypatch, handles=self.HANDLES, docs=list(self.DOCS))
        written = [op._doc for op in coll.ops]
        assert len(written) == 1
        assert written[0]["$set"]["url"] == HANDLE_URL
        assert written[0]["$set"]["username"] == "NewGautamAdani-q3o"

    def test_the_old_url_is_kept_not_dropped(self, monkeypatch):
        """`save()` looks a profile up by `urls` as well as by `url`, so an
        inbound reference to the id form must still find this document."""
        coll = _run_youtube(monkeypatch, handles=self.HANDLES, docs=list(self.DOCS))
        kept = coll.ops[0]._doc["$addToSet"]["urls"]["$each"]
        assert f"https://www.youtube.com/channel/{CID}" in kept
        assert HANDLE_URL in kept

    def test_a_channel_the_api_will_not_answer_for_is_left_alone(self, monkeypatch):
        """Deleted, suspended, or past today's quota. There is nothing
        truer to write than what is already stored."""
        coll = _run_youtube(monkeypatch, handles=self.HANDLES, docs=list(self.DOCS))
        assert all(op._filter["_id"] != 2 for op in coll.ops)

    def test_a_url_another_document_already_holds_is_skipped_not_merged(self, monkeypatch):
        """`(client_id, platform, url)` is a UNIQUE index, so this write
        would be rejected -- and merging two profiles means reconciling an
        analyst's triage on both, which a URL backfill has no business
        doing."""
        coll = _run_youtube(monkeypatch, handles=self.HANDLES, docs=list(self.DOCS))
        assert all(op._filter["_id"] != 3 for op in coll.ops)

    def test_a_dry_run_writes_nothing(self, monkeypatch):
        coll = _run_youtube(monkeypatch, handles=self.HANDLES,
                            docs=list(self.DOCS), dry_run=True)
        assert coll.ops == []

    def test_nothing_left_to_do_on_a_second_run(self, monkeypatch):
        """Idempotent: once rewritten, the row no longer matches the
        `/channel/UC` filter the migration selects on."""
        done = [{"_id": 1, "client_id": "c1", "entity_id": CID, "url": HANDLE_URL}]
        coll = _run_youtube(monkeypatch, handles=self.HANDLES, docs=done)
        assert coll.ops == []
