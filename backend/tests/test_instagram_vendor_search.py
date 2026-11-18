"""Instagram discovery through HikerAPI, with no Instagram account touched.

Two things are pinned here, against fixture payloads only (no key exists and
no real endpoint is ever called -- the transport is replaced in every test):

  NEVER A SILENT ZERO. The one answer filed as "nothing there" is a
  well-formed first page with an empty `users` list. A refused key, an empty
  balance, a rate limit, a 5xx, a body that is not JSON, a body with no
  `users` list, users with no username -- each ends the sweep on a stop code
  that resolves BROKEN, with error text naming HikerAPI.

  ZERO ACCOUNT USE. With the vendor configured, the discovery runner claims
  no pooled session, starts no browser, asks no per-account ceiling and no
  breaker -- and with it NOT configured (no key), Instagram takes exactly the
  account path it always took.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from backend.discovery import runner as R
from backend.platforms import registry
from backend.platforms.instagram import vendor_search as V
from backend.platforms.instagram.discovery_engine import K_USERS, Discovery, Sweep
from backend.shared import resilience

KEY = "test-key-not-real"


# --------------------------------------------------------------------- fixtures


def _user(n: int, **over) -> dict:
    """One users[] node in the shape HikerAPI's docs list for
    /v2/fbsearch/accounts (the same flat node Instagram's mobile search
    returns)."""
    node = {
        "strong_id__": str(1000 + n), "pk": 1000 + n, "pk_id": str(1000 + n),
        "id": str(1000 + n), "username": f"brand_{n}", "full_name": f"Brand {n}",
        "is_private": False, "is_verified": False,
        "profile_pic_id": f"{n}_1",
        "profile_pic_url": f"https://scontent.cdninstagram.com/v/t51/{n}.jpg",
        "has_anonymous_profile_picture": False, "account_badges": [],
        "latest_reel_media": 0, "search_serp_type": 61,
    }
    node.update(over)
    return node


def _page(users: list, *, has_more: bool, token=None, **extra) -> tuple[int, str]:
    body = {"num_results": len(users), "users": users, "has_more": has_more,
            "rank_token": "rank-1", "clear_client_cache": False,
            "page_token": token, "status": "ok", **extra}
    return 200, json.dumps(body)


class FakeHttp:
    """Stands in for vendor_search._http_get: hands out queued (status, body)
    answers in order and records every request. An Exception in the queue is
    raised instead, as a transport failure would be."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url: str, headers: dict, timeout: float):
        self.calls.append((url, dict(headers)))
        if not self.answers:
            raise AssertionError(f"unexpected extra request: {url}")
        nxt = self.answers.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    def params(self, i: int) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.calls[i][0]).query).items()}


