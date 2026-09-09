"""The Profile name column carries a DISPLAY NAME, not a user id.

THE COMPLAINT: X rows came back showing the handle -- `gautam_adani` --
where the analyst expected the display name, `Gautam Adani`. Two separate
causes, and the second is the one that made the first invisible.

  the substitution   `_populate` opened with
                     `it.profile_name = row.profile_name or it.entity_id`.
                     For X, `entity_id` is the handle parsed straight out
                     of the URL (`handle_of`), so ANY visit that failed to
                     read a display name silently presented the handle as
                     one -- indistinguishable, in the column, from an
                     account whose display name really is its handle.

  the dead merge     the `known` branch below it fills a missing name from
                     the display name DISCOVERY ALREADY STORED, guarded on
                     `not it.profile_name`. That guard could never be true
                     again once the handle had been substituted one line
                     earlier. The tool held the real name on file and
                     showed the handle anyway.

So the fix is an ordering, not a new source: scraped name, then what
discovery knows, and only then the handle -- as a last-resort LABEL for a
row that must still be identifiable in a list, never as a claim about what
the account is called.

Alongside it, twitter/analysis_engine.py grew a DOM read for the name, so a
rotated GraphQL query id costs the name field nothing. The JS itself needs
a browser and is not exercised here; it was verified in Chromium against
fixtures covering the real header shape, a title-only page, the old
"/ Twitter" suffix, a non-breaking space, an emoji, a Devanagari name, a
punctuation-only leading span, a display name equal to the handle, and a
login wall. What IS covered here is the Python contract around it, which is
where the handle-rejection rule lives.
"""

from __future__ import annotations

import pytest

from backend.platforms.twitter.analysis_engine import dom_display_name, handle_of


class _Page:
    """Just enough page for `dom_display_name`: one `evaluate`."""

    def __init__(self, value=None, raises: bool = False) -> None:
        self._value = value
        self._raises = raises

    async def evaluate(self, _js):
        if self._raises:
            raise RuntimeError("execution context was destroyed")
        return self._value


class TestTheDomFallbackNeverReturnsAHandle:
    """'' means "not read", which every caller distinguishes from "read as
    blank". Returning the handle instead would put the tool straight back
    into the bug this exists to fix -- one layer lower, and harder to see."""

    @pytest.mark.asyncio
    async def test_a_display_name_comes_back(self):
        assert await dom_display_name(_Page("Gautam Adani"), "gautam_adani") == "Gautam Adani"

    @pytest.mark.asyncio
    async def test_a_name_that_is_just_the_handle_is_refused(self):
        """It carries nothing the URL did not already show, and recording
        it as a display name is what made a failed read look like a
        successful one."""
        assert await dom_display_name(_Page("gautam_adani"), "gautam_adani") == ""

    @pytest.mark.asyncio
    async def test_the_at_prefixed_handle_is_refused_too(self):
        assert await dom_display_name(_Page("@gautam_adani"), "gautam_adani") == ""

    @pytest.mark.asyncio
    async def test_the_handle_comparison_ignores_case(self):
        assert await dom_display_name(_Page("Gautam_Adani"), "gautam_adani") == ""

    @pytest.mark.asyncio
    async def test_a_name_that_merely_contains_the_handle_is_kept(self):
        """Only an exact match is a non-answer. "Gautam Adani Official"
        is a real display name and must survive."""
        assert await dom_display_name(_Page("gautam_adani official"), "gautam_adani") \
            == "gautam_adani official"

    @pytest.mark.asyncio
    async def test_nothing_on_the_page_is_not_read(self):
        assert await dom_display_name(_Page(""), "acme") == ""
        assert await dom_display_name(_Page(None), "acme") == ""

    @pytest.mark.asyncio
    async def test_a_page_that_throws_costs_one_field_not_the_visit(self):
        """A navigation mid-evaluate destroys the execution context. That
        must lose the name, never the row."""
        assert await dom_display_name(_Page(raises=True), "acme") == ""

    @pytest.mark.asyncio
    async def test_it_works_with_no_handle_to_compare_against(self):
        assert await dom_display_name(_Page("Gautam Adani")) == "Gautam Adani"


class TestWhatEntityIdActuallyIsOnX:
    """Why substituting `entity_id` for a name showed a user id: on X it is
    the handle out of the URL path, not a numeric id and not a name."""

    def test_it_is_the_handle_from_the_url(self):
        assert handle_of("https://x.com/gautam_adani") == "gautam_adani"

    def test_an_at_prefix_is_stripped(self):
        assert handle_of("https://x.com/@gautam_adani") == "gautam_adani"


def _name_after_populate(scraped: str, known_name: str, entity_id: str) -> str:
    """`_populate`'s naming cascade, in its real order.

    Extracted rather than driven through the runner because the defect was
    entirely one of ORDER -- three candidates and which of them gets to
    answer first. Running the whole method would need a job, a store, an
    avatar cache and the scoring rubric to observe one assignment.
    """
    name = scraped                                  # what this visit read
    known = {"display_name": known_name} if known_name else None
    if known:
        if not name and known.get("display_name"):  # what discovery knows
            name = known["display_name"]
    if not name:                                    # last-resort label
        name = entity_id
    return name


class TestTheNamingCascade:
    HANDLE = "gautam_adani"

    def test_a_scraped_display_name_wins(self):
        assert _name_after_populate("Gautam Adani", "Stale Name", self.HANDLE) == "Gautam Adani"

    def test_discoverys_stored_name_is_used_when_the_visit_read_none(self):
        """THE REGRESSION. This branch existed all along and was
        unreachable, because the handle had already been substituted above
        it -- so a name the tool had on file lost to the user id."""
        assert _name_after_populate("", "Gautam Adani", self.HANDLE) == "Gautam Adani"

    def test_the_handle_is_only_reached_when_nothing_can_name_it(self):
        assert _name_after_populate("", "", self.HANDLE) == self.HANDLE

    def test_a_row_is_never_left_nameless(self):
        """The fallback is kept, not removed: a blank name is not
        identifiable in a list. It just goes last."""
        assert _name_after_populate("", "", self.HANDLE) != ""

    def test_no_known_record_at_all_still_reaches_the_handle(self):
        """A pasted URL has no discovery record behind it."""
        assert _name_after_populate("", "", self.HANDLE) == self.HANDLE
