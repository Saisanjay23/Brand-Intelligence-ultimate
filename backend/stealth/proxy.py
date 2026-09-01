"""Per-session proxy configuration, and the probe that proves one works.

Each pooled session can exit through its own IP instead of every session in
a 100-strong pool sharing the one machine's address, which is itself a
correlation signal across "different" accounts.

WHY THERE IS A PROBE
    A misconfigured proxy does not announce itself. The two failure modes
    that matter both look like success from the outside:

      * SILENT FALLBACK. Chromium does not implement SOCKS5
        username/password auth. Given socks5://user:pass@host:port it strips
        the credentials, the proxy refuses the unauthenticated connection,
        and Chrome then goes DIRECT -- so traffic leaves on the real IP
        while the UI happily shows a proxy attached. `probe_proxy` catches
        this by comparing the proxied exit IP against the direct one.

      * A WORKING PROXY THAT IS THE WRONG KIND. A datacenter IP is the
        single loudest network-layer signal there is; it does not matter how
        clean the browser fingerprint is if the address belongs to a hosting
        range. The probe reports that verdict rather than leaving it to be
        discovered when accounts start getting challenged.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional
from urllib.parse import urlparse

from backend.shared.logging import get_logger

log = get_logger("stealth.proxy")

# ip-api.com is the probe target because its free endpoint returns the two
# judgements that actually matter -- `hosting` (datacenter range) and
# `proxy` (known VPN/proxy exit) -- alongside geo. ipinfo.io is the fallback
# and carries org/timezone but no hosting flag, so a fallback result is
# reported as "unknown" rather than guessed at.
_INTEL_PRIMARY = (
    "http://ip-api.com/json/?fields=status,message,query,country,countryCode,"
    "city,timezone,isp,org,as,hosting,proxy,mobile"
)
_INTEL_FALLBACK = "https://ipinfo.io/json"


def build_proxy_config(proxy: Optional[dict]) -> Optional[dict]:
    """`proxy` is `{"server": ..., "username": ..., "password": ..., "timezone_id": ...}`
    (only `server` required); -> Playwright's `proxy` context-option shape, or
    None when there's nothing to configure."""
    if not proxy or not proxy.get("server"):
        return None
    return {
        "server": proxy["server"],
        **({"username": proxy["username"]} if proxy.get("username") else {}),
        **({"password": proxy["password"]} if proxy.get("password") else {}),
    }


def socks_auth_warning(proxy: Optional[dict]) -> Optional[str]:
    """The one proxy shape that fails DANGEROUSLY rather than loudly.

    Returns an explanation when `proxy` is a SOCKS proxy carrying
    credentials, else None. Chromium has never implemented SOCKS5
    user/password auth; it drops the credentials, the proxy refuses, and the
    browser silently falls back to a direct connection -- so the request
    goes out on the host's own IP while everything reports healthy.
    """
    if not proxy:
        return None
    scheme = urlparse(str(proxy.get("server") or "")).scheme.lower()
    if scheme.startswith("socks") and (proxy.get("username") or proxy.get("password")):
        return (
            "Chromium cannot authenticate to a SOCKS proxy. It drops the username and "
            "password, the proxy refuses the connection, and the browser then goes "
            "DIRECT -- traffic would leave on this machine's real IP while the session "
            "still looks proxied. Use an http:// proxy for credentialed access, or ask "
            "the provider for IP-whitelist access so no credentials are needed."
        )
    return None


async def _read_intel(ctx) -> dict:
    """Ask an IP-intelligence service what the world sees. Tries the primary
    (which reports datacenter/proxy flags) then the fallback (geo only)."""
    for url, source in ((_INTEL_PRIMARY, "ip-api"), (_INTEL_FALLBACK, "ipinfo")):
        try:
            res = await ctx.request.get(url, timeout=15000)
            if res.status != 200:
                continue
            data = json.loads(await res.text())
            if source == "ip-api" and data.get("status") == "success":
                return {
                    "ip": data.get("query"), "country": data.get("country"),
                    "country_code": data.get("countryCode"), "city": data.get("city"),
                    "timezone": data.get("timezone"), "isp": data.get("isp"),
                    "org": data.get("org") or data.get("as"),
                    "is_datacenter": bool(data.get("hosting")),
                    "is_known_proxy": bool(data.get("proxy")),
                    "is_mobile": bool(data.get("mobile")),
                    "source": source,
                }
            if source == "ipinfo" and data.get("ip"):
                return {
                    "ip": data.get("ip"), "country": data.get("country"),
                    "country_code": data.get("country"), "city": data.get("city"),
                    "timezone": data.get("timezone"), "isp": data.get("org"),
                    "org": data.get("org"),
                    # this service does not classify hosting ranges
                    "is_datacenter": None, "is_known_proxy": None, "is_mobile": None,
                    "source": source,
                }
        except Exception as e:
            log.debug(f"ip intel via {source} failed: {e}")
    return {}