def _opts(**kw):
    base = dict(max_results=0, max_pages=0, max_seconds=0, timeout=45, cancel=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _vendor_on_and_fast(monkeypatch):
    """Vendor configured, and no real waiting: the request spacing and the
    429 backoff are scheduling, not what these tests are about."""
    monkeypatch.setattr(V, "MIN_REQUEST_INTERVAL_S", 0.0)
    monkeypatch.setattr(V, "RATE_LIMIT_BACKOFF_S", 0.0)
    monkeypatch.setattr(V, "_next_slot", 0.0)
    monkeypatch.setattr(R.settings, "instagram_search_provider", "hikerapi")
    monkeypatch.setattr(R.settings, "hikerapi_key", KEY)


async def _sweep(http: FakeHttp, keyword: str = "brand", **opts) -> Sweep:
    return await V.sweep(keyword, "people", _opts(**opts), fetch=http)


def _outcome(s: Sweep) -> str:
    return resilience.sweep_outcome(s.stopped, s.complete)


# ------------------------------------------------------- what a good answer is


class TestPaging:
    @pytest.mark.asyncio
    async def test_a_multi_page_result_is_followed_to_its_end(self):
        http = FakeHttp(
            _page([_user(1), _user(2)], has_more=True, token="tok+1/="),
            _page([_user(3)], has_more=False, token=None),
        )
        s = await _sweep(http)
        assert [u.username for u in s.users] == ["brand_1", "brand_2", "brand_3"]
        assert (s.stopped, s.complete, _outcome(s)) == ("exhausted", True, resilience.SATISFIED)
        assert s.source == "hikerapi" and s.error == ""
        assert len(http.calls) == 2

    @pytest.mark.asyncio
    async def test_it_asks_the_documented_endpoint_with_the_documented_header(self):
        http = FakeHttp(
            _page([_user(1)], has_more=True, token="tok+1/="),
            _page([_user(2)], has_more=False),
        )
        await _sweep(http, keyword="gulf cryo")
        url, headers = http.calls[0]
        assert url.startswith("https://api.hikerapi.com/v2/fbsearch/accounts?")
        assert headers["x-access-key"] == KEY
        assert http.params(0) == {"query": "gulf cryo"}
        # The next page sends the token back, intact (an opaque token with
        # `+`, `/` and `=` must survive the trip).
        assert http.params(1) == {"query": "gulf cryo", "page_token": "tok+1/="}
        assert "%2B" in http.calls[1][0] and KEY not in http.calls[1][0]

    @pytest.mark.asyncio
    async def test_a_page_adding_nobody_new_ends_the_sweep_satisfied(self):
        http = FakeHttp(
            _page([_user(1), _user(2)], has_more=True, token="t1"),
            _page([_user(1), _user(2)], has_more=True, token="t2"),
        )
        s = await _sweep(http)
        assert (s.stopped, s.complete, _outcome(s)) == ("no-new-results", True, resilience.SATISFIED)
        assert len(s.users) == 2
        assert len(http.calls) == 2   # no third paid request

    @pytest.mark.asyncio
    async def test_the_page_budget_running_out_is_truncated_not_done(self):
        http = FakeHttp(
            _page([_user(1)], has_more=True, token="t1"),
            _page([_user(2)], has_more=True, token="t2"),
        )
        s = await _sweep(http, max_pages=2)
        assert (s.stopped, s.complete, _outcome(s)) == ("cap:pages", False, resilience.TRUNCATED)

    @pytest.mark.asyncio
    async def test_a_cursor_that_cannot_advance_is_not_re_sent(self):
        http = FakeHttp(
            _page([_user(1)], has_more=True, token="same"),
            _page([_user(2)], has_more=True, token="same"),
        )
        s = await _sweep(http)
        assert s.stopped == "cap:pages" and not s.complete
        assert len(http.calls) == 2

    @pytest.mark.asyncio
    async def test_the_result_cap_stops_at_once(self):
        http = FakeHttp(_page([_user(1), _user(2), _user(3)], has_more=True, token="t1"))
        s = await _sweep(http, max_results=2)
        assert s.stopped == "cap:results" and _outcome(s) == resilience.SATISFIED
        assert len(s.hits) == 2 and len(http.calls) == 1

    @pytest.mark.asyncio
    async def test_a_stop_is_honoured_before_any_request(self):
        http = FakeHttp()
        s = await _sweep(http, cancel=True)
        assert s.stopped == "cancelled" and http.calls == []

    @pytest.mark.asyncio
    async def test_results_stream_to_the_runner_as_rows(self):
        got: list = []
        http = FakeHttp(
            _page([_user(1)], has_more=True, token="t1"),
            _page([_user(2)], has_more=False),
        )
        await V.sweep("brand", "people", _opts(),
                      on_progress=lambda n, p, rows: got.extend(rows), fetch=http)
        assert [r.username for r in got] == ["brand_1", "brand_2"]


class TestEmpty:
    @pytest.mark.asyncio
    async def test_an_empty_first_page_is_the_one_clean_zero(self):
        s = await _sweep(FakeHttp(_page([], has_more=False)))
        assert (s.stopped, s.complete, _outcome(s)) == ("no-results", True, resilience.SATISFIED)
        assert s.hits == [] and s.error == ""


class TestRowsAndUsers:
    @pytest.mark.asyncio
    async def test_fields_reach_the_user_and_the_row(self):
        http = FakeHttp(_page([
            _user(1, full_name="Gulf Cryo Official", is_verified=True),
            _user(2, is_private=True, has_anonymous_profile_picture=True),
        ], has_more=False))
        s = await _sweep(http)
        a, b = s.users
        assert (a.username, a.full_name, a.verified, a.private) == (
            "brand_1", "Gulf Cryo Official", True, False)
        assert a.avatar == "https://scontent.cdninstagram.com/v/t51/1.jpg"
        assert a.entity_id == "1001" and a.has_custom_pic is True
        assert (b.private, b.anonymous_pic, b.has_custom_pic) == (True, True, False)

        row = s.hits[0]
        assert row.url == "https://www.instagram.com/brand_1/"
        assert (row.username, row.profile_name, row.verified) == (
            "brand_1", "Gulf Cryo Official", True)
        assert row.profile_pic_url == a.avatar and row.has_custom_pic is True
        # Provenance: every field the search filled says where it came from.
        assert row.src["profile_name"] == "discovery-search:hikerapi"
        assert s.hits[1].src["logo"] == "api-anonymous-flag"

    @pytest.mark.asyncio
    async def test_a_wrapped_user_node_is_unwrapped_not_dropped(self):
        s = await _sweep(FakeHttp(_page([{"user": _user(1)}], has_more=False)))
        assert [u.username for u in s.users] == ["brand_1"]


# ------------------------------------------------- what is NEVER a silent zero


class TestNeverASilentZero:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_a_refused_key_says_so(self, status):
        http = FakeHttp((status, json.dumps({"detail": "Invalid access key"})))
        s = await _sweep(http)
        assert s.stopped == "provider-auth" and _outcome(s) == resilience.BROKEN
        assert "HikerAPI key rejected" in s.error and str(status) in s.error
        assert "Invalid access key" in s.error and KEY not in s.error

    @pytest.mark.asyncio
    async def test_no_key_at_sweep_time_is_refused_without_a_request(self, monkeypatch):
        monkeypatch.setattr(R.settings, "hikerapi_key", "")
        http = FakeHttp()
        s = await _sweep(http)
        assert s.stopped == "provider-auth" and "HIKERAPI_KEY" in s.error
        assert http.calls == []

    @pytest.mark.asyncio
    async def test_an_empty_balance_is_quota(self):
        s = await _sweep(FakeHttp((402, '{"detail": "Insufficient balance"}')))
        assert s.stopped == "quota" and _outcome(s) == resilience.BROKEN
        assert "HikerAPI balance exhausted" in s.error

    @pytest.mark.asyncio
    async def test_a_rate_limit_is_retried_then_reported(self):
        http = FakeHttp(*[(429, "Too Many Requests")] * (V.RATE_LIMIT_RETRIES + 1))
        s = await _sweep(http)
        assert s.stopped == "rate_limited" and _outcome(s) == resilience.BROKEN
        assert "HikerAPI rate limit" in s.error
        assert len(http.calls) == V.RATE_LIMIT_RETRIES + 1

    @pytest.mark.asyncio
    async def test_one_rate_limit_then_an_answer_recovers(self):
        http = FakeHttp((429, ""), _page([_user(1)], has_more=False))
        s = await _sweep(http)
        assert s.stopped == "exhausted" and len(s.users) == 1 and s.error == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer, why", [
        ((200, "<html>Attention Required</html>"), "not JSON"),
        ((200, json.dumps(["not", "an", "object"])), "not an object"),
        ((200, json.dumps({"status": "ok", "has_more": False})), "without a `users` list"),
        ((200, json.dumps({"status": "fail", "message": "challenge_required"})), "status 'fail'"),
        ((200, json.dumps({"users": [{"pk": 1, "full_name": "x"}], "has_more": False})),
         "none carried a username"),
        ((500, "Internal Server Error"), "HTTP 500"),
        ((404, '{"detail": "Not found"}'), "HTTP 404"),
        ((410, ""), "retired"),
        (ConnectionResetError("reset by peer"), "request failed"),
    ])
    async def test_anything_that_is_not_a_usable_answer_is_broken(self, answer, why):
        s = await _sweep(FakeHttp(answer))
        assert s.stopped == "provider-error", s.error
        assert _outcome(s) == resilience.BROKEN and not s.complete
        assert "HikerAPI" in s.error and why in s.error
        assert s.hits == []

    @pytest.mark.asyncio
    async def test_a_missing_users_list_is_tallied_as_a_rename(self):
        s = await _sweep(FakeHttp((200, json.dumps({"status": "ok"}))))
        assert s.schema[K_USERS] == [0, 1]

    @pytest.mark.asyncio
    async def test_a_failure_on_a_later_page_keeps_what_was_found(self):
        http = FakeHttp(_page([_user(1), _user(2)], has_more=True, token="t1"), (503, "busy"))
        s = await _sweep(http)
        assert s.stopped == "provider-error" and not s.complete
        assert [r.username for r in s.hits] == ["brand_1", "brand_2"]

    def test_the_vendor_stop_codes_are_broken_and_have_words(self):
        for code in resilience.PROVIDER_STOPS:
            assert resilience.sweep_outcome(code, False) == resilience.BROKEN
            assert "vendor" in resilience.describe_stop(code)
        assert resilience.PROVIDER_FATAL_STOPS >= {"provider-auth", "quota", "rate_limited"}
        assert "provider-error" not in resilience.PROVIDER_FATAL_STOPS


