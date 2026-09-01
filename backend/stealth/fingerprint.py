"""The device identity a session presents: user-agent, viewport, and the
Chromium launch flags/binary that back them up.

Deliberately NOT diversifying user-agent across random versions, it must keep
matching the real Chrome major version actually installed (`chrome_binary()`
below); a UA claiming a version that isn't the binary actually running is a worse
tell than every session sharing one. Dynamically aligned with local binaries.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from backend.shared.logging import get_logger

log = get_logger("stealth.fingerprint")

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    # Linux/container locations. The original list had only
    # /usr/bin/google-chrome, which is NOT what a Debian/Alpine image or a
    # playwright base image typically ships -- on those the lookup missed,
    # the hardcoded default below was used, and every session then
    # advertised a Chrome version that no binary on the host actually had.
    # That is precisely the "worse tell" this module's docstring warns
    # about, so the deployment targets are enumerated rather than assumed.
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    "/opt/google/chrome/chrome",
]

# The version claimed when no real browser can be found. It ages the
# moment it is written, which is exactly why `CHROME_VERSION_DETECTED`
# exists: services/preflight_service.py alerts on a deployment that is
# running on this fallback, instead of letting a stale UA quietly become
# the fleet's most distinctive fingerprint.
FALLBACK_CHROME_MAJOR = "132"
FALLBACK_CHROME_FULL = "132.0.6834.83"


# Executable names to look for on PATH. Covers the usual Chrome/Chromium
# packaging across distros as well as a Windows install that put itself
# somewhere CHROME_PATHS does not enumerate.
CHROME_EXE_NAMES = (
    "chrome", "google-chrome", "google-chrome-stable", "chrome.exe",
    "chromium", "chromium-browser", "chromium.exe",
)

# Explicit override, checked first. The escape hatch for anyone whose
# install is somewhere none of the automatic strategies look -- a portable
# build, a non-standard prefix, a pinned version.
CHROME_BINARY_ENV = "CHROME_BINARY"


def _from_env() -> str | None:
    raw = (os.environ.get(CHROME_BINARY_ENV) or "").strip().strip('"')
    if not raw:
        return None
    if Path(raw).exists():
        return raw
    log.warning(
        f"{CHROME_BINARY_ENV}={raw!r} is set but no file exists there -- ignoring it "
        "and falling back to automatic detection"
    )
    return None


def _from_path() -> str | None:
    """Whatever the OS itself would run. The most portable single check
    there is, and it costs nothing."""
    for name in CHROME_EXE_NAMES:
        if found := shutil.which(name):
            return found
    return None


def _from_windows_registry() -> str | None:
    """Windows records the real install location, wherever it is. This is
    the authoritative answer on Windows -- a user who installed Chrome to
    another drive is invisible to a hardcoded Program Files path but not
    to this."""
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, key) as handle:
                value, _ = winreg.QueryValueEx(handle, None)
                if value and Path(value).exists():
                    return str(value)
        except OSError:
            continue
    return None


def _from_playwright() -> str | None:
    """Playwright downloads its own Chromium; if this project installed
    browsers at all, one is on disk. Better than advertising a version no
    binary here actually has."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
        return path if path and Path(path).exists() else None
    except Exception:
        return None


def _from_known_paths() -> str | None:
    for p in CHROME_PATHS:
        if p and Path(p).exists():
            return p
    return None


def chrome_binary() -> str | None:
    """The real Chrome/Chromium on THIS machine, however it got installed.

    A hardcoded path list is fine on the machine it was written for and
    silently wrong everywhere else: the lookup misses, the stale
    FALLBACK_CHROME_FULL below is what every session then advertises, and
    a UA claiming a version no binary on the host has is a worse tell than
    no spoofing at all. So this asks the system rather than assuming, in
    descending order of authority:

        1. $CHROME_BINARY          -- explicit operator override
        2. PATH                    -- what the OS itself would run
        3. Windows registry        -- the real install location on Windows
        4. well-known paths        -- the previous behaviour, kept
        5. Playwright's Chromium   -- whatever this project downloaded

    Result is cached: it is called per session start and none of these
    change while the process lives.
    """
    global _CHROME_BINARY_CACHED, _CHROME_BINARY_RESOLVED
    if _CHROME_BINARY_RESOLVED:
        return _CHROME_BINARY_CACHED

    # (label, callable) rather than reading __name__ off the function: the
    # label is for humans reading the log, and looking it up dynamically
    # would also couple this to how the functions are referenced.
    strategies = (
        ("CHROME_BINARY env", _from_env),
        ("PATH", _from_path),
        ("windows registry", _from_windows_registry),
        ("known install paths", _from_known_paths),
        ("playwright chromium", _from_playwright),
    )
    for label, strategy in strategies:
        try:
            found = strategy()
        except Exception as e:  # a detection strategy must never be fatal
            log.debug(f"chrome detection via {label} failed: {e}")
            found = None
        if found:
            _CHROME_BINARY_CACHED = found
            _CHROME_BINARY_RESOLVED = True
            log.info(f"chrome detected via {label}: {found}")
            return found

    log.warning(
        "no Chrome/Chromium binary found by any strategy -- sessions will advertise the "
        f"fallback version {FALLBACK_CHROME_FULL}, which ages badly. Set "
        f"{CHROME_BINARY_ENV}=/path/to/chrome to point this at your install."
    )
    _CHROME_BINARY_RESOLVED = True
    _CHROME_BINARY_CACHED = None
    return None


