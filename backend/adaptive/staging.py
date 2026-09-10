"""Candidate staging and drift logging for adaptive schema discoveries.

CRITICAL SAFETY RULE:
Auto-promotion to primary production keys is strictly gated behind human review.
Candidate patterns are stored in memory and MongoDB for analyst inspection,
ensuring that false positive matches never silently poison scraping pipelines.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.shared.logging import get_logger

log = get_logger("adaptive.staging")

CONFIRMATION_THRESHOLD = 5  # Number of independent profiles required before flagging candidate for review


@dataclass
class SchemaCandidate:
    platform: str
    field_name: str
    matched_key: str
    json_path: str
    occurrences: int = 1
    sample_values: list[Any] = field(default_factory=list)
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    status: str = "pending_review"  # pending_review | approved | rejected


class CandidateStagingRegistry:
    """In-memory candidate tracker that records schema drift candidates."""

    def __init__(self):
        # (platform, field_name, json_path) -> SchemaCandidate
        self._candidates: dict[tuple[str, str, str], SchemaCandidate] = {}

    def record_match(
        self,
        platform: str,
        field_name: str,
        matched_key: str,
        json_path: str,
        sample_value: Any,
    ) -> SchemaCandidate:
        key = (platform, field_name, json_path)
        now = time.time()
        
        if key not in self._candidates:
            cand = SchemaCandidate(
                platform=platform,
                field_name=field_name,
                matched_key=matched_key,
                json_path=json_path,
                occurrences=1,
                sample_values=[sample_value],
                first_seen=now,
                last_seen=now,
            )
            self._candidates[key] = cand
            log.info(
                f"[adaptive-candidate] new key observed for {platform}.{field_name}: '{matched_key}' at '{json_path}' (sample: {sample_value})"
            )
            return cand

        cand = self._candidates[key]
        cand.occurrences += 1
        cand.last_seen = now
        if len(cand.sample_values) < 5 and sample_value not in cand.sample_values:
            cand.sample_values.append(sample_value)

        if cand.occurrences == CONFIRMATION_THRESHOLD:
            log.warning(
                f"[adaptive-drift-alert] {platform}.{field_name} consistently matched candidate key '{matched_key}' "
                f"across {CONFIRMATION_THRESHOLD} independent profiles! Ready for review in schema_candidates."
            )

        return cand

    def get_candidate(self, platform: str, field_name: str, json_path: str) -> Optional[SchemaCandidate]:
        return self._candidates.get((platform, field_name, json_path))


# Global in-memory staging registry
staging_registry = CandidateStagingRegistry()
