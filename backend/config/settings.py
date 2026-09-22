"""Environment-tiered runtime settings.

One Settings object, read once from the process environment. `.env` is
loaded only in development; staging/production get real process
environment variables from the deployment platform.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent.parent

Environment = Literal["development", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: Environment = "development"

    # server
    host: str = "127.0.0.1"
    port: int = 8000

    # storage, one database, not one-per-platform -- see
    # database/connection.py's module docstring for why
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "brand_intelligence"

    # paths
    log_path: Path = ROOT / "logs"
    # NOT the cookie pools (those live in Mongo's `sessions` collection).
    # This is (a) where the one-off migration script reads the legacy
    # per-platform cookie JSON files from, and (b) where a non-cookie
    # session blob that genuinely has to be a file lives on, namely
    # Telethon's own MTProto `.session` sqlite file for Telegram.
    #
    # KNOWN LIMITATION, accepted rather than engineered around: two
    # instances of this process pointed at the same session_blob_path (e.g.
    # `python run.py` and `python run.py --port 8001` on one machine) share
    # this one SQLite file. Telethon opens it exclusively, so both trying to
    # run Telegram discovery/analysis at once raises `OperationalError:
    # database is locked` for whichever loses the race. Not a concern in
    # production (one background worker); a local dev running two instances
    # at once should expect Telegram specifically to fight over this file.
    # Every other platform is unaffected (Mongo-backed, not file-backed).
    session_blob_path: Path = ROOT / "session"
    # The legacy pre-GridFS evidence directory: where evidence screenshots
    # lived on disk before they moved into Mongo. Not written to anymore --
    # read only by the one-off migration script
    # (database/migrations/migrate_evidence_to_gridfs.py) that moves
    # whatever is still here into GridFS. The normal API path (this process,
    # `backend/main.py`) stores evidence screenshots in Mongo GridFS
    # directly, see database/repositories/evidence_repository.py
    #, specifically so captures live alongside the rest of this engine's
    # data rather than as files on one server's disk.
    evidence_path: Path = ROOT / "evidence"
    # Capturing evidence forces images to load in the scraping browser
    # (a blocked-asset context screenshots blank), which costs bandwidth and
    # makes each profile visit a little slower/heavier. On by default because
    # an un-evidenced finding is not much use downstream; set false to trade
    # the proof for speed.
    capture_evidence: bool = True
    # Auto-delete evidence screenshots older than N days from Mongo GridFS
    # (evidence.files and evidence.chunks) to prevent unlimited storage growth.
    evidence_retention_days: int = 7

    # alerts
    smtp_host: str = ""
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_pass: str = ""
    # NoDecode: these come from a comma-separated env string ("a@b.com,
    # c@d.com"), not JSON, without it, pydantic-settings tries to
    # json.loads() the raw env value for any list-typed field BEFORE
    # _split_csv below ever runs, and crashes the whole process on startup
    # the moment .env has a plain non-JSON value here (exactly what
    # write_env(), below in this file, writes).
    alert_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)
    alert_from: str = "alerts@brand-intelligence.local"

    # pacing, the single most important knob for staying unremarkable
    request_timeout_sec: int = 45
    analysis_delay_sec: float = 2.5
    # HOW MANY BROWSER WORKERS ANALYSIS MAY HOLD OPEN AT ONCE, PROCESS-WIDE.
    # A job scrapes every platform concurrently and each platform now works
    # its URLs through as many pooled sessions as it can safely claim (see
    # _MAX_SESSIONS_PER_PLATFORM in analysis/runner.py), so this is the only
    # number that bounds the total -- one worker is one Chromium context plus
    # its own Playwright driver plus its tabs, and per-platform caps cannot
    # see each other. Lower it on a small host; raising it costs RAM, not
    # stealth (each worker is a separate account on a separate egress).
    analysis_max_browser_workers: int = 4
    discovery_concurrency: int = 2
    # HOW MANY POOLED SESSIONS ONE PLATFORM'S SWEEP MAY CLAIM AT ONCE. Mirrors
    # analysis_max_browser_workers' reasoning one level up: a two-account
    # pool splits a keyword list across two workers instead of one session
    # working through all of it alone, and a session dying mid-sweep hands
    # its unfinished keywords to whichever session is still healthy (see
    # _MAX_SESSIONS_PER_PLATFORM in discovery/runner.py). A separate knob
    # from analysis's, not the same constant -- discovery and analysis have
    # different risk profiles (a sweep is many short requests, an analysis
    # visit is one long one) and different pools may be sized differently.
    discovery_max_parallel_sessions: int = 3
    # ONE KEYWORD AT A TIME, IN THE ORDER THEY WERE CONFIGURED. When on,
    # this overrides both discovery_max_parallel_sessions and
    # discovery_tab_concurrency: each platform claims exactly one session,
    # and that session sweeps keyword 1 through every tab (facebook: people,
    # then pages, then groups) before keyword 2 begins.
    #
    # DEFAULT OFF SINCE 2026-09-22 -- keyword sharding is now the normal
    # path. The engine for it was always here (see discovery/runner.py's
    # `_keyword_worker` and its shared queue, with failover between workers
    # and its own test module); what kept this switched on was the first of
    # the three reasons below, and that one has been fixed rather than
    # traded away. The other two are now accepted costs, deliberately:
    #
    #   readable progress   FIXED. The progress record carried ONE
    #                       `current_keyword` field, so with three workers
    #                       the chip named whichever coroutine wrote last
    #                       and a perfectly healthy sweep read as if it were
    #                       skipping keywords at random. There is now one
    #                       telemetry slot per worker (PlatformSweep.
    #                       note_worker), so the UI shows all three accounts
    #                       and what each is on, truthfully. No guessing.
    #
    #   result ordering     ACCEPTED. The discovery grid sorts by ascending
    #                       _id, i.e. insertion order, so page 1 is what the
    #                       platform's own search returned first (see
    #                       profile_repository.list_profiles). Parallel
    #                       workers interleave their writes, so a sharded
    #                       sweep's grid mixes keywords instead of finishing
    #                       one before starting the next. The sort was left
    #                       alone on purpose: changing it would reorder every
    #                       discovery view for every analyst, sharded or not,
    #                       which is a far larger change than the one being
    #                       made here.
    #
    #   footprint           ACCEPTED, AND THE ONE TO WATCH. N simultaneous
    #                       searches under N pooled accounts is N times the
    #                       concurrent load on one platform -- and this tool
    #                       has NO per-session proxy support, so all N leave
    #                       from ONE IP. Per-account load does drop (six
    #                       searches each instead of eighteen), but three
    #                       accounts visibly active together from one address
    #                       is a correlation signal that one account working
    #                       alone does not produce. Both of those are true at
    #                       once; the first is the reason this is on, the
    #                       second is the reason to turn it off again if
    #                       accounts start getting challenged.
    #
    # FAILOVER IS NOT LOST EITHER WAY. A worker whose session dies mid-
    # keyword re-queues that keyword; with several workers a surviving one
    # picks it up immediately, and with one, _sweep_platform claims a
    # replacement on its next round (see _MAX_CLAIM_ROUNDS).
    #
    # TURN BACK ON (DISCOVERY_SEQUENTIAL_KEYWORDS=true) if accounts start
    # drawing checkpoints, or whenever strict keyword-by-keyword ordering
    # matters more than sweep time.
    discovery_sequential_keywords: bool = False
    # THE MEDIAN GAP BETWEEN ONE KEYWORD SWEEP AND THE NEXT, in seconds, on
    # the same session. Discovery had no such gap at all: keywords ran
    # back-to-back, so a 15-keyword client hit Facebook with 45 searches
    # (15 x its three tabs) with nothing between them but page-load time.
    # That request cadence is the most machine-like thing a pooled account
    # does, and Facebook is where accounts actually get disabled.
    #
    # Spent through the session's own `pause()` (stealth/human.py), so this
    # is a MEDIAN with jitter, fatigue and circadian shaping on top -- not a
    # fixed sleep. Same meaning and same mechanism as analysis_delay_sec,
    # which is why the number matches it. Per-platform gaps are scaled off
    # this by _PLATFORM_INTER_KEYWORD_DELAY in discovery/runner.py.
    discovery_delay_sec: float = 2.5
    # Bound on browser workers open at once across the WHOLE process for
    # discovery, for the identical reason analysis_max_browser_workers
    # exists: _run sweeps every ready platform concurrently, so a
    # per-platform cap alone cannot see the total number of Chromium
    # contexts a job might try to hold at once.
    discovery_max_browser_workers: int = 4
    # How many of ONE platform's tabs may sweep at once for a single keyword.
    # Facebook is the only platform with more than one (people/pages/groups);
    # everywhere else this is inert. 1 restores strictly sequential tabs.
    #
    # DEFAULT 1 -- SEQUENTIAL -- ON PURPOSE. The implementation is complete
    # and correct (per-tab caps, staggered starts) and raising this to 3 does
    # work. It is off because the measured trade is bad:
    #
    #   concurrent tabs   ~1.3x faster, and THREE simultaneous searches on
    #                     one account. The tabs also contend for the single
    #                     browser context (people 5.8s -> 9.2s), so the gain
    #                     is smaller than the parallelism suggests, and
    #                     run-to-run variance (18.1s vs 26.1s) is wider than
    #                     the gain itself.
    #   skipping the      1.68x faster (42.2s -> 25.1s) AND one fewer
    #   login probe       authenticated request per job.
    #
    # The second is strictly better on both axes, so the first is not worth
    # buying a new request pattern on a live account for -- especially with
    # a pool that can be down to a single healthy session, where all three
    # searches land on the same account.
    #
    # Raise it to 3 when throughput matters more than footprint and the pool
    # is healthy. Nothing else needs to change.
    discovery_tab_concurrency: int = 1

    # DISCOVERY TIMING, EXPOSED SO IT CAN BE MEASURED RATHER THAN GUESSED.
    #
    # All three are CEILINGS on waiting for a real signal, not fixed sleeps:
    # a fast response returns immediately and never touches them, so lowering
    # one does not speed up a healthy sweep at all -- it only truncates a slow
    # one. That asymmetry is why these defaults match the values the engine
    # was deliberately raised TO (from 12/6/3) after live measurement: the
    # thing being bought was not latency, it was not missing real responses.
    #
    # They are settings rather than constants so the trade can be re-tested
    # against real sweeps without a code change. Watch `complete` and
    # `stopped` on each sweep's telemetry (GET /discovery/jobs/{id} ->
    # history[]): if lowering these raises the share of sweeps that end
    # "stalled" instead of "exhausted"/"end-of-serp", the speed was paid for
    # in results, not won.
    discovery_settle_sec: float = 20.0      # first results render
    discovery_page_wait_sec: float = 10.0   # one more results page
    discovery_patience: int = 5             # empty scrolls before "stalled"
    # 15 min, not 5: live-timed against a genuinely broad keyword ("nasa")
    # on Facebook, People and Pages were STILL finding ~1 new result/sec at
    # the old 300s ceiling, no sign of slowing -- that config was cutting
    # off well before those tabs would ever have reached a real end. A
    # keyword that already finishes fast is unaffected either way: the
    # sweep loop breaks the instant the platform reports no more results
    # (see facebook/discovery_engine.py's `state.has_next` check, which
    # fires every scroll regardless of this ceiling) -- confirmed live,
    # Facebook's Groups tab exhausted in 119s against a 420s test ceiling.
    # This is a safety backstop for the keywords that DON'T have a fast
    # natural end, not a target duration for the ones that do.
    discovery_max_seconds: float = 900
    headless: bool = True

    # Kill switch for backend/adaptive/'s last-resort schema healer. Off
    # returns analysis to exactly its pre-healer behaviour (standard tiers
    # only) with no code change or restart-time flag needed -- flip this the
    # moment the healer is suspected of costing more than it recovers.
    adaptive_healer_enabled: bool = True

    # --- persistent browser profiles & self-healing login ----------------
    # KEEP EACH ACCOUNT'S BROWSER PROFILE ON DISK between runs, under
    # session_blob_path/browser_profiles/<platform>_<session_id>.
    #
    # What that buys is everything a cookie jar cannot carry: localStorage,
    # IndexedDB, the device keys Meta and X mint per browser, and the
    # machine id Chrome itself writes. Replaying cookies into a blank
    # profile every run is a logged-in account arriving on a brand-new
    # device every single time, which is exactly the shape a "new device"
    # challenge exists to catch.
    #
    # Off returns the browser to a fresh ephemeral context per run, which is
    # what it did before this existed. Nothing else changes: cookies are
    # still injected from the database either way.
    # THE TIMEZONE EVERY BROWSER CONTEXT CLAIMS, as an IANA id.
    #
    # It has to match the country this host actually leaves from. Blank
    # means Asia/Kolkata, which is right while the traffic is Indian and
    # WRONG the moment the host runs behind a VPN -- every session then
    # announces Kolkata from a foreign exit, and a claimed timezone that
    # disagrees with the egress IP is a stronger tell than any of the
    # things this module spends its other knobs hiding.
    #
    # ipinfo.io/json reports the `timezone` of whatever address you are
    # actually leaving from; that is the value to put here.
    #
    # One host has one egress, so this is not per-platform and cannot be.
    # See stealth/timezone.py.
    browser_timezone_id: str = ""
    browser_persistent_profiles: bool = True
    # Delete a profile directory that has not been opened in this many days.
    # A Chrome profile is not small, and one per pooled account per platform
    # accumulates quietly. 0 disables the sweep.
    browser_profile_retention_days: int = 30
    # LOOK AT THE HOME FEED BEFORE GOING TO WORK. A real session starts
    # somewhere ordinary; ours used to open cold on a search URL or a
    # stranger's profile. One short home-feed view per session, at most once
    # every browser_warmup_min_gap_minutes, and never on a health check.
    # Failure is always non-fatal -- warming is a courtesy, not a step.
    browser_warmup_enabled: bool = True
    browser_warmup_min_gap_minutes: float = 15.0
    # RE-LOG IN BY ITSELF when a pooled account with stored credentials is
    # found expired or checkpointed. Off leaves the existing quarantine and
    # alert path exactly as it is.
    session_auto_relogin: bool = True
    # The floor between two automated login attempts on ONE account. This is
    # the single most important number here: a re-login that keeps failing
    # is a scripted password attempt every half hour on an account the
    # platform is already unhappy with, which is how an account stops being
    # recoverable at all. Deliberately hours, not minutes.
    session_relogin_cooldown_minutes: float = 180.0
    # Consecutive automated attempts before this account stops trying and
    # waits for a person. Self-healing that cannot heal must stop.
    session_relogin_max_attempts: int = 3

    # --- curl_cffi accelerator (shared/fast_http.py) ---------------------
    # THREE SWITCHES, NOT ONE, because the three uses carry very different
    # risk. All default on; each can be turned off on its own without a code
    # change, and turning one off returns that path to exactly the behaviour
    # it had before fast_http existed.
    #
    # The master switch. Off means shared/fast_http.py answers "unavailable"
    # to everything, so avatars go back to aiohttp, session checks go back to
    # the browser, and analysis pre-flights nothing.
    fast_http_enabled: bool = True
    # Let a cookie health check try one impersonated HTTPS request before
    # launching Chromium. Only a POSITIVE "still logged in" short-circuits;
    # anything else, including a positive-looking failure, still runs the
    # browser check (see fast_http.SessionVerdict). Turn this off if session
    # health verdicts are ever suspected of disagreeing with reality.
    session_fast_check_enabled: bool = True
    # Let an analysis batch ask the platform whether a profile URL still
    # exists before a browser worker is given it. Only a 404/410 from a
    # platform that reliably serves one settles the row; every other answer
    # is handed to the browser exactly as before. Turn this off to make every
    # pasted URL get a real visit no matter what HTTP says.
    analysis_preflight_enabled: bool = True

    # Browser-facing CORS. "*" is the historical default (this engine was
    # designed to sit behind a trusted internal path), but it is also what
    # lets any page in any browser on the network drive the whole API.
    # Set an explicit origin list in staging/production.
    # Default is localhost dev ports only; override with CORS_ALLOW_ORIGINS
    # as a comma-separated list for staging/production deployments.
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:5173",   # Vite dev server
            "http://localhost:4173",   # Vite preview
            "http://localhost:8000",   # same-origin (bundled dist)
            "http://127.0.0.1:8000",
        ]
    )

    # --- session quarantine backoff -------------------------------------
    # One 429 used to burn a session for a full day. Quarantine now grows
    # with CONSECUTIVE failures and resets the moment a session works
    # again, so a single bad afternoon can't take the whole pool offline.
    session_backoff_minutes: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [15, 60, 360, 1440])

    # a freshly analysed profile is held back from the default (client-
    # facing) view for this long, so an analyst who approved a false
    # positive has a window to revert it before anyone downstream sees the
    # scored result
    publish_hold_minutes: float = 10.0

    @field_validator("alert_emails", "cors_allow_origins", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @field_validator("session_backoff_minutes", mode="before")
    @classmethod
    def _split_csv_ints(cls, v):
        if isinstance(v, str):
            return [int(p.strip()) for p in v.split(",") if p.strip()]
        return v

    @field_validator("mongo_uri", mode="before")
    @classmethod
    def _clean_mongo_uri(cls, v):
        if isinstance(v, str) and not v.strip():
            return "mongodb://localhost:27017"
        return v or "mongodb://localhost:27017"

    @field_validator("mongo_db_name", mode="before")
    @classmethod
    def _clean_mongo_db_name(cls, v):
        if isinstance(v, str) and not v.strip():
            return "brand_intelligence"
        return v or "brand_intelligence"


def write_env(key: str, value: str) -> None:
    """Persist one KEY=VALUE pair to .env and the live process environment.

    Used when a credential (a YouTube API key, Telegram's api_id/api_hash)
    arrives through the API instead of a hand-edited file, so it survives a
    restart without anyone touching .env directly. Development-tier
    convenience only, staging/production rotate credentials through their
    own secrets store, not a file this process writes to itself.

    Raises RuntimeError in staging/production to prevent accidental
    filesystem mutation in containerised environments where the .env file
    is ephemeral and per-replica writes would not be shared across instances.
    """
    current_env = os.environ.get("ENV", "development").lower()
    if current_env in ("production", "staging"):
        raise RuntimeError(
            f"write_env({key!r}) is disabled in {current_env}. "
            "Inject credentials via your platform secrets store "
            "(environment variables, Vault, AWS Secrets Manager, etc.)."
        )
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, seen = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[key] = value


def _env_file_for(env: str) -> Optional[Path]:
    if env != "development":
        return None
    for candidate in (ROOT / ".env", ROOT / "backend" / ".env"):
        if candidate.exists():
            return candidate
    return None


@lru_cache
def get_settings() -> Settings:
    env = os.environ.get("ENV", "development")
    env_file = _env_file_for(env)
    if env_file:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    return Settings()


settings = get_settings()
