""""We did not look" must never be recorded as "this profile has no photo".

THE DEFECT THIS GUARDS, measured on live client data. Facebook's discovery
sweep reconciles the ids the search RENDERED against the edges it actually
parsed, and builds a Hit for every id in the gap. That Hit had no picture to
carry -- the resolve visit cannot read one in production, because Facebook
substitutes the VIEWER'S OWN photo into a privacy-restricted profile's
picture fields (see PAGE_CONTEXT_PICTURE_KEYS' own note) -- so it recorded
`has_custom_pic=False`.

False is a claim. It says the scraper looked and found the platform's stock
avatar. What actually happened is that nobody looked at all. For one live
keyword's People tab, ranked in Facebook's own order:

    rank   0-50    50 rows    0 without a picture URL
    rank  50-100   50 rows   38 without a picture URL
    rank 100-105    5 rows    2 without a picture URL

Every one of those 38 was a card reading "no logo" for a profile that
visibly has one -- exactly the report that led here.

THE RULE. `has_custom_pic` is tri-state:

    True   a real, account-chosen picture was seen in the payload
    False  the platform's own stock avatar was RECOGNISED
    None   we never got a picture to judge

None is what makes the difference safe in both directions: it is not a
false "no logo", and it is not a false "has a logo" either. It also cannot
destroy knowledge, because `profile_repository.save()` drops None from its
`$set` -- so an unknown from one sweep can never overwrite a real verdict
an earlier sweep established.
"""

from __future__ import annotations

from backend.shared.models.hit import Hit


class TestTheDefaultIsUnknown:
    def test_a_bare_hit_does_not_claim_anything(self):
        """A Hit built without a picture must not assert absence."""
        h = Hit(entity_id="1", name="X", url="https://x")
        assert h.has_custom_pic is None
        assert h.has_custom_pic is not False

    def test_a_real_picture_is_still_true(self):
        h = Hit(entity_id="1", name="X", url="https://x",
                avatar="https://cdn/pic.jpg", has_custom_pic=True)
        assert h.has_custom_pic is True

    def test_a_recognised_placeholder_is_still_false(self):
        """False keeps its meaning -- it is a verdict, not a default."""
        h = Hit(entity_id="1", name="X", url="https://x", has_custom_pic=False)
        assert h.has_custom_pic is False


class TestUnknownSurvivesTheWriteBoundary:
    """`save()` writes only values that are not None/""/{}, which is what
    stops an unknown from erasing a known verdict."""

    @staticmethod
    def _written(value) -> bool:
        # mirrors profile_repository.save()'s own filter
        return value not in (None, "", {})

    def test_a_known_verdict_is_written(self):
        assert self._written(True)
        assert self._written(False)

    def test_an_unknown_is_not_written(self):
        assert not self._written(None)

    def test_so_an_unknown_cannot_overwrite_a_previous_true(self):
        """The scenario that matters: sweep 1 saw the photo, sweep 2 hit the
        cap and never parsed this profile's edge. Sweep 2 must leave sweep
        1's answer alone rather than replacing it with a guess."""
        stored = {"has_logo": True}
        incoming = {"has_logo": None}
        merged = {**stored,
                  **{k: v for k, v in incoming.items() if self._written(v)}}
        assert merged["has_logo"] is True

    def test_but_a_real_false_still_corrects_a_stale_true(self):
        """A profile that swapped its photo for the default must still be
        able to move True -> False; only UNKNOWN is inert."""
        stored = {"has_logo": True}
        incoming = {"has_logo": False}
        merged = {**stored,
                  **{k: v for k, v in incoming.items() if self._written(v)}}
        assert merged["has_logo"] is False


class TestTheDistinctionIsThreeWay:
    def test_none_false_and_true_are_all_distinguishable(self):
        """A two-state field cannot express this, which is the whole reason
        the bug existed: `not has_custom_pic` was true for both 'stock
        avatar' and 'never looked'."""
        seen = {h.has_custom_pic for h in (
            Hit(entity_id="a", name="", url="", has_custom_pic=True),
            Hit(entity_id="b", name="", url="", has_custom_pic=False),
            Hit(entity_id="c", name="", url=""),
        )}
        assert seen == {True, False, None}

    def test_falsiness_alone_no_longer_decides_anything(self):
        unknown = Hit(entity_id="c", name="", url="")
        stock = Hit(entity_id="b", name="", url="", has_custom_pic=False)
        # both are falsy...
        assert not unknown.has_custom_pic and not stock.has_custom_pic
        # ...but they mean different things, and code must test identity
        assert unknown.has_custom_pic is not stock.has_custom_pic
