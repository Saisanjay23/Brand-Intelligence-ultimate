"""The three paths that now go through the curl_cffi accelerator, and the
promise each of them keeps when it does not.

Every one of these integrations is meant to be STRICTLY ADDITIVE: on its
good day it removes a cost, and on every other day the caller does exactly
what it did before `shared/fast_http.py` existed. So these tests spend most
of their assertions on the bad days --

    imagefetch      the SSRF allowlist is still re-checked on every redirect
                    hop, whichever client made the request; a transport
                    failure falls back to aiohttp; an HTTP STATUS does not,
                    because a status is an answer and asking the weaker
                    client again would just be a second request

    session checks  a fast "alive" ends the check, a fast "dead" does not --
                    only the browser is allowed to quarantine an account

    analysis        a pre-flight that proves nothing costs the batch nothing,
                    and a URL it settles is still a saved, counted row

Nothing here opens a browser, touches Mongo or makes a request.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.shared import fast_http


# ------------------------------------------------------------- imagefetch


def _fake_fast_response(status: int, headers: dict | None = None, body: bytes = b"") -> fast_http.FastResponse:
    return fast_http.FastResponse(
        status=status,
        headers={k.lower(): v for k, v in (headers or {}).items()},
        body=body,
        url="https://scontent.xx.fbcdn.net/p.jpg",
    )


@pytest.mark.asyncio
async def test_avatars_go_out_through_the_impersonated_client_first():
    """The 403 this whole integration exists for is decided on the TLS
    handshake, so the impersonated client has to be the one that asks."""
    from backend.shared.imagefetch import fetch_image

    fast = AsyncMock(return_value=_fake_fast_response(
        200, {"Content-Type": "image/jpeg"}, b"\xff\xd8jpegbytes"))
    aio = AsyncMock()
    with patch.object(fast_http, "fetch", fast), \
         patch.object(fast_http, "available", return_value=True), \
         patch("backend.shared.imagefetch._hop_aiohttp", aio):
        img = await fetch_image("https://scontent.xx.fbcdn.net/p.jpg")

    assert img.data == b"\xff\xd8jpegbytes"
    assert img.content_type == "image/jpeg"
    aio.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_answer_from_the_fast_client_falls_back_to_aiohttp():
    """curl_cffi ships compiled binaries and this path has been fetching
    avatars in production since before it arrived. A DNS/TLS/timeout
    failure must not be the end of the picture."""
    from backend.shared.imagefetch import FetchedImage, _Hop, fetch_image

    fast = AsyncMock(side_effect=fast_http.FastHttpTransportError("DNSError"))
    aio = AsyncMock(return_value=_Hop(200, "", "image/png", b"pngbytes"))
    with patch.object(fast_http, "fetch", fast), \
         patch.object(fast_http, "available", return_value=True), \
         patch("backend.shared.imagefetch._hop_aiohttp", aio):
        img = await fetch_image("https://pbs.twimg.com/p.png")

    assert isinstance(img, FetchedImage)
    assert img.data == b"pngbytes"
    aio.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_upstream_status_is_never_retried_on_the_weaker_client():
    """A 403 is a verdict the CDN already gave. Asking aiohttp -- which is
    the client the CDN refuses hardest -- would be a second request for the
    same answer."""
    from backend.shared.imagefetch import ImageFetchError, fetch_image

    fast = AsyncMock(return_value=_fake_fast_response(403))
    aio = AsyncMock()
    with patch.object(fast_http, "fetch", fast), \
         patch.object(fast_http, "available", return_value=True), \
         patch("backend.shared.imagefetch._hop_aiohttp", aio):
        with pytest.raises(ImageFetchError):
            await fetch_image("https://scontent.xx.fbcdn.net/p.jpg")
    aio.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_allowlist_is_re_checked_on_every_redirect_hop():
    """THE SSRF CONTROL. Redirects are followed by hand precisely so each
    target is re-validated; a client following them internally would skip
    every one of these checks."""
    from backend.shared.imagefetch import ImageFetchError, fetch_image

    fast = AsyncMock(return_value=_fake_fast_response(
        302, {"Location": "http://169.254.169.254/latest/meta-data/"}))
    with patch.object(fast_http, "fetch", fast), \
         patch.object(fast_http, "available", return_value=True):
        with pytest.raises(ImageFetchError) as caught:
            await fetch_image("https://scontent.xx.fbcdn.net/p.jpg")

    assert caught.value.status == 400
    assert "allowlisted" in caught.value.detail


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused_rather_than_fetched_again():
    from backend.shared.imagefetch import ImageFetchError, fetch_image

    fast = AsyncMock(side_effect=fast_http.FastHttpTooLarge("too big"))
    aio = AsyncMock()
    with patch.object(fast_http, "fetch", fast), \
         patch.object(fast_http, "available", return_value=True), \
         patch("backend.shared.imagefetch._hop_aiohttp", aio):
        with pytest.raises(ImageFetchError) as caught:
            await fetch_image("https://scontent.xx.fbcdn.net/p.jpg")

    assert "too large" in caught.value.detail
    aio.assert_not_awaited()


@pytest.mark.asyncio
async def test_with_the_accelerator_off_avatars_take_exactly_the_old_path():
    from backend.shared.imagefetch import _Hop, fetch_image

    fast = AsyncMock()
    aio = AsyncMock(return_value=_Hop(200, "", "image/jpeg", b"old path"))
    with patch.object(fast_http, "available", return_value=False), \
         patch.object(fast_http, "fetch", fast), \
         patch("backend.shared.imagefetch._hop_aiohttp", aio):
        img = await fetch_image("https://scontent.xx.fbcdn.net/p.jpg")

    assert img.data == b"old path"
    fast.assert_not_awaited()


# --------------------------------------------------------- session checking


def test_the_http_session_check_is_off_by_default():
    """It sends an account's live session cookies from a second client
    fingerprint (curl_cffi) -- the stolen-cookie pattern Meta scores. Opt-in
    only since 2026-09-24; see settings.session_fast_check_enabled."""
    from backend.config.settings import Settings

    assert Settings.model_fields["session_fast_check_enabled"].default is False


@pytest.mark.asyncio
async def test_a_confirmed_live_session_never_launches_a_browser(monkeypatch):
    from backend.config.settings import settings
    from backend.sessions import manager

    # Opted in explicitly: this pins what the check does WHEN enabled.
    monkeypatch.setattr(settings, "session_fast_check_enabled", True)
    with patch.object(fast_http, "check_session_alive",
                      AsyncMock(return_value=fast_http.SessionVerdict(True, "/me resolved to /zuck"))), \
         patch("backend.platforms.registry.get", side_effect=AssertionError("browser path taken")):
        ok, detail, conclusive = await manager.verify_session_item(
            "facebook", [{"name": "c_user", "value": "1"}])

    assert (ok, detail, conclusive) == (True, "", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [
    fast_http.SessionVerdict(False, "redirected to /login"),
    fast_http.SessionVerdict(None, "HTTP 403 (rejected session or bot wall)"),
])
async def test_only_the_browser_is_allowed_to_condemn_an_account(verdict):
    """A dead verdict over plain HTTP is indistinguishable from a bot wall
    or a rate limit. Acting on it would quarantine working accounts until
    the pool is empty and every sweep returns nothing -- which is
    indistinguishable, in the product, from a client having no impostors."""
    from backend.sessions import manager

    scraper = MagicMock()
    scraper.start = AsyncMock()
    scraper.stop = AsyncMock()
    scraper.check_session = AsyncMock(return_value=True)
    plat = MagicMock()
    plat.scraper.return_value = MagicMock(return_value=scraper)

    with patch.object(fast_http, "check_session_alive", AsyncMock(return_value=verdict)), \
         patch("backend.platforms.registry.get", return_value=plat):
        ok, _detail, conclusive = await manager.verify_session_item(
            "facebook", [{"name": "c_user", "value": "1"}])

    # The browser ran and its answer -- not the probe's -- is what came back.
    scraper.check_session.assert_awaited_once()
    assert (ok, conclusive) == (True, True)


@pytest.mark.asyncio
async def test_a_probe_that_raises_does_not_break_the_session_check():
    from backend.sessions import manager

    scraper = MagicMock()
    scraper.start = AsyncMock()
    scraper.stop = AsyncMock()
    scraper.check_session = AsyncMock(return_value=False)
    plat = MagicMock()
    plat.scraper.return_value = MagicMock(return_value=scraper)

    with patch.object(fast_http, "check_session_alive",
                      AsyncMock(side_effect=RuntimeError("libcurl exploded"))), \
         patch("backend.platforms.registry.get", return_value=plat):
        ok, detail, conclusive = await manager.verify_session_item(
            "facebook", [{"name": "c_user", "value": "1"}])

    assert (ok, conclusive) == (False, True)
    assert detail


@pytest.mark.asyncio
async def test_with_the_fast_check_off_every_session_check_uses_the_browser():
    from backend.config.settings import settings
    from backend.sessions import manager

    probe = AsyncMock()
    scraper = MagicMock()
    scraper.start = AsyncMock()
    scraper.stop = AsyncMock()
    scraper.check_session = AsyncMock(return_value=True)
    plat = MagicMock()
    plat.scraper.return_value = MagicMock(return_value=scraper)

    original = settings.session_fast_check_enabled
    try:
        settings.session_fast_check_enabled = False
        with patch.object(fast_http, "check_session_alive", probe), \
             patch("backend.platforms.registry.get", return_value=plat):
            ok, _d, _c = await manager.verify_session_item(
                "facebook", [{"name": "c_user", "value": "1"}])
    finally:
        settings.session_fast_check_enabled = original

    assert ok is True
    probe.assert_not_awaited()


# ------------------------------------------------------- analysis pre-flight


def _job_with(urls: list[tuple[str, str]]):
    """(url, platform) pairs -> a job and its by-platform grouping, built the
    way `_run` builds it."""
    from backend.analysis.runner import AnalysisItem, AnalysisJob

    items = [
        AnalysisItem(id=f"i{n}", raw_url=u, url=u, platform=p, entity_id=f"e{n}")
        for n, (u, p) in enumerate(urls)
    ]
    job = AnalysisJob(id="job1", items=items, total=len(items))
    by_platform: dict[str, list] = {}
    for it in items:
        by_platform.setdefault(it.platform, []).append(it)
        entry = job.platform_progress.setdefault(
            it.platform, {"status": "pending", "total": 0, "completed": 0,
                          "display_name": it.platform})
        entry["total"] += 1
    return job, by_platform


def _dead(url: str, status: int = 404):
    return fast_http.PreflightResult(
        url, is_dead=True, status_code=status,
        reason=f"the platform answered HTTP {status} for this profile URL", checked=True)


def _alive(url: str, status: int = 200):
    return fast_http.PreflightResult(url, status_code=status, reason=f"HTTP {status}", checked=True)


@pytest.mark.asyncio
async def test_a_url_the_platform_says_is_gone_is_settled_without_a_browser():
    from backend.analysis.runner import AnalysisRunner

    job, by_platform = _job_with([
        ("https://x.com/gone_77412", "twitter"),
        ("https://x.com/alive_one", "twitter"),
        ("https://x.com/alive_two", "twitter"),
    ])
    results = {
        "https://x.com/gone_77412": _dead("https://x.com/gone_77412"),
        "https://x.com/alive_one": _alive("https://x.com/alive_one"),
        "https://x.com/alive_two": _alive("https://x.com/alive_two"),
    }

    with patch.object(fast_http, "available", return_value=True), \
         patch.object(fast_http, "preflight_many", AsyncMock(return_value=results)), \
         patch("backend.analysis.runner.results_db.save", AsyncMock()) as saved:
        settled = await AnalysisRunner()._preflight(job, by_platform)

    assert settled == 1
    # The dead one is a SAVED, COUNTED row -- a pre-flight must never make a
    # URL quietly disappear from the batch.
    gone = next(i for i in job.items if i.url.endswith("gone_77412"))
    assert gone.status == "error"
    assert "404" in gone.error
    assert saved.await_count == 1
    assert job.completed == 1
    assert job.platform_progress["twitter"]["completed"] == 1
    # The live ones are still queued for a real visit.
    assert [i.url for i in by_platform["twitter"]] == [
        "https://x.com/alive_one", "https://x.com/alive_two"]


@pytest.mark.asyncio
async def test_a_whole_batch_coming_back_gone_settles_nothing():
    """THE BLANKET-404 GUARD. A client the platform has decided to block can
    404 everything, and each row would look exactly like a real finding. The
    batch is its own control: no URL answering alive means the 404s prove
    nothing about any individual profile."""
    from backend.analysis.runner import AnalysisRunner

    urls = [f"https://x.com/gone_{n}" for n in range(4)]
    job, by_platform = _job_with([(u, "twitter") for u in urls])
    results = {u: _dead(u) for u in urls}

    with patch.object(fast_http, "available", return_value=True), \
         patch.object(fast_http, "preflight_many", AsyncMock(return_value=results)), \
         patch("backend.analysis.runner.results_db.save", AsyncMock()) as saved:
        settled = await AnalysisRunner()._preflight(job, by_platform)

    assert settled == 0
    saved.assert_not_awaited()
    assert len(by_platform["twitter"]) == 4
    assert all(i.status == "pending" for i in job.items)
    assert job.completed == 0


@pytest.mark.asyncio
async def test_a_small_paste_of_genuinely_dead_links_still_settles():
    """The control needs something to control against. Two dead links in a
    two-link paste is an ordinary Tuesday, not evidence of a block."""
    from backend.analysis.runner import AnalysisRunner

    urls = ["https://x.com/gone_a", "https://x.com/gone_b"]
    job, by_platform = _job_with([(u, "twitter") for u in urls])

    with patch.object(fast_http, "available", return_value=True), \
         patch.object(fast_http, "preflight_many",
                      AsyncMock(return_value={u: _dead(u) for u in urls})), \
         patch("backend.analysis.runner.results_db.save", AsyncMock()):
        settled = await AnalysisRunner()._preflight(job, by_platform)

    assert settled == 2
    assert "twitter" not in by_platform


@pytest.mark.asyncio
async def test_the_settled_row_names_its_evidence_rather_than_asserting_a_verdict():
    """This row reaches an analyst with no screenshot behind it, because no
    browser ever visited. It has to say so."""
    from backend.analysis.runner import AnalysisRunner

    job, by_platform = _job_with([("https://x.com/gone_77412", "twitter")])
    with patch.object(fast_http, "available", return_value=True), \
         patch.object(fast_http, "preflight_many",
                      AsyncMock(return_value={
                          "https://x.com/gone_77412": _dead("https://x.com/gone_77412")})), \
         patch("backend.analysis.runner.results_db.save", AsyncMock()):
        await AnalysisRunner()._preflight(job, by_platform)

    error = job.items[0].error
    assert "404" in error
    assert "pre-flight" in error.lower()
    assert "no browser visit" in error.lower()
    assert "screenshot" in error.lower()


@pytest.mark.asyncio
async def test_a_platform_emptied_by_pre_flight_never_leases_a_session():
    from backend.analysis.runner import AnalysisRunner

    job, by_platform = _job_with([("https://x.com/gone_77412", "twitter")])
    with patch.object(fast_http, "available", return_value=True), \
         patch.object(fast_http, "preflight_many",
                      AsyncMock(return_value={
                          "https://x.com/gone_77412": _dead("https://x.com/gone_77412", 410)})), \
         patch("backend.analysis.runner.results_db.save", AsyncMock()):
        await AnalysisRunner()._preflight(job, by_platform)

    assert "twitter" not in by_platform
    # `_scrape_platform` is the only other thing that ever sets this, and it
    # is not going to run -- so the panel must not be left saying "pending".
    assert job.platform_progress["twitter"]["status"] == "done"


@pytest.mark.asyncio
async def test_platforms_with_no_provable_death_signal_are_left_entirely_alone():
    from backend.analysis.runner import AnalysisRunner

    job, by_platform = _job_with([
        ("https://www.tiktok.com/@someone", "tiktok"),
        ("https://www.facebook.com/someone", "facebook"),
    ])
    many = AsyncMock(return_value={})
    with patch.object(fast_http, "available", return_value=True), \
         patch.object(fast_http, "preflight_many", many):
        settled = await AnalysisRunner()._preflight(job, by_platform)

    assert settled == 0
    assert set(by_platform) == {"tiktok", "facebook"}
    many.assert_not_awaited()       # nothing even worth asking about


@pytest.mark.asyncio
async def test_a_pre_flight_that_blows_up_costs_the_batch_nothing():
    from backend.analysis.runner import AnalysisRunner

    job, by_platform = _job_with([("https://x.com/someone", "twitter")])
    with patch.object(fast_http, "available", return_value=True), \
         patch.object(fast_http, "preflight_many",
                      AsyncMock(side_effect=RuntimeError("libcurl exploded"))):
        settled = await AnalysisRunner()._preflight(job, by_platform)

    assert settled == 0
    assert len(by_platform["twitter"]) == 1
    assert job.items[0].status == "pending"


@pytest.mark.asyncio
async def test_a_cancelled_job_is_not_pre_flighted():
    from backend.analysis.runner import AnalysisRunner

    job, by_platform = _job_with([("https://x.com/someone", "twitter")])
    job.cancel.set()
    many = AsyncMock()
    with patch.object(fast_http, "available", return_value=True), \
         patch.object(fast_http, "preflight_many", many):
        assert await AnalysisRunner()._preflight(job, by_platform) == 0
    many.assert_not_awaited()


@pytest.mark.asyncio
async def test_with_the_pre_flight_off_every_url_still_gets_a_real_visit():
    from backend.analysis.runner import AnalysisRunner
    from backend.config.settings import settings

    job, by_platform = _job_with([("https://x.com/someone", "twitter")])
    many = AsyncMock()
    original = settings.analysis_preflight_enabled
    try:
        settings.analysis_preflight_enabled = False
        with patch.object(fast_http, "available", return_value=True), \
             patch.object(fast_http, "preflight_many", many):
            assert await AnalysisRunner()._preflight(job, by_platform) == 0
    finally:
        settings.analysis_preflight_enabled = original
    many.assert_not_awaited()
    assert len(by_platform["twitter"]) == 1


@pytest.mark.asyncio
async def test_each_platform_is_judged_on_its_own_pre_flight_answers():
    """Two platforms in one batch must not share a verdict: Instagram going
    quiet cannot hold back X's settled rows, and X's 404s cannot settle
    anything on Instagram."""
    from backend.analysis.runner import AnalysisRunner

    job, by_platform = _job_with([
        ("https://x.com/gone_a", "twitter"),
        ("https://x.com/alive_a", "twitter"),
        ("https://www.instagram.com/gone_b/", "instagram"),
        ("https://www.instagram.com/alive_b/", "instagram"),
    ])
    results = {
        "https://x.com/gone_a": _dead("https://x.com/gone_a"),
        "https://x.com/alive_a": _alive("https://x.com/alive_a"),
        "https://www.instagram.com/gone_b/": _dead("https://www.instagram.com/gone_b/", 200),
        "https://www.instagram.com/alive_b/": _alive("https://www.instagram.com/alive_b/"),
    }

    with patch.object(fast_http, "available", return_value=True), \
         patch.object(fast_http, "preflight_many", AsyncMock(return_value=results)), \
         patch("backend.analysis.runner.results_db.save", AsyncMock()):
        settled = await AnalysisRunner()._preflight(job, by_platform)

    assert settled == 2
    assert [i.url for i in by_platform["twitter"]] == ["https://x.com/alive_a"]
    assert [i.url for i in by_platform["instagram"]] == ["https://www.instagram.com/alive_b/"]
    assert job.completed == 2
    assert job.platform_progress["twitter"]["completed"] == 1
    assert job.platform_progress["instagram"]["completed"] == 1


# ------------------------------------------------- the periodic pool probe


def _pool(*items):
    async def _list_pool(platform_id):
        return [i for i in items if i.get("_platform") == platform_id]
    return _list_pool


def _sess(platform, sid, status="ready", cookies=None):
    return {"_platform": platform, "id": sid, "identifier": f"{platform}-{sid}",
            "status": status, "cookies": cookies if cookies is not None else [{"name": "c", "value": "1"}]}


@pytest.mark.asyncio
async def test_the_pool_probe_escalates_a_dead_answer_instead_of_acting_on_it():
    """The cheap probe is a trigger, not a verdict. `check_item` is the
    authoritative browser check and the only thing allowed to quarantine."""
    from backend.services import session_canary_service as canary

    check_item = AsyncMock(return_value={"ok": False, "detail": "session invalid or checkpointed"})
    with patch("backend.database.repositories.session_repository.list_pool",
               _pool(_sess("facebook", "s1"))), \
         patch.object(fast_http, "check_session_alive",
                      AsyncMock(return_value=fast_http.SessionVerdict(False, "redirected to /login"))), \
         patch("backend.sessions.manager.check_item", check_item), \
         patch("backend.sessions.manager._session_in_use", return_value=False):
        report = await canary.probe_pool_liveness(["facebook"])

    check_item.assert_awaited_once_with("facebook", "s1")
    assert report["escalated"][0]["confirmed_dead"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [
    fast_http.SessionVerdict(True, "/me resolved to /profile.php"),
    fast_http.SessionVerdict(None, "HTTP 403 (rejected session or bot wall)"),
])
async def test_the_pool_probe_leaves_a_live_or_unreadable_session_alone(verdict):
    """This pass is early warning, not a health ledger. Recording a pass
    here would reset another path's consecutive-failure ladder every half
    hour, so a genuinely dead session could never accumulate strikes."""
    from backend.services import session_canary_service as canary

    check_item = AsyncMock()
    with patch("backend.database.repositories.session_repository.list_pool",
               _pool(_sess("facebook", "s1"))), \
         patch.object(fast_http, "check_session_alive", AsyncMock(return_value=verdict)), \
         patch("backend.sessions.manager.check_item", check_item), \
         patch("backend.sessions.manager._session_in_use", return_value=False):
        report = await canary.probe_pool_liveness(["facebook"])

    check_item.assert_not_awaited()
    assert report["escalated"] == []


@pytest.mark.asyncio
async def test_the_pool_probe_skips_an_account_a_running_job_is_holding():
    """The same rule the browser sweep follows -- a second touch on the
    account being scraped this second is how checkpoints are earned."""
    from backend.services import session_canary_service as canary

    probe = AsyncMock()
    with patch("backend.database.repositories.session_repository.list_pool",
               _pool(_sess("facebook", "s1"))), \
         patch.object(fast_http, "check_session_alive", probe), \
         patch("backend.sessions.manager._session_in_use", return_value=True):
        report = await canary.probe_pool_liveness(["facebook"])

    probe.assert_not_awaited()
    assert report["probed"] == []


@pytest.mark.asyncio
async def test_the_pool_probe_skips_sessions_already_known_dead_or_empty():
    from backend.services import session_canary_service as canary

    probe = AsyncMock()
    with patch("backend.database.repositories.session_repository.list_pool",
               _pool(_sess("facebook", "s1", status="expired"),
                     _sess("facebook", "s2", cookies=[]))), \
         patch.object(fast_http, "check_session_alive", probe), \
         patch("backend.sessions.manager._session_in_use", return_value=False):
        report = await canary.probe_pool_liveness(["facebook"])

    probe.assert_not_awaited()
    assert report["probed"] == []


@pytest.mark.asyncio
async def test_an_escalation_that_cannot_run_is_recorded_rather_than_raised():
    """`check_item` refuses while a job holds the session and raises if it
    was deleted underneath us. Neither is this function's to resolve, and
    neither may take down the monitor sweep it runs inside."""
    from backend.services import session_canary_service as canary

    with patch("backend.database.repositories.session_repository.list_pool",
               _pool(_sess("instagram", "s9"))), \
         patch.object(fast_http, "check_session_alive",
                      AsyncMock(return_value=fast_http.SessionVerdict(False, "redirected to /accounts/login"))), \
         patch("backend.sessions.manager.check_item",
               AsyncMock(side_effect=RuntimeError("held by a running job"))), \
         patch("backend.sessions.manager._session_in_use", return_value=False):
        report = await canary.probe_pool_liveness(["instagram"])

    assert report["escalated"] == []
    assert "escalation did not run" in report["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_the_pool_probe_only_visits_platforms_that_have_a_probe():
    from backend.services import session_canary_service as canary

    probe = AsyncMock()
    with patch("backend.database.repositories.session_repository.list_pool",
               _pool(_sess("twitter", "s1"))), \
         patch.object(fast_http, "check_session_alive", probe), \
         patch("backend.sessions.manager._session_in_use", return_value=False):
        report = await canary.probe_pool_liveness(["twitter"])

    probe.assert_not_awaited()
    assert "no HTTP-provable session signal" in report["skipped"][0]["reason"]