# --------------------------------------------------------- when is it on at all


class TestSwitch:
    def test_on_only_with_the_provider_and_a_key(self, monkeypatch):
        assert V.active_provider() == "hikerapi"
        assert registry.get("instagram").discovery_vendor() == "hikerapi"
        monkeypatch.setattr(R.settings, "hikerapi_key", "  ")
        assert V.active_provider() == ""
        monkeypatch.setattr(R.settings, "hikerapi_key", KEY)
        monkeypatch.setattr(R.settings, "instagram_search_provider", "account")
        assert V.active_provider() == ""

    def test_no_other_platform_has_a_vendor(self):
        for pid, plat in registry.PLATFORMS.items():
            if pid != "instagram":
                assert plat.discovery_vendor() == "", pid

    def test_the_shipped_default_is_the_account_path(self):
        from backend.config.settings import Settings

        d = Settings(_env_file=None)
        assert (d.instagram_search_provider, d.hikerapi_key) == ("account", "")

    @pytest.mark.asyncio
    async def test_the_engine_routes_to_the_vendor_only_when_it_is_on(self, monkeypatch):
        http = FakeHttp(_page([_user(1)], has_more=False))
        monkeypatch.setattr(V, "_http_get", http)
        s = await Discovery(_opts(), None).sweep("brand")
        assert s.source == "hikerapi" and len(http.calls) == 1

        monkeypatch.setattr(R.settings, "hikerapi_key", "")
        assert Discovery(_opts(), None).vendor is False


