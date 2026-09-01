"""How a scan/sweep is run, the configuration every platform adapter's
constructor takes.

Lives here rather than inside `analysis` or `discovery` because it is a
shared parameter type used by every caller that constructs an adapter:
the analysis module's job runner, the sessions module's on-demand health
check, and the CLI alike. Ported unchanged from `backend/core/options.py`
and `backend/core/discovery_options.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ScanOptions:
    evidence: Optional[str] = None  # GridFS key prefix for screenshots; disables asset blocking
    ephemeral_screenshot: bool = False  # If True, captures screenshot and puts in row.screenshot_bytes instead of DB
    headful: bool = False
    timeout: int = 45  # per-navigation, seconds
    settle: float = 6.0  # cap on waiting for the profile payload
    delay: float = 2.5  # between profiles, jittered
    scrolls: int = 0  # newest post is in the first render
    concurrency: int = 1  # >1 is faster and more conspicuous
    keep_going: bool = False  # continue past a checkpoint


def captures_screenshot(args) -> bool:
    """Will this run take an evidence screenshot at all?

    True for BOTH ways of capturing one: persisted to GridFS (`evidence`)
    or held in memory (`ephemeral_screenshot`). Every browser platform's
    Scraper uses this to decide whether images may load, and it must cover
    both -- the stealth session blocks images by default (see
    stealth/browser.py), so a capture taken with them blocked is a
    screenshot of black boxes, which is worthless as evidence of what a
    profile actually looked like.

    This checked `evidence` alone until the analysis tool moved to
    memory-only results: it passes `evidence=None` precisely so nothing
    reaches GridFS, and every screenshot it took came back with the avatar
    and page imagery blacked out.

    Reads via getattr because adapters are documented as taking a
    "ScanOptions-shaped object", not necessarily a ScanOptions.
    """
    return bool(
        getattr(args, "evidence", None)
        or getattr(args, "ephemeral_screenshot", False)
    )


@dataclass
class DiscoveryOptions:
    """Separate from ScanOptions because the two phases tune against
    different limits: analysis is paced to protect the session on repeated
    profile visits, discovery is paced by how fast the platform hands over
    the next results page."""

    headful: bool = False
    timeout: int = 45
    # The network/GraphQL response is the primary data source on every
    # platform that has one (run_strategies tries "network:..." before
    # "dom:...", see each engine's sweep(), shared/extraction.py); DOM
    # only stands in when the network payload comes up completely empty.
    # These three numbers are what stand between "the response was just
    # slow" and "gave up and fell back to DOM (or stopped) too early",
    # raised from 12/6/3 specifically to make missing a real response as
    # unlikely as practical, since a sweep that never captures it either
    # falls back to the weaker DOM read or reports fewer results than
    # actually exist. Still bounded (not infinite): discovery_max_seconds
    # (default 300s/sweep) is the real backstop against a sweep hanging.
    settle: float = 20  # cap on waiting for the first results render
    page_wait: float = 10.0  # cap on waiting for one more results page
    patience: int = 5  # scrolls with no new ids before calling it stalled
    concurrency: int = 2  # keyword sweeps in flight at once
    progress_every: int = 5  # log a progress line every N result pages

    # Caps. People search is effectively unbounded on some platforms, so a
    # sweep needs a stop somewhere. Hitting any cap marks the sweep
    # INCOMPLETE rather than pretending the results ran out.
    max_results: int = 0  # 0 = no cap
    max_pages: int = 0  # 0 = no cap
    # Mirrors settings.discovery_max_seconds -- this dataclass default is
    # only what a caller gets that never passes max_seconds at all (the
    # live path, discovery/runner.py, always sets one explicitly from
    # settings). See that setting's own comment for why 900, not 300.
    max_seconds: float = 900  # per sweep; 0 = no cap
