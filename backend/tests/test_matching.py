"""Name matching and URL parsing -- the judgements the whole tool rests on.

THE CONTRACT WORTH PROTECTING
    `name_score` and `contiguous_letters_match` deliberately DISAGREE, and
    the UI depends on them disagreeing:

        name_score("Adani Gautam", "Gautam Adani")             == 100
        contiguous_letters_match("Adani Gautam", "Gautam Adani") is False

    name_score is token-overlap and word-order-insensitive, so a reordered
    name scores perfectly. contiguous_letters_match demands the keyword's
    letters appear as one unbroken run. That is exactly why "High Match" in
    the discovery grid gates on `name_exact_run` and NOT on a score
    threshold -- a reordered name is precisely what an analyst asking for
    High Match wants excluded, and a threshold alone would let it through.

    Anyone "simplifying" these two into one function breaks the High Match
    filter without breaking anything that raises. Hence these tests.
"""

from __future__ import annotations

import pytest

from backend.analysis.runner import parse_direct_url, to_ddmmyyyy
from backend.shared.text import contiguous_letters_match, name_score


class TestContiguousLettersMatch:
    """Letters-only, unbroken-run containment. The 'true High Match' signal."""

    def test_exact_name_matches(self):
        assert contiguous_letters_match("Gautam Adani", "Gautam Adani") is True

    def test_punctuation_and_case_are_ignored(self):
        # a vanity handle is still the same letter run
        assert contiguous_letters_match("gautam.adani.hq", "Gautam Adani") is True
        assert contiguous_letters_match("GAUTAM_ADANI", "gautam adani") is True

    def test_reordered_words_do_not_match(self):
        # THE distinguishing case -- see this module's docstring
        assert contiguous_letters_match("Adani Gautam", "Gautam Adani") is False

    def test_a_substituted_character_breaks_the_run(self):
        # leetspeak impersonation is NOT an exact run; it should be caught by
        # score/fuzzy logic instead, not by this
        assert contiguous_letters_match("G4utam Adani", "Gautam Adani") is False

    def test_keyword_embedded_in_a_longer_name_matches(self):
        assert contiguous_letters_match("Reliance Jio", "jio") is True

    def test_blank_inputs_are_false_not_an_error(self):
        assert contiguous_letters_match("", "acme") is False
        assert contiguous_letters_match("acme", "") in (True, False)  # must not raise


class TestNameScore:
    def test_identical_names_score_full(self):
        assert name_score("Gautam Adani", "Gautam Adani") == 100

    def test_word_order_does_not_reduce_the_score(self):
        # deliberate: the same person written the other way round is still
        # the same person, for SCORING purposes
        assert name_score("Adani Gautam", "Gautam Adani") == 100

    def test_unrelated_names_score_zero(self):
        assert name_score("totally other", "Gautam Adani") == 0

    def test_blank_is_zero_not_an_error(self):
        assert name_score("", "acme") == 0

    def test_score_is_bounded(self):
        for a, b in [("a", "a"), ("Gautam Adani", "gautam"), ("x y z", "z y x")]:
            assert 0 <= name_score(a, b) <= 100


class TestScoreAndExactRunDiverge:
    """Locks the divergence itself, so neither can be 'unified' into the
    other without a test failing."""

    def test_a_perfect_score_can_still_fail_the_exact_run(self):
        reordered = "Adani Gautam"
        target = "Gautam Adani"
        assert name_score(reordered, target) == 100
        assert contiguous_letters_match(reordered, target) is False, (
            "if this ever passes, High Match has silently started accepting "
            "reordered names -- see this module's docstring"
        )


class TestParseDirectUrl:
    @pytest.mark.parametrize("url,platform,entity", [
        ("https://www.facebook.com/nasa", "facebook", "nasa"),
        ("https://x.com/nasa", "twitter", "nasa"),              # x.com -> twitter
        ("https://twitter.com/nasa", "twitter", "nasa"),
        ("https://www.instagram.com/nasa/", "instagram", "nasa"),
        ("https://t.me/durov", "telegram", "durov"),
        ("https://www.tiktok.com/@someone", "tiktok", "someone"),
    ])
    def test_known_hosts_resolve(self, url, platform, entity):
        got = parse_direct_url(url)
        assert got is not None, f"{url} should be parseable"
        assert got[0] == platform
        assert got[2] == entity

    def test_scheme_is_optional(self):
        got = parse_direct_url("instagram.com/nasa/")
        assert got and got[0] == "instagram"
        assert got[1].startswith("https://")

    def test_youtube_handle_strips_the_at(self):
        got = parse_direct_url("https://www.youtube.com/@NASA")
        assert got and got[0] == "youtube"
        assert got[2] == "NASA", "the @ is a URL convention, not part of the id"

    @pytest.mark.parametrize("bad", [
        "https://example.com/x",     # not a supported platform
        "",                          # empty
        "   ",                       # whitespace
        "not a url at all",
    ])
    def test_unsupported_input_returns_none_rather_than_guessing(self, bad):
        assert parse_direct_url(bad) is None


class TestToDdmmyyyy:
    def test_iso_timestamp_converts(self):
        assert to_ddmmyyyy("2026-07-16T10:00:00Z") == "16-07-2026"

    def test_plain_date_converts(self):
        assert to_ddmmyyyy("2026-07-16") == "16-07-2026"

    def test_blank_stays_blank(self):
        # a missing date must stay missing, never become "today"
        assert to_ddmmyyyy("") == ""
        assert to_ddmmyyyy(None) == ""

    def test_unrecognised_passes_through_rather_than_becoming_a_guess(self):
        assert to_ddmmyyyy("garbage") == "garbage"
