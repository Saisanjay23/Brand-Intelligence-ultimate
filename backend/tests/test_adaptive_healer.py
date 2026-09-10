"""Tests for backend.adaptive self-healing schema recovery."""

from backend.adaptive.validators import (
    parse_count_strict,
    parse_name_strict,
    parse_timestamp_strict,
)
from backend.adaptive.healer import (
    find_audience_count,
    find_display_name,
    find_post_timestamp,
)
from backend.adaptive.staging import CandidateStagingRegistry, CONFIRMATION_THRESHOLD


def test_validators_count():
    assert parse_count_strict(1500) == 1500
    assert parse_count_strict("1.5M") == 1_500_000
    assert parse_count_strict("250K") == 250_000
    assert parse_count_strict(0) == 0
    assert parse_count_strict(-5) is None
    assert parse_count_strict(10_000_000_000) is None  # Exceeds max
    assert parse_count_strict("invalid") is None
    assert parse_count_strict(True) is None  # bool rejected


def test_validators_name():
    assert parse_name_strict("Gautam Adani") == "Gautam Adani"
    assert parse_name_strict("Facebook") is None  # Generic chrome
    assert parse_name_strict("Login") is None
    assert parse_name_strict("  ") is None
    assert parse_name_strict("A") is None  # Too short


def test_validators_timestamp():
    valid_epoch = 1720000000
    assert parse_timestamp_strict(valid_epoch) == valid_epoch
    # Millisecond timestamp
    assert parse_timestamp_strict(valid_epoch * 1000) == valid_epoch
    # Unreasonable timestamps
    assert parse_timestamp_strict(100) is None  # 1970
    assert parse_timestamp_strict(99999999999999) is None  # Far future


def test_healer_audience_count():
    # Mutated payload where follower_count was renamed to "profile_subscribers_v2"
    payload = {
        "user": {
            "id": "10001",
            "relationship_info": {
                "profile_subscribers_v2": 85400,
                "friends_count": 12,  # Should be excluded by exclude_tokens
                "following_count": 90,  # Excluded
            },
            "media_metrics": {
                "photo_count": 45,  # Excluded
            }
        }
    }
    val, key, path = find_audience_count(payload)
    assert val == 85400
    assert key == "profile_subscribers_v2"
    assert "relationship_info.profile_subscribers_v2" in path


def test_healer_display_name():
    # Mutated payload where name was renamed to "full_display_title"
    payload = {
        "viewer": {},
        "target_profile": {
            "metadata": {
                "full_display_title": "Adani Group Official",
                "user_id": 12345,
            }
        }
    }
    val, key, path = find_display_name(payload)
    assert val == "Adani Group Official"
    assert key == "full_display_title"


def test_healer_post_timestamp():
    payload = {
        "item": {
            "post_id": "999",
            "timeline_published_at": 1725000000,
            "cached_at": 1725000500,  # Excluded by exclude_tokens
        }
    }
    val, key, path = find_post_timestamp(payload)
    assert val == 1725000000
    assert key == "timeline_published_at"


def test_candidate_staging_registry():
    registry = CandidateStagingRegistry()
    
    # 1st match
    cand = registry.record_match("facebook", "followers", "followers_v2", "user.followers_v2", 1500)
    assert cand.occurrences == 1
    assert cand.status == "pending_review"
    
    # Repeat up to threshold
    for i in range(2, CONFIRMATION_THRESHOLD + 1):
        cand = registry.record_match("facebook", "followers", "followers_v2", "user.followers_v2", 1500 + i)
        
    assert cand.occurrences == CONFIRMATION_THRESHOLD
    assert cand.status == "pending_review"  # Remains gated for human review
    assert len(cand.sample_values) > 1
