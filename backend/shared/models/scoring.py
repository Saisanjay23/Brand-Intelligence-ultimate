"""The risk rubric. One definition, two entry points.

`compute_score` drives the tool's internal `risk_score`/`priority`;
`compute_incident_risk_score` drives the `RiskScore` on an exported
incident/takedown-report row (built in `analysis/runner.py`, see
`it.incident_row`). They differ only in how each learns whether the
account is active -- one from a last-post date, one from a flag its caller
already resolved -- and both then run the SAME cascade below. They were
once two implementations of one spec and drifted, so this file is
deliberately the one place that cascade is written down.

    logo match + name match:
        + location + active (posted within ACTIVE_WINDOW_DAYS)  -> 9
        + active, no location                                   -> 8
        + location (any activity) OR dormant (an old post)      -> 7
        + neither (no location, no post date at all)            -> 6
    name match only (no logo):
        + active                                                -> 5
        + dormant (an old post date is known)                   -> 4
        + no post date at all                                   -> 3
    no clear name match                                         -> 2 (floor)

A row only reaches scoring because a sweep already matched it to the
client's keywords, so "zero risk" is never the honest answer: the
candidate exists and an analyst still has to look at it. That is the floor
of 2. Anything above it requires the name to actually match the brand --
a logo, a location, or a recent post on an account that isn't even
plausibly named after the brand is noise, not corroboration.

WHY THE LOGO DOMINATES
    Anyone can register a handle containing a brand name; lifting the
    brand's actual profile photo is a deliberate act of passing-off, and it
    is what makes a fake convincing to a victim at a glance. So a logo
    match alone outweighs location and dormancy-vs-none combined (6 vs the
    +1/+2 those add), and it sets priority outright regardless of score.

WHY LOCATION AND DORMANCY DON'T STACK
    Once logo+name are confirmed, both are secondary corroboration of the
    same kind -- extra profile detail beyond a bare handle -- so either one
    alone earns the same +1 tier rather than summing. An account that
    demonstrably posted at some point is a more established, more
    convincing impersonation than one with no visible history, which is why
    dormant (7/4) outranks no-post-data (6/3) even though neither is live.

A blank or unknown field scores nothing, never a penalty: "not visible to
this session" is not evidence of innocence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

BASE = 2
W_NAME = 1  # a clear name match lifts the floor to 3

# 6 months, the standard dormant-account threshold in brand-protection
# practice. 30-day months, so a fixed 180 rather than a calendar-varying
# "6 months" -- deterministic regardless of which months the window spans.
ACTIVE_WINDOW_DAYS = 180

NAME_THRESHOLD = 80  # token-set ratio, 0..100

# The High/Medium/Low boundary behind the Match Level filter. Lives beside
# NAME_THRESHOLD rather than as a literal in the query builder so the two
# bands can only ever be defined once.
MEDIUM_MATCH_THRESHOLD = 50

MIN_SCORE = BASE
MAX_SCORE = 9

# The activity tiers the cascade distinguishes. "unknown" (no post date
# found) and "confirmed zero posts" deliberately collapse together: both
# mean no usable activity signal, and they differ only in the display-
# facing narrative (`Row.active_yes`), never in score.
ACTIVE, DORMANT, UNKNOWN = "active", "dormant", "unknown"


def resolve_match(automated: bool, analyst: Optional[bool], validated: bool) -> bool:
    """Whether a logo/name match counts, from the three things that can say
    so, in strict order of authority:

    1. An analyst's explicit call wins outright, either way. `False` really
       means false and is never OR-ed back to True by the scraper still
       believing otherwise -- undoing a match has to actually move the
       score, or the correction is meaningless.
    2. Otherwise a validated profile counts as matched: validation is the
       analyst confirming the impersonation, so both matches are its
       default until explicitly undone.
    3. Otherwise whatever the scraper detected -- every profile nobody has
       judged yet.
    """
    if analyst is not None:
        return bool(analyst)
    if validated:
        return True
    return bool(automated)


def _activity_tier(last_post_iso: Optional[str]) -> str:
    """active | dormant | unknown, from a last-post date alone."""
    if not last_post_iso:
        return UNKNOWN
    try:
        dt = datetime.strptime(last_post_iso[:10], "%Y-%m-%d")
    except ValueError:
        return UNKNOWN
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=ACTIVE_WINDOW_DAYS)
    return ACTIVE if dt >= cutoff else DORMANT


def _rubric(*, logo: bool, name_match: bool, location: bool, tier: str) -> int:
    """The cascade in the module docstring, and the only place it exists.

    Takes an already-resolved activity `tier` rather than a date, because
    the two entry points below establish it differently and everything
    after that point is identical.
    """
    if not name_match:
        return BASE
    if logo:
        if tier == ACTIVE:
            return MAX_SCORE if location else MAX_SCORE - 1
        if location or tier == DORMANT:
            return MAX_SCORE - 2
        return MAX_SCORE - 3
    if tier == ACTIVE:
        return BASE + 3
    if tier == DORMANT:
        return BASE + 2
    return BASE + W_NAME


def compute_score(
    has_logo: bool, has_name_match: bool, has_location: bool,
    last_post_iso: Optional[str] = "",
    *, logo_match: Optional[bool] = None, username_match: Optional[bool] = None,
    validated: bool = False,
) -> int:
    """The tool's internal risk score, BASE..MAX_SCORE.

    `logo_match`/`username_match` are the analyst's own call and
    `validated` whether they approved the profile; with the scraped
    `has_logo`/`has_name_match` they resolve through `resolve_match`.
    Everything after `last_post_iso` is keyword-only and optional, so a
    live scrape with no analyst input yet scores exactly as it always did.
    """
    return _rubric(
        logo=resolve_match(has_logo, logo_match, validated),
        name_match=resolve_match(has_name_match, username_match, validated),
        location=has_location,
        tier=_activity_tier(last_post_iso),
    )


def compute_incident_risk_score(
    *,
    has_logo: bool,
    has_name_match: bool,
    followers: Optional[int],
    location: Optional[str],
    last_post_iso: Optional[str],
    is_active: bool,
) -> int:
    """The `RiskScore` written onto an exported incident/takedown-report row.

    `has_logo`/`has_name_match` are expected ALREADY RESOLVED -- see
    `resolve_match` above, which folds an analyst's explicit call and the
    validated-profile default together with the scraper's own signal -- so
    undoing a match moves the exported rating down the same cascade.

    `is_active` is likewise resolved by the caller (it reads the same
    ACTIVE_WINDOW_DAYS defined above), so dormancy here means "a post date
    exists but is outside that window". `followers` is accepted for
    signature parity with callers; the rubric has never used it.
    """
    if is_active:
        tier = ACTIVE
    elif last_post_iso:
        tier = DORMANT
    else:
        tier = UNKNOWN
    return _rubric(
        logo=has_logo, name_match=has_name_match, location=bool(location), tier=tier,
    )
