"""Which candidates are worth a profile-page visit.

THE DEFECT THIS GUARDS. A sweep's reconciliation phase visited a profile
page for any hit missing a name OR missing an avatar. The visit's only
picture source is `_extract_entity`, whose avatar branch sits behind
`trust_page_context_avatar` -- which is OFF in production. So every result
whose search payload carried a DEFAULT picture (`iter_results` stores
avatar="" for those, and Facebook has a great many) bought a full
profile-page load that returned an empty avatar BY CONSTRUCTION.

It was the worst possible thing to spend: reconciliation is the slowest
phase of a sweep, and profile visits under one live account are the most
detectable thing the engine does. Cutting work that cannot pay off beats
adding concurrency to absorb it.

These tests pin both halves -- that the extraction really cannot return an
avatar with the flag off, and that a name-complete/picture-less hit is
therefore not sent to be visited.
"""

from backend.platforms.facebook.discovery_engine import (
    TRUST_PAGE_CONTEXT_AVATAR,
    _extract_entity,
)


def _payload(eid: str = "1001", name: str = "Impostor Inc") -> list:
    """A profile page's embedded JSON, carrying a real (non-default)
    picture for `eid` under one of the keys the extractor knows."""
    return [{
        "id": eid,
        "name": name,
        "profile_picture": {
            "uri": "https://scontent.fbom1-2.fna.fbcdn.net/v/t1.jpg?cstp=mx640x640&ctp=s50x50",
        },
    }]


class TestExtractionCannotReturnAnAvatarInProduction:
    def test_the_flag_is_off(self):
        """If this ever flips, the resolve-scope test below changes meaning
        -- which is exactly why the trigger keys off the flag rather than
        hardcoding the assumption."""
        assert TRUST_PAGE_CONTEXT_AVATAR is False

    def test_name_comes_back_but_avatar_does_not(self):
        name, avatar, has_custom = _extract_entity(_payload(), "1001")
        assert name == "Impostor Inc"
        assert avatar == ""
        assert has_custom is False

    def test_the_avatar_path_still_works_when_explicitly_enabled(self):
        """The plumbing is kept for a future reliable signal, so it must not
        rot -- and it is what makes the flag-keyed trigger meaningful."""
        name, avatar, has_custom = _extract_entity(
            _payload(), "1001", trust_page_context_avatar=True,
        )
        assert name == "Impostor Inc"
        assert avatar.startswith("https://scontent.")
        assert has_custom is True

    def test_a_page_carrying_another_entity_leaks_nothing(self):
        """Scoped by exact id: blank beats wrong, because a wrong name here
        surfaces as a false impersonation match."""
        name, avatar, _ = _extract_entity(_payload(eid="9999"), "1001")
        assert name == ""
        assert avatar == ""


def _needs_name(eid: str, name: str) -> bool:
    """The engine's own rule, mirrored: a name that is missing, or is just
    the numeric id echoed back, is not a name."""
    n = (name or "").strip()
    return not n or n == eid or n.isdigit()


def _to_resolve(hits: dict[str, tuple[str, str]]) -> set[str]:
    """`{eid: (name, avatar)}` -> the ids the sweep would visit, under the
    current flag."""
    return {
        eid for eid, (name, avatar) in hits.items()
        if _needs_name(eid, name) or (TRUST_PAGE_CONTEXT_AVATAR and not avatar)
    }


class TestWhatGetsVisited:
    def test_a_hit_missing_only_its_picture_is_not_visited(self):
        """The whole point. The visit could not return a picture anyway."""
        assert _to_resolve({"1001": ("Impostor Inc", "")}) == set()

    def test_a_hit_missing_its_name_is_visited(self):
        assert _to_resolve({"1001": ("", "https://scontent./x.jpg")}) == {"1001"}

    def test_a_name_that_is_just_the_id_counts_as_missing(self):
        """Facebook echoes the numeric id where a name is unavailable; that
        is what surfaced on cards as a bare number."""
        assert _to_resolve({"1001": ("1001", "")}) == {"1001"}
        assert _to_resolve({"1001": ("50840430092", "")}) == {"1001"}

    def test_a_complete_hit_is_not_visited(self):
        assert _to_resolve({"1001": ("Impostor Inc", "https://scontent./x.jpg")}) == set()

    def test_the_common_sweep_shape_collapses_to_almost_nothing(self):
        """A realistic mix: most results have names, many have no custom
        picture. Only the genuinely nameless are worth a page load -- one
        visit here instead of the four the old rule would have made."""
        hits = {
            "1": ("Alpha Corp", ""),                      # no pic -> skip
            "2": ("Beta Ltd", "https://scontent./b.jpg"),  # complete -> skip
            "3": ("Gamma", ""),                            # no pic -> skip
            "4": ("", ""),                                 # no name -> visit
        }
        assert _to_resolve(hits) == {"4"}
