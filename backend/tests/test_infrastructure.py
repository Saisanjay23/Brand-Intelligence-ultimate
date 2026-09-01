"""Failure classification, job eviction, cap resolution, proxy safety.

Every test here corresponds to a defect that actually shipped:

  * classify_failure checked rate-limit BEFORE auth, contradicting its own
    docstring, so a 403 carrying the word "quota" was cooled off for minutes
    instead of being reported as a dead session.
  * JobStore evicted down to exactly max_jobs and THEN inserted, leaving the
    table one over its ceiling until the next put().
  * socks_auth_warning guards the one proxy shape that fails DANGEROUSLY --
    Chromium drops SOCKS credentials and silently goes direct, so traffic
    leaves on the real IP while the UI shows a proxy attached.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import pytest

from backend.discovery.runner import _effective_cap, _resolve_cap
from backend.shared.job_store import JobStore
from backend.shared.resilience import classify_failure, is_transient
from backend.stealth.proxy import build_proxy_config, socks_auth_warning


# ------------------------------------------------------- classify_failure

class TestClassifyFailure:
    """Documented precedence: checkpoint beats auth beats rate-limit."""

    def test_checkpoint_wins_over_everything(self):
        assert classify_failure("checkpoint required, 429 rate limit") == "checkpointed"

    def test_auth_beats_rate_limit(self):
        # REGRESSION: this returned "rate_limited" before the fix, because
        # the rate-limit branch was tested first. A 403 is a dead session --
        # cooling it off for 15 minutes just delays discovering that.
        assert classify_failure("403 forbidden -- quota") == "expired"

    def test_plain_rate_limit_still_classifies(self):
        assert classify_failure("429 too many requests") == "rate_limited"
        assert classify_failure("FloodWait 300") == "rate_limited"

    def test_non_session_failures_leave_the_pool_alone(self):
        # a parser bug or a bad URL must NOT get a healthy session cooled off
        assert classify_failure("KeyError: 'edges'") is None
        assert classify_failure("could not read a channel reference") is None

    def test_accepts_an_exception_not_just_a_string(self):
        assert classify_failure(RuntimeError("session checkpointed")) == "checkpointed"

    def test_transient_is_separate_from_session_health(self):
        assert is_transient("net::ERR_TIMED_OUT") is True
        # a network blip is not a reason to burn the session
        assert classify_failure("net::ERR_TIMED_OUT") is None


# -------------------------------------------------------------- JobStore

@dataclass
class _Job:
    """Minimal TrackedJob: id, created_at, status, cancel."""
    id: str
    created_at: float
    status: str = "running"
    cancel: asyncio.Event = field(default_factory=asyncio.Event)


def _job(job_id: str, age_seconds: float = 0.0) -> _Job:
    """A job `age_seconds` old, measured against the real clock.

    JobStore computes age as `time.time() - created_at`, so a literal
    `created_at=1.0` is not "the first job" -- it is 1970, i.e. instantly
    past any TTL. Writing the fixtures that way made two tests here fail
    against perfectly correct code; the helper exists so that mistake
    cannot be made per-test.
    """
    return _Job(id=job_id, created_at=time.time() - age_seconds)


class TestJobStore:
    @pytest.mark.asyncio
    async def test_never_exceeds_max_jobs_even_immediately_after_put(self):
        # REGRESSION: eviction ran BEFORE insertion without reserving room,
        # so the table sat at max_jobs+1 until the next put().
        store: JobStore[_Job] = JobStore(
            max_jobs=3, ttl_seconds=9999, terminal_statuses=frozenset({"done"}))
        for i in range(10):
            # newest last, all comfortably inside the TTL so this exercises
            # PRESSURE eviction rather than expiry
            await store.put(_job(str(i), age_seconds=100 - i))
            stats = await store.stats()
            assert stats["jobs"] <= 3, f"exceeded ceiling after put #{i}: {stats['jobs']}"

    @pytest.mark.asyncio
    async def test_evicts_oldest_first(self):
        store: JobStore[_Job] = JobStore(
            max_jobs=2, ttl_seconds=9999, terminal_statuses=frozenset({"done"}))
        await store.put(_job("old", age_seconds=30))
        await store.put(_job("mid", age_seconds=20))
        await store.put(_job("new", age_seconds=10))
        assert await store.get("old") is None, "oldest should have been evicted"
        assert await store.get("new") is not None

    @pytest.mark.asyncio
    async def test_expired_job_reads_as_absent(self):
        store: JobStore[_Job] = JobStore(
            max_jobs=10, ttl_seconds=0.01, terminal_statuses=frozenset({"done"}))
        await store.put(_job("x", age_seconds=5))
        assert await store.get("x") is None, "a job past its TTL must read as gone"

    @pytest.mark.asyncio
    async def test_cancel_is_idempotent_and_never_raises(self):
        store: JobStore[_Job] = JobStore(
            max_jobs=10, ttl_seconds=9999, terminal_statuses=frozenset({"done"}))
        job = _job("j")
        await store.put(job)
        assert await store.cancel("j") is True
        assert job.cancel.is_set()
        assert await store.cancel("nope") is False       # unknown id
        job.status = "done"
        assert await store.cancel("j") is False          # already terminal

    @pytest.mark.asyncio
    async def test_session_hold_released_by_key(self):
        store: JobStore[_Job] = JobStore(
            max_jobs=10, ttl_seconds=9999, terminal_statuses=frozenset({"done"}))
        key = store.hold_session("facebook", "abc")
        assert store.holds_session("facebook", "abc")
        store.release_session(key)
        assert not store.holds_session("facebook", "abc")
        # a keyless platform (API/MTProto) has nothing to track
        assert store.hold_session("youtube", "") is None
        store.release_session(None)   # must not raise


# ------------------------------------------------------------ cap resolution

class TestCapResolution:
    def test_most_restrictive_positive_cap_wins(self):
        assert _effective_cap(100, 20, 50) == 20

    def test_zero_means_unset_not_unlimited_zero(self):
        # a 0 must never win by being the smallest number
        assert _effective_cap(0, 50, 0) == 50
        assert _effective_cap(0, 0, 0) == 0     # nothing configured -> uncapped

    def test_tab_cap_overrides_broader_caps(self):
        cap = _resolve_cap(
            "facebook", "groups", "individual", 100,
            platform_limits={"individual": {"facebook": 60}},
            platform_tab_limits={"facebook": {"groups": {"individual": 5}}},
        )
        assert cap == 5

    def test_falls_back_through_the_stack(self):
        # no tab cap -> per-type cap; no per-type cap -> blanket max_results
        assert _resolve_cap("twitter", "people", "individual", 100,
                            {"individual": {"twitter": 30}}, {}) == 30
        assert _resolve_cap("twitter", "people", "individual", 100, {}, {}) == 100
        assert _resolve_cap("twitter", "people", "individual", 0, {}, {}) == 0


# --------------------------------------------------------- proxy safety

class TestProxySafety:
    def test_socks_with_credentials_is_flagged(self):
        # Chromium cannot authenticate to SOCKS: it drops the credentials and
        # falls back to a DIRECT connection, exposing the real IP.
        warn = socks_auth_warning({"server": "socks5://h:1080", "username": "u", "password": "p"})
        assert warn and "DIRECT" in warn

    def test_socks_without_credentials_is_fine(self):
        assert socks_auth_warning({"server": "socks5://h:1080"}) is None

    def test_http_with_credentials_is_fine(self):
        assert socks_auth_warning(
            {"server": "http://h:8080", "username": "u", "password": "p"}) is None

    def test_no_proxy_is_not_a_warning(self):
        assert socks_auth_warning(None) is None

    def test_build_config_shape_and_omissions(self):
        assert build_proxy_config(None) is None
        assert build_proxy_config({}) is None
        assert build_proxy_config({"server": "http://h:8080"}) == {"server": "http://h:8080"}
        cfg = build_proxy_config(
            {"server": "http://h:8080", "username": "u", "password": "p", "timezone_id": "UTC"})
        # timezone_id is context config, not proxy config -- must not leak through
        assert cfg == {"server": "http://h:8080", "username": "u", "password": "p"}
