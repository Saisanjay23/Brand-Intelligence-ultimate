// Dedicated Proxy Configuration tab, pulled out of Sessions on purpose
// (see the app's nav) rather than folded into it, so proxy assignment
// isn't buried behind the credential-management modal. Every write here
// goes through the SAME backend routes SessionPanel's proxy fields would
// have used (PUT/DELETE /sessions/{platform}/{id}/proxy), this page adds
// no new API surface, just a UI for one that existed but had none.
//
// "Universal" input: one text box accepts a proxy string in whichever
// shape the analyst's provider actually handed them (host:port,
// host:port:user:pass, user:pass@host:port, scheme://host:port, ...).
// See services/proxyParser.ts for the exact grammar and
// backend/sessions/manager.py::_validate_proxy for the server-side
// backstop on whatever this parses to.
import { useState } from "react";
import { sessionsApi } from "../api/sessionsApi";
import type { ProxyTestResult } from "../api/sessionsApi";
import type { SessionInfo, SessionItem } from "../api/types";
import { PlatformIcon } from "../components/PlatformIcon";
import {
  ProxyNodeIcon,
  ZapIcon,
  AlertTriangleIcon,
  GlobeIcon,
  RefreshIcon,
  LayersIcon,
} from "../components/AppIcons";
import {
  ALLOWED_PROXY_SCHEMES,
  PROXY_FORMAT_EXAMPLES,
  describeParsedProxy,
  parseProxyString,
  type ParsedProxy,
} from "../services/proxyParser";

interface Props {
  sessions: SessionInfo[];
  onChanged: () => void;
}

// A per-session proxy is a Playwright context-launch option, it only
// means anything for the platforms that actually route through
// sessions/manager.py::session_for_job's cookie branch. YouTube (a REST
// API call, no browser) and Telegram (Telethon/MTProto, its own separate
// proxy mechanism) never read this field at all; the backend now refuses
// to store one for them (see manager.py::set_proxy's kind-gate), this
// mirrors that gate in the UI so the control is never offered where it
// would silently do nothing.
function supportsProxy(kind: SessionInfo["kind"]): boolean {
  return kind === "cookies";
}

function cooldownLabel(rateLimitedUntil: number | undefined): string {
  if (!rateLimitedUntil) return "";
  const remainingMs = rateLimitedUntil * 1000 - Date.now();
  if (remainingMs <= 0) return "";
  const hours = Math.floor(remainingMs / 3600000);
  const mins = Math.round((remainingMs % 3600000) / 60000);
  if (hours > 0) return `~${hours}h${mins ? ` ${mins}m` : ""}`;
  return `~${Math.max(1, mins)}m`;
}

