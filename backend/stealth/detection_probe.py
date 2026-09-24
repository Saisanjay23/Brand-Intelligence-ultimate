"""Measures how detectable this tool's own browser session actually is.

WHY THIS EXISTS
    Every claim in stealth/ ("we look like real Chrome", "not spoofing canvas
    is the right call") is an assertion until something measures it. This is
    the measurement. It launches a REAL `Session` -- the same class every
    engine uses, with the same launch args, the same identity, the same init
    script -- and runs the probes an anti-bot vendor would run, then grades
    the result.

    Run it before and after any stealth change. A change that does not move a
    verdict here is not a stealth improvement, it is a guess.

WHAT IT IS NOT
    Not a CAPTCHA solver and not a "bypass" tool. It never contacts a target
    platform; every probe runs against about:blank inside our own browser.
    It answers one question -- "what would a fingerprinting script see?" --
    and the answer is equally useful for making the tool look normal or for
    proving it already does.

READING THE OUTPUT
    FAIL  a signal that identifies this as automation on its own
    WARN  a signal that is survivable alone but corroborates other signals,
          or one that depends on the host (no GPU on a server, etc.)
    PASS  indistinguishable from an ordinary Chrome install

    The CDP check is the one that matters most in 2026: `Runtime.enable`
    lives at the DevTools-protocol layer, BELOW where `add_init_script` can
    reach, so a FAIL there cannot be patched from JavaScript at all -- it
    needs a patched driver (patchright/rebrowser) or a non-CDP one.

USAGE
    python -m backend.stealth.detection_probe
    python -m backend.stealth.detection_probe --headful
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

# Each probe returns raw facts only. Interpretation lives in Python (GRADERS
# below) so the verdict logic is readable, testable, and changeable without
# touching a JS string -- and so a browser quirk can never silently rewrite a
# verdict.
PROBE_JS = r"""
() => {
  const out = {};

  // ---- CDP Runtime.enable leak -------------------------------------------
  // The 2026 signal. When the DevTools Runtime domain is enabled (vanilla
  // Playwright/Puppeteer do this), the inspector SERIALIZES objects passed to
  // console.*, which invokes property getters on them. Nothing in page-land
  // does that. So a getter that fires here proves a debugger is attached, and
  // no page-level patch can hide it -- the serialization happens outside the
  // page.
  out.cdp_runtime_leak = (() => {
    try {
      let fired = false;
      const err = new Error('probe');
      Object.defineProperty(err, 'stack', {
        configurable: true,
        get() { fired = true; return ''; },
      });
      console.debug(err);
      return fired;
    } catch (e) {
      return null;  // could not run the check; graded as unknown, never as pass
    }
  })();

  // ---- the classic automation flags --------------------------------------
  out.webdriver = navigator.webdriver;
  out.has_window_chrome = typeof window.chrome === 'object' && window.chrome !== null;
  out.chrome_runtime = !!(window.chrome && window.chrome.runtime);
  out.plugins_length = navigator.plugins ? navigator.plugins.length : -1;
  out.mime_types_length = navigator.mimeTypes ? navigator.mimeTypes.length : -1;
  out.languages = Array.from(navigator.languages || []);

  // Old headless reported 0 for both. Still checked because a 0 here is
  // conclusive on its own.
  out.outer_width = window.outerWidth;
  out.outer_height = window.outerHeight;

  // ---- CDP artifacts left on the document --------------------------------
  // Some drivers leave attributes/properties behind on document or window.
  out.cdp_artifacts = (() => {
    const found = [];
    const suspects = [
      'cdc_adoQpoasnfa76pfcZLmcfl_Array',      // chromedriver
      'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
      'cdc_adoQpoasnfa76pfcZLmcfl_Symbol',
      '__driver_evaluate', '__webdriver_evaluate', '__selenium_evaluate',
      '__fxdriver_evaluate', '__driver_unwrapped', '__webdriver_unwrapped',
      '__selenium_unwrapped', '__fxdriver_unwrapped',
      '__playwright', '__puppeteer', '_Selenium_IDE_Recorder',
    ];
    for (const key of suspects) {
      try {
        if (key in window || key in document) found.push(key);
      } catch (e) { /* cross-origin guard, ignore */ }
    }
    return found;
  })();

  // ---- UA vs Client Hints vs platform consistency ------------------------
  // Inconsistency here is the thing that actually gets profiles scored as
  // fake -- more reliably than any single spoofed value.
  out.user_agent = navigator.userAgent;
  out.platform = navigator.platform;
  out.ua_data = (() => {
    if (!navigator.userAgentData) return null;
    const d = navigator.userAgentData;
    return {
      mobile: d.mobile,
      platform: d.platform,
      brands: (d.brands || []).map(b => ({ brand: b.brand, version: b.version })),
    };
  })();

  // ---- hardware claims ----------------------------------------------------
  out.hardware_concurrency = navigator.hardwareConcurrency;
  out.device_memory = navigator.deviceMemory;
  out.screen = {
    width: screen.width, height: screen.height,
    avail_width: screen.availWidth, avail_height: screen.availHeight,
    color_depth: screen.colorDepth, pixel_depth: screen.pixelDepth,
  };
  out.device_pixel_ratio = window.devicePixelRatio;

  // ---- WebGL --------------------------------------------------------------
  // A software renderer (SwiftShader/llvmpipe) is a strong headless/VM tell:
  // an ordinary desktop Chrome reports a real GPU through ANGLE.
  out.webgl = (() => {
    try {
      const c = document.createElement('canvas');
      const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
      if (!gl) return { available: false };
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      return {
        available: true,
        vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
        renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
        version: gl.getParameter(gl.VERSION),
      };
    } catch (e) {
      return { available: false, error: String(e) };
    }
  })();

  // ---- canvas -------------------------------------------------------------
  // Only checks that canvas PRODUCES a stable, non-trivial result. A blocked
  // or randomized canvas is itself the anomaly -- see browser.py on why this
  // tool deliberately does not spoof it.
  out.canvas = (() => {
    try {
      const draw = () => {
        const c = document.createElement('canvas');
        c.width = 240; c.height = 60;
        const ctx = c.getContext('2d');
        ctx.textBaseline = 'top';
        ctx.font = '14px Arial';
        ctx.fillStyle = '#f60';
        ctx.fillRect(0, 0, 120, 24);
        ctx.fillStyle = '#069';
        ctx.fillText('probe ⚡ 🔍', 2, 15);
        return c.toDataURL();
      };
      const a = draw(), b = draw();
      let h = 0;
      for (let i = 0; i < a.length; i++) { h = ((h << 5) - h + a.charCodeAt(i)) | 0; }
      return { stable: a === b, hash: String(h), length: a.length };
    } catch (e) {
      return { stable: null, error: String(e) };
    }
  })();

  // ---- permissions mismatch ----------------------------------------------
  // The classic headless tell: Notification.permission says 'denied' while
  // the Permissions API reports 'prompt'. Real Chrome agrees with itself.
  out.permissions_promise = (async () => {
    try {
      if (!navigator.permissions || !window.Notification) return null;
      const st = await navigator.permissions.query({ name: 'notifications' });
      return { permissions_api: st.state, notification_api: Notification.permission };
    } catch (e) {
      return null;
    }
  })();

  // ---- native-code masking on our own overrides ---------------------------
  // navigator_spoofing.py overrides these; a stringified override that does
  // not read as [native code] is a giveaway.
  out.tostring_native = (() => {
    const probe = (obj, prop) => {
      try {
        const d = Object.getOwnPropertyDescriptor(obj, prop);
        const fn = d && (d.get || d.value);
        if (typeof fn !== 'function') return 'not-a-function';
        return /\[native code\]/.test(Function.prototype.toString.call(fn))
          ? 'native' : 'JS-VISIBLE';
      } catch (e) { return 'error'; }
    };
    return {
      webdriver: probe(Navigator.prototype, 'webdriver'),
      hardwareConcurrency: probe(Navigator.prototype, 'hardwareConcurrency'),
      deviceMemory: probe(Navigator.prototype, 'deviceMemory'),
    };
  })();

  // ---- native function identity -----------------------------------------
  // What a patched function gives away. Real Chrome renders every built-in
  // as "function <name>() { [native code] }" -- the SAME string whether read
  // through Function.prototype.toString or through fn.toString(). A Proxy
  // wrapper comes back nameless ("function () {...}") through the first; a
  // toString-intercepting mask comes back with the wrong shape through the
  // second; a plain JS replacement comes back as its own source. All three
  // were live in this tool's init script until 2026-09-24.
  out.native_identity = (() => {
    const fts = Function.prototype.toString;
    const targets = [
      ['Object.keys', () => Object.keys, 'keys'],
      ['Object.getOwnPropertyNames', () => Object.getOwnPropertyNames, 'getOwnPropertyNames'],
      ['Object.getOwnPropertyDescriptor', () => Object.getOwnPropertyDescriptor, 'getOwnPropertyDescriptor'],
      ['permissions.query', () => navigator.permissions && navigator.permissions.query, 'query'],
      ['canvas.toDataURL', () => HTMLCanvasElement.prototype.toDataURL, 'toDataURL'],
      ['webgl.getParameter', () => WebGLRenderingContext.prototype.getParameter, 'getParameter'],
      ['RTCPeerConnection', () => window.RTCPeerConnection, 'RTCPeerConnection'],
      ['mediaDevices.enumerateDevices', () => navigator.mediaDevices && navigator.mediaDevices.enumerateDevices, 'enumerateDevices'],
      ['speechSynthesis.getVoices', () => window.speechSynthesis && speechSynthesis.getVoices, 'getVoices'],
    ];
    const bad = [];
    for (const [label, get, name] of targets) {
      try {
        const fn = get();
        if (typeof fn !== 'function') continue;
        const expected = `function ${name}() { [native code] }`;
        const viaProto = fts.call(fn);
        const viaSelf = String(fn.toString());
        if (viaProto !== expected || viaSelf !== expected) {
          bad.push(`${label}: ${JSON.stringify(viaProto).slice(0, 60)} / ${JSON.stringify(viaSelf).slice(0, 60)}`);
        }
      } catch (e) { bad.push(`${label}: threw ${e}`); }
    }
    return bad;
  })();

  // ---- own properties that real Chrome does not have ----------------------
  out.document_own_props = Object.getOwnPropertyNames(document);
  out.navigator_own_props = Object.getOwnPropertyNames(navigator);

  // ---- canvas must not be mutated by reading it ---------------------------
  out.canvas_read_mutates = (() => {
    try {
      const c = document.createElement('canvas'); c.width = 40; c.height = 20;
      const x = c.getContext('2d'); x.fillStyle = '#123456'; x.fillRect(0, 0, 40, 20);
      const before = Array.from(x.getImageData(0, 0, 40, 20).data).join(',');
      c.toDataURL();
      const after = Array.from(x.getImageData(0, 0, 40, 20).data).join(',');
      return before !== after;
    } catch (e) { return null; }
  })();

  // ---- WebGL main thread vs Web Worker ------------------------------------
  // An init script cannot reach worker scope, so any GPU spoof shows up as
  // two different GPUs in one browser.
  out.webgl_worker_promise = new Promise((resolve) => {
    try {
      const src = "try{const g=new OffscreenCanvas(1,1).getContext('webgl');" +
        "const e=g.getExtension('WEBGL_debug_renderer_info');" +
        "postMessage(e?g.getParameter(e.UNMASKED_RENDERER_WEBGL):null)}catch(err){postMessage(null)}";
      const w = new Worker(URL.createObjectURL(new Blob([src])));
      w.onmessage = (m) => resolve(m.data);
      setTimeout(() => resolve(undefined), 3000);
    } catch (e) { resolve(undefined); }
  });

  return Promise.all([out.permissions_promise, out.webgl_worker_promise]).then(([perm, wgl]) => {
    out.permissions = perm;
    out.webgl_worker_renderer = wgl;
    delete out.permissions_promise;
    delete out.webgl_worker_promise;
    return out;
  });
}
"""


def _chrome_major_from_ua(ua: str) -> str | None:
    import re
    m = re.search(r"Chrome/(\d+)", ua or "")
    return m.group(1) if m else None


def grade(raw: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(verdict, signal, detail) for each probe. Verdict is PASS/WARN/FAIL."""
    r: list[tuple[str, str, str]] = []

    # --- CDP: reported as UNVERIFIED, never as PASS -----------------------
    # This check is kept for its positive signal only. Under a positive
    # control -- Runtime.enable explicitly sent over a CDP session -- the
    # console-getter technique every 2026 write-up describes did NOT fire on
    # Chrome 152. Seven variants were tried (console.log/debug/dir/table on
    # Error.stack getters, plain enumerable getters, Symbol.toPrimitive,
    # Error.prepareStackTrace); all were blind or fired unconditionally.
    # Chrome appears to generate object previews lazily, so merely enabling
    # the domain no longer invokes getters.
    #
    # So a False here means "this technique saw nothing", which is NOT the
    # same as "there is no leak" -- and reporting it as PASS would be the
    # harness lying about its own blind spot. For a real verdict use the
    # reference detector, which exercises techniques this cannot:
    #     https://bot-detector.rebrowser.net/
    leak = raw.get("cdp_runtime_leak")
    if leak is True:
        r.append((
            "FAIL", "CDP Runtime.enable",
            "a console getter fired -- a debugger is attached and visible. This is "
            "below the JS layer; add_init_script CANNOT fix it. Needs a patched "
            "driver (patchright/rebrowser) or a non-CDP one.",
        ))
    else:
        r.append((
            "WARN", "CDP Runtime.enable",
            "UNVERIFIED -- this technique is blind on current Chrome (proven with a "
            "positive control), so silence here is not evidence of cleanliness. "
            "Check bot-detector.rebrowser.net for the authoritative answer.",
        ))

    # --- classic flags -----------------------------------------------------
    # Real Chrome reports the BOOLEAN false -- the property exists on
    # Navigator.prototype. undefined (JS) arrives here as None and is NOT a
    # pass: it means the property was deleted, which no genuine Chrome does.
    # This grader said PASS for None until rebrowser-bot-detector disagreed
    # and turned out to be right, so the bar is now "exactly false".
    wd = raw.get("webdriver")
    if wd is False:
        r.append(("PASS", "navigator.webdriver", "false (boolean) -- matches real Chrome"))
    elif wd is None:
        r.append((
            "FAIL", "navigator.webdriver",
            "undefined -- the property was DELETED. Real Chrome defines it as false, "
            "so removing it is itself the tell. Let the "
            "--disable-blink-features=AutomationControlled flag report false natively.",
        ))
    else:
        r.append(("FAIL", "navigator.webdriver", f"{wd!r} -- announces automation outright"))

    if raw.get("has_window_chrome"):
        r.append(("PASS", "window.chrome", "present"))
    else:
        r.append(("FAIL", "window.chrome", "missing -- real desktop Chrome always defines it"))
    if raw.get("chrome_runtime"):
        r.append((
            "FAIL", "chrome.runtime",
            "present on an ordinary https page -- real Chrome only exposes it to "
            "extension-connectable pages, so its presence reads as a stealth patch",
        ))

    plugins = raw.get("plugins_length", -1)
    if plugins == 0:
        r.append(("FAIL", "navigator.plugins", "0 -- the classic headless signature"))
    elif plugins < 0:
        r.append(("WARN", "navigator.plugins", "unreadable"))
    else:
        r.append(("PASS", "navigator.plugins", f"{plugins} plugin(s)"))

    if not raw.get("languages"):
        r.append(("FAIL", "navigator.languages", "empty -- headless tell"))
    else:
        r.append(("PASS", "navigator.languages", ", ".join(raw["languages"])))

    if raw.get("outer_width") == 0 or raw.get("outer_height") == 0:
        r.append((
            "FAIL", "window.outerWidth/Height",
            f"{raw.get('outer_width')}x{raw.get('outer_height')} -- 0 means no real window (headless)",
        ))
    else:
        r.append(("PASS", "window.outerWidth/Height", f"{raw.get('outer_width')}x{raw.get('outer_height')}"))

    artifacts = raw.get("cdp_artifacts") or []
    if artifacts:
        r.append(("FAIL", "driver artifacts", f"found on window/document: {', '.join(artifacts)}"))
    else:
        r.append(("PASS", "driver artifacts", "none of the known driver globals present"))

    # --- consistency (the signal that actually scores profiles as fake) ----
    ua = raw.get("user_agent") or ""
    ua_major = _chrome_major_from_ua(ua)
    uad = raw.get("ua_data")
    if not uad:
        r.append(("WARN", "Client Hints", "navigator.userAgentData absent -- expected on modern Chrome"))
    else:
        brand_major = None
        for b in uad.get("brands", []):
            if "Chrome" in b.get("brand", "") and "Chromium" not in b.get("brand", ""):
                brand_major = (b.get("version") or "").split(".")[0]
        if ua_major and brand_major and ua_major != brand_major:
            r.append((
                "FAIL", "UA vs Client Hints",
                f"UA says Chrome {ua_major}, Client Hints say {brand_major} -- "
                "self-contradiction is the strongest fake-profile signal there is",
            ))
        else:
            r.append(("PASS", "UA vs Client Hints", f"both report Chrome {ua_major or '?'}"))

        ua_plat = (uad.get("platform") or "").lower()
        nav_plat = (raw.get("platform") or "").lower()
        plat_ok = (
            ("win" in ua_plat and "win" in nav_plat)
            or ("mac" in ua_plat and "mac" in nav_plat)
            or ("linux" in ua_plat and "linux" in nav_plat)
        )
        detail = f"UA-CH {uad.get('platform')!r} vs navigator.platform {raw.get('platform')!r}"
        r.append(("PASS" if plat_ok else "FAIL", "platform consistency", detail))

    if "Windows NT" in ua and "linux" in (raw.get("platform") or "").lower():
        r.append((
            "FAIL", "UA vs OS",
            "UA claims Windows but the JS runtime is Linux -- a Linux host advertising "
            "a Windows UA is exactly the mismatch fingerprinters score on",
        ))

    # --- hardware ----------------------------------------------------------
    hc, dm = raw.get("hardware_concurrency"), raw.get("device_memory")
    if hc in (None, 0):
        r.append(("WARN", "hardwareConcurrency", "absent"))
    elif hc <= 2:
        r.append(("WARN", "hardwareConcurrency", f"{hc} -- low enough to read as a cheap VPS"))
    else:
        r.append(("PASS", "hardwareConcurrency", str(hc)))
    r.append(
        ("PASS", "deviceMemory", f"{dm} GB") if dm else ("WARN", "deviceMemory", "absent")
    )

    scr = raw.get("screen") or {}
    if scr.get("width", 0) <= 0 or scr.get("color_depth", 0) < 24:
        r.append(("WARN", "screen", f"{scr} -- implausible for a desktop"))
    else:
        r.append(("PASS", "screen", f"{scr.get('width')}x{scr.get('height')} @{scr.get('color_depth')}bpp"))

    # --- WebGL -------------------------------------------------------------
    gl = raw.get("webgl") or {}
    if not gl.get("available"):
        r.append(("FAIL", "WebGL", "unavailable -- desktop Chrome always has it"))
    else:
        rend = (gl.get("renderer") or "") or ""
        soft = any(s in rend.lower() for s in ("swiftshader", "llvmpipe", "software", "mesa offscreen"))
        if soft:
            r.append((
                "WARN", "WebGL renderer",
                f"{rend!r} -- software rendering, a strong headless/VM tell. "
                "On a server this is expected; it is also one of the loudest signals you emit.",
            ))
        elif not rend:
            r.append(("WARN", "WebGL renderer", "masked/unavailable"))
        else:
            r.append(("PASS", "WebGL renderer", rend))

    # --- canvas ------------------------------------------------------------
    cv = raw.get("canvas") or {}
    if cv.get("stable") is True:
        r.append(("PASS", "canvas", f"stable across draws (hash {cv.get('hash')}) -- not randomized, correct"))
    elif cv.get("stable") is False:
        r.append((
            "FAIL", "canvas",
            "UNSTABLE across two identical draws -- something is randomizing it. "
            "Randomized canvas is itself the anomaly; see browser.py's docstring.",
        ))
    else:
        r.append(("WARN", "canvas", f"unreadable: {cv.get('error')}"))

    # --- permissions -------------------------------------------------------
    perm = raw.get("permissions")
    if perm:
        pa, na = perm.get("permissions_api"), perm.get("notification_api")
        if pa == "prompt" and na == "denied":
            r.append(("FAIL", "permissions", "Permissions API says 'prompt' while Notification says 'denied' -- classic headless mismatch"))
        else:
            r.append(("PASS", "permissions", f"Permissions API {pa!r} / Notification {na!r} agree"))
    else:
        r.append(("WARN", "permissions", "could not query"))

    # --- native function identity (see PROBE_JS) ---------------------------
    bad = raw.get("native_identity")
    if bad:
        r.append((
            "FAIL", "native function identity",
            f"{len(bad)} built-in(s) do not stringify like real Chrome: {'; '.join(bad)[:400]}",
        ))
    elif bad is not None:
        r.append(("PASS", "native function identity", "every checked built-in reads as its own native function"))

    extra_doc = [p for p in (raw.get("document_own_props") or []) if p != "location"]
    if extra_doc:
        r.append(("FAIL", "document own properties",
                  f"{', '.join(extra_doc)} -- real Chrome's document owns only 'location'"))
    elif raw.get("document_own_props") is not None:
        r.append(("PASS", "document own properties", "only 'location', as in real Chrome"))
    if raw.get("navigator_own_props"):
        r.append(("FAIL", "navigator own properties",
                  f"{', '.join(raw['navigator_own_props'])} -- real Chrome's navigator owns none"))

    if raw.get("canvas_read_mutates") is True:
        r.append(("FAIL", "canvas read", "toDataURL() changed the canvas pixels -- a noise patch is drawing on it"))

    main_gl = ((raw.get("webgl") or {}).get("renderer") or "")
    worker_gl = raw.get("webgl_worker_renderer")
    if worker_gl and main_gl and worker_gl != main_gl:
        r.append(("FAIL", "WebGL main vs worker",
                  f"main thread {main_gl!r} but a Web Worker sees {worker_gl!r} -- one browser, two GPUs"))
    elif worker_gl and main_gl:
        r.append(("PASS", "WebGL main vs worker", "same GPU in both"))
    ua = raw.get("user_agent") or ""
    if "Windows NT" in ua and "apple" in main_gl.lower():
        r.append(("FAIL", "WebGL vs UA", f"a Windows user-agent with an Apple GPU ({main_gl!r}) cannot exist"))

    # --- our own overrides -------------------------------------------------
    ts = raw.get("tostring_native") or {}
    leaky = [k for k, v in ts.items() if v == "JS-VISIBLE"]
    if leaky:
        r.append((
            "FAIL", "override masking",
            f"{', '.join(leaky)} stringify as JavaScript, not [native code] -- "
            "the override is visible to any script that checks",
        ))
    else:
        r.append(("PASS", "override masking", "overrides read as [native code]"))

    return r