# ------------------------------------------------- the runner: zero account use


def _job(keywords: list[str], platform: str = "instagram") -> R.DiscoveryJob:
    plan = [
        R.kw_groups.KeywordPlan(
            search=kw, kw_type="individual",
            targets=(R.kw_groups.MatchTarget(parent=kw, terms=(kw,)),),
        )
        for kw in keywords
    ]
    job = R.DiscoveryJob(id="job-v", group_id="client-v", keyword_plan=plan)
    job.platforms[platform] = R.PlatformSweep(
        platform=platform, display_name="Instagram", keywords_total=len(plan))
    return job


def _wire_runner(monkeypatch, http: FakeHttp) -> dict:
    """The runner with everything account-shaped booby-trapped: any session
    claim, browser, budget, breaker or session verdict is recorded, and the
    tests assert none happened. Storage is faked as in the other runner
    tests; the vendor's transport is `http`."""
    touched: dict[str, list] = {"claimed": [], "browser": [], "budget": [],
                                "breaker": [], "marked": [], "missed": [], "recorded": []}

    async def _session_for_job(platform_id, *, wait_s=0.0):
        touched["claimed"].append(platform_id)
        raise R.sessions_engine.ConflictError(f"{platform_id}: no healthy sessions")

    def _session_cls(self):
        touched["browser"].append(self.id)
        raise AssertionError("a browser session was built")

    async def _budget(*a, **kw):
        touched["budget"].append(a[0])
        return True

    async def _breaker(platform_id, *a, **kw):
        touched["breaker"].append(platform_id)

    async def _mark_failed(platform_id, session_id, *a, **kw):
        touched["marked"].append((platform_id, session_id))

    async def _save_many(client_id, plat_id, phase, rows):
        return len(rows), len(rows)

    async def _plan(*a, **kw):
        return True

    async def _record(group_id, job_id, platform_id, tab, kw_type, keyword, outcome, **kw):
        touched["recorded"].append((keyword, outcome, kw.get("stop")))

    async def _miss(group_id, job_id, platform_id, tabs, items, reason):
        touched["missed"].extend((it.keyword, reason) for it in items)

    monkeypatch.setattr(V, "_http_get", http)
    monkeypatch.setattr(R.sessions_engine, "session_for_job", _session_for_job)
    monkeypatch.setattr(R.sessions_engine, "mark_session_failed", _mark_failed)
    monkeypatch.setattr(registry.Platform, "session_cls", _session_cls)
    monkeypatch.setattr(R.account_budget, "hold", _budget)
    monkeypatch.setattr(R, "_hold_for_breaker", _breaker)
    monkeypatch.setattr(R.profiles_db, "save_many", _save_many)
    monkeypatch.setattr(R.avatar_cache, "spawn", lambda *a, **kw: None)
    monkeypatch.setattr(R.coverage_db, "plan", _plan)
    monkeypatch.setattr(R.coverage_db, "record", _record)
    monkeypatch.setattr(R.coverage_db, "miss", _miss)
    monkeypatch.setitem(R._PLATFORM_INTER_KEYWORD_DELAY, "instagram", 0.0)
    monkeypatch.setattr(R, "_RETRY_BACKOFF_S", 0.0)
    return touched


