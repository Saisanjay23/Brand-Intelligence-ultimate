"""Which rendered-but-unparsed Facebook ids are worth a profile visit.

THE DEFECT THIS GUARDS, and it was expensive. Facebook's search cursor
reports every id it rendered; the sweep parses edges until its result cap
fires. Reconciliation then treats each rendered-but-unparsed id as a
candidate and visits its profile page to recover a name.

Those backfilled rows sort BEHIND the graphql-confirmed ones and are then
trimmed to the cap. So once the confirmed hits already fill the cap, every
backfill is guaranteed to be cut -- and the visit that produced it was pure
waste. Measured live, one keyword, cap 12, all three tabs:

    people   27.6s   13 visits (24.6s)   all 13 discarded
    pages    28.0s   11 visits (25.0s)   all 11 discarded
    groups   33.9s   12 visits (31.1s)   all 12 discarded
    total    89.4s   36 visits           0 survived

89% of the sweep. After bounding it: 12.5s, 0 visits, byte-identical
results -- a 7.2x speedup that also removed 36 profile loads from a live
account, which is the most detectable thing this engine does.

WHY THIS IS A UNIT TEST AND NOT A LIVE ONE. The saturated case is easy to
reproduce against Facebook; the interesting boundaries are not. A sweep that
ends naturally with room left under the cap depends on how many results the
keyword happens to have that day, so pinning the arithmetic here is the only
way to hold the rule steady -- including the two directions it must NOT go:
never starving an uncapped sweep, and never rationing a nameless CONFIRMED
hit, which is inside the cap and whose visit improves a row that ships.
"""

from __future__ import annotations

from backend.platforms.facebook.discovery_engine import _worth_visiting


def _ids(n: int, start: int = 0) -> set[str]:
    return {f"1000{i}" for i in range(start, start + n)}


class TestTheCapIsSaturated:
    """Confirmed hits already fill the cap, so no backfill can survive."""

    def test_nothing_is_visited(self):
        assert _worth_visiting(_ids(13), cap=12, parsed=12) == set()

    def test_nothing_is_visited_even_with_many_missing(self):
        assert _worth_visiting(_ids(500), cap=12, parsed=12) == set()

    def test_over_full_is_still_nothing_not_negative(self):
        """`parsed` can exceed the cap -- reconciliation runs after the
        pagination loop, which stops AT the limit, not before it. A naive
        `cap - parsed` would go negative and slice from the end."""
        assert _worth_visiting(_ids(5), cap=12, parsed=20) == set()


class TestThereIsRoomUnderTheCap:
    """A sweep that ended naturally can still fit backfilled rows."""

    def test_exactly_the_room_available_is_visited(self):
        got = _worth_visiting(_ids(10), cap=12, parsed=8)
        assert len(got) == 4

    def test_fewer_missing_than_room_visits_all_of_them(self):
        missing = _ids(3)
        assert _worth_visiting(missing, cap=12, parsed=8) == missing

    def test_one_slot_left_visits_exactly_one(self):
        assert len(_worth_visiting(_ids(9), cap=12, parsed=11)) == 1

    def test_the_choice_is_deterministic(self):
        """Same inputs, same ids -- so a re-sweep does not visit a different
        arbitrary subset each time and thrash the profiles it touches."""
        missing = _ids(10)
        assert (_worth_visiting(missing, cap=12, parsed=8)
                == _worth_visiting(missing, cap=12, parsed=8))


class TestUncapped:
    """cap == 0 means nothing is trimmed, so every visit can pay off."""

    def test_everything_is_visited(self):
        missing = _ids(40)
        assert _worth_visiting(missing, cap=0, parsed=200) == missing

    def test_an_uncapped_sweep_is_never_starved_by_parsed_count(self):
        missing = _ids(7)
        assert _worth_visiting(missing, cap=0, parsed=0) == missing


class TestDegenerateInputs:
    def test_no_missing_ids_is_no_visits(self):
        assert _worth_visiting(set(), cap=12, parsed=3) == set()
        assert _worth_visiting(set(), cap=0, parsed=3) == set()

    def test_it_returns_a_set_the_caller_can_union(self):
        """sweep() unions this with the nameless-confirmed ids, so the type
        matters as much as the contents."""
        assert isinstance(_worth_visiting(_ids(4), cap=12, parsed=8), set)

    def test_it_never_returns_ids_that_were_not_missing(self):
        missing = _ids(6)
        assert _worth_visiting(missing, cap=12, parsed=8) <= missing
