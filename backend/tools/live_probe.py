"""Read-only live probe: run the real discovery and analysis engines against
the real platform with a pooled session, and keep everything they saw.

WHY THIS EXISTS. The unit suite is pure logic by design (see pytest.ini): a
mock of Facebook's GraphQL response only proves the mock still matches
itself. Every accuracy claim this codebase makes about a platform -- which
key carries a post's owner, whether X still ships `default_profile_image`,
which Instagram endpoint answers -- was established against a live capture
and goes stale the day the platform changes. This is the tool that re-takes
that capture, on demand, and lays the engine's answer next to the raw bytes
it was derived from so the two can be compared by eye.

WHAT IT DOES, per platform:
  1. Claims ONE pooled session (released at the end, whatever happens),
     starts it, and confirms it is logged in. A dead session stops the
     probe for that platform -- it never retries on another account.
  2. Runs each keyword through the platform's own `Discovery.sweep`, with a
     small result cap, recording WHEN each streamed batch arrived (so "cards
     appear while the sweep runs" is measured, not assumed) and every
     platform response the context received.
  3. Runs each profile URL through the platform's own `Scraper.one`, saving
     the resulting Row, the evidence screenshot, and the raw responses.

It writes nothing to MongoDB except what the engines themselves persist
through the session pool (rotated cookies), and it never marks a session
failed: a probe is not a verdict on an account.

It is SLOW ON PURPOSE. The engines' own human pacing applies between every
profile, and the first sign of a checkpoint or login wall stops the
platform outright.

    python -m backend.tools.live_probe --platform facebook \\
        --keywords "Pranav Adani" --profiles https://www.facebook.com/x \\
        --out C:/tmp/probe --max-results 20 --tabs people
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from backend.config.settings import settings
from backend.platforms import registry
from backend.platforms.scan_options import DiscoveryOptions, ScanOptions
from backend.sessions import manager as sessions_engine

# Which responses are worth keeping, per platform: the ones every engine
# reads. Everything else a page loads (scripts, images, telemetry) is noise.
_KEEP = {
    "facebook": ("/api/graphql",),
    "instagram": ("/api/v1/", "/graphql", "/api/graphql", "wbloks"),
    "twitter": ("/i/api/graphql", "/graphql/"),
}
_MAX_BODY = 3_000_000


class Recorder:
    """Every relevant response a browser context receives, as JSONL."""

    def __init__(self, platform: str, path: Path):
        self.platform = platform
        self.path = path
        self.n = 0
        self._fh = path.open("a", encoding="utf-8")
        self._tasks: set[asyncio.Task] = set()

    def attach(self, ctx) -> None:
        ctx.on("response", lambda r: self._spawn(r))

    def _spawn(self, resp) -> None:
        t = asyncio.create_task(self._one(resp))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _one(self, resp) -> None:
        try:
            url = resp.url
            if not any(k in url for k in _KEEP.get(self.platform, ())):
                return
            body = await resp.text()
            post = ""
            try:
                post = (resp.request.post_data or "")[:4000]
            except Exception:
                pass
            self._fh.write(json.dumps({
                "t": time.time(), "url": url, "status": resp.status,
                "post": post, "body": body[:_MAX_BODY],
            }, ensure_ascii=False) + "\n")
            self.n += 1
        except Exception:
            return

    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._fh.close()


def _row_dict(row: Any) -> dict:
    d = asdict(row) if hasattr(row, "__dataclass_fields__") else dict(row)
    d.pop("screenshot_bytes", None)
    for derived in ("logo_yes", "active_yes", "risk", "priority"):
        try:
            d[derived] = getattr(row, derived)
        except Exception:
            pass
    return d


async def _discover(plat, session_item: dict, keywords: list[str], tabs: list[str],
                    max_results: int, out: Path, log: list[str]) -> bool:
    """Discovery half. Returns False when the session proved unusable."""
    options = DiscoveryOptions(
        concurrency=1, headful=not settings.headless,
        settle=settings.discovery_settle_sec, page_wait=settings.discovery_page_wait_sec,
        patience=settings.discovery_patience, max_results=max_results,
        max_seconds=240,
    )
    session = plat.session_cls()(options, session_item.get("cookies", []),
                                 session_id=str(session_item.get("id") or ""))
    session.on_cookies = sessions_engine.cookie_saver(plat.id, str(session_item.get("id") or ""))
    await session.start()
    rec = Recorder(plat.id, out / "discovery_responses.jsonl")
    rec.attach(session.ctx)
    try:
        if not await session.check_session():
            log.append(f"[{plat.id}] session is NOT logged in -- probe stopped")
            return False
        disc = plat.discoverer()(options, session.ctx)
        results = []
        for kw in keywords:
            for tab in tabs:
                started = time.time()
                batches: list[dict] = []

                async def on_progress(found, page, rows, _s=started, _b=batches):
                    _b.append({"after_s": round(time.time() - _s, 1), "found": found,
                               "rows": [r.url for r in rows]})

                try:
                    sweep = await disc.sweep(kw, tab, on_progress=on_progress)
                except TypeError:
                    sweep = await disc.sweep(kw, tab)
                streamed = {u for b in batches for u in b["rows"]}
                final = [h.url for h in sweep.hits]
                entry = {
                    "keyword": kw, "tab": tab, "stopped": sweep.stopped,
                    "complete": sweep.complete, "source": getattr(sweep, "source", ""),
                    "error": getattr(sweep, "error", ""), "seconds": round(sweep.seconds, 1),
                    "hits": len(final), "streamed_batches": batches,
                    "streamed_not_in_final": sorted(streamed - set(final)),
                    "first_card_after_s": batches[0]["after_s"] if batches else None,
                    "rows": [_row_dict(h) for h in sweep.hits],
                }
                results.append(entry)
                log.append(
                    f"[{plat.id}] {kw!r}/{tab}: {len(final)} hits, {sweep.stopped}, "
                    f"complete={sweep.complete}, source={entry['source']}, "
                    f"first card after {entry['first_card_after_s']}s, "
                    f"{len(batches)} streamed batch(es)"
                    + (f", ERROR {entry['error']}" if entry["error"] else ""))
                if sweep.stopped in ("checkpoint", "login", "rate_limited") or \
                        "checkpoint" in (entry["error"] or "").lower():
                    log.append(f"[{plat.id}] {sweep.stopped} -- probe stopped to protect the session")
                    (out / "discovery.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
                    return False
                await session.pause(1.0)
        (out / "discovery.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
        return True
    finally:
        await rec.close()
        try:
            await session.stop()
        except Exception:
            pass


async def _analyse(plat, session_item: dict, profiles: list[str], out: Path,
                   log: list[str], known_by_url: dict[str, dict]) -> None:
    options = ScanOptions(evidence=None, ephemeral_screenshot=True,
                          delay=settings.analysis_delay_sec, headful=not settings.headless,
                          warmup=True)
    scraper = plat.scraper()(options, session_item.get("cookies", []),
                             session_id=str(session_item.get("id") or ""))
    inner = getattr(scraper, "session", None)
    if inner is not None:
        inner.on_cookies = sessions_engine.cookie_saver(plat.id, str(session_item.get("id") or ""))
    await scraper.start()
    rec = Recorder(plat.id, out / "analysis_responses.jsonl")
    rec.attach(scraper.ctx)
    rows = []
    # The SAME mapping step the app runs after every visit -- it is where the
    # logo verdict is resolved across discovery, the visit and the pixels
    # (shared/logo_verdict.py), so probing the scraper alone would report a
    # verdict the app never shows.
    from backend.analysis.runner import AnalysisItem, AnalysisJob, AnalysisRunner
    mapper = AnalysisRunner()
    try:
        for i, url in enumerate(profiles, 1):
            t0 = time.time()
            known = known_by_url.get(url)
            row = await scraper.one(url, "", "", known=known)
            item = AnalysisItem(id=str(i), raw_url=url, url=scraper.normalize_url(url),
                                platform=plat.id, entity_id=row.profile_id)
            try:
                await mapper._populate(AnalysisJob(id="probe"), item, row, known)
            except Exception as e:                  # noqa: BLE001
                log.append(f"[{plat.id}] _populate failed: {type(e).__name__}: {e}")
            d = _row_dict(row)
            d["final_has_logo"] = item.has_logo
            d["final_risk"], d["final_priority"] = item.risk_score, item.priority
            d["known"] = known or {}
            d["seconds"] = round(time.time() - t0, 1)
            if row.screenshot_bytes:
                shot = out / f"shot_{i:02d}.png"
                shot.write_bytes(row.screenshot_bytes)
                d["screenshot_file"] = str(shot)
            rows.append(d)
            log.append(
                f"[{plat.id}] {i}/{len(profiles)} {url} -> {row.status} "
                f"name={row.profile_name!r} followers={row.followers} "
                f"loc={row.location!r} last_post={row.last_post_iso or '-'} "
                f"posts_seen={row.posts_seen or '-'} logo={item.has_logo} "
                f"src={row.src} notes={row.notes!r}")
            if row.status in ("CHECKPOINT", "LOGIN_REQUIRED"):
                log.append(f"[{plat.id}] {row.status} -- probe stopped to protect the session")
                break
            if i < len(profiles):
                await scraper.pause()
    finally:
        (out / "analysis.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
        await rec.close()
        try:
            await scraper.stop()
        except Exception:
            pass


async def _known_for(platform_id: str, urls: list[str]) -> dict[str, dict]:
    """What discovery already stored for each URL, shaped exactly like the
    seed "Analyse Validated" hands analysis (api/discovery.py), so a probe
    exercises the same merge the app does. Empty for a URL never discovered."""
    try:
        from backend.api.discovery import _seed_from_doc
        from backend.database.connection import db
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for u in urls:
        try:
            doc = await db()["profiles"].find_one(
                {"platform": platform_id, "$or": [{"url": u}, {"urls": u}]},
                sort=[("last_seen", -1)])
        except Exception:
            doc = None
        if doc:
            out[u] = _seed_from_doc(doc)
    return out


async def run(platform_id: str, keywords: list[str], profiles: list[str], tabs: list[str],
              max_results: int, out_root: Path) -> list[str]:
    plat = registry.get(platform_id)
    out = out_root / platform_id
    out.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    _, session_item = await sessions_engine.session_for_job(platform_id, wait_s=60)
    session_id = str(session_item.get("id") or "")
    log.append(f"[{platform_id}] probing with session {session_item.get('identifier')!r}")
    try:
        ok = True
        if keywords:
            ok = await _discover(plat, session_item, keywords, tabs or ["people"],
                                 max_results, out, log)
        if ok and profiles:
            await _analyse(plat, session_item, profiles, out, log,
                           await _known_for(platform_id, profiles))
    finally:
        sessions_engine.release_claim(platform_id, session_id)
        (out / "log.txt").write_text("\n".join(log), encoding="utf-8")
    return log


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--platform", required=True, choices=["facebook", "instagram", "twitter"])
    ap.add_argument("--keywords", default="", help="'|'-separated search terms")
    ap.add_argument("--profiles", default="", help="'|'-separated profile URLs")
    ap.add_argument("--tabs", default="people", help="comma-separated (facebook only)")
    ap.add_argument("--max-results", type=int, default=20)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    kws = [k.strip() for k in a.keywords.split("|") if k.strip()]
    profs = [p.strip() for p in a.profiles.split("|") if p.strip()]
    tabs = [t.strip() for t in a.tabs.split(",") if t.strip()]
    for line in asyncio.run(run(a.platform, kws, profs, tabs, a.max_results, Path(a.out))):
        print(line, flush=True)


if __name__ == "__main__":
    main()
