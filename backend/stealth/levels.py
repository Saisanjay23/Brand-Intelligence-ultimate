"""A platform's stealth LEVEL -> the concrete pacing and parallelism it runs at.

WHY ONE SWITCH. Stealth on a logged-in Meta account is mostly a matter of
volume and rhythm, and those are spread over four unrelated constants in two
runners and one engine (gap between keywords, gap between pages, gap between
analysis batches, accounts working at once). Tuning them one by one is how a
platform ends up half-careful: slow between keywords but three accounts at a
time from one IP. A level moves them together, so "high" means one coherent
posture and "standard" means exactly what the tool did before levels existed.

  standard  today's numbers, unchanged.
  high      a person working one account, slowly: one account at a time, longer
            gaps everywhere. Costs speed. A sweep takes roughly twice as long.

WHAT A LEVEL DOES NOT TOUCH: what is collected. No search, tab, field or
keyword is dropped at any level -- only the time between them and how many
accounts work at once. Keyword coverage is not a stealth lever.

ONLY INSTAGRAM HAS LEVELS TODAY. Every other platform returns the caller's own
default untouched, so adding this module changes nothing for them.

The value comes from `settings`, read at call time (not import time), so a
test or an operator changing it takes effect without a restart of the module.
"""

from __future__ import annotations

import random
from typing import Optional

from backend.config.settings import settings

STANDARD = "standard"
HIGH = "high"
LEVELS = (STANDARD, HIGH)

# keyword_gap  median seconds between one keyword's sweep and the next
# page_gap     median seconds between two result pages of one search
# batch_gap    median seconds between analysis batches (one profile each, on
#              Instagram)
# max_sessions accounts allowed to work this platform at once; None = no
#              level-imposed cap (the runner's own default applies)
_TABLE: dict[str, dict[str, dict[str, Optional[float]]]] = {
    "instagram": {
        STANDARD: {"keyword_gap": 8.0, "page_gap": 2.5, "batch_gap": 3.5, "max_sessions": None},
        HIGH:     {"keyword_gap": 16.0, "page_gap": 6.0, "batch_gap": 9.0, "max_sessions": 1},
    },
    # Threads signs in with the same Instagram account, so a flag on one is a
    # flag on both: same numbers, same single session.
    "threads": {
        STANDARD: {"keyword_gap": 8.0, "page_gap": 2.5, "batch_gap": 3.5, "max_sessions": None},
        HIGH:     {"keyword_gap": 16.0, "page_gap": 6.0, "batch_gap": 9.0, "max_sessions": 1},
    },
}


def level_for(platform_id: str) -> str:
    """The configured level for a platform. STANDARD for a platform with no
    levels, so callers can ask without checking first."""
    if platform_id not in _TABLE:
        return STANDARD
    return getattr(settings, f"{platform_id}_stealth", HIGH)


def value(platform_id: str, name: str, default):
    """This platform's number for `name` at its current level, or `default`
    when the platform has no levels or the level leaves it unset."""
    row = _TABLE.get(platform_id, {}).get(level_for(platform_id))
    if not row:
        return default
    v = row.get(name)
    return default if v is None else v


def session_cap(platform_id: str, default_cap: int) -> int:
    """Accounts allowed at once: the runner's own cap, lowered (never raised)
    by the level. A level can only ask for MORE care."""
    cap = value(platform_id, "max_sessions", None)
    if cap is None:
        return default_cap
    return max(1, min(int(default_cap), int(cap)))


def jittered(median: float, sigma: float = 0.35, floor: float = 0.5) -> float:
    """A gap drawn around `median`: mostly near it, occasionally long, never
    below `floor * median`. The shape a person has, and not a fixed number --
    identical gaps between identical actions are the single most-flagged
    rhythm there is (see stealth/human.py)."""
    if median <= 0:
        return 0.0
    return max(median * floor, median * random.lognormvariate(0.0, sigma))
