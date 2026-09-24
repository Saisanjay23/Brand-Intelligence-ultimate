"""The init script every browser context runs -- which is now NOTHING.

WHY IT IS EMPTY. Measured 2026-09-24, in the page's MAIN world (the world
Facebook's, Instagram's and X's own scripts run in -- see the note on
isolated worlds below), real Google Chrome driven by patchright with
`--disable-blink-features=AutomationControlled` and NO init script at all
presents an ordinary browser on every signal this module used to patch:

    navigator.webdriver        false (boolean, on Navigator.prototype)
    navigator.plugins          5 genuine entries, a real PluginArray
    window.chrome              {loadTimes, csi, app} -- and NO `runtime`,
                               exactly as on a normal https page
    permissions vs Notification 'prompt' / 'default', agreeing
    document.visibilityState   'visible' (headless keeps every page visible)
    navigator.connection       present, native
    navigator.getBattery       present, native
    mediaDevices / voices      native devices (blank labels, as without a
                               permission grant) / 24 native voices
    WebGL                      the real GPU, identical in a Web Worker
    Function.prototype.toString every API reports its own native name

The script that used to run here made every one of those WORSE, and did so
in ways a detection script finds in a single line:

  * `maskFunction` made every patched function describe itself as a GETTER:
    `Object.keys.toString()` returned "function get keys()", while
    `Function.prototype.toString.call(Object.keys)` returned a NAMELESS
    "function () { [native code] }". Real Chrome says "function keys()"
    both ways. Object.keys, getOwnPropertyNames, getOwnPropertyDescriptor,
    RTCPeerConnection, enumerateDevices and getVoices all carried it.
  * `permissions.query` was replaced with an unmasked function, so its
    JavaScript SOURCE CODE was readable -- the best-known automation tell
    there is (it is the puppeteer-stealth patch, fingerprinted for years).
  * `window.chrome.runtime` was created on every page. Real Chrome has no
    `chrome.runtime` on an ordinary site, so its presence says "patched".
  * `visibilityState`/`hidden` were defined as OWN properties of
    `document`; in real Chrome `document` owns only `location`.
  * A canvas "noise" layer drew onto the caller's canvas inside
    `toDataURL`, and WebGL claimed an "Apple M1" GPU under a Windows
    user-agent -- a device that cannot exist. (That block happened to fail
    on this build, which is the only reason it was not visible too.)

Each was added to hide something; with a genuine Chrome binary and a
patched driver there was nothing left to hide, so each only added a signal.
That is the conclusion this module's own earlier docstring reached about
canvas and WebGL ("less patching survives longer"); it applies to all of it.

THE ISOLATED-WORLD TRAP, which is how this went unnoticed. patchright runs
`page.evaluate` in an ISOLATED world by default. The init script runs in the
MAIN world. So every self-check this project ran -- including
`detection_probe.py` -- read a pristine browser and reported "0 FAIL"
while the platforms' own scripts saw every patch above. Any check of what a
platform sees must pass `isolated_context=False` (detection_probe does).

IF A SIGNAL EVER NEEDS FIXING AGAIN: fix it with a launch flag, a real
binary or the driver, never here -- and prove it with detection_probe in
the main world before and after. A JavaScript override is only acceptable
if the probe shows it leaving no trace, which none of the ones above did.
"""

from __future__ import annotations


def build_init_js() -> str:
    """The script added to every context. Empty on purpose -- see the
    module docstring for the measurement. Callers skip `add_init_script`
    when this is empty."""
    return ""