_CHROME_BINARY_CACHED: str | None = None
_CHROME_BINARY_RESOLVED = False


def _detect_chrome_version(binary_path: str | None) -> tuple[str, str]:
    """Dynamically extract major and full version strings from installed Chrome."""
    default_major = FALLBACK_CHROME_MAJOR
    default_full = FALLBACK_CHROME_FULL
    if not binary_path or not Path(binary_path).exists():
        return default_major, default_full
    try:
        if os.name == "nt":
            cmd = (
                f'powershell -NoProfile -Command "(Get-Item -LiteralPath '
                f'\'{binary_path}\').VersionInfo.ProductVersion"'
            )
            res = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=4, check=False
            )
            if match := re.search(r"(\d+)\.(\d+\.\d+\.\d+)", res.stdout):
                return match.group(1), match.group(0)
        else:
            res = subprocess.run(
                [binary_path, "--version"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if match := re.search(r"(\d+)\.(\d+\.\d+\.\d+)", res.stdout):
                return match.group(1), match.group(0)
    except Exception:
        pass
    return default_major, default_full


CHROME_MAJOR_VERSION, CHROME_FULL_VERSION = _detect_chrome_version(chrome_binary())

# False means no real browser was found and the stale fallback above is
# what every session is advertising. Read by services/preflight_service.py.
CHROME_VERSION_DETECTED = CHROME_FULL_VERSION != FALLBACK_CHROME_FULL

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_FULL_VERSION} Safari/537.36"
)

# A 100-session pool that all present the exact same viewport is itself a
# fingerprint, real analysts don't all run the same window size. Kept to
# common, unremarkable desktop resolutions rather than anything exotic.
VIEWPORTS = [
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
    {"width": 1920, "height": 1080},
    {"width": 1600, "height": 900},
    {"width": 1280, "height": 800},
]

# Hardware specs are NO LONGER spoofed -- these pools are kept only so
# get_identity() below keeps returning the keys some callers still read, and
# nothing consumes them for spoofing any more. An init script cannot reach
# Web Worker scope, so overriding these produced a main-thread-vs-worker
# contradiction (measured: main hc=8/dm=8 against worker hc=12/dm=32) that is
# far more identifying than any honest core count. See
# navigator_spoofing.py's note where the overrides used to live.
HARDWARE_CONCURRENCY = [8, 12, 16]
DEVICE_MEMORY = [8, 16]

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-extensions",
    "--disable-dev-shm-usage",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--no-first-run",
    "--disable-component-update",
    "--no-default-browser-check",
    # keep WebRTC from advertising local addresses.
    #
    # ONE --disable-features flag, deliberately. Chromium does NOT merge
    # repeated --disable-features=; the last occurrence wins and every
    # earlier one is silently discarded. This list used to carry a second
    # one (`IsolateOrigins,site-per-process`) ABOVE this line, so it was
    # never actually in effect -- verified by inspecting the list directly.
    #
    # It was not merged in when that was found, it was dropped, along with
    # the `--disable-site-isolation-trials` that accompanied it. Real
    # Chrome ships with site isolation ON; a browser that has it off is
    # itself an automation tell, and turning it off now would ADD a signal
    # rather than remove one. Since the flag was already inert, removing it
    # cannot change how any engine behaves -- the browser has been running
    # with site isolation enabled all along.
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-features=WebRtcHideLocalIpsWithMdns",
]


def _pick(seed: str, pool: list):
    """Stable pick from `pool`, same seed always lands on the same entry
    (so one session's fingerprint doesn't drift run to run), different seeds
    spread across the pool (so the whole session pool isn't identical)."""
    if not seed:
        return pool[0]
    idx = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(pool)
    return pool[idx]


def get_identity(session_id: str = "") -> dict:
    """Returns a coherent, stable device identity for a session context."""
    return {
        "ua": UA,
        "chrome_major": CHROME_MAJOR_VERSION,
        "chrome_full": CHROME_FULL_VERSION,
        "viewport": _pick(session_id, VIEWPORTS),
        "hardware_concurrency": _pick(session_id, HARDWARE_CONCURRENCY),
        "device_memory": _pick(session_id, DEVICE_MEMORY),
    }
