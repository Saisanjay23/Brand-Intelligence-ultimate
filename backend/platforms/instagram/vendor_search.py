"""Instagram keyword search through HikerAPI -- no Instagram account involved.

WHY THIS EXISTS. The pool's only Instagram account was suspended
(2026-11-17), and Instagram discovery (discovery_engine.py's
`Discovery.sweep`) needs a LOGGED-IN account for every keyword: it asks
Instagram's private mobile endpoint `/api/v1/users/search/` from inside that
account's own browser. No official Instagram/Meta API offers keyword ->
account search at all. HikerAPI is a paid vendor that runs Instagram's own
private account search (`fbsearch/accounts`) on its own infrastructure and
hands back the JSON, so a sweep can run with NONE of our accounts touched.

OPTIONAL AND OFF BY DEFAULT. It runs only when BOTH are set:

    INSTAGRAM_SEARCH_PROVIDER=hikerapi
    HIKERAPI_KEY=<access key from hikerapi.com/tokens>

With either missing, nothing here is ever called and Instagram discovery is
exactly what it was. When it IS on, discovery/runner.py claims no pooled
session, starts no browser, and skips the per-account ceilings
(shared/account_budget.py) for Instagram -- see `active_provider` and
registry.Platform.discovery_vendor. Analysis is untouched: it still visits
profiles on a logged-in account.

WHAT WAS VERIFIED FROM HIKERAPI'S OWN DOCS (read 2026-11-17), AND WHAT IS
ASSUMED. Nothing here has been run against the real service: no key exists
yet, and the suite tests against fixture payloads only.

  VERIFIED
    * Base URL https://api.hikerapi.com ("via Cloudflare"); the same API is
      also served at https://api.instagrapi.com ("without Cloudflare").
      -- OpenAPI `servers`, https://api.hikerapi.com/openapi.json
    * Auth: the access key in the `x-access-key` request header (OpenAPI
      securitySchemes.APIKeyHeader, type apiKey, in header).
      -- https://api.hikerapi.com/openapi.json and
         https://hiker-doc.readthedocs.io/en/latest/api-reference/v2/search/
    * Endpoint: GET /v2/fbsearch/accounts ("Search accounts"), query params
      `query` (required), `page_token` (optional; "pagination token from
      the previous response"), `safe_int` (optional; not used here). Also
      published: /v3/fbsearch/accounts (same params, same top-level shape);
      /v2/search/accounts (deprecated, "doesn't support paging");
      /v1/search/users (no paging, "will soon be deprecated").
      -- same two URLs
    * Response, top level: num_results, users, has_more, rank_token,
      clear_client_cache, page_token, status ("ok" in the example). Next
      page = send the response's `page_token` back as `page_token`.
    * users[] are FLAT user nodes carrying, among others: pk, pk_id, id,
      username, full_name, is_private, is_verified, profile_pic_url,
      profile_pic_id, has_anonymous_profile_picture, account_badges,
      latest_reel_media -- the same node Instagram's own mobile search
      returns, which is why discovery_engine.py's `iter_mobile_search_users`
      / `user_from_node` read it unchanged.
      -- https://hiker-doc.readthedocs.io/en/latest/api-reference/v2/search/
    * Status codes: 401 token missing, 402 payment required, 403 target
      private, 404 not found, 408 timeout, 410 endpoint retired, 422
      validation error, 429 too many requests, 500/503/504 server errors.
      Billed: 200, 400, 403, 404; everything else is free.
      -- https://hiker-doc.readthedocs.io/en/latest/api-reference/response-codes/
    * Rate limit: 1 request/s on the free trial, 15/s on paid plans; above it
      the answer is 429, and the advice is to pause 1-2 s and retry. Their
      own sample code treats 402 as "insufficient balance".
      -- https://hiker-doc.readthedocs.io/en/latest/guides/rate-limits/
    * Price: from $0.0006 per request. -- https://hikerapi.com

  ASSUMED (undocumented -- check these on the first live run)
    * How the LAST page is signalled. The docs only show a middle page
      (`has_more: true` plus a `page_token`). We stop on `has_more: false`,
      or on no `page_token` -- the rule the account sweep already uses for
      Instagram's own identical payload -- and on a page that adds nobody.
    * The error body shape. Not documented; read as `{"detail": ...}` (the
      422 is FastAPI's HTTPValidationError), falling back to the raw text.
    * That a WRONG key also answers 401 (the docs only say "token missing"),
      and what 403 means for a search -- "the account is private" cannot
      apply to a keyword, so it is read as the key being refused. Either
      way the error text carries the status and the vendor's own words.
    * That a keyword with no matches answers 200 with `users: []`, as
      Instagram itself does. A 404 is filed `provider-error`: loud, retried,
      owed -- never a silent "found nothing".
    * No Retry-After header is documented; the 429 backoff below is ours.

NEVER A SILENT ZERO. The one answer filed as "nothing there" is a 200 whose
body is well-formed, says `status: ok` (or says nothing) and carries an EMPTY
`users` list on the first page -> `no-results`. Everything else that is not a
usable answer ends the sweep on a stop code that resolves BROKEN, with error
text naming HikerAPI (shared/resilience.py::PROVIDER_STOPS):

    401/403 / no key          provider-auth   "HikerAPI key rejected (HTTP 401) ..."
    402                       quota           "HikerAPI balance exhausted ..."
    429 (after 2 retries)     rate_limited    "HikerAPI rate limit ..."
    any other status,         provider-error  "HikerAPI answered HTTP 503 ..."
    network failure, body
    not JSON, no `users`
    list, status != ok,
    users with no username

The first three would hit every later keyword identically (one key, one
balance), so the runner stops the platform on them and records the rest as
owed (shared/resilience.py::PROVIDER_FATAL_STOPS).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Awaitable, Callable, Optional

from backend.platforms.scan_options import cancelled
from backend.shared import diagnostics
from backend.shared.logging import get_logger
from backend.shared.schema_probe import SchemaProbe

log = get_logger("instagram")

# The provider name INSTAGRAM_SEARCH_PROVIDER selects, and the provenance
# stamped on every sweep and row it produces: `Sweep.source`, the
# `discovery-search:hikerapi` field marks, and the telemetry `source` column.
PROVIDER = "hikerapi"
SOURCE = PROVIDER

BASE_URL = "https://api.hikerapi.com"
# v2, not v3: v2 is the one whose user fields the docs list in full (v3's
# example elides them as "31 more fields"), and the one their MCP server
# ships in its core Search group. Same params, same top-level shape -- a
# one-line change if v2 is ever retired (that would arrive as HTTP 410,
# which is reported by name below, not swallowed).
SEARCH_PATH = "/v2/fbsearch/accounts"
AUTH_HEADER = "x-access-key"
# A generic client name -- nothing identifying this deployment. Sent because
# the default "Python-urllib/x" is the kind of user-agent a Cloudflare front
# tends to challenge, and a challenge would read as a 403.
USER_AGENT = "hikerapi-client/1.0 (python)"

REQUEST_TIMEOUT_S = 30.0
# Spacing between ANY two requests from this process. The free trial allows
# one per second, paid plans fifteen; one per second fits both, and it is the
# only pacing here -- there is no account of ours to protect, so none of the
# account-oriented gaps (stealth levels, the pacing governor, keyword pauses)
# apply to these calls.
MIN_REQUEST_INTERVAL_S = 1.0
# A 429 is retried this many times, after RATE_LIMIT_BACKOFF_S x attempt
# (the docs advise a 1-2 s pause). Unbilled, so retrying costs nothing.
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_BACKOFF_S = 2.0
# A search page is a few KB; this only bounds a pathological answer.
MAX_BODY_BYTES = 4 * 1024 * 1024

# Async (url, headers, timeout) -> (status, body text). Raises only when no
# answer was obtained at all. Swapped out by the tests.
Fetch = Callable[[str, dict, float], Awaitable[tuple[int, str]]]


# ------------------------------------------------------------ is it on?

_warned_no_key = False


def _configured() -> tuple[str, str]:
    """(provider, key) as configured, both stripped; ("", "") if settings
    cannot be read at all."""
    try:
        from backend.config.settings import settings
        provider = str(getattr(settings, "instagram_search_provider", "") or "").strip().lower()
        key = str(getattr(settings, "hikerapi_key", "") or "").strip()
        return provider, key
    except Exception:                                  # noqa: BLE001 - see active_provider
        return "", ""


def active_provider() -> str:
    """"hikerapi" when Instagram discovery's searches should go to the
    vendor, "" when they stay on a logged-in account (the default).

    Named by registry.Platform.discovery_vendor_path, which is how the
    discovery runner decides -- per platform, at the moment it starts one --
    whether Instagram needs a pooled session at all.

    THE PROVIDER WITHOUT A KEY IS OFF, AND SAYS SO ONCE. An operator who set
    the provider and forgot the key gets exactly today's behaviour -- the
    brief for this feature -- but not silently: one warning names the gap.
    """
    global _warned_no_key
    provider, key = _configured()
    if provider != PROVIDER:
        return ""
    if not key:
        if not _warned_no_key:
            _warned_no_key = True
            log.warning(
                "INSTAGRAM_SEARCH_PROVIDER=hikerapi but HIKERAPI_KEY is empty -- "
                "Instagram discovery stays on the logged-in account path")
        return ""
    return PROVIDER


def active() -> bool:
    return bool(active_provider())


# ------------------------------------------------------------ transport


def _get_sync(url: str, headers: dict, timeout: float) -> tuple[int, str]:
    """One blocking GET. Every HTTP status comes back as (status, body) --
    a status is an answer; only "no answer at all" raises. Plain urllib in a
    worker thread, the same choice YouTube's API client made (one dependency
    fewer than an async client, identical behaviour for an I/O-bound call)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.status), r.read(MAX_BODY_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_BODY_BYTES).decode("utf-8", "replace")
        except Exception:                              # noqa: BLE001 - detail only
            body = ""
        return int(e.code), body


