"""A scrape limit is a non-negative whole number, or it is refused.

The runner reads a cap with `c > 0`, so a NEGATIVE one was accepted by the API
and then behaved as "no cap": a typo for a protective limit became an
unlimited sweep, with a success response. 0 stays the one way to say
"unlimited".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.api.clients import ClientBody
from backend.api.discovery import StartDiscovery
from backend.api.models import MAX_CAP
from backend.discovery.runner import _resolve_cap


class TestClientCaps:
    def test_valid_caps_are_kept(self):
        body = ClientBody(
            platform_limits_individual={"facebook": 50, "twitter": 0},
            platform_limits_domain={"instagram": 10},
            platform_tab_limits={"facebook": {"people": {"individual": 5, "domain": 9}}},
        )
        assert body.platform_limits_individual == {"facebook": 50, "twitter": 0}
        assert body.platform_tab_limits["facebook"]["people"]["domain"] == 9

    @pytest.mark.parametrize("bad", [-1, -500, MAX_CAP + 1])
    def test_a_bad_flat_cap_is_refused(self, bad):
        with pytest.raises(ValidationError):
            ClientBody(platform_limits_individual={"facebook": bad})
        with pytest.raises(ValidationError):
            ClientBody(platform_limits_domain={"facebook": bad})

    @pytest.mark.parametrize("bad", [-1, MAX_CAP + 1])
    def test_a_bad_tab_cap_is_refused(self, bad):
        with pytest.raises(ValidationError):
            ClientBody(platform_tab_limits={"facebook": {"people": {"individual": bad}}})


class TestDiscoveryCaps:
    def _start(self, **kw):
        return StartDiscovery(group_id="acme", individual_keywords=["a"], **kw)

    @pytest.mark.parametrize("field", ["platform_limits_individual", "platform_limits_domain"])
    def test_a_negative_cap_is_refused(self, field):
        with pytest.raises(ValidationError):
            self._start(**{field: {"facebook": -3}})

    def test_a_negative_blanket_cap_is_refused(self):
        with pytest.raises(ValidationError):
            self._start(max_results=-1)

    @pytest.mark.parametrize("secs", [0, -5, 24 * 3600 + 1])
    def test_a_bad_time_budget_is_refused(self, secs):
        with pytest.raises(ValidationError):
            self._start(max_seconds=secs)

    def test_good_values_pass(self):
        s = self._start(max_results=25, max_seconds=600,
                        platform_limits_individual={"twitter": 40})
        assert (s.max_results, s.max_seconds) == (25, 600)


class TestTheMostRestrictiveCapWins:
    """The cap a cell actually runs under -- the logic the form feeds."""

    def test_tab_cap_beats_a_looser_flat_cap(self):
        cap = _resolve_cap(
            "facebook", "people", "individual", 0,
            {"individual": {"facebook": 100}},
            {"facebook": {"people": {"individual": 5}}},
        )
        assert cap == 5

    def test_flat_cap_applies_to_a_tab_with_no_tab_cap(self):
        cap = _resolve_cap(
            "facebook", "pages", "individual", 0,
            {"individual": {"facebook": 100}},
            {"facebook": {"people": {"individual": 5}}},
        )
        assert cap == 100

    def test_individual_and_domain_are_capped_independently(self):
        limits = {"individual": {"twitter": 10}, "domain": {"twitter": 30}}
        assert _resolve_cap("twitter", "default", "individual", 0, limits, {}) == 10
        assert _resolve_cap("twitter", "default", "domain", 0, limits, {}) == 30

    def test_nothing_set_means_uncapped(self):
        assert _resolve_cap("instagram", "default", "domain", 0, {}, {}) == 0
