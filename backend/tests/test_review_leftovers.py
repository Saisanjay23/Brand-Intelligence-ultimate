"""Leftovers from the bug review: keyword facet keeps the category filter, and
a failed session health check says WHY it failed."""

from __future__ import annotations

from types import SimpleNamespace

from backend.database.repositories import profile_repository as repo
from backend.sessions.manager import _why_check_failed


class TestKeywordFacetKeepsTheCategory:
    def _q(self):
        return repo._build_query(
            "acme", keyword="gautam adani", keyword_match_type="individual",
            client_keywords={"name_keywords": ["gautam adani", "karan adani"],
                             "domain_keywords": ["acme.com"]},
        )

    def test_only_the_selected_keyword_is_dropped(self):
        q = repo._without_keyword_value(self._q(), "gautam adani")
        flat = repr(q)
        assert "'karan adani'" in flat            # category clause still there
        assert "acme.com" not in flat             # and it is the INDIVIDUAL one
        assert {"keywords": "gautam adani"} not in q.get("$and", [])

    def test_the_old_helper_threw_the_category_away(self):
        q = repo._without(self._q(), "keywords")
        assert "karan adani" not in repr(q)       # documents the defect

    def test_no_keyword_selected_changes_nothing(self):
        q = repo._build_query(
            "acme", keyword_match_type="domain",
            client_keywords={"name_keywords": [], "domain_keywords": ["acme.com"]})
        assert repo._without_keyword_value(q, None) == q


class TestWhyAHealthCheckFailed:
    def _scraper(self, reason):
        return SimpleNamespace(session=SimpleNamespace(last_check_reason=reason))

    def test_suspended_says_logging_in_again_will_not_help(self):
        assert "will not fix" in _why_check_failed(self._scraper("suspended"))

    def test_expired_and_checkpointed_are_different_messages(self):
        a = _why_check_failed(self._scraper("expired"))
        b = _why_check_failed(self._scraper("checkpointed"))
        assert a.startswith("expired") and b.startswith("checkpointed")

    def test_unknown_falls_back_to_the_old_text(self):
        assert _why_check_failed(self._scraper("")) == "session invalid or checkpointed"
