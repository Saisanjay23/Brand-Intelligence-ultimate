"""Which Facebook tabs a run sweeps.

THE RULE, and it is the same one the platform selector beside it already
uses: EMPTY MEANS ALL, never none. Selecting nothing on the Run page is how
an analyst says "everything", so an empty list cannot mean an empty sweep --
and that also makes the setting free to add, since every existing caller
sends nothing and keeps its current behaviour exactly.

WHY IT IS WORTH HAVING. Facebook's three tabs cost very different amounts.
People is effectively unbounded for a common name and rides the max_seconds
ceiling; Pages and Groups are small finite sets that exhaust on their own in
seconds. With sweeps running strictly one keyword at a time, an analyst
hunting impersonating PAGES was paying for a People sweep on every single
keyword to get them -- which is most of the run.

Every degraded case resolves toward ALL rather than toward none: an
unrecognised tab name, a selection aimed at a platform that has no such tab,
a platform this list does not describe. A scope matching nothing would
produce a sweep that runs and finds nothing, which reads as "discovery is
broken" rather than as "that selection was wrong".
"""

from __future__ import annotations

from backend.discovery.runner import PLATFORM_TABS, tabs_for

ALL = ["people", "pages", "groups"]


class TestFacebookTabSelection:
    def test_no_selection_sweeps_every_tab(self):
        assert tabs_for("facebook", []) == ALL
        assert tabs_for("facebook", None) == ALL

    def test_one_tab_sweeps_only_that_tab(self):
        assert tabs_for("facebook", ["pages"]) == ["pages"]

    def test_two_tabs_sweep_both(self):
        assert tabs_for("facebook", ["pages", "groups"]) == ["pages", "groups"]

    def test_the_platforms_own_order_is_used_not_the_selections(self):
        """Results land in a predictable order however the chips were
        clicked -- and with one sweep at a time, tab order IS the order
        results arrive in."""
        assert tabs_for("facebook", ["groups", "people"]) == ["people", "groups"]

    def test_selecting_all_three_is_the_same_as_selecting_none(self):
        assert tabs_for("facebook", ALL) == tabs_for("facebook", [])

    def test_a_selection_matching_no_real_tab_falls_back_to_all(self):
        """A sweep that runs and can find nothing is worse than a wide one:
        it looks like a broken platform rather than a bad selection."""
        assert tabs_for("facebook", ["reels", "marketplace"]) == ALL

    def test_case_and_padding_do_not_matter(self):
        assert tabs_for("facebook", ["  PAGES  "]) == ["pages"]

    def test_it_is_inert_for_every_single_tab_platform(self):
        """Only Facebook has more than one tab; a Facebook-shaped selection
        must not be able to silence a platform it does not describe."""
        for pid in ("twitter", "instagram", "tiktok", "youtube", "telegram"):
            assert tabs_for(pid, ["pages"]) == PLATFORM_TABS.get(pid, ["people"])

    def test_an_unknown_platform_still_gets_a_tab_to_sweep(self):
        assert tabs_for("some-new-platform", ["pages"]) == ["people"]


class TestTheJobCarriesTheResolvedTabs:
    """Resolved once when the job is built, so the progress totals and the
    sweep itself can never disagree about how much work there is."""

    def test_the_sweep_unit_count_follows_the_selection(self):
        """`keywords_total` is searches x tabs. A Pages-only run on 4
        searches is 4 units, not 12 -- and a progress bar that counted 12
        would sit at a third full for a job that had finished."""
        searches = 4
        assert searches * len(tabs_for("facebook", ["pages"])) == 4
        assert searches * len(tabs_for("facebook", [])) == 12

    def test_narrowing_facebook_leaves_other_platforms_whole(self):
        resolved = {
            pid: tabs_for(pid, ["pages"])
            for pid in ("facebook", "twitter", "instagram")
        }
        assert resolved["facebook"] == ["pages"]
        assert resolved["twitter"] == ["people"]
        assert resolved["instagram"] == ["people"]
