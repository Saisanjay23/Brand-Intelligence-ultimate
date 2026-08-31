"""Shared response models.

Every route declares a concrete `response_model`, so `/openapi.json`
carries real schemas rather than a bare `{}`. That is what makes this
usable from another language: a Java (or any other) client is generated
from the spec instead of hand-written against example payloads, and a
field that changes shape breaks the build rather than surfacing as a
null at runtime.

Two conventions this API keeps everywhere:

  * Errors are always `{"detail": "<human-readable reason>"}` with a real
    HTTP status (see main.py's DomainError handler). There is no second
    error envelope to special-case.
  * Times are ISO-8601 UTC strings. Dates that a downstream sheet renders
    (last post, created) stay `YYYY-MM-DD`, because that is what they are
    -- a date, not an instant.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """Lifecycle of a discovery or analysis job. Terminal states are
    `done`, `cancelled` and `failed`; a client should stop polling on any
    of the three."""

    queued = "queued"
    running = "running"
    done = "done"
    cancelled = "cancelled"
    failed = "failed"


TERMINAL_STATUSES = {JobStatus.done, JobStatus.cancelled, JobStatus.failed}


class Platform(str, Enum):
    facebook = "facebook"
    twitter = "twitter"
    instagram = "instagram"
    youtube = "youtube"
    telegram = "telegram"
    tiktok = "tiktok"


class JobAccepted(BaseModel):
    """Returned by both job-creating endpoints (HTTP 202). Poll the job
    URL until `status` is terminal."""

    job_id: str = Field(..., description="Poll this id for progress and results.")
    status: JobStatus
    poll_url: str = Field(..., description="Ready-made URL to poll for this job's state.")


class SkippedInput(BaseModel):
    """One input the job refused, and why. Reported rather than dropped
    silently, so a caller can tell 40 URLs becoming 37 from a bug."""

    value: str
    reason: str


class Health(BaseModel):
    status: str
    mongo: bool = Field(..., description="Discovery cannot persist and sessions cannot be read without it.")


class PlatformState(BaseModel):
    """Whether a platform can be swept/scraped right now."""

    platform: Platform
    name: str
    enabled: bool
    session_state: str = Field(
        ...,
        description="ready | missing | incomplete | exhausted -- anything but "
                    "`ready` means no usable credentials in the pool.",
    )
    can_discover: bool
    stability_note: str = ""


class PlatformStateList(BaseModel):
    items: list[PlatformState]


class SessionEntry(BaseModel):
    """One credential in a platform's pool.

    Deliberately carries NO secret: never the cookie values, the API key,
    the username, the password or the 2FA secret. A session cookie IS the
    credential, so the pool is write-only from the API's point of view --
    you can add and replace credentials, and see their health, but never
    read them back. `is_api_key` and `cookie_count` are the only hints at
    what a row holds.

    Every field is optional-with-a-default on purpose: an operational view
    must keep rendering even if a row is mid-write or predates a field.
    """

    model_config = {"extra": "allow"}

    id: str = ""
    identifier: str = Field("", description="Your own label for this account.")
    status: str = Field("", description="ready | expired | checkpointed | rate_limited | unreadable")
    available: bool = Field(False, description="Usable for a scrape right now.")
    in_use: bool = Field(False, description="A job is holding it at this moment.")
    is_api_key: bool = False
    cookie_count: int = 0
    use_count: int = 0
    consecutive_failures: int = Field(0, description="Drives the quarantine backoff ladder.")
    rate_limited_until: float = Field(0, description="Epoch seconds; 0 when not cooling off.")
    last_used: float = 0
    dead_since: float = Field(0, description="Epoch seconds since it was declared dead; 0 if alive.")
    purge_in_days: Optional[float] = Field(None, description="Auto-removal countdown once dead.")
    proxy_host: str = Field("", description="Host only -- never the proxy credentials.")
    last_error: str = ""
    last_checked: str = ""
    last_check_ok: Optional[bool] = None
    expires_at: float = Field(0, description="Epoch seconds the soonest required cookie lapses.")


class SessionPool(BaseModel):
    """Every credential held for one platform, and whether the platform can
    be scraped right now. `state` is the answer discovery and analysis both
    gate on: anything other than `ready` means no usable credential."""

    model_config = {"extra": "allow"}

    platform: str = ""
    name: str = ""
    state: str = Field("", description="ready | missing | incomplete | exhausted")
    kind: str = Field("", description="cookies | api-key | mtproto")
    can_login: bool = False
    cookie_count: int = 0
    pool_total: int = 0
    pool_ready: int = 0
    expires: str = ""
    message: str = ""
    last_verified: str = ""
    sessions: list[SessionEntry] = Field(default_factory=list)
    login: Optional[dict[str, Any]] = Field(None, description="State of an in-flight interactive login, when one is running.")


class SessionPoolList(BaseModel):
    items: list[SessionPool]


class CheckResult(BaseModel):
    """Outcome of a live credential probe."""

    ok: bool
    detail: str = ""
    conclusive: bool = Field(
        True,
        description="False means the check was inconclusive (a network blip, a "
                    "timeout) and the credential's status was deliberately left "
                    "untouched rather than recorded as dead.",
    )
    session: Optional[SessionPool] = Field(None, description="The refreshed pool, so you need not re-fetch.")


class Deleted(BaseModel):
    deleted: bool
    pool: Optional[SessionPool] = None


class CancelResult(BaseModel):
    cancelled: bool = Field(
        ...,
        description="False means there was nothing to cancel -- unknown job id, "
                    "or it had already finished. Not an error.",
    )


class LoginState(BaseModel):
    """State of an interactive browser login. It opens a real browser
    window on the SERVER, so it is only usable where someone can see that
    machine's screen; a remote caller should paste cookies or credentials
    instead."""

    model_config = {"extra": "allow"}

    platform: str = ""
    status: str = ""
    message: str = ""
    started: str = ""
    finished: str = ""


class TelegramLoginState(BaseModel):
    """Telegram's MTProto login is multi-step: request a code, submit it,
    then submit a 2FA password if the account demands one. `status` says
    which step comes next -- `code_sent`, `need_password`, or `saved`."""

    model_config = {"extra": "allow"}

    status: str
    message: str = ""
    phone: str = ""


class Stats(BaseModel):
    """Occupancy of the in-memory job stores. Analysis results in
    particular are memory-only, so this is where a caller can see them
    approaching eviction."""

    discovery: dict[str, Any]
    analysis_jobs: dict[str, Any]
    analysis_rows: dict[str, Any]
