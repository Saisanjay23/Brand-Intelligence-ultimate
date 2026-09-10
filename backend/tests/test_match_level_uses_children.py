"""The badge is graded on the analyst's own permutations, not on the parent.

THE COMPLAINT. "High match / medium match should be based on the child
keywords." Parents were doing two jobs and only one of them well. As the
bucket -- the filter option, the name every hit is filed under, the label on
the export -- the parent is right, and that half was never in question. As
the thing a discovered NAME is compared against, it was wrong: an
impersonator does not register under the real name, which is the entire
reason an analyst curates permutations in the first place.

Take parent "Gautam Adani" with the curated child "adani gautam". A profile
actually called "Adani Gautam" is a dead-on hit for the term the analyst
decided was worth searching -- but graded against the PARENT it is
word-order-reversed, `contiguous_letters_match` says no, and it lands in
Medium among profiles that merely share a token.

WORSE, THE TWO HALVES OF THE BADGE DISAGREED. `name_exact_run` compared the
name against `Row.target`, the permutation actually typed into the search
box, while `name_score` compared it against the parent. High was decided on
the child and Medium/Low on the parent, so which string a hit was judged
against depended on which band it happened to fall into. There was one
comparison set in the design (`MatchTarget.terms`, `resolve_parent`) and it
had no caller at all: the plan's targets were dropped when the sweep queue
was built, so scoring never saw them.

WHAT IS PINNED HERE, in the order it matters:

  the grade comes from the children     the case above, end to end
  the parent stays the bucket           filtering by a parent must still
                                        return everything all of its
                                        children turned up
  no grade can go DOWN                  this runs against a live pipeline,
                                        so taking the BEST of a larger set
                                        is what makes it safe to switch on
  a childless parent is untouched       a client who curated nothing must
                                        sweep and grade exactly as before
  the card can say WHY                  `match_term` names the one keyword
                                        the verdict was reached against
"""

from __future__ import annotations

import pytest

from backend.discovery.runner import row_to_fields
from backend.shared import keywords as K
from backend.shared.models.row import Row
from backend.shared.text import contiguous_letters_match, name_score

MEDIUM = 50  # frontend MATCH_MEDIUM_THRESHOLD / shared.models.scoring


def _plan(parent: str, children: list[str]):
    plans = K.build_plans(
        {"keyword_groups": {"individual": [{"parent": parent, "children": children}]}})
    return plans[0]


def _grade(plan, name: str, searched: str = "") -> dict:
    """One discovered hit through the real row builder -> its stored fields
    plus the High/Medium/Low the UI would render from them."""
    row = Row(url=f"https://example.com/{name}", target=searched or plan.search,
              profile_name=name)
    fields = row_to_fields(row, plan.parent, searched, targets=plan.targets)
    fields["level"] = (
        "high" if fields["name_exact_run"]
        else "medium" if (fields["name_score"] or 0) >= MEDIUM
        else "low"
    )
    return fields


class TestTheGradeComesFromTheChildren:

    def test_a_reordered_name_matching_a_curated_child_is_high(self):
        """THE REGRESSION, in one case. Against the parent this is a
        reordered name and cannot be High; against the child the analyst
        actually curated it is exact."""
        plan = _plan("Gautam Adani", ["adani gautam"])
        got = _grade(plan, "Adani Gautam")
        assert got["level"] == "high"
        assert got["match_term"] == "adani gautam"

    def test_the_same_name_is_only_medium_without_that_child(self):
        """The other side of it, so the test above is pinning the CHILD and
        not something incidental about the name."""
        plan = _plan("Gautam Adani", [])
        assert _grade(plan, "Adani Gautam")["level"] == "medium"

    def test_a_handle_style_permutation_grades_the_profile_that_uses_it(self):
        """The case permutations exist for: an impersonator registered as a
        handle, which resembles the curated permutation and not the real
        name."""
        plan = _plan("Gautam Adani", ["gautamadanihq"])
        got = _grade(plan, "gautamadanihq official")
        assert got["level"] == "high"
        assert got["match_term"] == "gautamadanihq"

    def test_the_real_name_still_wins_when_the_profile_uses_it(self):
        """The parent stays in the comparison set. A client whose curated
        permutations are all handles must not have a profile literally
        called by the real name demoted for it."""
        plan = _plan("Gautam Adani", ["gautamadanihq", "adani.gautam.hq"])
        got = _grade(plan, "Gautam Adani Official")
        assert got["level"] == "high"
        assert got["match_term"] == "Gautam Adani"

    def test_an_unrelated_profile_is_still_low(self):
        """More terms must not mean more matches. A wider comparison set is
        only useful if it stays capable of saying no."""
        plan = _plan("Gautam Adani", ["adani gautam", "gautamadanihq"])
        got = _grade(plan, "Sunil Verma")
        assert got["level"] == "low"
        assert got["name_score"] == 0


