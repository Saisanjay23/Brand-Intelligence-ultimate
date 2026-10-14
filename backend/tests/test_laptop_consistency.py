"""The same code must give the same result on a fast laptop and a slow one.

Two symptoms differed by machine: X follower counts missing, and evidence
screenshots not captured. Both traced to code that only worked inside a
fixed, fast-machine time budget, or that had a single way of doing a thing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.platforms.twitter.analysis_engine import (
    Scraper, apply_dom_counts, count_from_text, dom_counts)
from backend.shared.models.row import Row
from backend.stealth import browser as B
from backend.stealth import fingerprint as F


# --- follower counts read off the page -----------------------------------

@pytest.mark.parametrize("text,expected", [
    ("1,234 Followers", (1234, True)),
    ("12 Following", (12, True)),
    ("1.2M Followers", (1_200_000, False)),
    ("154K Followers", (154_000, False)),
    ("0 Followers", (0, True)),
    ("", (None, False)),
    ("Followers", (None, False)),
])
def test_count_from_text(text, expected):
    assert count_from_text(text) == expected


class CountPage:
    def __init__(self, raw):
        self.raw = raw
        self.handle = None

    async def wait_for_selector(self, *a, **k):
        return None

    async def evaluate(self, js, handle=None):
        self.handle = handle
        return self.raw


@pytest.mark.asyncio
async def test_dom_counts_parses_both_links_for_this_handle():
    page = CountPage({"followers": "2.5M Followers", "following": "310 Following"})
    got = await dom_counts(page, "acme")
    assert page.handle == "acme"
    assert got == {"followers": (2_500_000, False), "following": (310, True)}


@pytest.mark.asyncio
async def test_dom_counts_never_raises():
    class Boom(CountPage):
        async def evaluate(self, js, handle=None):
            raise RuntimeError("page closed")
    assert await dom_counts(Boom({}), "acme") == {}


def test_dom_counts_only_fill_what_the_payload_left_empty():
    row = Row(url="https://x.com/acme", target="Acme")
    row.followers, row.followers_exact = 500, "yes"
    apply_dom_counts(row, {"followers": (999, True), "following": (7, True)})
    assert row.followers == 500          # the typed payload value wins
    assert row.friends == 7

    empty = Row(url="https://x.com/acme", target="Acme")
    apply_dom_counts(empty, {"followers": (1_200_000, False)})
    assert empty.followers == 1_200_000
    assert empty.followers_exact == "no"  # approximate, labelled as such


def test_a_wall_is_not_retried_but_a_slow_profile_is():
    ok = SimpleNamespace(url="https://x.com/acme")
    assert not Scraper._is_wall(ok, "Acme @acme 1,234 Followers")
    assert Scraper._is_wall(SimpleNamespace(url="https://x.com/i/flow/login"), "")
    assert Scraper._is_wall(ok, "Rate limit exceeded")


# --- the wait is a generous ceiling, not the old 6 seconds ----------------

def test_analysis_waits_long_enough_for_a_slow_machine():
    from backend.config.settings import settings
    assert settings.analysis_settle_sec >= 12


# --- screenshots have more than one way to succeed -----------------------

class ShotPage:
    def __init__(self, fail_normal=0, cdp_ok=True):
        self.calls = []
        self.fail_normal = fail_normal
        self.fronted = False
        self.context = SimpleNamespace(new_cdp_session=self._cdp)
        self.cdp_ok = cdp_ok

    async def bring_to_front(self):
        self.fronted = True

    async def screenshot(self, full_page=False, timeout=None, **kw):
        self.calls.append(kw)
        if len(self.calls) <= self.fail_normal:
            raise TimeoutError("Page.screenshot: Timeout exceeded")
        return b"png"

    async def _cdp(self, page):
        if not self.cdp_ok:
            raise RuntimeError("no cdp")

        async def send(method, params=None):
            return {"data": "cG5n"}  # b"png"

        async def detach():
            return None
        return SimpleNamespace(send=send, detach=detach)


@pytest.mark.asyncio
async def test_shoot_brings_the_tab_to_the_front():
    page = ShotPage()
    assert await B.shoot(page) == b"png"
    assert page.fronted


@pytest.mark.asyncio
async def test_shoot_retries_with_animations_frozen():
    page = ShotPage(fail_normal=1)
    assert await B.shoot(page) == b"png"
    assert page.calls[1] == {"animations": "disabled", "caret": "hide"}


@pytest.mark.asyncio
async def test_shoot_falls_back_to_devtools_capture():
    page = ShotPage(fail_normal=2)
    assert await B.shoot(page) == b"png"


@pytest.mark.asyncio
async def test_shoot_raises_the_real_cause_when_everything_fails():
    page = ShotPage(fail_normal=2, cdp_ok=False)
    with pytest.raises(TimeoutError):
        await B.shoot(page)


# --- the Chrome version comes from disk, not a 4s PowerShell --------------

def test_chrome_version_read_from_install_directory(tmp_path):
    (tmp_path / "chrome.exe").write_bytes(b"")
    for d in ("131.0.1.1", "146.0.7680.72", "locales", "Dictionaries"):
        (tmp_path / d).mkdir()
    assert F._version_from_install_dir(str(tmp_path / "chrome.exe")) == (
        "146", "146.0.7680.72")


def test_chrome_version_dir_absent_returns_none(tmp_path):
    (tmp_path / "chrome.exe").write_bytes(b"")
    assert F._version_from_install_dir(str(tmp_path / "chrome.exe")) is None
