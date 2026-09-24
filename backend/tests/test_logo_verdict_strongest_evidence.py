"""The logo verdict is the STRONGEST evidence any stage produced -- not
whichever stage ran last, and not "discovery always wins".

WHY IT CHANGED. Analysis used to inherit discovery's `has_logo` outright,
in both directions, because a Facebook profile visit can render the
viewer's own photo into a privacy-restricted profile. That protection
survives (a picture read off a page render is only OBSERVED evidence, so it
never outranks a recognised stock avatar), but the blanket rule also threw
away evidence strictly better than a search card's URL guess: Instagram's
own `has_anonymous_profile_picture`, X's `default_profile_image`, and the
pixel check on the downloaded bytes. See shared/logo_verdict.py.

A Logo "Yes" alone forces High priority, so every case here is checked
through to the score, not just the exported flag.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.analysis.runner import AnalysisItem, AnalysisJob, AnalysisRunner
from backend.shared import logo_verdict as lv
from backend.shared.models.row import Row


def _populate(*, row_kwargs: dict, known: dict | None, platform: str = "facebook",
              src_logo: str = "") -> tuple[AnalysisItem, Row]:
    runner = AnalysisRunner()
    item = AnalysisItem(id="i", raw_url="u", url=f"https://{platform}.com/x",
                        platform=platform, entity_id="x")
    row = Row(url=f"https://{platform}.com/x", target="", status="OK",
              profile_name="Acme", **row_kwargs)
    if src_logo:
        row.mark("logo", src_logo)
    asyncio.run(runner._populate(AnalysisJob(id="j"), item, row, known))
    return item, row


# ------------------------------------------------------------ the resolver

class TestResolver:
    def test_higher_strength_wins_in_either_direction(self):
        flag_yes = lv.evidence(True, "api-anonymous-flag")
        marker_no = lv.evidence(False, "")
        assert lv.resolve(marker_no, flag_yes) is flag_yes
        flag_no = lv.evidence(False, "api-anonymous-flag")
        observed_yes = lv.evidence(True, "")
        assert lv.resolve(observed_yes, flag_no) is flag_no

    def test_a_tie_goes_to_the_placeholder(self):
        a, b = lv.LogoEvidence(True, lv.PIXELS, "x"), lv.LogoEvidence(False, lv.PIXELS, "y")
        assert lv.resolve(a, b).value is False
        assert lv.resolve(b, a).value is False

    def test_unknown_never_overrides_anything(self):
        yes = lv.evidence(True, "")
        assert lv.resolve(yes, lv.UNKNOWN) is yes
        assert lv.resolve(lv.UNKNOWN, yes) is yes
        assert lv.resolve(lv.UNKNOWN, lv.UNKNOWN).known is False

    def test_an_analysts_edit_outranks_every_automated_signal(self):
        manual = lv.evidence(True, "manual")
        assert lv.resolve(manual, lv.LogoEvidence(False, lv.FLAG, "api-anonymous-flag")) is manual

    @pytest.mark.parametrize("value,source,strength", [
        (True, "", lv.OBSERVED),
        (False, "", lv.MARKER),
        (False, "dom-header", lv.MARKER),
        (True, "api-anonymous-flag", lv.FLAG),
        (False, "graphql-default-flag", lv.FLAG),
        (False, "default-avatar-hash", lv.PIXELS),
        (False, "generated-avatar", lv.PIXELS),
        (None, "api-anonymous-flag", lv.NONE),
    ])
    def test_strength_by_source(self, value, source, strength):
        assert lv.evidence(value, source).strength == strength

    def test_a_stored_document_is_read_with_its_persisted_strength(self):
        doc = {"has_logo": False, "logo_strength": lv.PIXELS, "logo_source": "generated-avatar"}
        assert lv.doc_evidence(doc) == lv.LogoEvidence(False, lv.PIXELS, "generated-avatar")

    def test_a_legacy_document_is_judged_by_its_value_and_sources(self):
        assert lv.doc_evidence({"has_logo": True}).strength == lv.OBSERVED
        assert lv.doc_evidence({"has_logo": True, "sources": {"logo": "manual"}}).strength == lv.MANUAL
        assert lv.doc_evidence({}).known is False


# ------------------------------------------------------ analysis merge

class TestAnalysisMerge:
    def test_instagrams_own_flag_beats_a_search_card_url_guess(self):
        """THE CASE THIS CHANGE EXISTS FOR. The card's URL looked like a real
        upload (Instagram had rotated its anonymous-avatar asset id), the
        profile visit read `has_anonymous_profile_picture: true`. No logo."""
        item, row = _populate(platform="instagram",
                              row_kwargs=dict(has_custom_pic=False),
                              src_logo="api-anonymous-flag",
                              known={"has_logo": True})
        assert item.has_logo is False
        assert row.logo_yes == "No"

    def test_a_facebook_page_render_yes_never_beats_a_recognised_stock_avatar(self):
        """The viewer-photo protection the old rule existed for: a picture
        read off a Facebook page render is OBSERVED, discovery's recognised
        placeholder outranks it."""
        item, row = _populate(row_kwargs=dict(has_custom_pic=True),
                              src_logo="dom-avatar",
                              known={"has_logo": False, "logo_strength": lv.MARKER})
        assert item.has_logo is False
        assert row.logo_yes == "No"

    def test_a_pixel_verdict_stored_by_discovery_is_kept(self):
        item, _ = _populate(row_kwargs=dict(has_custom_pic=True),
                            known={"has_logo": False, "logo_strength": lv.PIXELS,
                                   "logo_source": "generated-avatar"})
        assert item.has_logo is False

    def test_a_recognised_stock_url_on_the_visit_beats_an_unverified_yes(self):
        item, _ = _populate(row_kwargs=dict(has_custom_pic=False), known={"has_logo": True})
        assert item.has_logo is False

    def test_an_analysts_manual_yes_is_never_overridden(self):
        item, _ = _populate(platform="instagram",
                            row_kwargs=dict(has_custom_pic=False),
                            src_logo="api-anonymous-flag",
                            known={"has_logo": True, "logo_source": "manual"})
        assert item.has_logo is True

    def test_an_unknown_visit_keeps_discoverys_verdict(self):
        item, _ = _populate(row_kwargs={}, known={"has_logo": True})
        assert item.has_logo is True

    def test_no_known_record_leaves_the_visit_in_charge(self):
        assert _populate(row_kwargs=dict(has_custom_pic=True), known=None)[0].has_logo is True
        assert _populate(row_kwargs=dict(has_custom_pic=False), known=None)[0].has_logo is False

    def test_a_known_record_without_a_verdict_changes_nothing(self):
        assert _populate(row_kwargs=dict(has_custom_pic=True), known={})[0].has_logo is True
        assert _populate(row_kwargs=dict(has_custom_pic=True),
                         known={"has_logo": None})[0].has_logo is True

    def test_the_row_is_corrected_too_so_the_score_agrees(self):
        _, row = _populate(row_kwargs=dict(has_custom_pic=True),
                           known={"has_logo": False, "logo_strength": lv.PIXELS})
        assert row.has_custom_pic is False
        assert row.logo_yes == "No"