async def _exit_ip(proxy_config: Optional[dict], headless: bool = True) -> dict:
    """Launch a browser (optionally through `proxy_config`) and report what
    an origin server would see. Own browser, not a pooled Session: this must
    be runnable before any session exists and must never touch a live one."""
    from backend.stealth.browser import async_playwright
    from backend.stealth.fingerprint import LAUNCH_ARGS, chrome_binary

    pw = await async_playwright().start()
    browser = ctx = None
    try:
        opts = {"headless": headless, "args": LAUNCH_ARGS}
        if binary := chrome_binary():
            opts["executable_path"] = binary
        browser = await pw.chromium.launch(**opts)
        ctx_opts: dict = {}
        if proxy_config:
            ctx_opts["proxy"] = proxy_config
        ctx = await browser.new_context(**ctx_opts)
        return await _read_intel(ctx)
    finally:
        for obj, meth in ((ctx, "close"), (browser, "close"), (pw, "stop")):
            if obj:
                try:
                    await getattr(obj, meth)()
                except Exception:
                    pass


async def probe_proxy(proxy: Optional[dict], *, compare_direct: bool = True) -> dict:
    """Prove a proxy actually works, and say what kind of address it is.

    Returns a dict shaped for the API/UI:
        ok               did traffic actually egress through the proxy
        exit_ip/country/city/timezone/isp/org
        is_datacenter    True is the loudest network-layer signal there is
        is_known_proxy   listed as a VPN/proxy exit
        is_mobile        a mobile carrier range, the quietest kind
        latency_ms       round trip for the probe request
        direct_ip        this machine's own address, when compared
        warnings[]       human-readable problems, most severe first
        error            set when the probe could not run at all
    """
    warnings: list[str] = []
    if warn := socks_auth_warning(proxy):
        warnings.append(warn)

    cfg = build_proxy_config(proxy)
    if not cfg:
        return {"ok": False, "error": "no proxy configured", "warnings": warnings}

    started = time.time()
    try:
        intel = await _exit_ip(cfg)
    except Exception as e:
        return {
            "ok": False, "error": f"{type(e).__name__}: {e}",
            "warnings": warnings + ["The browser could not start through this proxy."],
        }
    latency_ms = int((time.time() - started) * 1000)

    if not intel.get("ip"):
        return {
            "ok": False, "error": "no response through the proxy",
            "latency_ms": latency_ms,
            "warnings": warnings + [
                "Nothing came back through this proxy -- it is unreachable, refusing "
                "connections, or blocking the check. Do not assign it to a session."
            ],
        }

    direct_ip = None
    if compare_direct:
        try:
            direct = await _exit_ip(None)
            direct_ip = direct.get("ip")
        except Exception:
            direct_ip = None

    ok = True
    # The critical check. Same address through the proxy as without it means
    # the proxy is not carrying the traffic at all.
    if direct_ip and intel.get("ip") == direct_ip:
        ok = False
        warnings.insert(0, (
            f"TRAFFIC IS NOT BEING PROXIED. The exit address ({intel['ip']}) is this "
            "machine's own. Chromium fell back to a direct connection -- for a SOCKS "
            "proxy with credentials that is expected and unfixable, otherwise the "
            "proxy is refusing the connection. Anything run on this session would "
            "expose the real IP."
        ))

    if intel.get("is_datacenter"):
        warnings.append(
            f"This is a DATACENTER address ({intel.get('org') or intel.get('isp') or '?'}). "
            "It works, but hosting ranges are the single loudest network-layer signal to "
            "Meta -- a residential or mobile exit is worth far more than any further "
            "browser-fingerprint work."
        )
    if intel.get("is_known_proxy"):
        warnings.append("This address is on public VPN/proxy lists, which many platforms score against.")
    if intel.get("is_datacenter") is None:
        warnings.append(
            "Could not classify this address as datacenter or residential (the primary "
            "intelligence service did not answer); geo shown is from a fallback."
        )

    return {
        "ok": ok,
        "exit_ip": intel.get("ip"),
        "direct_ip": direct_ip,
        "country": intel.get("country"),
        "country_code": intel.get("country_code"),
        "city": intel.get("city"),
        "timezone": intel.get("timezone"),
        "isp": intel.get("isp"),
        "org": intel.get("org"),
        "is_datacenter": intel.get("is_datacenter"),
        "is_known_proxy": intel.get("is_known_proxy"),
        "is_mobile": intel.get("is_mobile"),
        "latency_ms": latency_ms,
        "intel_source": intel.get("source"),
        "warnings": warnings,
    }
