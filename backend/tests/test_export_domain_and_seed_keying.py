"""The Platform-Format (incident) export's Domain column, and the seed
lookup that feeds its AssetName column.

Three separate bugs sat behind one analyst-visible symptom -- "the Domain
and Asset Name columns are wrong in the Excel":

  * Domain fell back to `it.platform`, so a client with no domain on record
    shipped a takedown report whose Domain column read "instagram" /
    "youtube" / "telegram". Never a domain, and different on every row.
  * `seed_by_url` was keyed by the STORED url but read by the NORMALIZED
    one. Facebook's normalize_url adds "www.", so every Facebook profile
    stored without it lost its whole seed -- `main_keyword` included, which
    is what AssetName reports.
  * `web.facebook.com` / `m.facebook.com` were absent from _PLATFORM_HOSTS
    and only "www." was stripped, so those URLs were rejected as "not a
    supported platform URL" and never analysed at all.
"""

from __future__ import annotations

import asyncio

from backend.analysis.runner import (
    AnalysisItem, AnalysisJob, AnalysisRunner, parse_direct_url,
)
from backend.shared.models.row import Row


def _row(platform: str, *, known: dict | None = None, job_kwargs: dict | None = None,
         entity: str = "adanigroup") -> dict:
    runner = AnalysisRunner()
    url = f"https://www.{platform}.com/{entity}"
    item = AnalysisItem(id="i", raw_url=url, url=url, platform=platform, entity_id=entity)
    asyncio.run(runner._populate(
        AnalysisJob(id="j", **(job_kwargs or {})), item, Row(url=url, target="Adani"), known,
    ))
    return item.incident_row


class TestDomainColumnIsTheClientsDomainOrNothing:
    def test_client_domain_is_used_on_every_platform(self):
        """The same client domain, whatever platform the row came from."""
        for platform in ("facebook", "instagram", "twitter", "youtube", "telegram"):
            row = _row(platform, job_kwargs={"org_id": "ADANI", "domain": "adani.com"})
            assert row["Domain"] == "adani.com", f"{platform} row lost the client domain"

    def test_missing_client_domain_is_blank_never_the_platform_name(self):
        """The actual reported bug. A platform id in a Domain column is a
        plausible-looking value that was never a domain."""
        for platform in ("facebook", "instagram", "youtube", "telegram"):
            row = _row(platform, job_kwargs={"org_id": "ADANI"})
            assert row["Domain"] == "", f"{platform} row wrote {row['Domain']!r} as a domain"

    def test_pasted_url_batch_with_no_client_is_blank_too(self):
        assert _row("twitter")["Domain"] == ""


class TestAssetNameIsTheParentKeyword:
    def test_individual_and_domain_parents_both_export(self):
        """"For both individuals and domain" -- neither type is special."""
        for parent in ("Gautam Adani", "adanigroup.com"):
            row = _row("facebook", known={"main_keyword": parent},
                       job_kwargs={"org_id": "ADANI", "domain": "adani.com"})
            assert row["AssetName"] == parent


class TestSeedSurvivesUrlNormalization:
    def test_facebook_url_without_www_still_finds_its_seed(self):
        """The AssetName bug at its source: discovery keys the seed by the
        stored url, `start()` looks it up by the normalized one."""
        stored = "https://facebook.com/adanigroup/"
        runner = AnalysisRunner()
        job, skipped = asyncio.run(runner.start(
            [stored], seed_by_url={stored: {"main_keyword": "Adani Group"}},
        ))
        assert not skipped, skipped
        assert len(job.items) == 1
        it = job.items[0]
        assert it.url != stored, "precondition: this url is the one normalization rewrites"
        assert job.seed_by_url.get(it.url) == {"main_keyword": "Adani Group"}

    def test_already_normalized_key_still_works(self):
        """Re-keying must not break the callers that were already matching."""
        stored = "https://www.facebook.com/adanigroup"
        runner = AnalysisRunner()
        job, _ = asyncio.run(runner.start(
            [stored], seed_by_url={stored: {"main_keyword": "Adani Group"}},
        ))
        assert job.seed_by_url.get(job.items[0].url) == {"main_keyword": "Adani Group"}


class TestAlternateFacebookFrontEndsAreAccepted:
    def test_web_and_mobile_hosts_resolve_to_the_same_profile(self):
        canonical = parse_direct_url("https://www.facebook.com/adanigroup")
        assert canonical is not None
        for host in ("web.facebook.com", "m.facebook.com",
                     "mobile.facebook.com", "facebook.com"):
            got = parse_direct_url(f"https://{host}/adanigroup")
            assert got is not None, f"{host} was rejected as an unsupported platform"
            assert got[0] == "facebook"
            assert got[2] == canonical[2], f"{host} resolved to a different entity_id"

    def test_an_unrelated_mobile_host_is_still_unknown(self):
        """Stripping the prefix must not turn every m.* host into a match."""
        assert parse_direct_url("https://m.example.com/adanigroup") is None
