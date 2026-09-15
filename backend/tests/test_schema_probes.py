"""Every engine's parse path still runs, and still reports which keys it read.

WHY THIS FILE EXISTS, SPECIFICALLY. The schema probes were threaded into six
engines' parse functions by editing each one, and the full test suite passed
afterwards -- while `telegram/discovery_engine.py` was calling
`entity_from(obj, probe)` against a function that still took one argument.
Every Telegram sweep would have raised `TypeError` on its first result.

Nothing caught it because nothing in the suite ever called these parsers.
The discovery tests drive the RUNNER with a fake discoverer, so they cover
scheduling, caps, coverage and streaming beautifully and never touch a line
of real parsing code. An import check does not help either: the mismatch is
at a call site inside an `async def`, which Python is perfectly happy to
compile.

So this file does the one thing that was missing -- it pushes a payload
shaped like the real thing through each engine's own parser and checks both
halves: the results still come out, and the probe recorded the attributes it
walked past.

THESE ARE HAND-BUILT PAYLOADS, NOT CAPTURES. They prove the code runs and
the probe is wired; they cannot prove the key names still match what the
platform serves today. Only a live sweep does that, and when a name does
drift, the probe is what says so -- see services/engine_health_service.py.
"""

from __future__ import annotations

import json

from backend.shared.schema_probe import SchemaProbe


class TestFacebookSearchPayload:
    def test_results_and_pagination_both_parse_and_report(self):
        from backend.platforms.facebook import discovery_engine as F

        cursor = json.dumps({
            "result_ids_shown": ["100001", "100002"],
            "is_end_of_serp": False,
            "unit_id_logging_fields": {"num_total_results": 240},
            "flow_cursors_serialized": {
                "t": json.dumps({"processed_unicorn_ids": ["100003"]})},
        })
        blob = {"data": {"serpResponse": {"results": {
            "edges": [{"rendering_strategy": {"view_model": {
                "__typename": next(iter(F.VIEW_MODELS)),
                "profile": {
                    "id": "100001", "name": "Acme Corp", "__typename": "User",
                    "profile_url": "https://www.facebook.com/acme",
                    "profile_picture": {"uri": "https://cdn/pic.jpg"},
                    "is_verified": True,
                },
            }}}],
            "page_info": {"has_next_page": True, "end_cursor": cursor},
        }}}}

        probe = SchemaProbe()
        hits = list(F.iter_results(blob, probe))
        state = F.page_state(blob, probe)

        assert [h.entity_id for h in hits] == ["100001"]
        assert hits[0].name == "Acme Corp"
        # The cursor is the completeness signal -- losing it silently costs
        # every "we reached the real end of the results" verdict.
        assert state is not None and state.ids_shown == ["100001", "100002"]
        assert state.total_results == 240
        assert probe.broken_keys(min_samples=1) == []

    def test_an_edge_whose_shape_moved_is_recorded_as_a_miss(self):
        """The whole point of the probe. Facebook still sends edges, we
        still walk them, and the key we read is not there any more -- which
        is a rename, and is nothing at all like an empty search."""
        from backend.platforms.facebook import discovery_engine as F

        blob = {"results": {"edges": [
            {"renderingStrategy": {"viewModel": {"profile": {"id": "1"}}}},
            {"renderingStrategy": {"viewModel": {"profile": {"id": "2"}}}},
        ]}}

        probe = SchemaProbe()
        assert list(F.iter_results(blob, probe)) == []
        assert F.K_VIEW_MODEL in probe.broken_keys(min_samples=2)

    def test_an_empty_search_records_no_misses_at_all(self):
        """The false positive that would make the probe useless. No edges
        means nobody matched, which is the commonest outcome there is."""
        from backend.platforms.facebook import discovery_engine as F

        probe = SchemaProbe()
        assert list(F.iter_results({"results": {"edges": []}}, probe)) == []
        assert probe.broken_keys(min_samples=1) == []


