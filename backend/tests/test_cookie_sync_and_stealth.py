"""Tests for mid-sweep cookie synchronization and natural wheel scroll stealth mechanisms."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from backend.stealth.browser import Session
from backend.stealth.mouse_movement import natural_scroll_down
from backend.platforms.facebook.analysis_engine import Scraper as FacebookScraper
from backend.platforms.instagram.analysis_engine import Scraper as InstagramScraper
from backend.platforms.twitter.analysis_engine import Scraper as TwitterScraper
from backend.platforms.scan_options import ScanOptions


@pytest.mark.asyncio
async def test_session_sync_cookies_calls_on_cookies():
    session = Session(options=MagicMock(), cookies=[])
    mock_ctx = MagicMock()
    mock_ctx.cookies = AsyncMock(return_value=[
        {"name": "sessionid", "value": "fresh_123", "domain": ".instagram.com"},
        {"name": "csrftoken", "value": "token_abc", "domain": ".instagram.com"},
    ])
    session.ctx = mock_ctx

    saved_cookies = []
    async def fake_saver(cookies):
        saved_cookies.extend(cookies)

    session.on_cookies = fake_saver

    await session.sync_cookies()

    assert len(saved_cookies) == 2
    assert saved_cookies[0]["value"] == "fresh_123"
    assert saved_cookies[1]["value"] == "token_abc"


@pytest.mark.asyncio
async def test_session_sync_cookies_safe_on_error():
    session = Session(options=MagicMock(), cookies=[])
    mock_ctx = MagicMock()
    mock_ctx.cookies = AsyncMock(side_effect=RuntimeError("Browser context disconnected"))
    session.ctx = mock_ctx

    async def fake_saver(cookies):
        pass

    session.on_cookies = fake_saver

    # Must not raise
    await session.sync_cookies()


@pytest.mark.asyncio
async def test_session_stop_calls_sync_cookies():
    session = Session(options=MagicMock(), cookies=[])
    session.sync_cookies = AsyncMock()

    await session.stop()

    session.sync_cookies.assert_awaited_once()


@pytest.mark.asyncio
async def test_facebook_and_instagram_scraper_delegate_sync_cookies():
    opts = ScanOptions(timeout=10)
    
    fb = FacebookScraper(opts, cookies=[])
    fb.session = MagicMock()
    fb.session.sync_cookies = AsyncMock()
    await fb.sync_cookies()
    fb.session.sync_cookies.assert_awaited_once()

    ig = InstagramScraper(opts, cookies=[])
    ig.session = MagicMock()
    ig.session.sync_cookies = AsyncMock()
    await ig.sync_cookies()
    ig.session.sync_cookies.assert_awaited_once()

    tw = TwitterScraper(opts, cookies=[])
    tw.session = MagicMock()
    tw.session.sync_cookies = AsyncMock()
    await tw.sync_cookies()
    tw.session.sync_cookies.assert_awaited_once()



@pytest.mark.asyncio
async def test_natural_scroll_down_dispatches_discrete_wheel_ticks():
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    page.evaluate = AsyncMock(return_value={"scrollY": 100, "scrollHeight": 2000, "innerHeight": 800})
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.wheel = AsyncMock()

    await natural_scroll_down(page, distance=600, to_bottom=False)

    page.mouse.move.assert_awaited_once()
    assert page.mouse.wheel.await_count >= 4
    # All dy amounts should be positive downward ticks
    for call in page.mouse.wheel.call_args_list:
        args, _ = call
        assert args[0] == 0  # dx
        assert args[1] > 0  # dy


@pytest.mark.asyncio
async def test_natural_scroll_down_handles_to_bottom():
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    page.evaluate = AsyncMock(return_value={"scrollY": 200, "scrollHeight": 1800, "innerHeight": 800})
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.wheel = AsyncMock()

    await natural_scroll_down(page, distance=300, to_bottom=True)

    # Remaining is 1800 - (200 + 800) = 800. Target distance is > 800.
    assert page.mouse.wheel.await_count >= 4
    total_scrolled = sum(call[0][1] for call in page.mouse.wheel.call_args_list)
    assert total_scrolled >= 800


@pytest.mark.asyncio
async def test_natural_scroll_down_graceful_on_closed_page():
    page = MagicMock()
    page.is_closed = MagicMock(return_value=True)
    page.mouse = MagicMock()
    page.mouse.wheel = AsyncMock()

    await natural_scroll_down(page, distance=500)
    page.mouse.wheel.assert_not_called()


def test_hd_picture_url_rewrites_meta_cdn_crop():
    from backend.shared.avatars import hd_picture_url

    # Standard Meta CDN URL with 150x150 crop but 1080x1080 max upload bound
    url = (
        "https://scontent.fdel1-2.fna.fbcdn.net/v/t39.30808-1/1234_n.jpg"
        "?stp=c0.0.150.150a_dst-jpg_p150x150_q65_tt1"
        "&_nc_cat=108&ccb=1-7&cstp=mx1080x1080&ctp=s150x150&oh=abc&oe=123"
    )
    upgraded = hd_picture_url(url)
    assert "ctp=s1080x1080" in upgraded
    assert "p1080x1080" in upgraded

    # Non-cstp URL is returned unchanged
    plain = "https://pbs.twimg.com/profile_images/123/avatar.jpg"
    assert hd_picture_url(plain) == plain
    assert hd_picture_url("") == ""


def test_extract_instagram_hd_avatar_priority():
    from backend.shared.avatars import extract_instagram_hd_avatar

    # 1. Prioritizes hd_profile_pic_url_info
    node1 = {
        "hd_profile_pic_url_info": {"url": "https://scontent.cdninstagram.com/hd_info.jpg?cstp=mx1080x1080&ctp=s150x150"},
        "profile_pic_url_hd": "https://scontent.cdninstagram.com/hd_str.jpg",
        "profile_pic_url": "https://scontent.cdninstagram.com/thumb.jpg",
    }
    assert "hd_info.jpg" in extract_instagram_hd_avatar(node1)
    assert "ctp=s1080x1080" in extract_instagram_hd_avatar(node1)

    # 2. Prioritizes largest hd_profile_pic_versions when hd_info is absent
    node2 = {
        "hd_profile_pic_versions": [
            {"url": "https://scontent.cdninstagram.com/320.jpg", "width": 320},
            {"url": "https://scontent.cdninstagram.com/1080.jpg", "width": 1080},
            {"url": "https://scontent.cdninstagram.com/640.jpg", "width": 640},
        ],
        "profile_pic_url": "https://scontent.cdninstagram.com/thumb.jpg",
    }
    assert "1080.jpg" in extract_instagram_hd_avatar(node2)

    # 3. Falls back to profile_pic_url_hd string
    node3 = {
        "profile_pic_url_hd": "https://scontent.cdninstagram.com/hd_str.jpg",
        "profile_pic_url": "https://scontent.cdninstagram.com/thumb.jpg",
    }
    assert "hd_str.jpg" in extract_instagram_hd_avatar(node3)

    # 4. Falls back to standard thumbnail
    node4 = {"profile_pic_url": "https://scontent.cdninstagram.com/thumb.jpg"}
    assert "thumb.jpg" in extract_instagram_hd_avatar(node4)


def test_imagefetch_headers_configured():
    from backend.shared.imagefetch import IMAGE_FETCH_HEADERS

    assert "User-Agent" in IMAGE_FETCH_HEADERS
    assert "Mozilla/5.0" in IMAGE_FETCH_HEADERS["User-Agent"]
    assert "Sec-Fetch-Dest" in IMAGE_FETCH_HEADERS


@pytest.mark.asyncio
async def test_analysis_runner_caches_new_avatar():
    from backend.analysis.runner import AnalysisItem, AnalysisJob, AnalysisRunner
    from backend.shared.models.row import Row

    runner = AnalysisRunner()
    job = AnalysisJob(id="job1")
    it = AnalysisItem(
        id="item1", raw_url="https://www.instagram.com/test/",
        url="https://www.instagram.com/test/", platform="instagram", entity_id="test"
    )
    row = Row(
        target="Test Brand",
        url="https://www.instagram.com/test/",
        profile_name="Test Brand",
        profile_pic_url="https://scontent.cdninstagram.com/v/test_pic.jpg",
        status="OK",
    )

    # cache_one returns (sha, fingerprint) -- the fingerprint rides along on
    # the decode the fetch already pays for. Analysis only consumes the sha.
    with patch("backend.services.avatar_cache.cache_one",
               AsyncMock(return_value=("mock_sha_123", None, None))):
        await runner._populate(job, it, row, known=None)

    assert it.avatar_sha == "mock_sha_123"
    assert it.profile_image_url == "https://scontent.cdninstagram.com/v/test_pic.jpg"


@pytest.mark.asyncio
async def test_cache_one_retries_transient_failure():
    from backend.services.avatar_cache import cache_one
    from backend.shared.imagefetch import FetchedImage, ImageFetchError

    call_count = 0
    async def fake_fetch(url):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ImageFetchError("502 Bad Gateway")
        return FetchedImage(data=b"fake_image_bytes", content_type="image/jpeg")

    with patch("backend.services.avatar_cache.fetch_image", side_effect=fake_fetch), \
         patch("backend.services.avatar_cache.avatars_db.store", AsyncMock(return_value="sha_recovered")):
        sha, _fp, _vec = await cache_one("https://scontent.cdninstagram.com/pic.jpg", retries=1)

    assert call_count == 2
    assert sha == "sha_recovered"


@pytest.mark.asyncio
async def test_facebook_dom_search_hits_uses_hd_picture_url():
    from backend.platforms.facebook.discovery_engine import dom_search_hits

    mock_page = MagicMock()
    mock_page.evaluate = AsyncMock(return_value=[{
        "entity_id": "1001",
        "name": "Jane Doe",
        "url": "https://www.facebook.com/janedoe",
        "avatar": (
            "https://scontent.fdel1-2.fna.fbcdn.net/v/t39.30808-1/pic.jpg"
            "?stp=c0.0.150.150a_dst-jpg_p150x150_q65_tt1"
            "&cstp=mx1080x1080&ctp=s150x150&oh=abc&oe=123"
        ),
    }])

    hits = await dom_search_hits(mock_page, keyword="jane", tab="people")
    assert len(hits) == 1
    # Must be rewritten to HD
    assert "ctp=s1080x1080" in hits[0].avatar
    assert "p1080x1080" in hits[0].avatar