async def probe(headful: bool = False, session_id: str = "probe") -> dict[str, Any]:
    """Launch a real Session with production config and run every probe."""
    from backend.platforms.scan_options import ScanOptions
    from backend.stealth.browser import Session

    opts = ScanOptions(headful=headful)
    session = Session(opts, cookies=[], session_id=session_id)
    await session.start()
    page = await session.ctx.new_page()
    try:
        # A fulfilled https:// origin rather than about:blank. Nothing leaves
        # the machine -- the route below answers the request locally -- but
        # the page still gets a SECURE CONTEXT, which about:blank is not.
        # That matters: navigator.userAgentData (Client Hints) is gated on a
        # secure context, so probing from about:blank reported it "absent"
        # and produced a warning about a browser that was in fact fine.
        await page.route(
            "**/*",
            lambda route: asyncio.ensure_future(
                route.fulfill(
                    status=200, content_type="text/html",
                    body="<html><head><title>probe</title></head><body>probe</body></html>",
                )
            ),
        )
        await page.goto("https://stealth-probe.local/", wait_until="domcontentloaded")
        # THE MAIN WORLD, where the platforms' own scripts run. patchright
        # evaluates in an ISOLATED world by default, which sees a pristine
        # browser no matter what the init script did -- this probe reported
        # "0 FAIL" for months while the main world carried every patch that
        # navigator_spoofing.py's docstring now lists. Vanilla playwright has
        # no such parameter and always evaluates in the main world.
        try:
            return await page.evaluate(PROBE_JS, isolated_context=False)
        except TypeError:
            return await page.evaluate(PROBE_JS)
    finally:
        try:
            await page.close()
        finally:
            await session.stop()


