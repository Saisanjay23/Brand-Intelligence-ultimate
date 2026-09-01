#!/usr/bin/env python3
"""
Brand Intelligence -- the only file you need to run.

    python run.py                start everything (sets up on first run)
    python run.py --setup        install dependencies, browsers and the UI
    python run.py --check        verify prerequisites, print status, exit
    python run.py --dev          serve the UI from Vite instead, hot-reloaded
    python run.py --build        force a UI rebuild first
    python run.py --port 9000    serve somewhere else

First run does the whole install by itself. After that `python run.py` just
starts the server.

ONE PORT, ONE URL. The API and the UI are the same server: backend/main.py
mounts the built `frontend/dist` at `/`, so http://127.0.0.1:8000 IS the
app -- no second process, no separate frontend port to remember. The
browser is opened for you once the port actually answers.

The build is rebuilt automatically whenever anything under `frontend/src`
is newer than the last one (see `ui_is_stale`), so editing the UI and
re-running this never silently serves the previous build.

`--dev` is the exception: there the UI comes from Vite on :5173 (hot
reload, proxying the API through to this process), and that is the URL
opened.

ONE WORKER, ON PURPOSE
    Live job state is in memory and the scanners drive real logged-in
    sessions, so a second worker would both lose progress and double the
    footprint on the platforms. The durable record is MongoDB plus the
    workbooks under runs/.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist"

# packages whose import name differs from the pip name
IMPORT_NAMES = {
    "playwright": "playwright",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "pydantic": "pydantic",
    "motor": "motor",
    "pymongo": "pymongo",
    "telethon": "telethon",
    "openpyxl": "openpyxl",
    "apscheduler": "apscheduler",
}

OK, BAD, WARN = "ok ", "FAIL", "-- "


def say(mark: str, label: str, detail: str = "") -> None:
    print(f"  [{mark}] {label}{' -- ' + detail if detail else ''}")


def npm() -> str:
    """npm is a .cmd shim on Windows, which subprocess will not find unaided."""
    return shutil.which("npm") or shutil.which("npm.cmd") or "npm"


def have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def missing_packages() -> list[str]:
    return [pip for pip, mod in IMPORT_NAMES.items() if not have(mod)]


def ui_is_stale() -> bool:
    """True when something under frontend/src is newer than the last build.

    Without this, editing the UI and re-running `python run.py` silently
    serves the PREVIOUS build -- the change appears to have done nothing,
    which is a genuinely baffling thing to debug. Cheap to check: one mtime
    walk against dist/index.html.
    """
    index = DIST / "index.html"
    if not index.is_file():
        return True
    src = FRONTEND / "src"
    if not src.is_dir():
        return False
    built = index.stat().st_mtime
    for path in src.rglob("*"):
        if path.is_file() and path.stat().st_mtime > built:
            return True
    # index.html and the config files are sources too, not just src/
    for name in ("index.html", "vite.config.ts", "package.json"):
        f = FRONTEND / name
        if f.is_file() and f.stat().st_mtime > built:
            return True
    return False


def open_when_ready(url: str, timeout: float = 90.0) -> None:
    """Open `url` in a browser once something is actually listening there.

    Replaces a fixed `Timer(1.0, ...)`, which raced the server: uvicorn's
    first bind (plus Mongo ping and session-monitor startup) regularly takes
    longer than a second, so the browser landed on a connection-refused page
    and the analyst had to reload by hand. Any HTTP response at all -- 200
    or 404 -- proves the port is up; only a connection error means keep
    waiting.
    """
    import threading
    import time
    import urllib.error
    import urllib.request
    import webbrowser

    def wait() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(url, timeout=2)
                break
            except urllib.error.HTTPError:
                break  # it answered, just not with a 200 -- that's up
            except Exception:
                time.sleep(0.4)
        else:
            return  # never came up; don't open a dead tab
        webbrowser.open(url)

    threading.Thread(target=wait, daemon=True).start()


def mongo_ok() -> tuple[bool, str]:
    try:
        from pymongo import MongoClient

        from backend.config.settings import settings

        MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=1500).server_info()
        return True, settings.mongo_uri
    except Exception as e:
        return False, f"{type(e).__name__} -- is mongod running?"


# ─────────────────────────────────── setup ────────────────────────────────── #


def run(cmd: list[str], cwd: Path | None = None, why: str = "") -> bool:
    if why:
        print(f"\n> {why}")
    try:
        subprocess.run(cmd, cwd=cwd, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  failed: {e}")
        return False


def setup(force: bool = False) -> bool:
    """Install everything needed, skipping whatever is already in place."""
    ok = True

    if force or missing_packages():
        ok &= run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                str(ROOT / "requirements.txt"),
            ],
            why="installing Python packages",
        )
    else:
        say(OK, "python packages already installed")

    # Chromium for Playwright: cheap to re-run, it no-ops when present
    if have("playwright"):
        run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            why="installing the Chromium build for Playwright",
        )

    if FRONTEND.is_dir() and (shutil.which(npm()) or shutil.which("npm.cmd")):
        if force or not (FRONTEND / "node_modules").is_dir():
            ok &= run(
                [npm(), "install", "--no-audit", "--no-fund"],
                cwd=FRONTEND,
                why="installing UI dependencies",
            )
        # Staleness, not just absence: a dist built before the last UI edit
        # would otherwise be served as-is, so the edit appears to have done
        # nothing. See ui_is_stale().
        if force or ui_is_stale():
            ok &= run([npm(), "run", "build"], cwd=FRONTEND, why="building the UI")
        else:
            say(OK, "UI already built and up to date")
    elif not FRONTEND.is_dir():
        say(WARN, "frontend directory not found", "running in backend-only API mode; the UI will not be served")
    else:
        say(WARN, "npm not found", "the API will run; the UI will not be served")

    for d in ("session", "logs"):
        (ROOT / d).mkdir(exist_ok=True)
    return ok


# ─────────────────────────────────── check ────────────────────────────────── #


def check() -> bool:
    ok = True
    print("prerequisites:")
    say(OK, "python", sys.version.split()[0])

    if gone := missing_packages():
        say(BAD, "python packages", f"missing {', '.join(gone)} -- run --setup")
        ok = False
    else:
        say(OK, "python packages")

    good, detail = mongo_ok()
    say(OK if good else BAD, "mongodb", detail)
    ok &= good

    say(
        OK if DIST.is_dir() else WARN,
        "frontend build",
        "" if DIST.is_dir() else "run --setup",
    )

    try:
        from backend.platforms import registry

        async def check_platforms():
            for p in registry.PLATFORMS.values():
                state = await registry.session_state(p)
                say(OK if state == "ready" else WARN, f"{p.name:<14}", state)

        print("\nplatforms:")
        asyncio.run(check_platforms())
        print(
            "\n  sessions are managed in the UI: pick a platform, then "
            "'Log in' or 'Paste'"
        )
    except Exception as e:
        say(BAD, "platform registry", str(e))
        ok = False
    return ok


# ──────────────────────────────────── run ─────────────────────────────────── #


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--setup",
        action="store_true",
        help="install dependencies, browsers and the UI, then exit",
    )
    ap.add_argument(
        "--check", action="store_true", help="verify prerequisites and exit"
    )
    ap.add_argument("--build", action="store_true", help="force a UI rebuild")
    ap.add_argument(
        "--dev", action="store_true", help="also run Vite with hot reload on :5173"
    )
    ap.add_argument(
        "--reload", action="store_true",
        help="restart the API when code changes "
             "(WINDOWS: breaks all browser scraping -- see the warning it prints)",
    )
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    a = ap.parse_args()

    if a.setup:
        print("Brand Intelligence -- setup")
        ok = setup(force=True)
        print()
        check()
        sys.exit(0 if ok else 1)

    if a.check:
        sys.exit(0 if check() else 1)

    # first run, or a missing piece: install without being asked.
    # `--dev` serves the UI from Vite, so a stale dist doesn't matter there
    # and rebuilding it would just cost startup time for nothing.
    needs_ui_build = FRONTEND.is_dir() and not a.dev and ui_is_stale()
    if missing_packages() or needs_ui_build or a.build:
        print("Brand Intelligence -- setup\n")
        setup(force=a.build)
        print()

    good, detail = mongo_ok()
    if not good:
        print(f"\n  MongoDB is not reachable ({detail}).")
        print(
            "  Jobs will run but nothing will be stored. Start mongod, "
            "or set MONGO_URI in .env\n"
        )

    vite = None
    if a.dev:
        print("starting Vite on :5173 (hot reload)")
        vite = subprocess.Popen([npm(), "run", "dev"], cwd=FRONTEND)

    try:
        import uvicorn

        api = f"http://127.0.0.1:{a.port}"
        # In --dev the UI is served by Vite (with hot reload) and proxies the
        # API through to this port, so the analyst wants :5173 -- opening the
        # API port there would serve the last BUILD instead, silently
        # bypassing the very hot-reload dev mode exists for.
        url = "http://127.0.0.1:5173" if a.dev else api
        print(f"\n  Brand Intelligence -> {url}")
        if a.dev:
            print(f"  API                -> {api}")
        print(f"  API docs           -> {api}/docs\n")

        # --reload on Windows runs the app in a supervised child whose event
        # loop cannot spawn subprocesses, and Playwright needs one for its
        # driver. The result is not a crash: the API serves happily and every
        # browser platform fails with an empty "NotImplementedError" while
        # YouTube and Telegram keep working, so it reads as a platform bug.
        # Warned about at the point the flag is actually typed.
        if a.reload and sys.platform == "win32":
            print(
                "  WARNING: --reload breaks browser scraping on Windows.\n"
                "           Facebook, Instagram, Twitter and TikTok will all fail with\n"
                "           an empty 'NotImplementedError'; YouTube/Telegram still work,\n"
                "           so it looks like a platform bug rather than this flag.\n"
                "           Use plain `python run.py` (add --dev for frontend hot-reload,\n"
                "           which is unaffected).\n",
                # flushed: stdout is block-buffered when this is not a tty
                # (nohup, a service wrapper, CI), and a warning that only
                # appears after the process dies is not a warning.
                flush=True,
            )

        open_when_ready(url)

        uvicorn.run(
            "backend.main:app",
            host="127.0.0.1",
            port=a.port,
            reload=a.reload,
            workers=1,
        )
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if vite is not None:
            vite.terminate()


if __name__ == "__main__":
    main()
