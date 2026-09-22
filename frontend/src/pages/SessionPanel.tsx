import { useState, type CSSProperties, type FC } from "react";
import { sessionsApi } from "../api/sessionsApi";
import type { SessionInfo, SessionItem } from "../api/types";
import { confirmAction } from "../utils/confirmAction";

interface Props {
  sessions: SessionInfo[];
  onChanged: () => void;
}

interface ModalState {
  isOpen: boolean;
  platform?: SessionInfo;
  mode: "create" | "update";
  targetSession?: { id: string; identifier: string; isApiKey?: boolean };
}

import { PlatformIcon } from "../components/PlatformIcon";
import {
  AlertTriangleIcon,
  DatabaseIcon,
  ZapIcon,
  ShieldIcon,
  SearchIcon,
  RefreshIcon,
  TrashIcon,
} from "../components/AppIcons";

// How long these login cookies have left. Only worth showing when it's
// close enough to act on, a token good for another eight months is noise,
// one good for three days is the next thing you should deal with.
const EXPIRY_WARN_DAYS = 14;

function expiryLabel(expiresAt: number | undefined): { text: string; urgent: boolean } | null {
  if (!expiresAt) return null;
  const days = (expiresAt * 1000 - Date.now()) / 86400000;
  if (days <= 0) return { text: "login cookies have expired", urgent: true };
  if (days > EXPIRY_WARN_DAYS) return null;
  if (days < 1) return { text: "expires in under a day", urgent: true };
  return { text: `expires in ${Math.round(days)}d`, urgent: days <= 3 };
}

function cooldownLabel(rateLimitedUntil: number | undefined): string {
  if (!rateLimitedUntil) return "";
  const remainingMs = rateLimitedUntil * 1000 - Date.now();
  if (remainingMs <= 0) return "";
  const hours = Math.floor(remainingMs / 3600000);
  const mins = Math.round((remainingMs % 3600000) / 60000);
  if (hours > 0) return `cooldown ~${hours}h${mins ? ` ${mins}m` : ""}`;
  return `cooldown ~${Math.max(1, mins)}m`;
}

const embeddedStyles = `
@keyframes modalPopIn {
  from { opacity: 0; transform: scale(0.96) translateY(-6px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

@keyframes runningPulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}

.tab-btn {
  transition: all 0.2s ease;
}
.tab-btn:hover {
  background: var(--bg-hover, #344054);
  color: var(--text-primary, #ffffff);
}

.action-btn {
  transition: all 0.2s ease;
}
.action-btn:hover {
  background: var(--primary, #8838dd) !important;
  color: #ffffff !important;
}
`;

// What the self-healer has actually done to this account, in a sentence.
//
// The attempt count on its own was misleading in both directions: "3 failed
// attempts" reads as a burned account when it may be one that has recovered
// itself a dozen times and hit a bad afternoon, and a bare "Auto-Login"
// badge reads as working when self-healing may never once have succeeded
// here. Both halves are stored on the session row now, so this can say
// which it is rather than leave it to be assumed.
function selfHealingStory(ss: {
  relogin_attempts?: number;
  relogin_last_success?: number;
  relogin_total_successes?: number;
}): string {
  const failed = ss.relogin_attempts ?? 0;
  const wins = ss.relogin_total_successes ?? 0;
  const when = ss.relogin_last_success
    ? new Date(ss.relogin_last_success * 1000).toLocaleString()
    : "";
  const history = wins
    ? `Has signed itself back in ${wins} time(s), most recently ${when}.`
    : "Has never yet managed to sign itself back in.";
  if (failed > 0) {
    return `${failed} failed automatic attempt(s) since it last worked. ${history}`;
  }
  return wins
    ? history
    : "Credentials are stored, so this account signs itself back in when it is found logged out.";
}