async def _http_get(url: str, headers: dict, timeout: float) -> tuple[int, str]:
    return await asyncio.to_thread(_get_sync, url, headers, timeout)


# ------------------------------------------------------------ pacing

# The earliest moment the next request may leave, process-wide. Reserved
# before any await, so two sweeps interleaving on one event loop still queue
# behind each other rather than both reading the same free slot.
_next_slot = 0.0


async def _sleep(seconds: float, options: Any) -> None:
    """Sleep, sliced so a Stop lands within a fifth of a second."""
    deadline = time.monotonic() + max(0.0, seconds)
    while not cancelled(options):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.2, remaining))


async def _wait_turn(options: Any) -> None:
    global _next_slot
    now = time.monotonic()
    start = max(now, _next_slot)
    _next_slot = start + MIN_REQUEST_INTERVAL_S
    if start > now:
        await _sleep(start - now, options)


# ------------------------------------------------------------ one page


def _detail(body: str) -> str:
    """The vendor's own words about a refusal, short enough for a chip. The
    error body shape is undocumented: `detail`/`message`/`error` if it is a
    JSON object, the raw text otherwise."""
    text = (body or "").strip()
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        for k in ("detail", "message", "error", "exc_type"):
            if data.get(k):
                return " ".join(str(data[k]).split())[:200]
    return " ".join(text.split())[:200] or "(empty body)"