class TestAddingChildrenNeverLowersAGrade:
    """The safety property. This ships onto a pipeline with rows already in
    it, and `best_match` takes the BEST term rather than the searched one --
    so every hit grades at least as well as it did before. Without that,
    turning this on would silently re-band existing results."""

    @pytest.mark.parametrize("name", [
        "Gautam Adani", "Gautam Adani Official", "Adani Gautam",
        "gautam.adani.hq", "Gautam", "Sunil Verma", "",
    ])
    def test_no_name_grades_worse_with_children_than_without(self, name):
        order = {"low": 0, "medium": 1, "high": 2}
        bare = _grade(_plan("Gautam Adani", []), name)
        rich = _grade(
            _plan("Gautam Adani", ["adani gautam", "gautamadanihq"]), name)
        assert order[rich["level"]] >= order[bare["level"]]
        assert (rich["name_score"] or 0) >= (bare["name_score"] or 0)


class TestTheParentIsStillTheBucket:
    """The half that was already right and must stay right: the filter
    dropdown offers parents, and filtering by one has to return everything
    all of its children found. That works because the stored `keyword` is
    the parent no matter which child did the finding."""

    @pytest.mark.parametrize("searched", ["adani gautam", "gautamadanihq"])
    def test_every_child_files_its_hits_under_the_parent(self, searched):
        plan = _plan("Gautam Adani", ["adani gautam", "gautamadanihq"])
        assert _grade(plan, "Adani Gautam", searched)["keyword"] == "Gautam Adani"

    def test_the_children_are_what_gets_searched(self):
        """Unchanged, and restated here because the whole split depends on
        it: children are the search set, the parent is the bucket."""
        plan = _plan("Gautam Adani", ["adani gautam", "gautamadanihq"])
        assert plan.search in ("adani gautam", "gautamadanihq")
        assert plan.parent == "Gautam Adani"

    def test_only_parents_are_offered_as_filter_options(self):
        groups = K.groups_for_client({"keyword_groups": {"individual": [
            {"parent": "Gautam Adani", "children": ["adani gautam"]},
            {"parent": "Jeet Adani", "children": ["jeet.adani"]},
        ]}})
        assert K.parents_of(groups, K.INDIVIDUAL) == ["Gautam Adani", "Jeet Adani"]

    def test_a_shared_permutation_files_under_the_parent_it_resembles(self):
        """One search, two possible homes. `best_match` picks per hit, so a
        term listed under two parents no longer dumps everything into
        whichever group happened to be saved first."""
        plans = K.build_plans({"keyword_groups": {"individual": [
            {"parent": "Gautam Adani", "children": ["adani"]},
            {"parent": "Jeet Adani", "children": ["adani"]},
        ]}})
        shared = next(p for p in plans if p.search == "adani")
        assert _grade(shared, "Jeet Adani Official")["keyword"] == "Jeet Adani"
        assert _grade(shared, "Gautam Adani Official")["keyword"] == "Gautam Adani"


class TestAChildlessParentIsUntouched:
    """A client who has curated no permutations must sweep, score and file
    exactly as before -- that is what makes this safe to deploy without a
    migration or a backfill."""

    @pytest.mark.parametrize("name,level", [
        ("Gautam Adani", "high"),
        ("Gautam Adani Official", "high"),
        ("Adani Gautam", "medium"),
        ("Sunil Verma", "low"),
    ])
    def test_it_grades_against_its_own_name(self, name, level):
        assert _grade(_plan("Gautam Adani", []), name)["level"] == level

    def test_a_caller_passing_no_targets_at_all_still_grades(self):
        """`row_to_fields` is called with no targets by tests and by any
        ad-hoc path that predates permutations. It has to fall back to the
        parent rather than grading against nothing."""
        row = Row(url="https://example.com/x", target="Gautam Adani",
                  profile_name="Gautam Adani Official")
        fields = row_to_fields(row, "Gautam Adani")
        assert fields["name_exact_run"] is True
        assert fields["name_score"] == 100
        assert fields["match_term"] == "Gautam Adani"