async def _run(job: R.DiscoveryJob, platform: str = "instagram") -> None:
    await R.DiscoveryRunner()._sweep_platform(
        job, platform, max_results=0, max_seconds=None,
        platform_limits_individual={}, platform_limits_domain={}, platform_tab_limits={},
    )


class TestRunnerUsesNoAccount:
    @pytest.mark.asyncio
    async def test_a_vendor_sweep_claims_nothing_and_starts_nothing(self, monkeypatch):
        http = FakeHttp(
            _page([_user(1), _user(2)], has_more=True, token="t1"),
            _page([_user(3)], has_more=False),
            _page([_user(4)], has_more=False),
        )
        touched = _wire_runner(monkeypatch, http)
        job = _job(["gulf cryo", "gulfcryo"])

        await _run(job)

        prog = job.platforms["instagram"]
        assert prog.status == "done", prog.note
        assert prog.keywords_done == 2 and prog.found == 4
        assert [h.source for h in job.history] == ["hikerapi", "hikerapi"]
        assert {c[1]["x-access-key"] for c in http.calls} == {KEY}
        assert touched["claimed"] == [] and touched["browser"] == []
        assert touched["budget"] == [] and touched["breaker"] == []
        assert touched["marked"] == []

    @pytest.mark.asyncio
    async def test_a_rejected_key_stops_the_platform_and_owes_the_rest(self, monkeypatch):
        http = FakeHttp((401, '{"detail": "Invalid access key"}'))
        touched = _wire_runner(monkeypatch, http)
        job = _job(["kw1", "kw2", "kw3"])

        await _run(job)

        prog = job.platforms["instagram"]
        # One request, not one per keyword: the same key would be refused
        # for every one of them.
        assert len(http.calls) == 1
        # `partial`, the runner's word for "one keyword reached a verdict,
        # the rest are owed" -- never `done`.
        assert prog.status == "partial"
        assert "HikerAPI key rejected" in prog.note
        assert touched["recorded"] == [("kw1", resilience.BROKEN, "provider-auth")]
        assert [k for k, _ in touched["missed"]] == ["kw2", "kw3"]
        assert all("HikerAPI key rejected" in why for _, why in touched["missed"])
        # Nothing account-shaped was blamed or touched on the way.
        assert touched["marked"] == [] and touched["claimed"] == []

    @pytest.mark.asyncio
    async def test_a_dead_session_pool_does_not_skip_a_vendor_platform(self, monkeypatch):
        async def _missing(plat):
            return "missing"

        monkeypatch.setattr(registry, "session_state", _missing)
        ready, skipped = await R.DiscoveryRunner().platform_readiness(["instagram", "threads"])
        assert ready == ["instagram"]
        # Threads shares Instagram's ACCOUNT, not its vendor: unaffected.
        assert skipped == {"threads": "session missing"}

    @pytest.mark.asyncio
    async def test_without_a_key_instagram_takes_the_account_path(self, monkeypatch):
        monkeypatch.setattr(R.settings, "hikerapi_key", "")
        http = FakeHttp()
        touched = _wire_runner(monkeypatch, http)
        job = _job(["kw1"])

        await _run(job)

        # The pool was asked, exactly as before this feature existed; the
        # vendor never was.
        assert touched["claimed"] == ["instagram"]
        assert http.calls == []
        assert job.platforms["instagram"].status == "failed"

    @pytest.mark.asyncio
    async def test_without_a_key_readiness_reads_the_pool(self, monkeypatch):
        monkeypatch.setattr(R.settings, "hikerapi_key", "")

        async def _missing(plat):
            return "missing"

        monkeypatch.setattr(registry, "session_state", _missing)
        ready, skipped = await R.DiscoveryRunner().platform_readiness(["instagram"])
        assert ready == [] and skipped == {"instagram": "session missing"}
