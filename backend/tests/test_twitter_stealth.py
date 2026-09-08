"""Tests for Twitter / X stealth, anti-bot mechanisms, pointer telemetry, and shadowban prevention."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from backend.platforms.twitter.discovery_engine import TwitterSession, Discovery
from backend.platforms.twitter.analysis_engine import Scraper as TwitterScraper
from backend.platforms.scan_options import ScanOptions, DiscoveryOptions
from backend.stealth.mouse_movement import hover_element_safely


def test_twitter_session_always_loads_images():
    """Confirms TwitterSession declares ALWAYS_LOAD_IMAGES = True so pbs.twimg.com CDN traffic is genuine."""
    assert TwitterSession.ALWAYS_LOAD_IMAGES is True


@pytest.mark.asyncio
async def test_twitter_scraper_delegates_sync_cookies():
    """Confirms TwitterScraper implements sync_cookies and delegates to underlying Session."""
    opts = ScanOptions(timeout=10)
    scraper = TwitterScraper(opts, cookies=[])
    scraper.session = MagicMock()
    scraper.session.sync_cookies = AsyncMock()

    await scraper.sync_cookies()
    scraper.session.sync_cookies.assert_awaited_once()


@pytest.mark.asyncio
async def test_hover_element_safely_missing_element():
    """Confirms hover_element_safely gracefully returns False on non-existent elements."""
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    page.query_selector = AsyncMock(return_value=None)

    res = await hover_element_safely(page, '[data-testid="UserCell"]')
    assert res is False


@pytest.mark.asyncio
async def test_hover_element_safely_dispatches_moves():
    """Confirms hover_element_safely smoothly moves mouse to bounding box coordinates."""
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    mock_el = MagicMock()
    mock_el.bounding_box = AsyncMock(return_value={"x": 100, "y": 200, "width": 300, "height": 80})
    page.query_selector = AsyncMock(return_value=mock_el)
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()

    res = await hover_element_safely(page, '[data-testid="UserCell"]')
    assert res is True
    assert page.mouse.move.await_count >= 10


@pytest.mark.asyncio
async def test_twitter_discovery_sweep_invokes_natural_scroll():
    """Confirms Discovery.sweep uses natural_scroll_down and humanize_interaction."""
    mock_page = MagicMock()
    mock_page.goto = AsyncMock()
    mock_page.wait_for_function = AsyncMock()
    mock_page.close = AsyncMock()
    mock_page.on = MagicMock()
    mock_page.is_closed = MagicMock(return_value=False)

    mock_ctx = MagicMock()
    mock_ctx.new_page = AsyncMock(return_value=mock_page)

    opts = DiscoveryOptions(
        timeout=5,
        settle=0.1,
        page_wait=0.1,
        max_results=2,
        concurrency=1,
    )
    disc = Discovery(opts, mock_ctx)

    with patch("backend.platforms.twitter.discovery_engine.natural_scroll_down", AsyncMock()) as mock_scroll, \
         patch("backend.platforms.twitter.discovery_engine.humanize_interaction", AsyncMock()) as mock_human, \
         patch("backend.platforms.twitter.discovery_engine.run_strategies", AsyncMock(return_value=MagicMock(value=[], degraded=False))):
        
        sweep = await disc.sweep("testbrand")

    mock_human.assert_awaited()
    mock_scroll.assert_awaited()
    assert sweep.keyword == "testbrand"


@pytest.mark.asyncio
async def test_twitter_discovery_sweep_handles_429_rate_limit():
    """Confirms HTTP 429 triggers graceful rate_limited stop without looping stalls."""
    captured_callbacks = []

    mock_page = MagicMock()
    mock_page.goto = AsyncMock()
    mock_page.wait_for_function = AsyncMock()
    mock_page.close = AsyncMock()
    mock_page.on = MagicMock(side_effect=lambda event, cb: captured_callbacks.append(cb))
    mock_page.is_closed = MagicMock(return_value=False)

    mock_ctx = MagicMock()
    mock_ctx.new_page = AsyncMock(return_value=mock_page)

    opts = DiscoveryOptions(timeout=5, settle=0.1, page_wait=0.1, max_results=5)
    disc = Discovery(opts, mock_ctx)

    # Trigger a 429 response
    async def fake_goto(*args, **kwargs):
        resp = MagicMock()
        resp.url = "https://x.com/i/api/graphql/SearchTimeline"
        resp.status = 429
        for cb in captured_callbacks:
            await cb(resp)

    mock_page.goto = AsyncMock(side_effect=fake_goto)

    with patch("backend.platforms.twitter.discovery_engine.natural_scroll_down", AsyncMock()), \
         patch("backend.platforms.twitter.discovery_engine.humanize_interaction", AsyncMock()), \
         patch("backend.platforms.twitter.discovery_engine.run_strategies", AsyncMock(return_value=MagicMock(value=[], degraded=False))):
        
        sweep = await disc.sweep("testbrand")

    assert sweep.stopped == "rate_limited"
