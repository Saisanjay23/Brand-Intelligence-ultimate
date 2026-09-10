"""Bounded fuzzy schema healer for extracted entity payloads.

Runs off the main asyncio event loop via asyncio.to_thread.
Enforces strict node limits, maximum depth, and cooperative time checks
so it cannot stall the single-worker asyncio server under any circumstance.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterator, Optional

# Rapidfuzz is a HARD requirement for this path to guarantee that string similarity
# scoring runs in compiled C++ and releases the Python GIL during heavy loops.
try:
    from rapidfuzz.fuzz import token_set_ratio as rapidfuzz_scorer
except ImportError as err:
    raise ImportError(
        "backend.adaptive requires rapidfuzz for off-thread GIL release: pip install rapidfuzz"
    ) from err

from backend.adaptive.validators import (
    parse_count_strict,
    parse_name_strict,
    parse_timestamp_strict,
)


def _iter_kv_bounded(
    obj: Any,
    max_depth: int = 5,
    current_depth: int = 0,
    path: str = "",
) -> Iterator[tuple[str, str, Any]]:
    """Recursively yields (key_name, full_path, value) within a strict depth bound."""
    if current_depth > max_depth:
        return

    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                continue
            curr_path = f"{path}.{k}" if path else k
            yield k, curr_path, v
            if isinstance(v, (dict, list)):
                yield from _iter_kv_bounded(v, max_depth, current_depth + 1, curr_path)

    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            if isinstance(item, (dict, list)):
                curr_path = f"{path}[{idx}]"
                yield from _iter_kv_bounded(item, max_depth, current_depth + 1, curr_path)


def find_field_bounded(
    payload: Any,
    target_tokens: set[str],
    exclude_tokens: set[str],
    validator: Callable[[Any], Any],
    *,
    max_depth: int = 5,
    node_budget: int = 300,
    time_budget_sec: float = 0.05,
    min_score: float = 75.0,
) -> tuple[Optional[Any], Optional[str], Optional[str]]:
    """Traverses payload within bounded limits and returns (validated_value, matched_key, path).

    Cooperative time check: checks time.monotonic() every 20 nodes to prevent runaway traversal.
    """
    if not payload:
        return None, None, None

    start_time = time.monotonic()
    nodes_visited = 0
    best_match: tuple[Any, str, str, float] | None = None

    # Normalise targets
    target_str = " ".join(target_tokens).lower()

    for key, path, val in _iter_kv_bounded(payload, max_depth=max_depth):
        nodes_visited += 1

        # 1. Cooperative node budget check
        if nodes_visited > node_budget:
            break

        # 2. Cooperative timeout check every 20 nodes
        if nodes_visited % 20 == 0 and (time.monotonic() - start_time) > time_budget_sec:
            break

        key_lower = key.lower()

        # 3. Reject if key contains an explicit exclude token (e.g. "friends", "following", "likes")
        if any(exc in key_lower for exc in exclude_tokens):
            continue

        # 4. Fast-path substring check, or compiled C++ rapidfuzz comparison
        has_sub = any(t in key_lower for t in target_tokens)
        score = 100.0 if has_sub else rapidfuzz_scorer(target_str, key_lower)

        if score >= min_score:
            validated = validator(val)
            if validated is not None:
                if best_match is None or score > best_match[3]:
                    best_match = (validated, key, path, score)
                    if score == 100.0:
                        # Perfect match found; bail early
                        break

    if best_match is not None:
        return best_match[0], best_match[1], best_match[2]

    return None, None, None


# Specialized convenience functions for specific entity fields


def find_audience_count(
    payload: Any,
    *,
    node_budget: int = 300,
    time_budget_sec: float = 0.05,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Heals audience/follower count with strict exclusion of friends, following, likes, and media."""
    target_tokens = {"follower", "followers", "subscriber", "subscribers", "fan", "audience"}
    exclude_tokens = {"friend", "friends", "following", "like", "likes", "photo", "video", "post", "media"}
    return find_field_bounded(
        payload,
        target_tokens=target_tokens,
        exclude_tokens=exclude_tokens,
        validator=parse_count_strict,
        node_budget=node_budget,
        time_budget_sec=time_budget_sec,
    )


def find_display_name(
    payload: Any,
    *,
    node_budget: int = 300,
    time_budget_sec: float = 0.05,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Heals display name, rejecting generic site labels."""
    target_tokens = {"name", "display_name", "full_name", "title"}
    exclude_tokens = {"user_id", "file", "icon", "badge", "url", "image"}
    return find_field_bounded(
        payload,
        target_tokens=target_tokens,
        exclude_tokens=exclude_tokens,
        validator=parse_name_strict,
        node_budget=node_budget,
        time_budget_sec=time_budget_sec,
    )


def find_post_timestamp(
    payload: Any,
    *,
    node_budget: int = 300,
    time_budget_sec: float = 0.05,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Heals post publish/creation timestamp."""
    target_tokens = {"publish_time", "creation_time", "created_at", "post_time", "published_at"}
    exclude_tokens = {"expire", "modified", "cached", "fetched", "session"}
    return find_field_bounded(
        payload,
        target_tokens=target_tokens,
        exclude_tokens=exclude_tokens,
        validator=parse_timestamp_strict,
        node_budget=node_budget,
        time_budget_sec=time_budget_sec,
    )
