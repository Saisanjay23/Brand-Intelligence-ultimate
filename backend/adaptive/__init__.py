"""Adaptive self-healing package for Brand Intelligence Suite.

Provides off-thread, bounded schema healing for analysis field extraction
when primary hard-coded keys encounter platform schema drift.
"""

from backend.adaptive.healer import (
    find_audience_count,
    find_display_name,
    find_field_bounded,
    find_post_timestamp,
)
from backend.adaptive.staging import staging_registry

__all__ = [
    "find_audience_count",
    "find_display_name",
    "find_post_timestamp",
    "find_field_bounded",
    "staging_registry",
]