async def _fetch_page(
    keyword: str, page_token: Optional[str], key: str, options: Any, fetch: Fetch,
) -> tuple[Optional[dict], str, str]:
    """One search page -> (payload, "", "") on a usable 200, or
    (None, stop code, error text) on anything else. Only the ENVELOPE is
    judged here (status code, JSON, `status` field); whether the payload
    carries a `users` list is the caller's to check, because the caller
    holds the schema probe that should record it."""
    params = {"query": keyword}
    if page_token:
        params["page_token"] = str(page_token)
    url = f"{BASE_URL}{SEARCH_PATH}?{urllib.parse.urlencode(params)}"
    # The key travels in the header and NOWHERE else -- never in the URL
    # (which is logged) and never in an error text.
    headers = {AUTH_HEADER: key, "accept": "application/json", "user-agent": USER_AGENT}
    timeout = min(REQUEST_TIMEOUT_S, float(getattr(options, "timeout", 0) or REQUEST_TIMEOUT_S))

    retries = 0
    while True:
        await _wait_turn(options)
        if cancelled(options):
            return None, "cancelled", ""
        try:
            status, body = await fetch(url, headers, timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:                          # noqa: BLE001 - reported, below
            return None, "provider-error", (
                f"provider-error: HikerAPI request failed before any answer "
                f"({type(e).__name__}: {e})")
        if status == 429 and retries < RATE_LIMIT_RETRIES:
            retries += 1
            await _sleep(RATE_LIMIT_BACKOFF_S * retries, options)
            if cancelled(options):
                return None, "cancelled", ""
            continue
        break

    if status == 200:
        try:
            data = json.loads(body)
        except ValueError:
            return None, "provider-error", (
                f"provider-error: HikerAPI answered HTTP 200 with a body that is not "
                f"JSON: {' '.join((body or '').split())[:120]!r}")
        if not isinstance(data, dict):
            return None, "provider-error", (
                f"provider-error: HikerAPI answered HTTP 200 with JSON that is not an "
                f"object ({type(data).__name__})")
        # Instagram's own soft refusal, relayed: a 200 whose body says it
        # FAILED is not an empty search (the account sweep learned the same).
        if data.get("status") not in (None, "ok"):
            return None, "provider-error", (
                f"provider-error: HikerAPI answered HTTP 200 with status "
                f"{data.get('status')!r}: {_detail(body)}")
        return data, "", ""
    if status in (401, 403):
        return None, "provider-auth", (
            f"provider-auth: HikerAPI key rejected (HTTP {status}): {_detail(body)}")
    if status == 402:
        return None, "quota", (
            f"quota: HikerAPI balance exhausted (HTTP 402 Payment Required) -- top up "
            f"the HikerAPI account: {_detail(body)}")
    if status == 429:
        return None, "rate_limited", (
            f"rate_limited: HikerAPI rate limit (HTTP 429) still refusing after "
            f"{RATE_LIMIT_RETRIES} retries: {_detail(body)}")
    if status == 410:
        return None, "provider-error", (
            f"provider-error: HikerAPI has retired {SEARCH_PATH} (HTTP 410) -- switch "
            f"SEARCH_PATH in platforms/instagram/vendor_search.py to its replacement")
    return None, "provider-error", (
        f"provider-error: HikerAPI answered HTTP {status}: {_detail(body)}")


def _flat(node: Any) -> Any:
    """A users[] entry as a flat user node. The documented shape is already
    flat; a `{"user": {...}}` wrapper (Instagram's web topsearch shape) is
    unwrapped rather than read as a node with no username."""
    if isinstance(node, dict) and "username" not in node and isinstance(node.get("user"), dict):
        return node["user"]
    return node


# ------------------------------------------------------------ the sweep


async def sweep(
    keyword: str, tab: str, options: Any, on_progress=None, *, fetch: Optional[Fetch] = None,
):
    """One keyword through HikerAPI -> the same `Sweep` the account path
    returns (discovery_engine.py), with `source = "hikerapi"`.

    Pages through `page_token` up to `options.max_pages` (or the engine's
    DEFAULT_MAX_PAGES), and stops on whichever comes first, each with its
    own stop code: the caller's result cap (`cap:results`), the vendor
    saying there is no more (`exhausted`), a later page adding nobody
    (`no-new-results` -- the rule the account sweep uses, for the same
    reason: every extra request costs, here in money), a cursor that cannot
    advance or the page budget running out (`cap:pages`), the time budget
    (`cap:seconds`), a Stop (`cancelled`), or a failure (see the module
    docstring). Whatever earlier pages found is kept on every ending.
    """
    # Imported here, not at module top: discovery_engine imports this module,
    # and the registry loads `active_provider` without needing the engine.
    from backend.platforms.instagram.discovery_engine import (
        DEFAULT_MAX_PAGES, K_USERS, Sweep, iter_mobile_search_users, user_to_row,
    )

    fetch = fetch or _http_get
    out = Sweep(keyword=keyword, tab=tab, source=SOURCE)
    probe = SchemaProbe()
    started = time.time()
    by_name: dict = {}
    notified = 0
    max_results = int(getattr(options, "max_results", 0) or 0)

    async def _emit() -> None:
        """Stream what is new to the runner -- the account sweep's own
        contract: capped like the final trim, a broken callback never aborts
        the sweep."""
        nonlocal notified
        if on_progress is None:
            return
        users = list(by_name.values())
        if max_results:
            users = users[:max_results]
        fresh = users[notified:]
        if not fresh:
            return
        notified = len(users)
        rows = [user_to_row(u, keyword, source=SOURCE) for u in fresh if u.url]
        if not rows:
            return
        try:
            res = on_progress(len(users), out.pages, rows)
            if asyncio.iscoroutine(res):
                await res
        except Exception:                               # noqa: BLE001 - by contract
            pass

    try:
        key = _configured()[1]
        if not key:
            out.stopped = "provider-auth"
            out.error = "provider-auth: HikerAPI key is not set (HIKERAPI_KEY is empty)"
        else:
            max_pages = int(getattr(options, "max_pages", 0) or 0) or DEFAULT_MAX_PAGES
            max_seconds = float(getattr(options, "max_seconds", 0) or 0)
            page_token: Optional[str] = None
            for page_no in (range(max_pages) if max_pages > 0 else itertools.count()):
                if cancelled(options):
                    out.stopped = "cancelled"
                    break
                if max_seconds and time.time() - started >= max_seconds:
                    out.stopped = "cap:seconds"
                    break

                data, stop, err = await _fetch_page(keyword, page_token, key, options, fetch)
                if stop:
                    out.stopped, out.error = stop, err
                    break

                users = data.get("users")
                if not isinstance(users, list):
                    # The container renamed or gone: a rename, not an empty
                    # search (an empty search still sends `users: []`).
                    probe.miss(K_USERS)
                    out.stopped = "provider-error"
                    out.error = (
                        f"provider-error: HikerAPI answered HTTP 200 without a `users` "
                        f"list (keys: {', '.join(sorted(map(str, data))[:12]) or 'none'})")
                    break

                new_users = 0
                parsed = 0
                for user in iter_mobile_search_users({"users": [_flat(n) for n in users]}, probe):
                    parsed += 1
                    if user.username.lower() not in by_name:
                        by_name[user.username.lower()] = user
                        new_users += 1
                if users and not parsed:
                    # Entries came back and not one had a username: the node
                    # shape moved under us. Reading it as "nobody matched"
                    # would be exactly the silent zero this must not produce.
                    out.stopped = "provider-error"
                    out.error = (
                        f"provider-error: HikerAPI returned {len(users)} result(s) but none "
                        f"carried a username -- the users[] shape has changed")
                    break

                if new_users:
                    out.pages += 1
                    await _emit()

                if max_results and len(by_name) >= max_results:
                    out.stopped = "cap:results"
                    break

                if page_no == 0 and not users:
                    # The one clean zero: a well-formed first page, status ok,
                    # and an empty list. A real and common answer.
                    out.stopped = "no-results"
                    out.complete = True
                    break

                has_more = data.get("has_more")
                prev_token = page_token
                page_token = data.get("page_token") or data.get("next_max_id") or None

                if has_more is False or (not has_more and not page_token):
                    out.stopped = "exhausted"
                    out.complete = True
                    break
                if not page_token or page_token == prev_token:
                    # More promised, but no way to ask for it: what was
                    # fetched stands, and the sweep says it did not reach
                    # the end rather than re-sending the same paid request.
                    out.stopped = "cap:pages"
                    break
                if page_no > 0 and new_users == 0:
                    out.stopped = "no-new-results"
                    out.complete = True
                    break

            if not out.stopped:
                # The page budget ran out with more on offer: truncated,
                # never "reached the end".
                out.stopped = "cap:pages"

        out.users = list(by_name.values())
        out.hits = [user_to_row(u, keyword, source=SOURCE) for u in out.users if u.url]
        if max_results:
            out.hits = out.hits[:max_results]
    except Exception as e:                              # noqa: BLE001 - reported on the Sweep
        out.users = list(by_name.values())
        out.hits = [user_to_row(u, keyword, source=SOURCE) for u in out.users if u.url]
        out.stopped = "error"
        out.error = f"HikerAPI search raised {type(e).__name__}: {e}"
        out.where = diagnostics.where(e)
    finally:
        out.seconds = time.time() - started
        out.schema = probe.report()
        if broken := probe.broken_keys():
            log.warning(
                f"instagram/hikerapi: {keyword!r} -- attribute(s) no longer served where "
                f"we look for them: {', '.join(broken)}. HikerAPI's search payload shape "
                f"has changed; see vendor_search.py / iter_mobile_search_users")

    if out.error:
        log.warning(f"instagram/hikerapi: search for {keyword!r} ended {out.stopped} -- {out.error}")
    return out