export function SessionPanel({ sessions, onChanged }: Props) {
  const [modal, setModal] = useState<ModalState>({ isOpen: false, mode: "create" });
  const [activeTabs, setActiveTabs] = useState<Record<string, "pool" | "controls">>({});
  const [busyPlatform, setBusyPlatform] = useState<string>("");
  const [globalError, setGlobalError] = useState<string>("");
  // which single account is being live-checked right now, and what the last
  // check said, kept per session id so checking one row doesn't grey out
  // the whole platform card the way a pool-wide action does
  const [checkingId, setCheckingId] = useState<string>("");
  const [checkResult, setCheckResult] = useState<{ id: string; ok: boolean; detail: string } | null>(null);
  // Same per-row treatment as the live check above: a re-login opens a real
  // browser and can take half a minute, so the row it belongs to says so
  // rather than the whole platform card greying out.
  const [reloggingId, setReloggingId] = useState<string>("");

  const reloginOneSession = async (platform: string, sessionId: string) => {
    setReloggingId(sessionId);
    setCheckResult(null);
    setGlobalError("");
    try {
      const res = await sessionsApi.reloginSessionItem(platform, sessionId);
      setCheckResult({
        id: sessionId,
        ok: res.ok,
        detail: res.ok ? "Signed back in — fresh cookies stored" : res.detail,
      });
      onChanged();
    } catch (e) {
      setGlobalError((e as Error).message);
    } finally {
      setReloggingId("");
    }
  };

  const checkOneSession = async (platform: string, sessionId: string) => {
    setCheckingId(sessionId);
    setCheckResult(null);
    setGlobalError("");
    try {
      const res = await sessionsApi.checkSessionItem(platform, sessionId);
      setCheckResult({ id: sessionId, ok: res.ok, detail: res.detail });
      onChanged();
    } catch (e) {
      setGlobalError((e as Error).message);
    } finally {
      setCheckingId("");
    }
  };

  const handleAction = async (fn: () => Promise<unknown>, platform: string) => {
    setBusyPlatform(platform);
    setGlobalError("");
    try {
      await fn();
      onChanged();
    } catch (e) {
      setGlobalError((e as Error).message);
    } finally {
      setBusyPlatform("");
    }
  };

  const setTab = (platformId: string, tab: "pool" | "controls") => {
    setActiveTabs((prev) => ({ ...prev, [platformId]: tab }));
  };

  const expiringSessions = sessions.flatMap((p) =>
    (p.sessions || [])
      .map((s) => ({ platform: p.platform, ...s, expiry: expiryLabel(s.expires_at) }))
      .filter((s) => s.expiry && s.expiry.urgent)
  );

  return (
    <div style={{ padding: "24px", color: "var(--text-main, #f2f4f7)", position: "relative", maxWidth: "1600px", margin: "0 auto" }}>
      <style>{embeddedStyles}</style>

      {/* Header Area */}
      <div style={{ marginBottom: "24px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <h1 style={{ fontSize: "22px", fontWeight: 700, color: "var(--text-primary, #fff)", margin: 0, letterSpacing: "-0.3px" }}>
            Platform Credential & Session Pool
          </h1>
          <p style={{ fontSize: "13px", color: "var(--text-muted, #98a2b3)", margin: "4px 0 0 0" }}>
            Manage browser sessions, API keys, and cookie storage for all scraping platforms.
          </p>
        </div>
      </div>

      {expiringSessions.length > 0 && (
        <div style={{
          padding: "12px 16px", background: "rgba(253, 183, 27, 0.1)", border: "1px solid rgba(253, 183, 27, 0.35)",
          color: "var(--warn-yellow, #FDB71B)", borderRadius: "10px", marginBottom: "20px", fontSize: "13px",
          display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap",
        }}>
          <AlertTriangleIcon size={16} color="var(--warn-yellow, #FDB71B)" />
          <span>
            <strong>{expiringSessions.length} session(s) require attention:</strong>{" "}
            {expiringSessions.map((s) => `${s.platform} (${s.identifier || "account"} — ${s.expiry?.text})`).join(", ")}
          </span>
        </div>
      )}

      {globalError && (
        <div style={{
          padding: "10px 16px",
          background: "var(--bg-surface-alt, #1d2939)",
          border: "1px solid var(--border-color, #344054)",
          borderRadius: "8px",
          color: "var(--text-primary, #ffffff)",
          fontSize: "13px",
          fontWeight: 500,
          display: "flex",
          alignItems: "center",
          gap: "10px",
          marginBottom: "20px"
        }}>
          <span style={{ display: "flex", alignItems: "center", gap: "6px" }}>
            <AlertTriangleIcon size={15} color="var(--danger)" /> Notice: {globalError}
          </span>
          <button
            onClick={() => setGlobalError("")}
            style={{ background: "transparent", border: "none", color: "var(--text-main, #fff)", cursor: "pointer", fontSize: "14px", fontWeight: 700, marginLeft: "auto" }}
          >
            ✕
          </button>
        </div>
      )}

      {/* Grid of Uniform Platform Modules */}
      <div className="session-grid-layout">
        {sessions.map((s) => {
          const tab = activeTabs[s.platform] || "pool";
          const poolCount = s.sessions?.length || 0;
          const isUsable = (x: SessionItem) =>
            x.status !== "running_login" && (x.available ?? x.status === "ready");
          const activeCount = s.sessions?.filter(isUsable).length || 0;
          const isAtMax = poolCount >= 20;
          const isBusy = busyPlatform === s.platform;

          return (
            <div
              key={s.platform}
              style={{
                background: "var(--bg-surface, #1e2837)",
                border: "1px solid var(--border-color, #344054)",
                borderRadius: "12px",
                padding: "14px",
                boxShadow: "0 4px 12px rgba(0, 0, 0, 0.35)",
                display: "flex",
                flexDirection: "column",
                height: "380px", /* Compact identical height for all platform modules */
                overflow: "hidden",
                position: "relative"
              }}
            >
              {/* Platform Header */}
              <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "12px", flexShrink: 0 }}>
                <div style={{
                  width: "36px",
                  height: "36px",
                  borderRadius: "8px",
                  background: "var(--bg-surface-alt, #1d2939)",
                  border: "1px solid var(--border-subtle, rgba(255,255,255,0.08))",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flexShrink: 0
                }}>
                  <PlatformIcon platform={s.platform} size={22} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div title={s.name} style={{ fontSize: "16px", fontWeight: 700, color: "var(--text-primary, #fff)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                    {s.name}
                  </div>
                  <div style={{ fontSize: "11px", color: "var(--text-muted, #98a2b3)", display: "flex", alignItems: "center", gap: "5px", marginTop: "1px" }}>
                    <span>Type: <strong style={{ color: "var(--text-body, #f2f4f7)" }}>{s.kind.toUpperCase()}</strong></span>
                    <span>•</span>
                    <span>Active: <strong style={{ color: "var(--text-body, #ffffff)" }}>{activeCount}</strong></span>
                  </div>
                </div>

                {/* Add Session Button */}
                {(
                  <button
                    disabled={isAtMax || !!busyPlatform}
                    onClick={() => setModal({ isOpen: true, mode: "create", platform: s })}
                    style={{
                      padding: "6px 10px",
                      borderRadius: "6px",
                      background: isAtMax ? "var(--bg-surface-3, #344054)" : "var(--primary, #8838dd)",
                      color: "var(--text-primary, #fff)",
                      border: "1px solid var(--border-subtle, rgba(255,255,255,0.08))",
                      fontWeight: 600,
                      fontSize: "11px",
                      cursor: isAtMax ? "not-allowed" : "pointer",
                      transition: "all 0.2s ease",
                      display: "flex",
                      alignItems: "center",
                      gap: "4px",
                      flexShrink: 0
                    }}
                    title={isAtMax ? "Pool limit reached (20 accounts)" : "Add account credentials"}
                  >
                    <span style={{ fontSize: "12px", fontWeight: 800 }}>＋</span>
                    <span>Add</span>
                  </button>
                )}
              </div>

              {/* Tab Navigation */}
              <div style={{
                display: "flex",
                background: "var(--bg-app, #101828)",
                padding: "3px",
                borderRadius: "8px",
                marginBottom: "12px",
                border: "1px solid var(--border-color, #344054)",
                flexShrink: 0
              }}>
                <button
                  className="tab-btn"
                  onClick={() => setTab(s.platform, "pool")}
                  style={{
                    flex: 1,
                    padding: "6px 8px",
                    borderRadius: "6px",
                    border: "none",
                    background: tab === "pool" ? "var(--bg-light, #ffffff)" : "transparent",
                    color: tab === "pool" ? "var(--cyan, #8838dd)" : "var(--text-primary, #ffffff)",
                    fontSize: "11px",
                    fontWeight: tab === "pool" ? 700 : 500,
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: "5px",
                    boxShadow: tab === "pool" ? "0 2px 6px rgba(0, 0, 0, 0.2)" : "none"
                  }}
                >
                  <DatabaseIcon size={13} />
                  <span>Pool</span>
                  <span style={{
                    padding: "1px 6px",
                    borderRadius: "999px",
                    background: tab === "pool" ? "var(--cyan, #8838dd)" : "var(--bg-surface-3, #344054)",
                    color: "var(--text-main)",
                    fontSize: "10px",
                    fontWeight: 700
                  }}>
                    {poolCount}/20
                  </span>
                </button>
                <button
                  className="tab-btn"
                  onClick={() => setTab(s.platform, "controls")}
                  style={{
                    flex: 1,
                    padding: "6px 8px",
                    borderRadius: "6px",
                    border: "none",
                    background: tab === "controls" ? "var(--bg-light, #ffffff)" : "transparent",
                    color: tab === "controls" ? "var(--cyan, #8838dd)" : "var(--text-primary, #ffffff)",
                    fontSize: "11px",
                    fontWeight: tab === "controls" ? 700 : 500,
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: "5px",
                    boxShadow: tab === "controls" ? "0 2px 6px rgba(0, 0, 0, 0.2)" : "none"
                  }}
                >
                  <ZapIcon size={13} />
                  <span>Verification</span>
                </button>
              </div>

              {/* Tab 1 Content: Session Pool */}
              {tab === "pool" && (
                <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0, gap: "10px" }}>
                  {/* Capacity Gauge */}
                  <div style={{ flexShrink: 0 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: "11px", color: "var(--text-muted, #98a2b3)", marginBottom: "3px" }}>
                      <span>Rotation Capacity</span>
                      <span style={{ fontWeight: 600, color: "var(--text-body, #ffffff)" }}>
                        {poolCount}/20 Accounts
                      </span>
                    </div>
                    <div style={{ width: "100%", height: "4px", background: "var(--bg-app, #101828)", borderRadius: "2px", overflow: "hidden" }}>
                      <div style={{
                        height: "100%",
                        width: `${Math.min(100, (poolCount / 20) * 100)}%`,
                        background: "var(--primary, #8838dd)",
                        transition: "width 0.3s ease"
                      }} />
                    </div>
                  </div>

                  {/* Sessions Scroll Area */}
                  <div style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: "8px", paddingRight: "2px" }}>
                    {(!s.sessions || s.sessions.length === 0) ? (
                      <div style={{
                        padding: "24px 12px",
                        textAlign: "center",
                        background: "var(--bg-surface-alt, #1d2939)",
                        borderRadius: "8px",
                        border: "1px dashed var(--border-color, #344054)",
                        margin: "auto 0"
                      }}>
                        <div style={{ display: "flex", justifyContent: "center", marginBottom: "6px" }}>
                          <ShieldIcon size={24} color="var(--text-muted)" />
                        </div>
                        <div style={{ fontSize: "12px", fontWeight: 600, color: "var(--text-primary, #fff)" }}>No accounts saved</div>
                        <div style={{ fontSize: "11px", color: "var(--text-muted, #98a2b3)", margin: "3px auto 0 auto" }}>
                          Click <strong>"＋ Add"</strong> above to input cookies or keys.
                        </div>
                      </div>
                    ) : (
                      s.sessions.map((ss, index) => {
                        const isReady = isUsable(ss);
                        const loggingIn = ss.status === "running_login";
                        const cooldown = cooldownLabel(ss.rate_limited_until);
                        const expiry = expiryLabel(ss.expires_at);
                        const checking = checkingId === ss.id;

                        const running = !!ss.in_use;

                        return (
                          <div
                            key={ss.id}
                            style={{
                              background: running ? "var(--bg-surface-3, #26344a)" : "var(--bg-surface-alt, #1d2939)",
                              border: running ? "1px solid var(--accent, #7c5cff)" : "1px solid var(--border-color, #344054)",
                              boxShadow: running ? "0 0 0 1px var(--accent, #7c5cff) inset" : "none",
                              borderRadius: "6px",
                              padding: "8px 10px",
                              display: "flex",
                              justifyContent: "space-between",
                              alignItems: "center",
                              gap: "8px",
                              flexShrink: 0
                            }}
                          >
                            <div style={{ minWidth: 0, flex: 1 }}>
                              <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                                <span style={{
                                  fontSize: "10px",
                                  fontWeight: 700,
                                  color: "var(--text-muted, #98a2b3)",
                                  background: "var(--bg-app, #101828)",
                                  padding: "1px 5px",
                                  borderRadius: "3px",
                                  border: "1px solid var(--border-subtle, rgba(255,255,255,0.08))"
                                }}>
                                  #{index + 1}
                                </span>
                                <span title={ss.identifier || `Account ${index + 1}`} style={{ fontSize: "13px", fontWeight: 600, color: "var(--text-primary, #fff)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                  {ss.identifier || `Account ${index + 1}`}
                                </span>
                                {running && (
                                  <span
                                    title="A job is using this session right now"
                                    style={{
                                      display: "inline-flex",
                                      alignItems: "center",
                                      gap: "4px",
                                      fontSize: "9px",
                                      fontWeight: 700,
                                      color: "var(--accent, #7c5cff)",
                                      background: "rgba(124, 92, 255, 0.15)",
                                      border: "1px solid var(--accent, #7c5cff)",
                                      borderRadius: "10px",
                                      padding: "1px 7px",
                                      flexShrink: 0
                                    }}
                                  >
                                    <span style={{
                                      width: "6px", height: "6px", borderRadius: "50%",
                                      background: "var(--accent, #7c5cff)",
                                      animation: "runningPulse 1.2s ease-in-out infinite"
                                    }} />
                                    RUNNING NOW
                                  </span>
                                )}
                              </div>
                              <div style={{ fontSize: "11px", color: "var(--text-muted, #98a2b3)", marginTop: "2px", display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
                                <span>{ss.cookie_count > 0 ? `${ss.cookie_count} cookies` : s.kind === "api-key" || ss.is_api_key ? "API Key" : "Active"}</span>
                                {/* How this account authenticates. Credentials on file mean
                                    the pool can sign it back in by itself when it is found
                                    logged out, which is the difference between an account
                                    that needs a person and one that does not. */}
                                {ss.auth_kind === "auto-login" ? (
                                  <span
                                    title={
                                      ss.relogin_running
                                        ? "Signing this account back in right now"
                                        : selfHealingStory(ss)
                                    }
                                    style={{ color: "var(--success, #12B76A)", fontWeight: 600 }}
                                  >
                                    • 🤖 {ss.relogin_running ? "Re-logging in…" : "Auto-Login"}
                                    {(ss.relogin_attempts ?? 0) > 0 && !ss.relogin_running
                                      ? ` (${ss.relogin_attempts} failed)`
                                      : ""}
                                  </span>
                                ) : s.kind === "cookies" ? (
                                  <span title="Cookies only. If this account is logged out, someone has to paste a fresh export by hand. Add a username and password under Edit to let it sign itself back in.">
                                    • 🔒 Cookie Session
                                  </span>
                                ) : null}
                                {/* The persistent browser profile is otherwise
                                    invisible: it changes how the platform sees this
                                    account and leaves no trace anywhere an operator
                                    looks. Only meaningful where a browser is used at
                                    all, so API-key and MTProto rows never show it. */}
                                {s.kind === "cookies" && (
                                  <span
                                    title={
                                      ss.has_browser_profile
                                        ? "This account has its own browser profile on disk -- it keeps the same localStorage, IndexedDB and device keys between runs instead of arriving as a brand-new browser every time."
                                        : "No browser profile yet. One is created the first time a sweep uses this account."
                                    }
                                    style={{ color: ss.has_browser_profile ? "var(--text-secondary, #d8d8d8)" : "var(--text-muted, #98a2b3)" }}
                                  >
                                    • 💾 {ss.has_browser_profile ? "Device kept" : "No device yet"}
                                  </span>
                                )}
                                {cooldown && (
                                  <span style={{ color: "var(--text-secondary, #d8d8d8)" }}>
                                    • ⌛ {cooldown}
                                    {(ss.consecutive_failures ?? 0) > 1 && (
                                      <span title="Consecutive failures since this account last worked -- the cooldown lengthens each time (15m → 1h → 6h → 24h)">
                                        {" "}({ss.consecutive_failures} failures in a row)
                                      </span>
                                    )}
                                  </span>
                                )}
                                <span title="Total times this session has been picked for a job">• Used {ss.use_count ?? 0}×</span>
                                {expiry && (
                                  <span
                                    title="When the soonest of this account's login cookies lapses. Re-login and paste fresh cookies before then to avoid a failed sweep."
                                    style={{ color: expiry.urgent ? "var(--danger, #F04438)" : "var(--warning, #F79009)", fontWeight: 600 }}
                                  >
                                    • 🔑 {expiry.text}
                                  </span>
                                )}
                              </div>

                              {/* Why it stopped working. Without this a dead row is
                                  just red, and "logged out", "checkpointed" and
                                  "rate-limited" all need different responses. */}
                              {checkResult?.id === ss.id && (
                                <div
                                  style={{
                                    fontSize: "11px",
                                    fontWeight: 600,
                                    color: checkResult.ok ? "var(--success, #12B76A)" : "var(--danger, #F04438)",
                                    marginTop: "4px",
                                  }}
                                >
                                  {checkResult.ok
                                    ? "✅ Checked just now — this account is logged in and working"
                                    : `❌ Checked just now — ${checkResult.detail || "this account is not usable"}`}
                                </div>
                              )}

                              {ss.last_error && !(checkResult?.id === ss.id) && (
                                <div
                                  title={ss.last_error}
                                  style={{
                                    fontSize: "11px",
                                    color: "var(--text-secondary, #d8d8d8)",
                                    background: "rgba(240, 68, 56, 0.10)",
                                    border: "1px solid rgba(240, 68, 56, 0.35)",
                                    borderRadius: "4px",
                                    padding: "3px 6px",
                                    marginTop: "4px",
                                    overflow: "hidden",
                                    textOverflow: "ellipsis",
                                    whiteSpace: "nowrap",
                                  }}
                                >
                                  ⚠️ {ss.last_error}
                                </div>
                              )}
                            </div>

                            {/* Status & Actions Matching Live Discovery Violet & White Theme */}
                            <div style={{ display: "flex", alignItems: "center", gap: "6px", flexShrink: 0 }}>
                              <div style={{
                                padding: "3px 8px",
                                borderRadius: "4px",
                                fontSize: "10px",
                                fontWeight: 700,
                                display: "flex",
                                flexDirection: "column",
                                alignItems: "center",
                                background: loggingIn
                                  ? "var(--warning, #F79009)"
                                  : isReady ? "var(--success, #12B76A)" : "var(--danger, #F04438)",
                                color: "#ffffff",
                                border: "1px solid var(--border-subtle, rgba(255,255,255,0.1))"
                              }}>
                                <div>{loggingIn ? "LOGGING IN…" : `LIVE · ${isReady ? "ACTIVE" : "INACTIVE"}`}</div>
                                {ss.last_used > 0 && (
                                  <div style={{ fontSize: "8px", fontWeight: 500, marginTop: "1px", opacity: 0.9 }}>
                                    Last scrape: {new Date(ss.last_used * 1000).toLocaleString()}
                                  </div>
                                )}
                                {ss.purge_in_days != null && (
                                  <div title="Dead accounts are auto-removed after a 7-day grace period unless deleted or rewritten first" style={{ fontSize: "8px", fontWeight: 500, marginTop: "1px", opacity: 0.9 }}>
                                    {ss.purge_in_days > 0 ? `Auto-removes in ~${ss.purge_in_days}d` : "Auto-removes soon"}
                                  </div>
                                )}
                              </div>

                              <button
                                className="action-btn"
                                disabled={!!busyPlatform || !!checkingId || !!ss.in_use}
                                onClick={() => checkOneSession(s.platform, ss.id)}
                                style={{
                                  padding: "4px 8px",
                                  borderRadius: "5px",
                                  background: "var(--bg-surface-3, #344054)",
                                  border: "1px solid var(--border-subtle, rgba(255,255,255,0.08))",
                                  color: "var(--text-body, #ffffff)",
                                  fontSize: "10px",
                                  fontWeight: 600,
                                  cursor: ss.in_use ? "not-allowed" : "pointer"
                                }}
                                title={
                                  ss.in_use
                                    ? "A job is using this account right now -- checking it at the same time risks a checkpoint"
                                    : "Log in with this account's saved credentials right now and report back whether it still works"
                                }
                              >
                                {checking ? "…" : "Check"}
                              </button>
                              {ss.can_relogin && (
                                <button
                                  className="action-btn"
                                  disabled={!!busyPlatform || !!reloggingId || !!ss.in_use || !!ss.relogin_running}
                                  onClick={() => reloginOneSession(s.platform, ss.id)}
                                  style={{
                                    padding: "4px 8px",
                                    borderRadius: "5px",
                                    background: "var(--bg-surface-3, #344054)",
                                    border: "1px solid var(--border-subtle, rgba(255,255,255,0.08))",
                                    color: "var(--text-body, #ffffff)",
                                    fontSize: "10px",
                                    fontWeight: 600,
                                    cursor: ss.in_use ? "not-allowed" : "pointer"
                                  }}
                                  title={
                                    ss.in_use
                                      ? "A job is using this account right now -- signing in at the same time risks a checkpoint"
                                      : "Sign this account back in now using its stored username, password and 2FA secret"
                                  }
                                >
                                  {reloggingId === ss.id ? "Signing in…" : "Re-Login"}
                                </button>
                              )}
                              <button
                                className="action-btn"
                                disabled={!!busyPlatform}
                                onClick={() => setModal({
                                  isOpen: true,
                                  mode: "update",
                                  platform: s,
                                  targetSession: { id: ss.id, identifier: ss.identifier, isApiKey: ss.is_api_key }
                                })}
                                style={{
                                  padding: "4px 8px",
                                  borderRadius: "5px",
                                  background: "var(--bg-surface-3, #344054)",
                                  border: "1px solid var(--border-subtle, rgba(255,255,255,0.08))",
                                  color: "var(--text-body, #ffffff)",
                                  fontSize: "10px",
                                  fontWeight: 600,
                                  cursor: "pointer"
                                }}
                                title="Update account cookies or keywords"
                              >
                                Edit
                              </button>
                              <button
                                className="action-btn"
                                disabled={!!busyPlatform}
                                onClick={() => handleAction(() => sessionsApi.deleteSessionItem(s.platform, ss.id), s.platform)}
                                style={{
                                  padding: "4px 7px",
                                  borderRadius: "5px",
                                  background: "var(--bg-surface-3, #344054)",
                                  border: "1px solid var(--border-subtle, rgba(255,255,255,0.08))",
                                  color: "var(--text-primary, #ffffff)",
                                  fontSize: "10px",
                                  fontWeight: 600,
                                  cursor: "pointer"
                                }}
                                title="Delete account from pool"
                              >
                                ✕
                              </button>
                            </div>
                          </div>
                        );
                      })
                    )}
                  </div>
                </div>
              )}

              {/* Tab 2 Content: Diagnostics & Controls */}
              {tab === "controls" && (
                <div style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: "10px" }}>
                  {s.state === "checkpointed" && (
                    <div style={{ padding: "10px", background: "var(--bg-surface-alt, #1d2939)", borderRadius: "6px", border: "1px solid var(--border-color, #344054)", color: "var(--text-secondary, #d8d8d8)", fontSize: "11px", display: "flex", alignItems: "center", gap: "6px" }}>
                      <AlertTriangleIcon size={14} color="var(--warn-yellow, #FDB71B)" />
                      <span><strong>Checkpoint:</strong> {s.message || "Platform may require verification."}</span>
                    </div>
                  )}

                  <div style={{ background: "var(--bg-surface-alt, #1d2939)", padding: "10px", borderRadius: "6px", border: "1px solid var(--border-color, #344054)" }}>
                    <div style={{ fontSize: "11px", fontWeight: 600, color: "var(--text-primary, #fff)", marginBottom: "3px", display: "flex", alignItems: "center", gap: "6px" }}>
                      <SearchIcon size={13} color="var(--cyan)" />
                      <span>Health Verification Status</span>
                    </div>
                    <div style={{ fontSize: "11px", color: "var(--text-muted, #98a2b3)" }}>
                      {s.last_verified ? `Last check: ${new Date(s.last_verified).toLocaleString()}` : "No verification sweep recorded yet."}
                    </div>
                  </div>

                  <div style={{ display: "flex", flexDirection: "column", gap: "8px", marginTop: "auto", paddingTop: "6px" }}>
                    {s.state !== "missing" && (
                      <button
                        className="action-btn"
                        disabled={!!busyPlatform}
                        onClick={() => handleAction(() => sessionsApi.checkSessionNow(s.platform), s.platform)}
                        style={{
                          width: "100%",
                          padding: "8px",
                          borderRadius: "6px",
                          background: "var(--bg-surface-3, #344054)",
                          border: "1px solid var(--border-color, #344054)",
                          color: "var(--text-primary, #fff)",
                          fontSize: "12px",
                          fontWeight: 600,
                          cursor: "pointer",
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                          gap: "6px"
                        }}
                      >
                        {isBusy ? "Checking..." : (
                          <>
                            <RefreshIcon size={14} /> Verify Sweep Now
                          </>
                        )}
                      </button>
                    )}

                    {s.can_login && (
                      <button
                        className="action-btn"
                        disabled={!!busyPlatform}
                        onClick={() => handleAction(() => sessionsApi.launchLogin(s.platform), s.platform)}
                        style={{
                          width: "100%",
                          padding: "8px",
                          borderRadius: "6px",
                          background: "var(--primary, #8838dd)",
                          color: "var(--text-primary, #fff)",
                          border: "none",
                          fontSize: "12px",
                          fontWeight: 600,
                          cursor: "pointer",
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                          gap: "6px"
                        }}
                      >
                        {isBusy ? "Launching..." : (
                          <>
                            <ZapIcon size={14} /> Launch Login
                          </>
                        )}
                      </button>
                    )}

                    {s.state !== "missing" && (
                      <button
                        className="action-btn"
                        disabled={!!busyPlatform}
                        onClick={async () => {
                          if (await confirmAction(`Delete ALL accounts for ${s.name}?`)) {
                            handleAction(() => sessionsApi.deleteSessionPool(s.platform), s.platform);
                          }
                        }}
                        style={{
                          width: "100%",
                          padding: "8px",
                          borderRadius: "6px",
                          background: "var(--bg-surface-3, #344054)",
                          border: "1px solid var(--border-color, #344054)",
                          color: "var(--text-primary, #ffffff)",
                          fontSize: "12px",
                          fontWeight: 600,
                          cursor: "pointer",
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                          gap: "6px"
                        }}
                      >
                        <TrashIcon size={14} /> Clear Pool ({poolCount})
                      </button>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* MODAL DIALOG: CLEAN & SIMPLE TWO-FIELD FORM */}
      {modal.isOpen && modal.platform && (
        modal.mode === "create" && modal.platform.kind === "mtproto" ? (
          <TelegramLoginModal
            onClose={() => setModal({ isOpen: false, mode: "create" })}
            onSuccess={() => {
              setModal({ isOpen: false, mode: "create" });
              onChanged();
            }}
          />
        ) : (
          <SessionEditModal
            platform={modal.platform}
            mode={modal.mode}
            targetSession={modal.targetSession}
            onClose={() => setModal({ isOpen: false, mode: "create" })}
            onSuccess={() => {
              setModal({ isOpen: false, mode: "create" });
              onChanged();
            }}
          />
        )
      )}
    </div>
  );
}

const SessionEditModal: FC<{
  platform: SessionInfo;
  mode: "create" | "update";
  targetSession?: { id: string; identifier: string; isApiKey?: boolean };
  onClose: () => void;
  onSuccess: () => void;
}> = ({ platform, mode, targetSession, onClose, onSuccess }) => {
  const isUpdate = mode === "update";
  const [identifier, setIdentifier] = useState<string>(targetSession?.identifier || "");
  const [cookieBlob, setCookieBlob] = useState<string>("");
  const [apiKey, setApiKey] = useState<string>("");
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  // TWO WAYS TO AUTHENTICATE ONE ACCOUNT, and they are not variants of each
  // other. Pasted cookies are a snapshot that goes stale and has to be
  // replaced by hand; stored credentials let the pool sign the account back
  // in by itself when it is found logged out.
  const [authMode, setAuthMode] = useState<"cookies" | "credentials">("cookies");
  const [username, setUsername] = useState<string>("");
  const [password, setPassword] = useState<string>("");
  const [twoFactorSecret, setTwoFactorSecret] = useState<string>("");
  const [showPassword, setShowPassword] = useState<boolean>(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);

    try {
      if (isUpdate && targetSession) {
        await sessionsApi.updateSessionItem(platform.platform, targetSession.id, {
          identifier: identifier.trim(),
          ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
          ...(cookieBlob.trim() ? { blob: cookieBlob.trim() } : {}),
          // Sent only when this form is actually editing credentials.
          // Omitting a field leaves the stored one alone, which is what
          // keeps "rename this account" from wiping its password.
          ...(authMode === "credentials"
            ? {
                username: username.trim(),
                password,
                two_factor_secret: twoFactorSecret.replace(/\s+/g, ""),
              }
            : {}),
        });
      } else if (authMode === "credentials") {
        await sessionsApi.saveCredentials(platform.platform, {
          identifier: identifier.trim() || "Auto-Login Account",
          username: username.trim(),
          password,
          two_factor_secret: twoFactorSecret.replace(/\s+/g, ""),
        });
      } else {
        if (platform.platform === "youtube" || platform.kind === "api-key") {
          await sessionsApi.saveApiKey(platform.platform, apiKey.trim(), identifier.trim() || "YouTube API Key");
        } else {
          await sessionsApi.saveCookies(platform.platform, cookieBlob.trim(), identifier.trim() || "Unnamed Account");
        }
      }
      onSuccess();
    } catch (err: any) {
      alert(err?.message || "Failed to save session");
    } finally {
      setIsSubmitting(false);
    }
  };

  const isApiKeyType = platform.platform === "youtube" || platform.kind === "api-key" || targetSession?.isApiKey;
  // Only offered where an automated login actually exists. `can_login` is
  // the server's own answer (sessions/manager.py::LOGIN_FLOW), so a
  // platform with no implemented flow never shows a tab that cannot work.
  const supportsCredentials = !isApiKeyType && platform.can_login;
  const credentialsMode = supportsCredentials && authMode === "credentials";

  return (
    <div style={{
      position: "fixed",
      top: 0,
      left: 0,
      right: 0,
      bottom: 0,
      background: "rgba(0, 0, 0, 0.75)",
      backdropFilter: "blur(4px)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      zIndex: 1000,
      padding: "20px"
    }}>
      <div style={{
        background: "var(--bg-surface, #1e2837)",
        border: "1px solid var(--border-color, #344054)",
        borderRadius: "12px",
        padding: "24px",
        width: "100%",
        maxWidth: "500px",
        boxShadow: "0 10px 25px rgba(0, 0, 0, 0.5)",
        maxHeight: "90vh",
        overflowY: "auto"
      }}>
        {/* Modal Header */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
            <span style={{
              width: "40px",
              height: "40px",
              borderRadius: "8px",
              background: "var(--bg-surface-alt, #1d2939)",
              border: "1px solid var(--border-color, #344054)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}>
              <PlatformIcon platform={platform.platform} size={24} />
            </span>
            <div>
              <h3 style={{ margin: 0, fontSize: "17px", fontWeight: 700, color: "var(--text-primary, #fff)" }}>
                {isUpdate ? `Update ${targetSession?.identifier}` : `Add Session — ${platform.name}`}
              </h3>
            </div>
          </div>
          <button
            onClick={onClose}
            style={{ background: "transparent", border: "none", color: "var(--text-muted, #98a2b3)", cursor: "pointer", fontSize: "16px", fontWeight: 700 }}
          >
            ✕
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
          {/* How this account authenticates. Hidden entirely for API-key
              platforms and for any platform with no automated login flow,
              because an option that cannot work is worse than no option. */}
          {supportsCredentials && (
            <div style={{ display: "flex", gap: "6px", padding: "4px", borderRadius: "8px", background: "var(--bg-surface-alt, #1d2939)", border: "1px solid var(--border-color, #344054)" }}>
              {([["cookies", "Paste Cookies"], ["credentials", "Auto-Login Credentials"]] as const).map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setAuthMode(key)}
                  style={{
                    flex: 1,
                    padding: "7px 10px",
                    borderRadius: "6px",
                    border: "none",
                    cursor: "pointer",
                    fontSize: "12px",
                    fontWeight: 600,
                    background: authMode === key ? "var(--primary, #8838dd)" : "transparent",
                    color: authMode === key ? "#fff" : "var(--text-muted, #98a2b3)",
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
          )}

          {/* Field 1: Identifier / Keywords */}
          <div>
            <label style={{ display: "block", fontSize: "13px", fontWeight: 600, color: "var(--text-body, #f2f4f7)", marginBottom: "6px" }}>
              Account Keywords / Identifier
            </label>
            <input
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              placeholder="e.g., Marketing Account #1"
              style={modalInputStyle}
              required={!isUpdate}
            />
          </div>

          {/* Field 2a: stored login credentials */}
          {credentialsMode && (
            <>
              <div>
                <label style={{ display: "block", fontSize: "13px", fontWeight: 600, color: "var(--text-body, #f2f4f7)", marginBottom: "6px" }}>
                  Username / Email / Handle
                </label>
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="scraper.account@example.com"
                  autoComplete="off"
                  style={modalInputStyle}
                  required={!isUpdate}
                />
              </div>
              <div>
                <label style={{ display: "block", fontSize: "13px", fontWeight: 600, color: "var(--text-body, #f2f4f7)", marginBottom: "6px" }}>
                  Password
                </label>
                <div style={{ position: "relative" }}>
                  <input
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    type={showPassword ? "text" : "password"}
                    autoComplete="new-password"
                    placeholder={isUpdate ? "Leave blank to keep the stored password" : ""}
                    style={{ ...modalInputStyle, paddingRight: "56px" }}
                    required={!isUpdate}
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((v) => !v)}
                    style={{
                      position: "absolute", right: "8px", top: "50%", transform: "translateY(-50%)",
                      background: "transparent", border: "none", cursor: "pointer",
                      color: "var(--text-muted, #98a2b3)", fontSize: "11px", fontWeight: 600,
                    }}
                  >
                    {showPassword ? "Hide" : "Show"}
                  </button>
                </div>
              </div>
              <div>
                <label style={{ display: "block", fontSize: "13px", fontWeight: 600, color: "var(--text-body, #f2f4f7)", marginBottom: "6px" }}>
                  2FA Secret Key (TOTP) — optional
                </label>
                <input
                  value={twoFactorSecret}
                  onChange={(e) => setTwoFactorSecret(e.target.value)}
                  placeholder="JBSWY3DPEHPK3PXP"
                  autoComplete="off"
                  spellCheck={false}
                  style={{ ...modalInputStyle, fontFamily: "var(--font-mono, monospace)" }}
                />
                <p style={{ margin: "6px 0 0", fontSize: "11px", lineHeight: 1.45, color: "var(--text-muted, #98a2b3)" }}>
                  The base32 secret shown when you set up your authenticator app (usually 16–32
                  characters, spaces are fine). With it, this account can clear its own 2FA prompt
                  and sign itself back in. Without it, a login that hits 2FA will stop and wait for you.
                </p>
              </div>
              <p style={{ margin: 0, padding: "9px 11px", borderRadius: "8px", fontSize: "11px", lineHeight: 1.45, background: "var(--bg-surface-alt, #1d2939)", border: "1px solid var(--border-color, #344054)", color: "var(--text-muted, #98a2b3)" }}>
                Stored so this account can sign itself back in when it is found logged out. Use a
                dedicated scraper account, never a personal one — these are kept in the tool's own
                database in readable form.
              </p>
            </>
          )}

          {/* Field 2b: Cookies / API Key */}
          <div style={{ display: credentialsMode ? "none" : "block" }}>
            <label style={{ display: "block", fontSize: "13px", fontWeight: 600, color: "var(--text-body, #f2f4f7)", marginBottom: "6px" }}>
              {isApiKeyType ? "API Key" : "Paste JSON Cookies"}
            </label>
            {isApiKeyType ? (
              <input
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="AIzaSy..."
                style={modalInputStyle}
                required={!isUpdate}
              />
            ) : (
              <textarea
                value={cookieBlob}
                onChange={(e) => setCookieBlob(e.target.value)}
                placeholder='[{"name": "c_user", "value": "..."}]'
                style={{ ...modalInputStyle, height: "140px", resize: "vertical", fontFamily: "var(--font-mono, monospace)", fontSize: "12px" }}
                required={!isUpdate && !credentialsMode}
              />
            )}
          </div>

          {/* Actions */}
          <div style={{ display: "flex", gap: "10px", marginTop: "8px", justifyContent: "flex-end" }}>
            <button
              type="button"
              onClick={onClose}
              className="action-btn"
              style={{
                padding: "9px 16px",
                borderRadius: "8px",
                background: "var(--bg-surface-3, #344054)",
                border: "1px solid var(--border-color, #344054)",
                color: "var(--text-primary, #fff)",
                fontSize: "13px",
                fontWeight: 600,
                cursor: "pointer"
              }}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSubmitting}
              className="action-btn"
              style={{
                padding: "9px 20px",
                borderRadius: "8px",
                background: "var(--primary, #8838dd)",
                border: "1px solid var(--border-subtle, rgba(255,255,255,0.1))",
                color: "var(--text-primary, #fff)",
                fontSize: "13px",
                fontWeight: 600,
                cursor: "pointer"
              }}
            >
              {isSubmitting
                ? credentialsMode
                  ? "Logging in and initializing persistent session…"
                  : "Saving..."
                : credentialsMode && !isUpdate
                  ? "Save & Log In"
                  : "Save Session"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};

// Telegram's MTProto login can't reuse the generic cookie/API-key form: it's
// a three-step handshake (api id/hash + phone -> code -> optional 2FA
// password) with server-side state held between steps, see
// backend/services/telegram_login_service.py.
const TelegramLoginModal: FC<{
  onClose: () => void;
  onSuccess: () => void;
}> = ({ onClose, onSuccess }) => {
  const [step, setStep] = useState<"phone" | "code" | "password">("phone");
  const [apiId, setApiId] = useState<string>("");
  const [apiHash, setApiHash] = useState<string>("");
  const [phone, setPhone] = useState<string>("");
  const [code, setCode] = useState<string>("");
  const [password, setPassword] = useState<string>("");
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string>("");

  const handleClose = () => {
    if (step !== "phone") {
      sessionsApi.telegramLoginCancel().catch(() => {});
    }
    onClose();
  };

  const handleSendCode = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);
    setError("");
    try {
      await sessionsApi.telegramLoginStart(Number(apiId.trim()), apiHash.trim(), phone.trim());
      setStep("code");
    } catch (err: any) {
      setError(err?.message || "Failed to request a login code");
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleSubmitCode = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);
    setError("");
    try {
      const res = await sessionsApi.telegramLoginCode(code.trim());
      if (res.status === "need_password") {
        setStep("password");
      } else {
        onSuccess();
      }
    } catch (err: any) {
      setError(err?.message || "Code rejected");
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleSubmitPassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);
    setError("");
    try {
      await sessionsApi.telegramLoginPassword(password);
      onSuccess();
    } catch (err: any) {
      setError(err?.message || "Wrong password");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div style={{
      position: "fixed", top: 0, left: 0, right: 0, bottom: 0,
      background: "rgba(0, 0, 0, 0.75)", backdropFilter: "blur(4px)",
      display: "flex", alignItems: "center", justifyContent: "center",
      zIndex: 1000, padding: "20px"
    }}>
      <div style={{
        background: "var(--bg-surface, #1e2837)",
        border: "1px solid var(--border-color, #344054)",
        borderRadius: "12px", padding: "24px", width: "100%", maxWidth: "440px",
        boxShadow: "0 10px 25px rgba(0, 0, 0, 0.5)", maxHeight: "90vh", overflowY: "auto"
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
          <h3 style={{ margin: 0, fontSize: "17px", fontWeight: 700, color: "var(--text-primary, #fff)" }}>
            Add Session — Telegram
          </h3>
          <button
            onClick={handleClose}
            style={{ background: "transparent", border: "none", color: "var(--text-muted, #98a2b3)", cursor: "pointer", fontSize: "16px", fontWeight: 700 }}
          >
            ✕
          </button>
        </div>

        {error && (
          <div style={{
            marginBottom: "14px", padding: "10px 12px", borderRadius: "8px",
            background: "rgba(240, 68, 56, 0.1)", border: "1px solid rgba(240, 68, 56, 0.3)",
            color: "var(--red, #f04438)", fontSize: "12px"
          }}>
            {error}
          </div>
        )}

        {step === "phone" && (
          <form onSubmit={handleSendCode} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
            <div>
              <label style={{ display: "block", fontSize: "13px", fontWeight: 600, color: "var(--text-body, #f2f4f7)", marginBottom: "6px" }}>
                API ID
              </label>
              <input value={apiId} onChange={(e) => setApiId(e.target.value)} placeholder="12345678" style={modalInputStyle} required />
            </div>
            <div>
              <label style={{ display: "block", fontSize: "13px", fontWeight: 600, color: "var(--text-body, #f2f4f7)", marginBottom: "6px" }}>
                API Hash
              </label>
              <input value={apiHash} onChange={(e) => setApiHash(e.target.value)} placeholder="from my.telegram.org" style={modalInputStyle} required />
            </div>
            <div>
              <label style={{ display: "block", fontSize: "13px", fontWeight: 600, color: "var(--text-body, #f2f4f7)", marginBottom: "6px" }}>
                Phone Number
              </label>
              <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+15551234567" style={modalInputStyle} required />
            </div>
            <div style={{ display: "flex", gap: "10px", marginTop: "8px", justifyContent: "flex-end" }}>
              <button type="button" onClick={handleClose} className="action-btn" style={modalCancelBtnStyle}>Cancel</button>
              <button type="submit" disabled={isSubmitting} className="action-btn" style={modalSubmitBtnStyle}>
                {isSubmitting ? "Sending..." : "Send Code"}
              </button>
            </div>
          </form>
        )}

        {step === "code" && (
          <form onSubmit={handleSubmitCode} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
            <div style={{ fontSize: "12px", color: "var(--text-muted, #98a2b3)" }}>
              A login code was sent to {phone.trim()} via Telegram.
            </div>
            <div>
              <label style={{ display: "block", fontSize: "13px", fontWeight: 600, color: "var(--text-body, #f2f4f7)", marginBottom: "6px" }}>
                Login Code
              </label>
              <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="12345" style={modalInputStyle} required autoFocus />
            </div>
            <div style={{ display: "flex", gap: "10px", marginTop: "8px", justifyContent: "flex-end" }}>
              <button type="button" onClick={handleClose} className="action-btn" style={modalCancelBtnStyle}>Cancel</button>
              <button type="submit" disabled={isSubmitting} className="action-btn" style={modalSubmitBtnStyle}>
                {isSubmitting ? "Verifying..." : "Verify Code"}
              </button>
            </div>
          </form>
        )}

        {step === "password" && (
          <form onSubmit={handleSubmitPassword} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
            <div style={{ fontSize: "12px", color: "var(--text-muted, #98a2b3)" }}>
              This account has two-factor authentication enabled.
            </div>
            <div>
              <label style={{ display: "block", fontSize: "13px", fontWeight: 600, color: "var(--text-body, #f2f4f7)", marginBottom: "6px" }}>
                2FA Password
              </label>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} style={modalInputStyle} required autoFocus />
            </div>
            <div style={{ display: "flex", gap: "10px", marginTop: "8px", justifyContent: "flex-end" }}>
              <button type="button" onClick={handleClose} className="action-btn" style={modalCancelBtnStyle}>Cancel</button>
              <button type="submit" disabled={isSubmitting} className="action-btn" style={modalSubmitBtnStyle}>
                {isSubmitting ? "Verifying..." : "Unlock"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
};

const modalCancelBtnStyle: CSSProperties = {
  padding: "9px 16px", borderRadius: "8px", background: "var(--bg-surface-3, #344054)",
  border: "1px solid var(--border-color, #344054)", color: "var(--text-primary, #fff)",
  fontSize: "13px", fontWeight: 600, cursor: "pointer"
};

const modalSubmitBtnStyle: CSSProperties = {
  padding: "9px 20px", borderRadius: "8px", background: "var(--primary, #8838dd)",
  border: "1px solid var(--border-subtle, rgba(255,255,255,0.1))", color: "var(--text-primary, #fff)",
  fontSize: "13px", fontWeight: 600, cursor: "pointer"
};

const modalInputStyle: CSSProperties = {
  width: "100%",
  background: "var(--bg-app, #101828)",
  border: "1px solid var(--border-color, #344054)",
  borderRadius: "8px",
  color: "var(--text-primary, #fff)",
  fontSize: "13px",
  padding: "10px 12px",
  outline: "none",
  boxSizing: "border-box"
};