class TestTwitterSearchPayload:
    def test_a_modern_payload_with_no_legacy_block_parses(self):
        """X has been moving fields out of `legacy` one at a time, and
        already ships responses with no `legacy` key at all. Handled, and
        recorded as a hit -- a migration the engine absorbed is not
        something to wake anybody for."""
        from backend.platforms.twitter import discovery_engine as T

        blob = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {
            "instructions": [{"type": "TimelineAddEntries", "entries": [
                {"entryId": "user-1", "content": {"itemContent": {"user_results": {
                    "result": {
                        "__typename": "User", "rest_id": "1",
                        "core": {"screen_name": "acme", "name": "Acme",
                                 "created_at": "Mon Jan 01 00:00:00 +0000 2020"},
                        "avatar": {"image_url": "https://pbs/pic.jpg"},
                    },
                }}}},
                {"entryId": "cursor-bottom", "content": {
                    "entryType": "TimelineTimelineCursor",
                    "cursorType": "Bottom", "value": "DAABC"}},
            ]}],
        }}}}}

        probe = SchemaProbe()
        state = T.search_state(blob, probe)

        assert [u.handle for u in state.users] == ["acme"]
        assert state.bottom_cursor == "DAABC"
        assert probe.broken_keys(min_samples=1) == []

    def test_a_renamed_handle_field_is_named(self):
        from backend.platforms.twitter import discovery_engine as T

        res = {"__typename": "User", "rest_id": "1",
               "core": {"username": "acme", "name": "Acme"},
               "avatar": {"image_url": "https://pbs/p.jpg"}}

        probe = SchemaProbe()
        T._user_from_result(res, probe)

        assert T.K_HANDLE in probe.broken_keys(min_samples=1)
        # The fields that did NOT move must not be dragged in with it.
        assert T.K_NAME not in probe.broken_keys(min_samples=1)


class TestTheRemainingEngines:
    def test_instagram_mobile_search_parses_and_reports(self):
        from backend.platforms.instagram import discovery_engine as I

        blob = {"users": [{
            "username": "acme", "pk": "9", "full_name": "Acme",
            "profile_pic_url": "https://cdn/p.jpg", "is_verified": True,
        }]}

        probe = SchemaProbe()
        users = list(I.iter_mobile_search_users(blob, probe))

        assert [u.username for u in users] == ["acme"]
        assert probe.broken_keys(min_samples=1) == []

    def test_tiktok_accepts_either_spelling_of_the_handle(self):
        """TikTok serves camelCase on one endpoint and snake_case on
        another. Both are hits; only a node with neither is a rename."""
        from backend.platforms.tiktok import discovery_engine as K

        for spelling in ("uniqueId", "unique_id"):
            probe = SchemaProbe()
            blob = {"user_list": [{"user_info": {
                spelling: "acme", "nickname": "Acme", "uid": "7",
                "avatar_larger": {"url_list": ["https://cdn/a.jpg"]},
            }}]}
            users = list(K.iter_users(blob, probe))
            assert [u.username for u in users] == ["acme"], spelling
            assert K.K_TT_USERNAME not in probe.broken_keys(min_samples=1)

    def test_telegram_entity_parses_with_a_probe(self):
        """THE REGRESSION THIS FILE WAS WRITTEN FOR. `entity_from` was being
        called with a probe it did not accept, so every Telegram sweep would
        have raised TypeError on its first result -- with a green test suite
        either side of it, because nothing called this function."""
        from backend.platforms.telegram import discovery_engine as G

        class Channel:
            id, username, title, broadcast = 5, "acmechan", "Acme Channel", True
            photo, verified, scam = object(), True, False
            restricted, fake, premium = False, False, False
            participants_count, date = 1200, None

        probe = SchemaProbe()
        ent = G.entity_from(Channel(), probe)

        assert ent is not None
        assert ent.username == "acmechan" and ent.kind == "channel"
        assert probe.broken_keys(min_samples=1) == []

    def test_every_engine_carries_a_schema_field_on_its_sweep(self):
        """The tally has to have somewhere to ride out on, or the runner
        folds nothing into telemetry and the detector never sees it."""
        from backend.platforms.facebook import discovery_engine as F
        from backend.platforms.instagram import discovery_engine as I
        from backend.platforms.telegram import discovery_engine as G
        from backend.platforms.tiktok import discovery_engine as K
        from backend.platforms.twitter import discovery_engine as T
        from backend.platforms.youtube import discovery_engine as Y

        for mod in (F, T, I, K, Y, G):
            assert "schema" in mod.Sweep.__dataclass_fields__, mod.__name__
