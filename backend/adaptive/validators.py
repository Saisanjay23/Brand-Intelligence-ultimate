"""Strict sanity validators for adaptive field extraction.

Ensures candidate values found by fuzzy schema matching conform to realistic,
platform-agnostic domain constraints before they can be considered valid.
"""

from __future__ import annotations

from typing import Any, Optional

MAX_AUDIENCE = 5_000_000_000
MIN_EPOCH = 1075593600  # 2004-02-01 (Facebook launch era)
MAX_FUTURE_EPOCH_DELTA = 86400 * 2  # At most 2 days in the future (clock skew)

GENERIC_NAMES = {
    "facebook", "login", "notifications", "home", "search",
    "instagram", "twitter", "x", "telegram", "tiktok", "youtube",
    "profile", "feed", "user", "settings", "help", "about",
}


def parse_count_strict(val: Any, max_val: int = MAX_AUDIENCE) -> Optional[int]:
    """Validates and parses integer counts (followers, subscribers, friends).

    Rejects negative numbers, zero if zero is not allowed, or numbers exceeding
    sane social media audience limits (5 Billion).
    """
    if val is None or isinstance(val, bool):
        return None

    if isinstance(val, int):
        return val if 0 <= val <= max_val else None

    if isinstance(val, float):
        if val.is_integer() and 0 <= val <= max_val:
            return int(val)
        return None

    if isinstance(val, str):
        cleaned = val.strip().replace(",", "")
        if not cleaned:
            return None
        # Handle "1.5M", "200K", "10B"
        multipliers = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
        last_char = cleaned[-1].lower()
        if last_char in multipliers:
            try:
                num = float(cleaned[:-1])
                res = int(num * multipliers[last_char])
                return res if 0 <= res <= max_val else None
            except ValueError:
                return None
        if cleaned.isdigit():
            num = int(cleaned)
            return num if 0 <= num <= max_val else None

    return None


def parse_name_strict(val: Any) -> Optional[str]:
    """Validates that a candidate name is a non-empty human/brand string and not site chrome."""
    if not val or not isinstance(val, str):
        return None
    cleaned = val.strip()
    if len(cleaned) < 2 or len(cleaned) > 100:
        return None
    if cleaned.lower() in GENERIC_NAMES:
        return None
    # Must contain at least one letter or digit (not just punctuation)
    if not any(c.isalnum() for c in cleaned):
        return None
    return cleaned


def parse_timestamp_strict(val: Any, current_time: Optional[float] = None) -> Optional[int]:
    """Validates epoch timestamps, converting millisecond epochs if necessary."""
    if val is None or isinstance(val, bool):
        return None

    import time
    now = current_time or time.time()
    max_epoch = now + MAX_FUTURE_EPOCH_DELTA

    if isinstance(val, (int, float)):
        int_val = int(val)
        # Check if millisecond timestamp (13 digits)
        if int_val > 100_000_000_000:
            int_val //= 1000
        if MIN_EPOCH <= int_val <= max_epoch:
            return int_val
        return None

    if isinstance(val, str) and val.strip().isdigit():
        return parse_timestamp_strict(int(val.strip()), now)

    return None
