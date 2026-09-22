"""Per-worker sweep telemetry -- the thing that kept keyword sharding off.

THE ENGINE WAS NEVER THE PROBLEM. `discovery/runner.py` has claimed several
pooled sessions, sharded the keyword queue across them and failed over
between them for as long as `discovery_sequential_keywords` has existed; the
switch defaults to on for three stated reasons, and the FIRST of them is
this one: the progress record had a single `current_keyword` field, so with
three workers the chip named whichever coroutine wrote to it last. A sweep
that was working perfectly read as if it were skipping keywords at random,
which is worse than slow -- it is a tool you cannot trust while you watch it.

One slot per worker fixes that at the source. These tests pin the two things
that matter about it:

    with ONE worker   the legacy fields are written exactly as before, so
                      the ordinary case is untouched and nothing that reads
                      them has to know this exists
    with SEVERAL      no keyword is claimed as "the" current one, because
                      there isn't one -- the old behaviour was to pick at
                      random and present it as fact
"""

from __future__ import annotations

import time

import pytest

from backend.discovery.runner import PlatformSweep


def _sweep(**kw) -> PlatformSweep:
    base = dict(platform="facebook", display_name="Facebook",
                keywords_total=18, keywords_done=0)
    base.update(kw)
    return PlatformSweep(**base)


# ------------------------------------------------------------ one worker


def test_a_single_worker_writes_the_legacy_fields_exactly_as_before():
    """The sequential path is still the default, and it must be completely
    unaffected by the existence of slots."""
    p = _sweep()
    p.workers = 1
    p.worker_slots = [{}]
    p.note_worker(0, account="fb_bot_1", keyword="cyfirma", tab="people",
                  step="Searching PEOPLE tab...")

    assert p.current_keyword == "cyfirma"
    assert p.current_tab == "people"
    assert p.current_step == "Searching PEOPLE tab..."
    assert p.item_started_at_ts is not None


def test_a_single_worker_still_exposes_its_slot():
    p = _sweep()
    p.worker_slots = [{}]
    p.note_worker(0, account="fb_bot_1", keyword="cyfirma", tab="pages")
    assert p.to_dict()["worker_slots"][0]["account"] == "fb_bot_1"
    assert p.to_dict()["worker_slots"][0]["keyword"] == "cyfirma"


# --------------------------------------------------------- several workers


def test_workers_never_overwrite_each_others_keyword():
    """THE FLICKER, DIRECTLY. Three workers, three keywords, and all three
    survive -- where a single shared field kept only the last write."""
    p = _sweep()
    p.workers = 3
    p.worker_slots = [{}, {}, {}]
    p.note_worker(0, account="A", keyword="cyfirma", tab="people")
    p.note_worker(1, account="B", keyword="cyfirma_support", tab="pages")
    p.note_worker(2, account="C", keyword="cyfirma-help", tab="groups")

    slots = p.to_dict()["worker_slots"]
    assert [s["keyword"] for s in slots] == [
        "cyfirma", "cyfirma_support", "cyfirma-help"]
    assert [s["account"] for s in slots] == ["A", "B", "C"]


def test_no_single_keyword_is_presented_as_the_current_one():
    """With three workers there is no true answer, and the old behaviour --
    naming whichever wrote last -- was a guess presented as a fact."""
    p = _sweep(keywords_done=8)
    p.workers = 3
    p.worker_slots = [{}, {}, {}]
    for i, kw in enumerate(("a", "b", "c")):
        p.note_worker(i, account=str(i), keyword=kw, tab="people")

    assert p.current_keyword == ""
    assert p.current_tab == ""
    assert "3 accounts sweeping in parallel" in p.current_step
    assert "8/18 done" in p.current_step


def test_the_elapsed_timer_tracks_the_oldest_thing_still_running():
    """The chip's timer means "how long has this been going". With several
    workers it has to follow the slowest, or it resets every time any one of
    them moves on and the sweep looks permanently fast."""
    p = _sweep()
    p.workers = 2
    p.worker_slots = [{}, {}]
    p.note_worker(0, account="A", keyword="first", tab="people")
    oldest = p.worker_slots[0]["started_at_ts"]
    time.sleep(0.01)
    p.note_worker(1, account="B", keyword="second", tab="pages")

    assert p.item_started_at_ts == pytest.approx(oldest, abs=1e-6)


def test_a_worker_moving_on_does_not_reset_the_timer_to_now():
    p = _sweep()
    p.workers = 2
    p.worker_slots = [{}, {}]
    p.note_worker(0, account="A", keyword="slow-one", tab="people")
    oldest = p.worker_slots[0]["started_at_ts"]
    time.sleep(0.01)
    p.note_worker(1, account="B", keyword="kw2", tab="pages")
    time.sleep(0.01)
    p.note_worker(1, account="B", keyword="kw3", tab="pages")

    assert p.item_started_at_ts == pytest.approx(oldest, abs=1e-6)


def test_slots_grow_to_fit_a_worker_index_they_were_not_sized_for():
    """Defensive: a worker must never be able to raise IndexError over
    telemetry. Reporting is not allowed to be the reason a sweep dies."""
    p = _sweep()
    p.worker_slots = []
    p.note_worker(2, account="C", keyword="kw", tab="people")
    assert len(p.worker_slots) == 3
    assert p.worker_slots[2]["keyword"] == "kw"


# ----------------------------------------------------------------- teardown


def test_clearing_leaves_nothing_pointing_at_finished_work():
    """A slot left behind reads as a keyword still in flight after the
    platform is done -- the same class of lie the flicker was."""
    p = _sweep()
    p.workers = 2
    p.worker_slots = [{}, {}]
    p.note_worker(0, account="A", keyword="kw", tab="people")
    p.note_worker(1, account="B", keyword="kw2", tab="pages")

    p.clear_workers()

    assert p.worker_slots == []
    assert p.current_keyword == ""
    assert p.current_tab == ""
    assert p.current_step == ""
    assert p.item_started_at_ts is None


def test_the_progress_payload_keeps_every_field_the_ui_already_reads():
    """`worker_slots` is additive. Nothing that existed may disappear, or a
    running job's chip goes blank for everyone on the old contract."""
    p = _sweep()
    d = p.to_dict()
    for key in ("platform", "display_name", "status", "keywords_total",
                "keywords_done", "found", "new", "note", "current_keyword",
                "current_tab", "current_step", "item_started_at_ts",
                "started_at_ts", "finished_at_ts", "workers"):
        assert key in d, f"{key} disappeared from the progress payload"
    assert d["worker_slots"] == []