class TestTheCardCanSayWhy:

    def test_the_matched_term_is_the_one_the_grade_came_from(self):
        plan = _plan("Gautam Adani", ["adani gautam", "gautamadanihq"])
        name = "gautamadanihq"
        got = _grade(plan, name)
        assert got["match_term"] == "gautamadanihq"
        # The chip and the badge describe the SAME comparison: re-running the
        # High Match test against the term the card shows has to agree with
        # the flag that was stored.
        assert contiguous_letters_match(name, got["match_term"]) is got["name_exact_run"]

    def test_an_exact_run_outranks_a_higher_scoring_near_miss(self):
        """The ranking is (exact_run, score, ...) IN THAT ORDER, because
        High is decided on the run regardless of where the fuzzy score
        lands. If score led instead, the term shown on the card could be one
        that does not explain the badge sitting next to it.

        Driven with stub matchers rather than the real ones: the property
        under test is the RANKING, and it should hold whatever the scoring
        stack happens to return for a given pair of strings. This is what
        `best_match` takes injected functions for.
        """
        plan = _plan("Gautam Adani", ["adani gautam"])
        # The parent scores higher; only the child is an exact run.
        got = K.best_match(
            plan, "whatever",
            scorer=lambda _n, term: 99 if term == "Gautam Adani" else 10,
            exact_predicate=lambda _n, term: term == "adani gautam",
        )
        assert got.exact_run is True
        assert got.term == "adani gautam"
        assert got.score == 10

    def test_the_more_specific_term_breaks_a_tie(self):
        """Two terms matching equally well is not a coin toss. The one
        pinning down more letters wins, which is what stops a short child
        shared between two parents out-voting a parent's own full name --
        see `best_match` and the shared-permutation case above."""
        # The specific term is the CHILD here, so it is compared second --
        # otherwise the parent would win on ordering alone and this would
        # pass with no tie-break at all.
        plan = _plan("Adani", ["Jeet Adani Enterprises"])
        got = K.best_match(
            plan, "whatever",
            scorer=lambda _n, _t: 100,            # dead level
            exact_predicate=lambda _n, _t: True,  # dead level
        )
        assert got.term == "Jeet Adani Enterprises"

    def test_a_tie_keeps_the_canonical_name(self):
        """Parent first in the terms tuple, so when nothing separates them
        the real name is what the card shows -- the better label."""
        plan = _plan("Acme Corp", ["acme corp"])
        assert _grade(plan, "Acme Corp")["match_term"] == "Acme Corp"


class TestTheComparisonSetItself:
    """`match_terms_for` is where the parent and its children become one
    comparison set; everything above depends on its shape."""

    def test_it_is_the_parent_then_the_children(self):
        assert K.match_terms_for("Gautam Adani", ["adani gautam", "gautamadanihq"]) == (
            "Gautam Adani", "adani gautam", "gautamadanihq")

    def test_a_childless_parent_is_just_itself(self):
        assert K.match_terms_for("Gautam Adani") == ("Gautam Adani",)

    def test_a_child_repeating_its_parent_is_not_compared_twice(self):
        assert K.match_terms_for("Acme", ["ACME", "acme ltd"]) == ("Acme", "acme ltd")

    def test_blank_and_non_string_entries_are_dropped(self):
        """A malformed client document must degrade to "that entry is
        ignored", never to a crash on every sweep."""
        assert K.match_terms_for("Acme", ["", "  ", None, 7, "acme ltd"]) == (
            "Acme", "acme ltd")

    def test_best_match_on_an_empty_target_set_is_not_a_crash(self):
        got = K.best_match((), "anything", name_score, contiguous_letters_match)
        assert got.score == 0 and got.exact_run is False and got.term == ""

    def test_a_scorer_that_raises_cannot_take_a_sweep_down(self):
        def boom(*_a):
            raise RuntimeError("matching stack is unhappy")

        got = K.best_match(
            _plan("Acme", ["acme ltd"]), "Acme", boom, contiguous_letters_match)
        assert got.parent == "Acme"
        assert got.score == 0
