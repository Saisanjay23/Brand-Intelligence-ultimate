"""Sweep reports: the three counts, and the email they render into.

PURE LOGIC ONLY -- the renderers take a report dict and return a string, so
they are testable without Mongo or SMTP (see this suite's scope note). The
COUNTS themselves come from profile_repository's own `validated_age` clause,
which is tested in test_validated_age.py; what is tested here is that the
report presents them honestly and cannot break the email.

WHAT THESE GUARD

  1. THE ARITHMETIC THE READER WILL CHECK. New + Delta must equal Total. A
     reader who adds the first two columns and gets a different third stops
     trusting every other number in the mail.

  2. AN EMPTY REPORT MUST EXPLAIN ITSELF. Validating is a manual step that
     happens AFTER a sweep, so "New: 0" is the NORMAL state right after one
     finishes. A report that shows a bare zero reads like the sweep failed.

  3. PROFILE NAMES CANNOT BREAK THE EMAIL. Display names are scraped from
     the platform -- impersonators use quotes, angle brackets and emoji. An
     unescaped one would corrupt the HTML or inject into the reader's mail
     client.
"""

import pytest

from backend.services.report_service import (render_client_html,
                                             render_combined_html, render_text)


def report(new=3, delta=17, name="Acme Corp", samples=None, logo=2, pending=140):
    return {
        "client_id": "acme",
        "name": name,
        "generated_at": "2026-09-07T04:00:00+00:00",
        "validated": {"new": new, "delta": delta, "total": new + delta},
        "pending": {"new": 12, "total": pending},
        "logo_matches": logo,
        "samples": samples if samples is not None else [{
            "name": "Acme Support", "platform": "twitter",
            "url": "https://twitter.com/acme_support",
            "name_score": 92, "logo_similarity": 97, "logo_tier": "phash",
        }],
    }


class TestTheArithmetic:
    def test_new_plus_delta_equals_total(self):
        r = report(new=3, delta=17)
        assert r["validated"]["total"] == 20
        html = render_client_html(r)
        assert ">3<" in html and ">17<" in html and ">20<" in html

    def test_all_three_labels_are_present(self):
        html = render_client_html(report())
        for label in ("New", "Delta", "Total"):
            assert label in html

    def test_combined_totals_are_the_sum_of_its_clients(self):
        clients = [report(new=3, delta=17, name="A"), report(new=1, delta=8, name="B")]
        combined = {
            "generated_at": "2026-09-07T04:00:00+00:00",
            "clients": clients,
            "totals": {"new": 4, "delta": 25, "total": 29, "logo_matches": 4, "pending": 280},
        }
        html = render_combined_html(combined)
        assert ">4<" in html and ">25<" in html and ">29<" in html
        assert "A" in html and "B" in html


class TestAnEmptyReportExplainsItself:
    def test_zero_newly_validated_says_why(self):
        """The normal state right after a sweep: profiles found, none triaged
        yet. Without the explanation this reads as a failed sweep."""
        html = render_client_html(report(new=0, delta=20, samples=[]))
        assert "manual" in html.lower()
        assert "0" in html

    def test_a_client_with_nothing_at_all_still_renders(self):
        html = render_client_html(report(new=0, delta=0, logo=0, pending=0, samples=[]))
        assert "Total" in html and len(html) > 500

    def test_combined_with_no_clients_renders(self):
        html = render_combined_html({
            "generated_at": "x", "clients": [],
            "totals": {"new": 0, "delta": 0, "total": 0, "logo_matches": 0, "pending": 0}})
        assert "0 clients" in html or "clients" in html


class TestScrapedTextCannotBreakTheEmail:
    @pytest.mark.parametrize("hostile", [
        '<script>alert(1)</script>',
        'Acme "Official" <support@acme.com>',
        "O'Brien & Sons <b>",
        "Gautam Adani गौतम अदाणी parody Ⓜ️",
        "</td></tr><tr><td>injected",
    ])
    def test_a_hostile_display_name_is_escaped(self, hostile):
        """Impersonator names are attacker-chosen strings that land straight
        in an analyst's inbox. They must be escaped, not rendered."""
        html = render_client_html(report(samples=[{
            "name": hostile, "platform": "twitter", "url": "https://x.com/a",
            "name_score": 50, "logo_similarity": None, "logo_tier": "",
        }]))
        assert "<script>" not in html
        assert "</td></tr><tr><td>injected" not in html

    def test_a_hostile_url_is_escaped(self):
        html = render_client_html(report(samples=[{
            "name": "x", "platform": "twitter",
            "url": 'https://x.com/a" onmouseover="alert(1)',
            "name_score": None, "logo_similarity": None, "logo_tier": "",
        }]))
        assert 'onmouseover="alert(1)"' not in html

    def test_a_hostile_client_name_is_escaped(self):
        html = render_client_html(report(name="<img src=x onerror=alert(1)>"))
        assert "<img src=x" not in html


class TestPlainTextAlternative:
    def test_it_carries_the_same_three_numbers(self):
        """A mail client that refuses HTML shows this instead. A report
        nobody can read is a report nobody acts on."""
        text = render_text(report(new=3, delta=17))
        assert "3" in text and "17" in text and "20" in text
        assert "<" not in text        # genuinely plain

    def test_it_names_the_client(self):
        assert "Acme Corp" in render_text(report())


class TestMissingFieldsDoNotCrashTheRender:
    def test_a_sample_with_no_scores_renders(self):
        html = render_client_html(report(samples=[{
            "name": "No Scores", "platform": "facebook", "url": "https://fb.com/x",
            "name_score": None, "logo_similarity": None, "logo_tier": "",
        }]))
        assert "No Scores" in html
        assert "None" not in html      # never leak a Python None into an email