export function ProxyPanel({ sessions, onChanged }: Props) {
  const [busyId, setBusyId] = useState<string>(""); // `${platform}:${sessionId}`
  const [error, setError] = useState<string>("");
  const [editing, setEditing] = useState<string>(""); // same key, "" = none open
  const [rawInput, setRawInput] = useState<string>("");
  const [timezoneId, setTimezoneId] = useState<string>("");
  const [showFormats, setShowFormats] = useState(false);
  // Result of the pre-save probe, and whether one is in flight. Kept beside
  // the editor state because it is only ever meaningful for the string
  // currently in `rawInput` -- see clearProbe.
  const [probe, setProbe] = useState<ProxyTestResult | null>(null);
  const [probing, setProbing] = useState(false);

  const parsed: ParsedProxy | null = editing ? parseProxyString(rawInput) : null;
  const showParseError = editing && rawInput.trim().length > 0 && !parsed;

  // A probe result outlives the string it describes unless something drops
  // it. Showing "✓ residential" under a proxy the analyst has since edited
  // would be worse than showing nothing, so every path that changes the
  // input clears it.
  const clearProbe = () => setProbe(null);

  const openEditor = (platform: string, sessionId: string) => {
    const key = `${platform}:${sessionId}`;
    setEditing(key);
    setRawInput("");
    setTimezoneId("");
    setError("");
    clearProbe();
  };

  const closeEditor = () => {
    setEditing("");
    setRawInput("");
    setTimezoneId("");
    clearProbe();
  };

  const run = async (key: string, fn: () => Promise<unknown>) => {
    setBusyId(key);
    setError("");
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusyId("");
    }
  };

  const testProxy = async () => {
    if (!parsed) return;
    setProbing(true);
    setProbe(null);
    setError("");
    try {
      setProbe(await sessionsApi.testProxy({
        server: parsed.server, username: parsed.username, password: parsed.password,
      }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setProbing(false);
    }
  };

  const saveProxy = (platform: string, sessionId: string) => {
    if (!parsed) return;
    const key = `${platform}:${sessionId}`;
    run(key, () =>
      sessionsApi.setSessionProxy(platform, sessionId, {
        server: parsed.server,
        username: parsed.username,
        password: parsed.password,
        timezone_id: timezoneId.trim() || undefined,
      }),
    ).then(() => closeEditor());
  };

  const clearProxy = (platform: string, sessionId: string) => {
    const key = `${platform}:${sessionId}`;
    run(key, () => sessionsApi.clearSessionProxy(platform, sessionId));
  };

  const checkSession = (platform: string) => {
    run(`check:${platform}`, () => sessionsApi.checkSessionNow(platform));
  };

  const [testingProxies, setTestingProxies] = useState(false);
  const [proxyLatencies, setProxyLatencies] = useState<Record<string, { ok: boolean; ms?: number; err?: string }>>({});

  const testAllProxies = async () => {
    setTestingProxies(true);
    const results: Record<string, { ok: boolean; ms?: number; err?: string }> = {};
    const tasks: Promise<void>[] = [];

    sessions.forEach((p) => {
      p.sessions.forEach((s) => {
        if (!s.proxy_host) return;
        const key = `${p.platform}:${s.id}`;
        tasks.push(
          (async () => {
            const start = performance.now();
            try {
              const res = await sessionsApi.checkSessionNow(p.platform);
              const elapsed = Math.round(performance.now() - start);
              results[key] = { ok: res.ok, ms: elapsed, err: res.ok ? undefined : (res.detail || "Failed") };
            } catch (err) {
              results[key] = { ok: false, err: (err as Error).message };
            }
          })()
        );
      });
    });

    if (!tasks.length) {
      setTestingProxies(false);
      return;
    }

    await Promise.all(tasks);
    setProxyLatencies(results);
    setTestingProxies(false);
  };

  const proxyCapable = sessions.filter((s) => supportsProxy(s.kind));
  const notApplicable = sessions.filter((s) => !supportsProxy(s.kind));

  return (
    <div style={{ padding: "24px", color: "var(--text-main, #f2f4f7)", maxWidth: "1200px", margin: "0 auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "20px", flexWrap: "wrap", gap: "12px" }}>
        <div>
          <h1 style={{ fontSize: "22px", fontWeight: 700, color: "var(--text-primary, #fff)", margin: 0, letterSpacing: "-0.3px", display: "flex", alignItems: "center", gap: "10px" }}>
            <ProxyNodeIcon size={24} color="var(--cyan)" />
            <span>Proxy Configuration</span>
          </h1>
          <p style={{ fontSize: "13px", color: "var(--text-muted, #98a2b3)", margin: "4px 0 0 0" }}>
            Each pooled session can exit through its own proxy instead of every account in the pool sharing
            this machine's IP.
          </p>
        </div>
        <button
          className="btn-cyber-primary"
          onClick={testAllProxies}
          disabled={testingProxies}
          style={{ width: "auto", padding: "8px 16px", fontSize: "12px", marginTop: 0, display: "inline-flex", alignItems: "center", gap: "6px" }}
        >
          <ZapIcon size={14} />
          <span>{testingProxies ? "Testing Proxies…" : "Test All Proxies"}</span>
        </button>
      </div>

      {error && (
        <div style={{
          padding: "10px 16px", background: "rgba(233, 80, 83,0.1)", border: "1px solid rgba(233, 80, 83,0.25)",
          color: "var(--danger)", borderRadius: "10px", marginBottom: "16px", fontSize: "13px",
          display: "flex", justifyContent: "space-between", alignItems: "center", gap: "10px",
        }}>
          <span style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            <AlertTriangleIcon size={15} color="var(--danger)" /> {error}
          </span>
          <button onClick={() => setError("")} style={{ background: "transparent", border: "none", color: "inherit", cursor: "pointer", fontSize: "14px", fontWeight: 700 }}>
            ✕
          </button>
        </div>
      )}

      {/* Universal-format help, collapsed by default, one click away */}
      <div style={{
        background: "var(--bg-surface, #1e2837)", border: "1px solid var(--border-color, #344054)",
        borderRadius: "10px", marginBottom: "20px", overflow: "hidden",
      }}>
        <button
          onClick={() => setShowFormats((v) => !v)}
          style={{
            width: "100%", textAlign: "left", padding: "10px 14px", background: "transparent", border: "none",
            color: "var(--text-primary, #fff)", fontSize: "12px", fontWeight: 600, cursor: "pointer",
            display: "flex", justifyContent: "space-between", alignItems: "center",
          }}
        >
          <span style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            <LayersIcon size={14} color="var(--cyan)" />
            <span>Accepted proxy formats ({ALLOWED_PROXY_SCHEMES.join(", ")})</span>
          </span>
          <span>{showFormats ? "▾" : "▸"}</span>
        </button>
        {showFormats && (
          <div style={{ padding: "0 14px 14px", display: "flex", flexDirection: "column", gap: "6px" }}>
            {PROXY_FORMAT_EXAMPLES.map((f) => (
              <div key={f.example} style={{ display: "flex", gap: "10px", fontSize: "12px", alignItems: "baseline" }}>
                <code style={{
                  background: "var(--bg-app, #101828)", padding: "2px 8px", borderRadius: "5px",
                  color: "var(--cyan-bright, #9a50e9)", fontFamily: "var(--font-mono, monospace)",
                }}>
                  {f.example}
                </code>
                <span style={{ color: "var(--text-muted, #98a2b3)" }}>{f.label}</span>
              </div>
            ))}
            <div style={{ fontSize: "11px", color: "var(--text-muted, #98a2b3)", marginTop: "4px" }}>
              Scheme defaults to <code>http</code> when omitted. Credentials are never echoed back once saved,
              only the host is shown afterward.
            </div>
          </div>
        )}
      </div>

      {proxyCapable.map((s) => (
        <div key={s.platform} style={{
          background: "var(--bg-surface, #1e2837)", border: "1px solid var(--border-color, #344054)",
          borderRadius: "12px", padding: "16px", marginBottom: "16px",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "12px" }}>
            <PlatformIcon platform={s.platform} size={24} />
            <div style={{ fontSize: "15px", fontWeight: 700, color: "var(--text-primary, #fff)" }}>{s.name}</div>
            <div style={{ fontSize: "11px", color: "var(--text-muted, #98a2b3)" }}>
              {s.sessions.length} session{s.sessions.length === 1 ? "" : "s"} in pool
            </div>
            <button
              onClick={() => checkSession(s.platform)}
              disabled={busyId === `check:${s.platform}` || s.state === "missing"}
              title="Runs a live check against this platform's current session -- confirms a newly-set proxy actually lets the session through"
              style={{
                marginLeft: "auto", padding: "5px 10px", borderRadius: "6px", fontSize: "11px", fontWeight: 600,
                background: "var(--bg-surface-3, #344054)", border: "1px solid var(--border-color, #344054)",
                color: "var(--text-primary, #fff)", cursor: "pointer",
                display: "inline-flex", alignItems: "center", gap: "5px",
              }}
            >
              <RefreshIcon size={12} />
              <span>{busyId === `check:${s.platform}` ? "Checking…" : "Check Session"}</span>
            </button>
          </div>

          {!s.sessions.length ? (
            <div style={{ padding: "16px", textAlign: "center", fontSize: "12px", color: "var(--text-muted, #98a2b3)" }}>
              No sessions in this platform's pool yet -- add one under Sessions first.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
              {s.sessions.map((item: SessionItem, idx: number) => {
                const key = `${s.platform}:${item.id}`;
                const isEditing = editing === key;
                const isBusy = busyId === key;
                const cooldown = cooldownLabel(item.rate_limited_until);
                return (
                  <div key={item.id} style={{
                    background: "var(--bg-surface-alt, #1d2939)", border: "1px solid var(--border-color, #344054)",
                    borderRadius: "8px", padding: "10px 12px",
                  }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
                      <span style={{
                        fontSize: "10px", fontWeight: 700, color: "var(--text-muted, #98a2b3)",
                        background: "var(--bg-app, #101828)", padding: "1px 5px", borderRadius: "3px",
                      }}>
                        #{idx + 1}
                      </span>
                      <span style={{ fontSize: "13px", fontWeight: 600, color: "var(--text-primary, #fff)" }}>
                        {item.identifier || `Account ${idx + 1}`}
                      </span>
                      {item.available === false && (
                        <span style={{ fontSize: "10px", color: "var(--danger, #F04438)", display: "inline-flex", alignItems: "center", gap: "4px" }}>
                          <AlertTriangleIcon size={11} color="var(--danger)" />
                          <span>{item.status}{cooldown ? ` · cooling off ${cooldown}` : ""}</span>
                        </span>
                      )}
                      <span style={{ flex: 1 }} />
                      {proxyLatencies[key] && (
                        <span
                          style={{
                            fontSize: "11px",
                            fontWeight: 700,
                            padding: "2px 8px",
                            borderRadius: "12px",
                            background: proxyLatencies[key].ok
                              ? (proxyLatencies[key].ms! < 500 ? "rgba(34, 197, 94, 0.15)" : "rgba(253, 183, 27, 0.15)")
                              : "rgba(239, 68, 68, 0.15)",
                            color: proxyLatencies[key].ok
                              ? (proxyLatencies[key].ms! < 500 ? "var(--success)" : "var(--warn-yellow)")
                              : "var(--danger)",
                            display: "inline-flex",
                            alignItems: "center",
                            gap: "4px",
                          }}
                        >
                          <span style={{ width: 6, height: 6, borderRadius: "50%", background: proxyLatencies[key].ok ? "var(--success)" : "var(--danger)" }} />
                          {proxyLatencies[key].ok ? `${proxyLatencies[key].ms}ms` : (proxyLatencies[key].err || "Failed")}
                        </span>
                      )}
                      <span style={{ fontSize: "12px", color: item.proxy_host ? "var(--text-primary, #fff)" : "var(--text-muted, #98a2b3)", display: "inline-flex", alignItems: "center", gap: "5px" }}>
                        {item.proxy_host ? (
                          <>
                            <GlobeIcon size={12} color="var(--cyan)" />
                            <span>{item.proxy_host}</span>
                          </>
                        ) : (
                          "no proxy set (direct)"
                        )}
                      </span>
                      <button
                        onClick={() => (isEditing ? closeEditor() : openEditor(s.platform, item.id))}
                        disabled={isBusy}
                        style={{
                          padding: "4px 9px", borderRadius: "5px", fontSize: "11px", fontWeight: 600,
                          background: "var(--bg-surface-3, #344054)", border: "1px solid var(--border-color, #344054)",
                          color: "var(--text-primary, #fff)", cursor: "pointer",
                        }}
                      >
                        {isEditing ? "Cancel" : item.proxy_host ? "Change" : "Set Proxy"}
                      </button>
                      {item.proxy_host && (
                        <button
                          onClick={() => clearProxy(s.platform, item.id)}
                          disabled={isBusy}
                          title="Remove this session's proxy -- it will exit through this machine's own IP instead"
                          style={{
                            padding: "4px 9px", borderRadius: "5px", fontSize: "11px", fontWeight: 600,
                            background: "transparent", border: "1px solid var(--border-color, #344054)",
                            color: "var(--danger, #F04438)", cursor: "pointer",
                          }}
                        >
                          {isBusy ? "…" : "Clear"}
                        </button>
                      )}
                    </div>

                    {isEditing && (
                      <div style={{ marginTop: "10px", paddingTop: "10px", borderTop: "1px solid var(--border-color, #344054)" }}>
                        <input
                          autoFocus
                          value={rawInput}
                          onChange={(e) => { setRawInput(e.target.value); clearProbe(); }}
                          placeholder="paste any proxy string -- host:port, user:pass@host:port, socks5://host:port, ..."
                          style={{
                            width: "100%", background: "var(--bg-app, #101828)", border: "1px solid var(--border-color, #344054)",
                            borderRadius: "7px", color: "var(--text-primary, #fff)", fontSize: "12px",
                            fontFamily: "var(--font-mono, monospace)", padding: "8px 10px", outline: "none", boxSizing: "border-box",
                          }}
                        />
                        <div style={{ display: "flex", alignItems: "center", gap: "10px", marginTop: "8px", flexWrap: "wrap" }}>
                          <input
                            value={timezoneId}
                            onChange={(e) => setTimezoneId(e.target.value)}
                            placeholder="IANA timezone (optional, e.g. America/New_York)"
                            title="Spoofs the browser's reported timezone to match the proxy's exit location, so an IP that geolocates to New York doesn't show up alongside a browser reporting Asia/Kolkata -- a fingerprint mismatch that's a giveaway on its own"
                            style={{
                              flex: 1, minWidth: "220px", background: "var(--bg-app, #101828)", border: "1px solid var(--border-color, #344054)",
                              borderRadius: "7px", color: "var(--text-primary, #fff)", fontSize: "12px", padding: "7px 10px",
                              outline: "none", boxSizing: "border-box",
                            }}
                          />
                          <button
                            onClick={() => testProxy()}
                            disabled={!parsed || probing}
                            title="Route a throwaway browser through this proxy and report the address the world actually sees"
                            style={{
                              padding: "7px 14px", borderRadius: "7px", fontSize: "12px", fontWeight: 600,
                              background: "transparent",
                              border: "1px solid var(--border-subtle, rgba(255,255,255,0.18))",
                              color: parsed ? "var(--text-primary, #fff)" : "var(--text-dim, #98A2B3)",
                              cursor: parsed && !probing ? "pointer" : "not-allowed", whiteSpace: "nowrap",
                            }}
                          >
                            {probing ? "Testing…" : "Test"}
                          </button>
                          <button
                            onClick={() => saveProxy(s.platform, item.id)}
                            disabled={!parsed || isBusy}
                            style={{
                              padding: "7px 16px", borderRadius: "7px", fontSize: "12px", fontWeight: 600,
                              background: parsed ? "var(--primary, #8838dd)" : "var(--bg-surface-3, #344054)",
                              border: "none", color: "#fff", cursor: parsed ? "pointer" : "not-allowed",
                            }}
                          >
                            {isBusy ? "Saving…" : "Save"}
                          </button>
                        </div>
                        <div style={{ marginTop: "6px", fontSize: "11px" }}>
                          {parsed && (
                            <span style={{ color: "var(--success, #12B76A)" }}>
                              ✓ parsed as {describeParsedProxy(parsed)}
                            </span>
                          )}
                          {showParseError && (
                            <span style={{ color: "var(--danger, #F04438)" }}>
                              ✕ couldn't parse that as a proxy -- see the format list above
                            </span>
                          )}
                        </div>

                        {/* Live probe result. Deliberately verbose: a proxy
                            that "saves fine" can still be sending traffic
                            out on the real IP (Chromium's SOCKS fallback) or
                            be a datacenter range, and neither is visible
                            from the parsed string alone. */}
                        {probe && (
                          <div
                            style={{
                              marginTop: "8px", padding: "9px 11px", borderRadius: "8px", fontSize: "11.5px",
                              background: probe.ok ? "rgba(18,183,106,0.08)" : "rgba(240,68,56,0.10)",
                              border: `1px solid ${probe.ok ? "rgba(18,183,106,0.35)" : "rgba(240,68,56,0.40)"}`,
                            }}
                          >
                            <div style={{ fontWeight: 700, color: probe.ok ? "var(--success, #12B76A)" : "var(--danger, #F04438)" }}>
                              {probe.ok ? "✓ Traffic is exiting through this proxy" : "✕ Not usable"}
                            </div>
                            {probe.exit_ip && (
                              <div style={{ marginTop: "4px", color: "var(--text-dim, #98A2B3)" }}>
                                exit <strong style={{ color: "var(--text-primary,#fff)" }}>{probe.exit_ip}</strong>
                                {probe.city || probe.country ? ` · ${[probe.city, probe.country].filter(Boolean).join(", ")}` : ""}
                                {probe.timezone ? ` · ${probe.timezone}` : ""}
                                {typeof probe.latency_ms === "number" ? ` · ${probe.latency_ms}ms` : ""}
                              </div>
                            )}
                            {probe.org && (
                              <div style={{ color: "var(--text-dim, #98A2B3)" }}>{probe.org}</div>
                            )}
                            {probe.is_datacenter !== null && probe.is_datacenter !== undefined && (
                              <div style={{ marginTop: "3px", fontWeight: 600,
                                            color: probe.is_datacenter ? "var(--warning, #F79009)" : "var(--success, #12B76A)" }}>
                                {probe.is_datacenter
                                  ? "⚠ datacenter range — the loudest network-layer signal"
                                  : probe.is_mobile ? "✓ mobile carrier range — the quietest kind"
                                  : "✓ residential range"}
                              </div>
                            )}
                            {probe.timezone && (
                              <button
                                onClick={() => setTimezoneId(probe.timezone as string)}
                                style={{
                                  marginTop: "6px", padding: "3px 8px", borderRadius: "6px", fontSize: "10.5px",
                                  background: "transparent", border: "1px solid var(--border-subtle, rgba(255,255,255,0.18))",
                                  color: "var(--text-primary,#fff)", cursor: "pointer",
                                }}
                                title="Match the browser timezone to where this proxy actually exits"
                              >
                                use {probe.timezone} as this session's timezone
                              </button>
                            )}
                            {(probe.warnings || []).map((w: string, i: number) => (
                              <div key={i} style={{ marginTop: "5px", color: "var(--warning, #F79009)", lineHeight: 1.45 }}>
                                {w}
                              </div>
                            ))}
                            {probe.error && (
                              <div style={{ marginTop: "5px", color: "var(--danger, #F04438)" }}>{probe.error}</div>
                            )}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ))}

      {notApplicable.length > 0 && (
        <div style={{
          background: "var(--bg-surface-alt, #1d2939)", border: "1px dashed var(--border-color, #344054)",
          borderRadius: "10px", padding: "14px 16px", fontSize: "12px", color: "var(--text-muted, #98a2b3)",
        }}>
          <div style={{ fontWeight: 600, color: "var(--text-primary, #fff)", marginBottom: "6px" }}>
            Not applicable to every platform
          </div>
          {notApplicable.map((s) => (
            <div key={s.platform} style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "4px" }}>
              <PlatformIcon platform={s.platform} size={16} />
              <span>
                <strong style={{ color: "var(--text-primary, #fff)" }}>{s.name}</strong>
                {" — "}
                {s.kind === "api-key"
                  ? "reached through its own API directly, no browser session to route through a proxy"
                  : "connects via its own protocol client (Telethon/MTProto), which has a separate proxy mechanism this page doesn't manage"}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