def render(raw: dict[str, Any], results: list[tuple[str, str, str]]) -> str:
    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    results = sorted(results, key=lambda t: order.get(t[0], 3))
    fails = sum(1 for v, _, _ in results if v == "FAIL")
    warns = sum(1 for v, _, _ in results if v == "WARN")
    passes = sum(1 for v, _, _ in results if v == "PASS")

    mark = {"FAIL": "[FAIL]", "WARN": "[WARN]", "PASS": "[ OK ]"}
    lines = [
        "",
        "=" * 78,
        "  STEALTH DETECTION PROBE -- what a fingerprinting script would see",
        "=" * 78,
        f"  {fails} FAIL   {warns} WARN   {passes} PASS",
        "-" * 78,
    ]
    for verdict, signal, detail in results:
        lines.append(f"{mark.get(verdict, '[????]')}  {signal}")
        for chunk in _wrap(detail, 68):
            lines.append(f"         {chunk}")
    lines += [
        "-" * 78,
        f"  UA: {raw.get('user_agent', '?')}",
        "=" * 78,
        "",
    ]
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, line, out = str(text).split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out or [""]


async def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--headful", action="store_true", help="run with a visible window")
    ap.add_argument("--json", action="store_true", help="dump raw probe output as JSON")
    ap.add_argument("--session-id", default="probe", help="seed for the device identity")
    args = ap.parse_args()

    raw = await probe(headful=args.headful, session_id=args.session_id)
    if args.json:
        print(json.dumps(raw, indent=2, default=str))
        return
    print(render(raw, grade(raw)))


if __name__ == "__main__":
    asyncio.run(_main())
