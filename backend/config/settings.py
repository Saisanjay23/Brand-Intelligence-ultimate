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

    # storage, one database, not one-per-platform (see docs/adr/0004)
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
    # Fallback evidence directory for the STANDALONE engine only
    # (`backend/engine/`, which deliberately runs with no MongoDB, see its
    # own README) when a caller doesn't pass `--evidence-dir`. The normal API
    # path (this process, `backend/main.py`) stores evidence screenshots in
    # Mongo GridFS instead, see database/repositories/evidence_repository.py
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
    # write_env() writes, see services/settings_service.py).
    alert_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)
    alert_from: str = "alerts@brand-intelligence.local"

    # pacing, the single most important knob for staying unremarkable
    request_timeout_sec: int = 45
    analysis_delay_sec: float = 2.5
    # Declared but NOT read anywhere, and the file it used to name
    # (analysis_service.py) no longer exists. Analysis concurrency is
    # per-platform rather than one global number -- see
    # _PLATFORM_CONCURRENCY and _PLATFORM_INTER_BATCH_DELAY in
    # analysis/runner.py, which are what actually decide how many of a
    # platform's URLs run at once and how long it waits between chunks.
    # Left declared so a .env that already carries it still loads.
    analysis_concurrency: int = 2
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
    # ONE KEYWORD AT A TIME, IN THE ORDER THEY WERE CONFIGURED. Overrides
    # both discovery_max_parallel_sessions and discovery_tab_concurrency:
    # each platform claims exactly one session, and that session sweeps
    # keyword 1 through every tab (facebook: people, then pages, then
    # groups) before keyword 2 begins.
    #
    # DEFAULT ON, because "one by one" is a correctness property here, not a
    # pacing preference. Three things depend on it:
    #
    #   readable progress   the progress chip names ONE keyword and ONE tab.
    #                       With parallel workers it named whichever
    #                       coroutine wrote to it last, so a sweep that was
    #                       working perfectly read as if it were skipping
    #                       keywords at random.
    #   result ordering     the discovery grid sorts by ascending _id, i.e.
    #                       insertion order, precisely so page 1 is what the
    #                       platform's own search returned first (see
    #                       profile_repository.list_profiles). Parallel
    #                       workers interleave their writes, which destroys
    #                       that ordering for every keyword involved.
    #   footprint           N simultaneous searches under N pooled accounts
    #                       is N times the concurrent load on one platform.
    #
    # FAILOVER IS NOT LOST. A single worker whose session dies mid-keyword
    # still re-queues that keyword and _sweep_platform claims a replacement
    # session for it on its next round (see _MAX_CLAIM_ROUNDS) -- recovery
    # is the round loop's job, never the worker count's.
    #
    # Turn OFF only when total sweep throughput matters more than any of the
    # above and the pool is genuinely healthy.
    discovery_sequential_keywords: bool = True
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

    # webhook callbacks (job completion push-back to the caller)
    webhook_timeout_sec: float = 10.0
    webhook_max_retries: int = 3
    # A `callback_url` is caller-supplied, and this process will POST a job
    # payload to it with retries, that is a server-side request forgery
    # primitive unless it is constrained. Empty means "any public host is
    # allowed" (still never a private/loopback/link-local address, see
    # services/webhook_service.py); set a comma-separated host list to
    # narrow it to exactly the callers you expect.
    webhook_allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # HMAC-SHA256 over the exact JSON body, sent as `X-BI-Signature`, so a
    # receiver can prove a callback really came from this engine and wasn't
    # forged by anything else that learned the URL. Empty disables signing.
    webhook_secret: str = ""

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
    # scored result, see docs/adr/0007-publish-hold.md
    publish_hold_minutes: float = 10.0

    # how many clients the always-on round-robin engine processes
    # concurrently, see services/round_robin_service.py. Too high and 400
    # clients' discovery jobs pile up behind the same handful of per-platform
    # session locks for no benefit; too low and a full lap over 400 clients
    # takes unnecessarily long.
    round_robin_slots: int = 4

    @field_validator("alert_emails", "webhook_allowed_hosts", "cors_allow_origins", mode="before")
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
