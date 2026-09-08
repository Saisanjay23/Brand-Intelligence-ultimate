"""Test-suite isolation.

KEEP THE TEST SUITE OUT OF THE AUDIT TRAIL.

`shared/logging.py` attaches a JSONL file handler that appends every record
to `settings.log_path / brand_intel.jsonl`, and its own docstring says why
that file exists: "A scrape must stay auditable weeks later when a report
drives a takedown and someone asks where a field came from." The handler
resolves `settings.log_path` inside `emit()`, on every write, so a test run
appends to the SAME file the running server is writing -- and pytest sets up
no isolation of its own.

What that actually cost, twice, in one afternoon:

  * `facebook/search['Gautam Adani'/people]: every strategy failed` was read
    as a six-day-old coverage bug and reported as one. Some of those lines
    were real; others were fixtures. Separating them took a keyword-by-tab
    breakdown, and the giveaway was a fixture keyword nobody would ever
    search for real ('Zzyxwq Adanixyz Fakebrand').

  * `could not persist refreshed cookies: Browser context disconnected`,
    115 occurrences, was reported to the operator as a ~20% production
    failure rate on the last cookie write. Every single one turned out to
    be this suite: the surrounding lines name `fb_bot_1`, `twitter/c1` and
    `x.com/target`, which are fixtures, not accounts.

Both diagnoses were wrong in the same direction -- test data read as
production incidents -- and an audit trail that cannot be trusted to
describe only real scraping is not an audit trail. Redirecting the path is
enough: `emit()` re-reads it per record, so nothing needs to know it moved.
"""

from __future__ import annotations

import pytest

from backend.config.settings import settings


@pytest.fixture(autouse=True, scope="session")
def _isolate_audit_log(tmp_path_factory):
    """Point the JSONL audit trail at a throwaway directory for the run.

    Session-scoped and autouse: no test should have to remember this, and
    the very first record written (module import time, before any test
    body runs) must already land somewhere harmless.
    """
    original = settings.log_path
    settings.log_path = tmp_path_factory.mktemp("audit-log")
    try:
        yield
    finally:
        settings.log_path = original
