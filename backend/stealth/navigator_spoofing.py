"""The init script every browser context runs, hardened against inspection.

No canvas / WebGL / audio fingerprint spoofing, and no playwright-stealth.
Those patches are detectable in themselves: overriding those prototypes
reads as a privacy extension, and Facebook responds by never finishing its
render (an infinite spinner), Twitter with "Something went wrong", and
Instagram by not hydrating at all.

Instead, this script applies strictly necessary defenses with native function
masking so property descriptor audits (`toString()` checks) return standard
`[native code]` signatures, polyfills desktop `window.chrome`, stabilizes
headless notification permissions, locks hardware stats to realistic counts,
scrubs CDP runtime variables, masks WebRTC local host IPs, and synthesizes
realistic media devices and power/network telemetry.
"""

from __future__ import annotations


def build_init_js() -> str:
    """Builds a robust initialization script with native function masking and multi-tier shielding."""
    return f"""
(() => {{
    try {{
        const maskFunction = (fn, name) => {{
            const rep = `function get ${{name}}() {{ [native code] }}`;
            return new Proxy(fn, {{
                get: (target, prop) => prop === 'toString' ? () => rep : target[prop]
            }});
        }};

        // navigator.webdriver is deliberately NOT touched here.
        //
        // This used to delete the property and redefine it to return
        // undefined. That is what a real browser never looks like: Chrome
        // defines `webdriver` as an accessor on Navigator.PROTOTYPE that
        // returns the boolean false, so `typeof navigator.webdriver` is
        // "boolean" and `'webdriver' in navigator` is true. Deleting it
        // makes the typeof "undefined", which no genuine Chrome produces --
        // the concealment was itself the signal. rebrowser-bot-detector
        // flagged exactly this and said so plainly: "This property
        // shouldn't be undefined. You might have it deleted manually."
        //
        // `--disable-blink-features=AutomationControlled` (fingerprint.py's
        // LAUNCH_ARGS) already makes Chrome report the honest, correct
        // false. Measured with the flag on and this override removed:
        //   value=false  type=boolean  inNavigator=true
        //   onProto=true onInstance=false
        // which is indistinguishable from an ordinary install. Adding JS on
        // top of a browser that is already correct could only make it wrong,
        // so nothing goes here -- see browser.py's docstring on why less
        // patching survives longer.

        Object.defineProperty(document, 'visibilityState', {{
            get: maskFunction(() => 'visible', 'visibilityState'),
            configurable: true
        }});

        Object.defineProperty(document, 'hidden', {{
            get: maskFunction(() => false, 'hidden'),
            configurable: true
        }});

        // EVERY navigator override below targets Navigator.PROTOTYPE, never
        // the `navigator` instance. In a real browser navigator carries no
        // own properties at all -- Object.getOwnPropertyNames(navigator)
        // returns [] and every field resolves through the prototype chain.
        // Defining on the instance leaves fingerprints that are trivial to
        // enumerate: this used to report
        //   ['hardwareConcurrency', 'deviceMemory', 'getBattery']
        // which rebrowser-bot-detector flags directly ("should return empty
        // array") -- three names that say "someone patched this" without any
        // need to inspect their values.
        // hardwareConcurrency and deviceMemory are deliberately NOT spoofed.
        //
        // They used to be, seeded per session, to stop a low-spec VPS from
        // advertising 2 vCPU. That backfired, measurably: `add_init_script`
        // runs in the page's main world ONLY -- it does not reach Web Worker
        // scope -- so a worker kept reporting the machine's real hardware
        // while the main thread reported the spoof. Measured here:
        //     main thread  hc=8   dm=8    (spoofed)
        //     web worker   hc=12  dm=32   (real)
        // Any script can spawn a worker and compare those in a few lines,
        // and a self-contradicting browser is a far louder signal than a
        // modest core count -- plenty of real users are on 4-core laptops,
        // none are on a machine that reports two different CPUs at once.
        // deviceandbrowserinfo.com flags exactly this as
        // `hasInconsistentWorkerValues`.
        //
        // Spoofing consistently is not an option: the worker global cannot
        // be patched from an init script. So the honest values ship, which
        // are also usually the better ones. If a deployment host genuinely
        // has implausible specs, fix the HOST -- that is an ops decision,
        // not something to paper over per-session.

        if (!window.chrome || !window.chrome.runtime) {{
            window.chrome = window.chrome || {{}};
            window.chrome.runtime = window.chrome.runtime || {{}};
            window.chrome.app = window.chrome.app || {{
                isInstalled: false,
                InstallState: {{
                    DISABLED: 'disabled',
                    INSTALLED: 'installed',
                    NOT_INSTALLED: 'not_installed'
                }},
                getDetails: maskFunction(() => null, 'getDetails'),
                getIsInstalled: maskFunction(() => false, 'getIsInstalled'),
                runningState: maskFunction(() => 'cannot_run', 'runningState')
            }};
            window.chrome.csi = window.chrome.csi || maskFunction(() => ({{}}), 'csi');
            window.chrome.loadTimes = window.chrome.loadTimes || maskFunction(() => ({{}}), 'loadTimes');
        }}

        if (window.navigator && window.navigator.permissions) {{
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = function query(parameters) {{
                if (parameters && parameters.name === 'notifications') {{
                    return Promise.resolve({{ state: Notification.permission, onchange: null }});
                }}
                return originalQuery.apply(this, arguments);
            }};
        }}

        if (!navigator.plugins || navigator.plugins.length === 0) {{
            const makePluginArray = () => {{
                const arr = [{{
                    description: "Portable Document Format",
                    filename: "internal-pdf-viewer",
                    name: "Chrome PDF Plugin"
                }}];
                arr.item = (i) => arr[i];
                arr.namedItem = (n) => arr[0];
                arr.refresh = () => {{}};
                return arr;
            }};
            Object.defineProperty(Navigator.prototype, 'plugins', {{
                get: maskFunction(() => makePluginArray(), 'plugins'),
                configurable: true
            }});
            Object.defineProperty(Navigator.prototype, 'mimeTypes', {{
                get: maskFunction(() => {{
                    const arr = [{{
                        description: "Portable Document Format",
                        suffixes: "pdf",
                        type: "application/x-google-chrome-pdf"
                    }}];
                    arr.item = (i) => arr[i];
                    arr.namedItem = (n) => arr[0];
                    return arr;
                }}, 'mimeTypes'),
                configurable: true
            }});
        }}

        // --- 1. CDP & Playwright Variable Scrubbing ---
        try {{
            const blockRegex = /^(cdc_|__playwright|__puppeteer|__webdriver|\\$chrome)/i;
            const cleanProps = (props) => props.filter(p => !blockRegex.test(p));
            const origGetOwnPropertyNames = Object.getOwnPropertyNames;
            Object.defineProperty(Object, 'getOwnPropertyNames', {{
                value: maskFunction(function(obj) {{
                    const result = origGetOwnPropertyNames(obj);
                    if (obj === window || obj === document) {{
                        return cleanProps(result);
                    }}
                    return result;
                }}, 'getOwnPropertyNames'),
                configurable: true,
                writable: true
            }});
            const origKeys = Object.keys;
            Object.defineProperty(Object, 'keys', {{
                value: maskFunction(function(obj) {{
                    const result = origKeys(obj);
                    if (obj === window || obj === document) {{
                        return cleanProps(result);
                    }}
                    return result;
                }}, 'keys'),
                configurable: true,
                writable: true
            }});
            const origGetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;
            Object.defineProperty(Object, 'getOwnPropertyDescriptor', {{
                value: maskFunction(function(obj, prop) {{
                    if ((obj === window || obj === document) && typeof prop === 'string' && blockRegex.test(prop)) {{
                        return undefined;
                    }}
                    return origGetOwnPropertyDescriptor(obj, prop);
                }}, 'getOwnPropertyDescriptor'),
                configurable: true,
                writable: true
            }});
            for (const prop of origGetOwnPropertyNames(window)) {{
                if (blockRegex.test(prop)) {{
                    try {{ delete window[prop]; }} catch (e) {{}}
                }}
            }}
        }} catch (e) {{}}

        // --- 2. Mocking Media Devices & Speech Synthesis ---
        try {{
            if (navigator.mediaDevices) {{
                const origEnumerate = navigator.mediaDevices.enumerateDevices;
                navigator.mediaDevices.enumerateDevices = maskFunction(() => {{
                    return origEnumerate.call(navigator.mediaDevices).then(devices => {{
                        if (devices && devices.length > 0) return devices;
                        return [
                            {{ deviceId: 'default', kind: 'audiooutput', label: 'Default Audio Output', groupId: 'default_audio_group' }},
                            {{ deviceId: 'internal_mic', kind: 'audioinput', label: 'Internal Microphone', groupId: 'default_audio_group' }},
                            {{ deviceId: 'internal_webcam', kind: 'videoinput', label: 'Integrated Camera', groupId: 'default_video_group' }}
                        ];
                    }});
                }}, 'enumerateDevices');
            }}
            if (window.speechSynthesis && typeof window.speechSynthesis.getVoices === 'function') {{
                const origGetVoices = window.speechSynthesis.getVoices;
                window.speechSynthesis.getVoices = maskFunction(function() {{
                    const voices = origGetVoices.apply(this, arguments);
                    if (voices && voices.length > 0) return voices;
                    return [
                        {{ default: true, lang: 'en-US', localService: true, name: 'Microsoft David Desktop - English (United States)', voiceURI: 'Microsoft David Desktop - English (United States)' }},
                        {{ default: false, lang: 'en-US', localService: true, name: 'Microsoft Zira Desktop - English (United States)', voiceURI: 'Microsoft Zira Desktop - English (United States)' }}
                    ];
                }}, 'getVoices');
            }}
        }} catch (e) {{}}

        // --- 3. WebRTC Local STUN/ICE Candidate Masking ---
        try {{
            const wrapRTC = (RTCConstructor) => {{
                if (!RTCConstructor) return undefined;
                return new Proxy(RTCConstructor, {{
                    construct: (target, args) => {{
                        const pc = new target(...args);
                        const origAddIceCandidate = pc.addIceCandidate;
                        if (origAddIceCandidate) {{
                            pc.addIceCandidate = maskFunction(function(candidate) {{
                                if (candidate && candidate.candidate && /((192\\.168\\.)|(10\\.)|(172\\.1[6-9]\\.)|(172\\.2[0-9]\\.)|(172\\.3[0-1]\\.))/.test(candidate.candidate)) {{
                                    return Promise.resolve();
                                }}
                                return origAddIceCandidate.apply(this, arguments);
                            }}, 'addIceCandidate');
                        }}
                        return pc;
                    }}
                }});
            }};
            if (window.RTCPeerConnection) window.RTCPeerConnection = wrapRTC(window.RTCPeerConnection);
            if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = wrapRTC(window.webkitRTCPeerConnection);
            if (window.mozRTCPeerConnection) window.mozRTCPeerConnection = wrapRTC(window.mozRTCPeerConnection);
        }} catch (e) {{}}

        // --- 4. Battery & Broadband Network Information APIs ---
        try {{
            if (!navigator.connection) {{
                Object.defineProperty(Navigator.prototype, 'connection', {{
                    get: maskFunction(() => ({{
                        effectiveType: '4g',
                        rtt: 50,
                        downlink: 10,
                        saveData: false,
                        onchange: null
                    }}), 'connection'),
                    configurable: true
                }});
            }}
            if (!navigator.getBattery) {{
                Object.defineProperty(Navigator.prototype, 'getBattery', {{
                    value: maskFunction(() => Promise.resolve({{
                        charging: true,
                        chargingTime: 0,
                        dischargingTime: Infinity,
                        level: 1.0,
                        onchargingchange: null,
                        onchargingtimechange: null,
                        ondischargingtimechange: null,
                        onlevelchange: null
                    }}), 'getBattery'),
                    configurable: true,
                    writable: true
                }});
            }}
        }} catch (e) {{}}

    }} catch (err) {{
        // Swallow unhandled setup errors to ensure page rendering never fails
    }}

    // --- 5. Canvas & WebGL Noise Injection ---
    try {{
        const addNoise = (canvas) => {{
            try {{
                const ctx = canvas.getContext('2d');
                if (ctx) {{
                    const shift = {{ r: Math.random() * 2 - 1, g: Math.random() * 2 - 1, b: Math.random() * 2 - 1 }};
                    ctx.fillStyle = `rgba(${{Math.abs(shift.r)}}, ${{Math.abs(shift.g)}}, ${{Math.abs(shift.b)}}, 0.01)`;
                    ctx.fillRect(0, 0, canvas.width || 1, canvas.height || 1);
                }}
            }} catch(e) {{}}
        }};
        
        if (HTMLCanvasElement.prototype.toDataURL) {{
            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = maskFunction(function() {{
                addNoise(this);
                return origToDataURL.apply(this, arguments);
            }}, 'toDataURL');
        }}
        if (HTMLCanvasElement.prototype.toBlob) {{
            const origToBlob = HTMLCanvasElement.prototype.toBlob;
            HTMLCanvasElement.prototype.toBlob = maskFunction(function() {{
                addNoise(this);
                return origToBlob.apply(this, arguments);
            }}, 'toBlob');
        }}
        
        if (window.WebGLRenderingContext) {{
            const origGetParameter = window.WebGLRenderingContext.prototype.getParameter;
            window.WebGLRenderingContext.prototype.getParameter = maskFunction(function(param) {{
                const res = origGetParameter.apply(this, arguments);
                if (param === 37445) return 'Google Inc. (Apple)';
                if (param === 37446) return 'ANGLE (Apple, Apple M1, OpenGL 4.1)';
                return res;
            }}, 'getParameter');
        }}
        if (window.WebGL2RenderingContext) {{
            const origGetParameter2 = window.WebGL2RenderingContext.prototype.getParameter;
            window.WebGL2RenderingContext.prototype.getParameter = maskFunction(function(param) {{
                const res = origGetParameter2.apply(this, arguments);
                if (param === 37445) return 'Google Inc. (Apple)';
                if (param === 37446) return 'ANGLE (Apple, Apple M1, OpenGL 4.1)';
                return res;
            }}, 'getParameter');
        }}
    }} catch (e) {{}}

}})();
"""


# Module-level convenience build. Nothing imports it today (browser.py calls
# build_init_js() directly), but it is cheap and keeps the module importable
# as a script for eyeballing the generated JS.
INIT_JS = build_init_js()
