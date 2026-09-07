import React, { useEffect, useState, useCallback } from "react";
import toast from "react-hot-toast";
import {
  alertsApi,
  type AlertSettings,
  type Incident,
  type CanaryReport,
} from "../api/alertsApi";
import { PlatformIcon } from "../components/PlatformIcon";
import {
  AlertBellIcon,
  RefreshIcon,
  ShieldIcon,
  ZapIcon,
  AlertTriangleIcon,
} from "../components/AppIcons";

type SubTab = "incidents" | "canary" | "settings" | "guide";

const SMTP_PRESETS: {
  name: string;
  host: string;
  port: number;
  ssl: boolean;
  notes: string;
}[] = [
  {
    name: "Gmail",
    host: "smtp.gmail.com",
    port: 587,
    ssl: false,
    notes: "Requires a Google App Password (not your normal password). Generate at myaccount.google.com/apppasswords.",
  },
  {
    name: "Outlook / 365",
    host: "smtp.office365.com",
    port: 587,
    ssl: false,
    notes: "Requires an App Password or SMTP AUTH enabled in Microsoft 365 Admin.",
  },
  {
    name: "SendGrid",
    host: "smtp.sendgrid.net",
    port: 587,
    ssl: false,
    notes: "Use 'apikey' as the SMTP username, and your SendGrid API key as the password.",
  },
  {
    name: "Mailgun",
    host: "smtp.mailgun.org",
    port: 587,
    ssl: false,
    notes: "Use your Mailgun SMTP credentials (postmaster@yourdomain.com).",
  },
  {
    name: "Local Relay",
    host: "localhost",
    port: 1025,
    ssl: false,
    notes: "Useful for local development with MailHog, Mailpit, or local SMTP simulators.",
  },
];

export function AlertsIncidentsPanel() {
  const [subTab, setSubTab] = useState<SubTab>("incidents");
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [canary, setCanary] = useState<CanaryReport | null>(null);
  const [settings, setSettings] = useState<AlertSettings | null>(null);

  const [loading, setLoading] = useState(false);
  const [runningCanary, setRunningCanary] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [sendingTest, setSendingTest] = useState(false);

  const [severityFilter, setSeverityFilter] = useState<string>("");
  const [newEmailInput, setNewEmailInput] = useState<string>("");
  const [testEmailTarget, setTestEmailTarget] = useState<string>("");
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [activePreset, setActivePreset] = useState<(typeof SMTP_PRESETS)[0] | null>(null);

  const refreshAll = useCallback(async () => {
    setLoading(true);
    try {
      const [incRes, canaryRes, setRes] = await Promise.all([
        alertsApi.getIncidents({ severity: severityFilter }),
        alertsApi.getCanaryStatus(),
        alertsApi.getSettings(),
      ]);
      setIncidents(incRes.incidents);
      setCounts(incRes.counts);
      setCanary(canaryRes);
      setSettings(setRes);
    } catch (err: any) {
      toast.error(`Failed to load alert telemetry: ${err.message}`);
    } finally {
      setLoading(false);
    }
  }, [severityFilter]);

  useEffect(() => {
    void refreshAll();
  }, [refreshAll]);

  const handleRunCanary = async () => {
    setRunningCanary(true);
    try {
      const rep = await alertsApi.runCanarySweep();
      setCanary(rep);
      const incRes = await alertsApi.getIncidents({ severity: severityFilter });
      setIncidents(incRes.incidents);
      setCounts(incRes.counts);
      if (rep.overall_healthy) {
        toast.success("Canary sweep complete: all pooled platforms are healthy.");
      } else {
        toast.error(`Canary sweep: ${rep.errors.length} platform issue(s) detected.`);
      }
    } catch (err: any) {
      toast.error(`Canary check failed: ${err.message}`);
    } finally {
      setRunningCanary(false);
    }
  };

  const handleDismissIncident = async (id: string) => {
    try {
      await alertsApi.dismissIncident(id);
      setIncidents((prev) => prev.filter((i) => i.id !== id));
      toast.success("Incident dismissed");
    } catch (err: any) {
      toast.error(`Failed to dismiss: ${err.message}`);
    }
  };

  const handleClearAllIncidents = async () => {
    if (!confirm("Clear all recorded incidents?")) return;
    try {
      const res = await alertsApi.clearAllIncidents();
      setIncidents([]);
      setCounts({});
      toast.success(`Cleared ${res.cleared} incident(s)`);
    } catch (err: any) {
      toast.error(`Failed to clear incidents: ${err.message}`);
    }
  };

  const handleAddEmail = () => {
    const email = newEmailInput.trim().toLowerCase();
    if (!email || !email.includes("@")) {
      toast.error("Please enter a valid email address");
      return;
    }
    if (settings?.alert_emails.includes(email)) {
      toast.error("Email is already in recipient list");
      return;
    }
    setSettings((prev) =>
      prev ? { ...prev, alert_emails: [...prev.alert_emails, email] } : prev
    );
    setNewEmailInput("");
  };

  const handleRemoveEmail = (email: string) => {
    setSettings((prev) =>
      prev
        ? { ...prev, alert_emails: prev.alert_emails.filter((e) => e !== email) }
        : prev
    );
  };

  const handleApplyPreset = (preset: (typeof SMTP_PRESETS)[0]) => {
    setActivePreset(preset);
    setSettings((prev) =>
      prev
        ? {
            ...prev,
            smtp_host: preset.host,
            smtp_port: preset.port,
            smtp_ssl: preset.ssl,
          }
        : prev
    );
    toast.success(`Applied ${preset.name} configuration presets`);
  };

  const handleSaveSettings = async () => {
    if (!settings) return;
    setSavingSettings(true);
    try {
      const updated = await alertsApi.saveSettings(settings);
      setSettings(updated);
      toast.success("Alert settings saved and active in backend");
    } catch (err: any) {
      toast.error(`Failed to save settings: ${err.message}`);
    } finally {
      setSavingSettings(false);
    }
  };

  const handleSendTestEmail = async () => {
    if (!settings?.smtp_host?.trim()) {
      const msg = "SMTP host is not configured yet. Please select a preset (like Gmail or Outlook) and enter your credentials above.";
      setTestResult({ ok: false, message: msg });
      toast.error(msg);
      return;
    }

    const targetEmail = testEmailTarget.trim();
    if (!targetEmail && (!settings?.alert_emails || settings.alert_emails.length === 0)) {
      const msg = "Please enter an email address in the 'Specific test email' field or add an Alert Recipient above.";
      setTestResult({ ok: false, message: msg });
      toast.error(msg);
      return;
    }

    setSendingTest(true);
    setTestResult(null);
    try {
      // Auto-save settings first so that any newly entered credentials or presets are applied immediately
      const saved = await alertsApi.saveSettings(settings);
      setSettings(saved);

      const res = await alertsApi.sendTestEmail(targetEmail || undefined);
      setTestResult({ ok: true, message: res.detail || "Test email dispatched successfully!" });
      toast.success("Test alert email dispatched successfully!");
    } catch (err: any) {
      setTestResult({ ok: false, message: err.message });
      toast.error(`SMTP Test Failed: ${err.message}`);
    } finally {
      setSendingTest(false);
    }
  };

  const criticalCount = counts.critical || 0;
  const warningCount = counts.warning || 0;
  const totalIncidents = incidents.length;

  return (
    <div style={{ animation: "fadeUp 0.35s ease", display: "flex", flexDirection: "column", gap: "20px" }}>
      {/* ─── Hero Overview Banner ──────────────────────────────────── */}
      <div
        style={{
          background: "linear-gradient(135deg, var(--background-color-dark, #101828) 0%, var(--background-color-dark2, #1D2939) 100%)",
          border: "1px solid var(--border-color, #344054)",
          borderRadius: "12px",
          padding: "20px 24px",
          boxShadow: "0 4px 20px rgba(0, 0, 0, 0.25)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          flexWrap: "wrap",
          gap: "20px",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "18px" }}>
          <div
            style={{
              width: "50px",
              height: "50px",
              borderRadius: "10px",
              background: canary?.overall_healthy
                ? "linear-gradient(135deg, rgba(136, 56, 221, 0.2) 0%, rgba(154, 80, 233, 0.1) 100%)"
                : "linear-gradient(135deg, rgba(221, 56, 59, 0.2) 0%, rgba(247, 144, 9, 0.1) 100%)",
              border: `1px solid ${canary?.overall_healthy ? "rgba(136, 56, 221, 0.45)" : "rgba(221, 56, 59, 0.45)"}`,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: canary?.overall_healthy ? "var(--ext-link-color, #9A50E9)" : "var(--alert-color, #DD383B)",
              boxShadow: canary?.overall_healthy
                ? "0 0 16px rgba(136, 56, 221, 0.2)"
                : "0 0 16px rgba(221, 56, 59, 0.2)",
            }}
          >
            <AlertBellIcon size={24} color="currentColor" />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
              <span style={{ fontSize: "18px", fontWeight: 700, color: "var(--text-main, #FFFFFF)", letterSpacing: "-0.3px" }}>
                Threat Alerts & Proactive Canary Watchdog
              </span>
              <span
                style={{
                  fontSize: "11px",
                  fontWeight: 700,
                  letterSpacing: "0.6px",
                  textTransform: "uppercase",
                  padding: "3px 10px",
                  borderRadius: "20px",
                  background: canary?.overall_healthy
                    ? "rgba(0, 193, 77, 0.12)"
                    : "rgba(221, 56, 59, 0.12)",
                  color: canary?.overall_healthy ? "var(--success-color, #00C14D)" : "var(--alert-color, #DD383B)",
                  border: `1px solid ${canary?.overall_healthy ? "rgba(0, 193, 77, 0.35)" : "rgba(221, 56, 59, 0.35)"}`,
                }}
              >
                {canary?.overall_healthy ? "ALL POOLS OPERATIONAL" : "ACTION REQUIRED"}
              </span>
            </div>

            <div style={{ fontSize: "13px", color: "var(--text-muted, #98A2B3)", marginTop: "4px" }}>
              {canary?.last_run
                ? `Proactive canary check ran at ${new Date(canary.last_run).toLocaleTimeString()} • ${
                    canary.warnings.length
                  } expiration warning(s) • ${settings?.alert_emails.length || 0} active email recipient(s)`
                : "Continuous 30m heartbeat monitoring active across Facebook, Instagram, Twitter, Telegram, TikTok & YouTube."}
            </div>
          </div>
        </div>

        {/* Quick Actions */}
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <button
            className="btn btn-secondary"
            onClick={refreshAll}
            disabled={loading}
            style={{ display: "inline-flex", alignItems: "center", gap: "6px", fontSize: "13px" }}
          >
            <RefreshIcon size={14} />
            <span>Refresh</span>
          </button>

          <button
            className="btn btn-primary"
            onClick={handleRunCanary}
            disabled={runningCanary}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "8px",
              padding: "9px 18px",
              fontSize: "13px",
              fontWeight: 600,
            }}
          >
            <ZapIcon size={14} color="#FFFFFF" />
            <span>{runningCanary ? "Checking Sessions…" : "Run Canary Sweep Now"}</span>
          </button>
        </div>
      </div>

      {/* ─── Segmented Navigation Bar ───────────────────────────────── */}
      <div
        style={{
          display: "flex",
          gap: "8px",
          background: "var(--background-color-dark, #101828)",
          border: "1px solid var(--border-color, #344054)",
          padding: "5px",
          borderRadius: "8px",
        }}
      >
        <button
          onClick={() => setSubTab("incidents")}
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "8px",
            padding: "9px 14px",
            borderRadius: "6px",
            fontSize: "13px",
            fontWeight: 700,
            cursor: "pointer",
            border: subTab === "incidents" ? "1px solid var(--primary, #8838DD)" : "1px solid transparent",
            background: subTab === "incidents" ? "linear-gradient(135deg, rgba(136, 56, 221, 0.22), rgba(154, 80, 233, 0.12))" : "transparent",
            color: subTab === "incidents" ? "var(--text-main, #FFFFFF)" : "var(--text-muted, #98A2B3)",
            boxShadow: subTab === "incidents" ? "0 2px 8px rgba(0, 0, 0, 0.2)" : "none",
            transition: "all 0.15s ease",
          }}
        >
          <AlertBellIcon size={15} color={subTab === "incidents" ? "var(--primary, #8838DD)" : "currentColor"} />
          <span>Live Incidents</span>
          {totalIncidents > 0 && (
            <span
              style={{
                fontSize: "11px",
                fontWeight: 700,
                padding: "1px 7px",
                borderRadius: "10px",
                background: criticalCount > 0 ? "rgba(221, 56, 59, 0.25)" : "rgba(136, 56, 221, 0.3)",
                color: criticalCount > 0 ? "var(--alert-color, #DD383B)" : "var(--ext-link-color, #9A50E9)",
                border: `1px solid ${criticalCount > 0 ? "rgba(221, 56, 59, 0.4)" : "rgba(136, 56, 221, 0.4)"}`,
              }}
            >
              {totalIncidents}
            </span>
          )}
        </button>

        <button
          onClick={() => setSubTab("canary")}
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "8px",
            padding: "9px 14px",
            borderRadius: "6px",
            fontSize: "13px",
            fontWeight: 700,
            cursor: "pointer",
            border: subTab === "canary" ? "1px solid var(--primary, #8838DD)" : "1px solid transparent",
            background: subTab === "canary" ? "linear-gradient(135deg, rgba(136, 56, 221, 0.22), rgba(154, 80, 233, 0.12))" : "transparent",
            color: subTab === "canary" ? "var(--text-main, #FFFFFF)" : "var(--text-muted, #98A2B3)",
            boxShadow: subTab === "canary" ? "0 2px 8px rgba(0, 0, 0, 0.2)" : "none",
            transition: "all 0.15s ease",
          }}
        >
          <ShieldIcon size={15} color={subTab === "canary" ? "var(--primary, #8838DD)" : "currentColor"} />
          <span>Session Canary Matrix</span>
        </button>

        <button
          onClick={() => setSubTab("settings")}
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "8px",
            padding: "9px 14px",
            borderRadius: "6px",
            fontSize: "13px",
            fontWeight: 700,
            cursor: "pointer",
            border: subTab === "settings" ? "1px solid var(--primary, #8838DD)" : "1px solid transparent",
            background: subTab === "settings" ? "linear-gradient(135deg, rgba(136, 56, 221, 0.22), rgba(154, 80, 233, 0.12))" : "transparent",
            color: subTab === "settings" ? "var(--text-main, #FFFFFF)" : "var(--text-muted, #98A2B3)",
            boxShadow: subTab === "settings" ? "0 2px 8px rgba(0, 0, 0, 0.2)" : "none",
            transition: "all 0.15s ease",
          }}
        >
          <span>⚙️ Email Notifications & SMTP</span>
        </button>

        <button
          onClick={() => setSubTab("guide")}
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "8px",
            padding: "9px 14px",
            borderRadius: "6px",
            fontSize: "13px",
            fontWeight: 700,
            cursor: "pointer",
            border: subTab === "guide" ? "1px solid var(--primary, #8838DD)" : "1px solid transparent",
            background: subTab === "guide" ? "linear-gradient(135deg, rgba(136, 56, 221, 0.22), rgba(154, 80, 233, 0.12))" : "transparent",
            color: subTab === "guide" ? "var(--text-main, #FFFFFF)" : "var(--text-muted, #98A2B3)",
            boxShadow: subTab === "guide" ? "0 2px 8px rgba(0, 0, 0, 0.2)" : "none",
            transition: "all 0.15s ease",
          }}
        >
          <span>📖 Setup Guide</span>
        </button>
      </div>

      {/* ─── TAB 1: Live Incidents Feed ─────────────────────────────── */}
      {subTab === "incidents" && (
        <div
          style={{
            background: "var(--bg-card)",
            border: "1px solid var(--border-subtle)",
            borderRadius: "12px",
            padding: "24px",
            display: "flex",
            flexDirection: "column",
            gap: "18px",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              flexWrap: "wrap",
              gap: "12px",
              borderBottom: "1px solid var(--border-subtle)",
              paddingBottom: "14px",
            }}
          >
            <div>
              <div style={{ fontSize: "16px", fontWeight: 700, color: "var(--text-main)" }}>
                Live Security & Session Incidents Log
              </div>
              <div style={{ fontSize: "12px", color: "var(--text-muted)", marginTop: "2px" }}>
                Real-time operational alerts: expired tokens, checkpoint challenges, and impersonator discoveries
              </div>
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
              {incidents.length > 0 && (
                <button
                  className="btn btn-secondary"
                  onClick={handleClearAllIncidents}
                  style={{ fontSize: "12px", padding: "5px 12px" }}
                >
                  Clear All Incidents
                </button>
              )}
            </div>
          </div>

          {/* Filter Pills */}
          <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
            {[
              { id: "", label: `All Events (${incidents.length})` },
              { id: "critical", label: `Critical (${counts.critical || 0})` },
              { id: "warning", label: `Warning (${counts.warning || 0})` },
              { id: "info", label: `Informational (${counts.info || 0})` },
            ].map((f) => (
              <button
                key={f.id}
                onClick={() => setSeverityFilter(f.id)}
                style={{
                  padding: "6px 14px",
                  borderRadius: "20px",
                  fontSize: "12px",
                  fontWeight: 600,
                  cursor: "pointer",
                  border:
                    severityFilter === f.id
                      ? "1px solid var(--primary, #8838DD)"
                      : "1px solid var(--border-color, #344054)",
                  background:
                    severityFilter === f.id
                      ? "linear-gradient(135deg, rgba(136, 56, 221, 0.25), rgba(154, 80, 233, 0.15))"
                      : "var(--background-color-dark, #101828)",
                  color: severityFilter === f.id ? "var(--text-main, #FFFFFF)" : "var(--text-muted, #98A2B3)",
                  boxShadow: severityFilter === f.id ? "0 2px 8px rgba(136, 56, 221, 0.2)" : "none",
                  transition: "all 0.15s ease",
                }}
              >
                {f.label}
              </button>
            ))}
          </div>

          {/* Incidents Stream */}
          <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
            {incidents.length === 0 ? (
              <div
                style={{
                  textAlign: "center",
                  padding: "60px 20px",
                  border: "1px dashed var(--border-color, #344054)",
                  borderRadius: "10px",
                  background: "var(--background-color-dark, #101828)",
                }}
              >
                <div style={{ fontSize: "32px", marginBottom: "8px" }}>🛡️</div>
                <div style={{ fontSize: "15px", fontWeight: 700, color: "var(--text-main, #FFFFFF)" }}>
                  No Active Incidents
                </div>
                <div style={{ fontSize: "13px", color: "var(--text-muted, #98A2B3)", marginTop: "4px", maxWidth: "420px", margin: "4px auto 0" }}>
                  All social platform sessions, API keys, and automated sweeps are operating normally without interruption.
                </div>
              </div>
            ) : (
              incidents.map((inc) => (
                <div
                  key={inc.id}
                  style={{
                    background: "var(--background-color-dark, #101828)",
                    border: `1px solid ${
                      inc.severity === "critical"
                        ? "rgba(221, 56, 59, 0.35)"
                        : inc.severity === "warning"
                        ? "rgba(247, 144, 9, 0.35)"
                        : "var(--border-color, #344054)"
                    }`,
                    borderRadius: "10px",
                    padding: "16px 18px",
                    display: "flex",
                    flexDirection: "column",
                    gap: "10px",
                    transition: "border 0.2s ease",
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "8px" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                      <span
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: "6px",
                          padding: "4px 10px",
                          borderRadius: "6px",
                          fontSize: "12px",
                          fontWeight: 700,
                          textTransform: "capitalize",
                          background: "rgba(255, 255, 255, 0.05)",
                          border: "1px solid var(--border-color, #344054)",
                          color: "var(--text-main, #FFFFFF)",
                        }}
                      >
                        <PlatformIcon platform={inc.platform} size={15} />
                        {inc.platform}
                      </span>

                      <span
                        style={{
                          fontSize: "11px",
                          fontWeight: 700,
                          textTransform: "uppercase",
                          padding: "2px 8px",
                          borderRadius: "10px",
                          background:
                            inc.severity === "critical"
                              ? "rgba(221, 56, 59, 0.15)"
                              : inc.severity === "warning"
                              ? "rgba(247, 144, 9, 0.15)"
                              : "rgba(143, 173, 204, 0.15)",
                          color:
                            inc.severity === "critical"
                              ? "var(--alert-color, #DD383B)"
                              : inc.severity === "warning"
                              ? "#F79009"
                              : "var(--tertiary-text-color-dark, #8FADCC)",
                          border: `1px solid ${
                            inc.severity === "critical"
                              ? "rgba(221, 56, 59, 0.35)"
                              : inc.severity === "warning"
                              ? "rgba(247, 144, 9, 0.35)"
                              : "rgba(143, 173, 204, 0.35)"
                          }`,
                        }}
                      >
                        {inc.severity}
                      </span>

                      <span style={{ fontSize: "14px", fontWeight: 700, color: "var(--text-main, #FFFFFF)" }}>
                        {inc.error_type}
                      </span>
                    </div>

                    <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
                      <span style={{ fontSize: "12px", color: "var(--text-muted, #98A2B3)" }}>
                        {new Date(inc.ts).toLocaleString()}
                      </span>
                      <button
                        onClick={() => handleDismissIncident(inc.id)}
                        style={{
                          background: "none",
                          border: "none",
                          color: "var(--text-muted, #98A2B3)",
                          cursor: "pointer",
                          fontSize: "14px",
                          padding: "4px 8px",
                          borderRadius: "4px",
                        }}
                        title="Dismiss incident"
                      >
                        ✕
                      </button>
                    </div>
                  </div>

                  <div style={{ fontSize: "13px", color: "var(--text-main, #FFFFFF)", lineHeight: 1.5 }}>
                    {inc.message}
                  </div>

                  {inc.fix && (
                    <div
                      style={{
                        background: "rgba(136, 56, 221, 0.08)",
                        borderLeft: "3px solid var(--primary, #8838DD)",
                        padding: "10px 14px",
                        borderRadius: "3px",
                        fontSize: "12px",
                        color: "var(--secondary-text-color-dark, #D8D8D8)",
                        display: "flex",
                        alignItems: "flex-start",
                        gap: "8px",
                      }}
                    >
                      <strong style={{ color: "var(--ext-link-color, #9A50E9)" }}>Action:</strong>
                      <span>{inc.fix}</span>
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {/* ─── TAB 2: Session Canary Matrix ──────────────────────────── */}
      {subTab === "canary" && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "18px",
          }}
        >
          <div
            style={{
              background: "var(--bg-card)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "12px",
              padding: "20px 24px",
            }}
          >
            <div style={{ fontSize: "16px", fontWeight: 700, color: "var(--text-main)" }}>
              Platform Session Health & Token Expiration Watchdog
            </div>
            <div style={{ fontSize: "12px", color: "var(--text-muted)", marginTop: "3px" }}>
              Automated canary inspections run every 30 minutes. If tokens are expiring within 24h, you will receive an early warning alert.
            </div>
          </div>

          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
              gap: "16px",
            }}
          >
            {["facebook", "instagram", "twitter", "telegram", "tiktok", "youtube"].map((plat) => {
              const info = canary?.platforms[plat];
              const warnings = info?.warnings || [];
              const status = info?.status || "healthy";

              let statusColor = "#34C759";
              let statusLabel = "HEALTHY";
              if (status === "error") {
                statusColor = "#FF3B30";
                statusLabel = "UNAVAILABLE";
              } else if (status === "warning") {
                statusColor = "#FF9500";
                statusLabel = "EXPIRING SOON";
              } else if (status === "unconfigured") {
                statusColor = "#8E8E93";
                statusLabel = "NOT CONFIGURED";
              }

              return (
                <div
                  key={plat}
                  style={{
                    background: "var(--bg-card)",
                    border: `1px solid ${warnings.length > 0 ? "rgba(255, 149, 0, 0.4)" : "var(--border-subtle)"}`,
                    borderRadius: "12px",
                    padding: "20px",
                    display: "flex",
                    flexDirection: "column",
                    gap: "14px",
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                      <PlatformIcon platform={plat} size={24} />
                      <span style={{ fontSize: "16px", fontWeight: 700, color: "var(--text-main)", textTransform: "capitalize" }}>
                        {plat}
                      </span>
                    </div>

                    <span
                      style={{
                        fontSize: "11px",
                        fontWeight: 700,
                        padding: "3px 10px",
                        borderRadius: "12px",
                        background: `${statusColor}22`,
                        color: statusColor,
                        border: `1px solid ${statusColor}66`,
                      }}
                    >
                      {statusLabel}
                    </span>
                  </div>

                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "1fr 1fr 1fr",
                      gap: "10px",
                      background: "rgba(255, 255, 255, 0.02)",
                      border: "1px solid var(--border-subtle)",
                      borderRadius: "8px",
                      padding: "10px",
                      textAlign: "center",
                    }}
                  >
                    <div>
                      <div style={{ fontSize: "11px", color: "var(--text-muted)" }}>Total Accounts</div>
                      <div style={{ fontSize: "16px", fontWeight: 700, color: "var(--text-main)", marginTop: "2px" }}>
                        {info?.total ?? 0}
                      </div>
                    </div>
                    <div>
                      <div style={{ fontSize: "11px", color: "var(--text-muted)" }}>Available</div>
                      <div style={{ fontSize: "16px", fontWeight: 700, color: "#34C759", marginTop: "2px" }}>
                        {info?.available ?? 0}
                      </div>
                    </div>
                    <div>
                      <div style={{ fontSize: "11px", color: "var(--text-muted)" }}>Quarantined</div>
                      <div style={{ fontSize: "16px", fontWeight: 700, color: (info?.dead ?? 0) > 0 ? "#FF3B30" : "var(--text-muted)", marginTop: "2px" }}>
                        {info?.dead ?? 0}
                      </div>
                    </div>
                  </div>

                  {warnings.length > 0 ? (
                    <div
                      style={{
                        background: "rgba(255, 149, 0, 0.08)",
                        border: "1px solid rgba(255, 149, 0, 0.3)",
                        borderRadius: "6px",
                        padding: "10px 12px",
                        fontSize: "12px",
                        color: "#FF9500",
                        display: "flex",
                        flexDirection: "column",
                        gap: "4px",
                      }}
                    >
                      <div style={{ fontWeight: 700 }}>⚠️ Upcoming Expiration Deadline:</div>
                      {warnings.map((w, idx) => (
                        <div key={idx}>
                          Account <strong>{w.identifier}</strong> expires in ~{w.remaining_hours} hours. Please re-export cookies under Sessions.
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div style={{ fontSize: "12px", color: "var(--text-muted)" }}>
                      ✓ All session cookies and API tokens valid with no immediate expiration deadlines.
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* ─── TAB 3: Email Alerts & SMTP Setup ───────────────────────── */}
      {subTab === "settings" && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: "20px",
          }}
        >
          {/* Left: Recipient Emails & Triggers */}
          <div
            style={{
              background: "var(--bg-card)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "12px",
              padding: "24px",
              display: "flex",
              flexDirection: "column",
              gap: "20px",
            }}
          >
            <div>
              <div style={{ fontSize: "16px", fontWeight: 700, color: "var(--text-main)" }}>
                Alert Recipients & Triggers
              </div>
              <div style={{ fontSize: "12px", color: "var(--text-muted)", marginTop: "3px" }}>
                Define which operators receive alerts and which incident levels trigger immediate emails
              </div>
            </div>

            {/* Recipient Manager */}
            <div>
              <label style={{ display: "block", fontSize: "12px", fontWeight: 700, color: "var(--text-main)", marginBottom: "8px" }}>
                Notification Recipient Addresses
              </label>

              <div style={{ display: "flex", gap: "8px", marginBottom: "10px" }}>
                <input
                  type="email"
                  placeholder="analyst@company.com"
                  value={newEmailInput}
                  onChange={(e) => setNewEmailInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleAddEmail();
                  }}
                  style={{
                    flex: 1,
                    background: "var(--bg-input)",
                    border: "1px solid var(--border-subtle)",
                    borderRadius: "6px",
                    padding: "9px 12px",
                    color: "var(--text-main)",
                    fontSize: "13px",
                  }}
                />
                <button className="btn btn-secondary" onClick={handleAddEmail} style={{ fontSize: "13px", padding: "0 16px" }}>
                  Add
                </button>
              </div>

              {/* Tags */}
              <div style={{ display: "flex", flexWrap: "wrap", gap: "8px", minHeight: "42px" }}>
                {settings?.alert_emails.length === 0 ? (
                  <div style={{ fontSize: "12px", color: "var(--text-muted)", fontStyle: "italic" }}>
                    No recipient addresses registered. Email notifications are currently inactive.
                  </div>
                ) : (
                  settings?.alert_emails.map((email) => (
                    <span
                      key={email}
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: "8px",
                        background: "rgba(136, 56, 221, 0.15)",
                        border: "1px solid rgba(136, 56, 221, 0.4)",
                        borderRadius: "18px",
                        padding: "4px 12px",
                        fontSize: "12px",
                        color: "var(--ext-link-color, #9A50E9)",
                        fontWeight: 600,
                      }}
                    >
                      {email}
                      <button
                        onClick={() => handleRemoveEmail(email)}
                        style={{
                          background: "none",
                          border: "none",
                          color: "var(--ext-link-color, #9A50E9)",
                          cursor: "pointer",
                          padding: 0,
                          fontSize: "13px",
                          lineHeight: 1,
                        }}
                      >
                        ✕
                      </button>
                    </span>
                  ))
                )}
              </div>
            </div>

            {/* Notification Toggles */}
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "12px",
                padding: "16px",
                background: "rgba(255, 255, 255, 0.02)",
                borderRadius: "8px",
                border: "1px solid var(--border-subtle)",
              }}
            >
              <div style={{ fontSize: "13px", fontWeight: 700, color: "var(--text-main)" }}>
                Incident Event Triggers
              </div>

              <label style={{ display: "flex", alignItems: "flex-start", gap: "10px", fontSize: "13px", cursor: "pointer" }}>
                <input
                  type="checkbox"
                  style={{ marginTop: "3px" }}
                  checked={settings?.alert_on_session_dead ?? true}
                  onChange={(e) =>
                    setSettings((prev) => (prev ? { ...prev, alert_on_session_dead: e.target.checked } : prev))
                  }
                />
                <div>
                  <div style={{ fontWeight: 600, color: "var(--text-main)" }}>Session Outage & Checkpoint Alerts</div>
                  <div style={{ fontSize: "12px", color: "var(--text-muted)" }}>
                    Immediate alert when a platform rejects credentials, challenges with 2FA, or enters quarantine.
                  </div>
                </div>
              </label>

              <label style={{ display: "flex", alignItems: "flex-start", gap: "10px", fontSize: "13px", cursor: "pointer" }}>
                <input
                  type="checkbox"
                  style={{ marginTop: "3px" }}
                  checked={settings?.alert_on_session_expiring ?? true}
                  onChange={(e) =>
                    setSettings((prev) => (prev ? { ...prev, alert_on_session_expiring: e.target.checked } : prev))
                  }
                />
                <div>
                  <div style={{ fontWeight: 600, color: "var(--text-main)" }}>Proactive Token Expiry Anticipation</div>
                  <div style={{ fontSize: "12px", color: "var(--text-muted)" }}>
                    Sends an early warning 24 hours before cookies expire so you can refresh before sweeps fail.
                  </div>
                </div>
              </label>

              <label style={{ display: "flex", alignItems: "flex-start", gap: "10px", fontSize: "13px", cursor: "pointer" }}>
                <input
                  type="checkbox"
                  style={{ marginTop: "3px" }}
                  checked={settings?.alert_on_critical_incident ?? true}
                  onChange={(e) =>
                    setSettings((prev) => (prev ? { ...prev, alert_on_critical_incident: e.target.checked } : prev))
                  }
                />
                <div>
                  <div style={{ fontWeight: 600, color: "var(--text-main)" }}>Critical Impersonator Incidents</div>
                  <div style={{ fontSize: "12px", color: "var(--text-muted)" }}>
                    Dispatches alert when high-risk verified brand lookalikes or phishing operations are identified.
                  </div>
                </div>
              </label>
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "auto" }}>
              <button
                className="btn btn-primary"
                onClick={handleSaveSettings}
                disabled={savingSettings}
                style={{ fontSize: "13px", padding: "9px 20px" }}
              >
                {savingSettings ? "Saving Settings…" : "Save Alert Settings"}
              </button>
            </div>
          </div>

          {/* Right: SMTP Server Settings & Live Test */}
          <div
            style={{
              background: "var(--bg-card)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "12px",
              padding: "24px",
              display: "flex",
              flexDirection: "column",
              gap: "18px",
            }}
          >
            <div>
              <div style={{ fontSize: "16px", fontWeight: 700, color: "var(--text-main)", display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
                SMTP Server Configuration
                {settings?.smtp_host ? (
                  <span style={{ fontSize: "11px", fontWeight: 600, padding: "2px 8px", borderRadius: "12px", background: "rgba(52, 199, 89, 0.15)", color: "#34C759", border: "1px solid rgba(52, 199, 89, 0.3)" }}>
                    ● Configured ({settings.smtp_host}:{settings.smtp_port})
                  </span>
                ) : (
                  <span style={{ fontSize: "11px", fontWeight: 600, padding: "2px 8px", borderRadius: "12px", background: "rgba(255, 149, 0, 0.15)", color: "#FF9500", border: "1px solid rgba(255, 149, 0, 0.3)" }}>
                    ⚠️ Not Configured Yet
                  </span>
                )}
              </div>
              <div style={{ fontSize: "12px", color: "var(--text-muted)", marginTop: "3px" }}>
                Connect your mail transport service to deliver formatted HTML security alerts
              </div>
            </div>

            {/* Presets */}
            <div>
              <div style={{ fontSize: "11px", fontWeight: 700, textTransform: "uppercase", color: "var(--text-muted)", marginBottom: "6px" }}>
                1-Click Provider Presets
              </div>
              <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
                {SMTP_PRESETS.map((p) => {
                  const isSelected = activePreset?.name === p.name || settings?.smtp_host === p.host;
                  return (
                    <button
                      key={p.name}
                      onClick={() => handleApplyPreset(p)}
                      style={{
                        padding: "6px 14px",
                        borderRadius: "6px",
                        fontSize: "12px",
                        fontWeight: 600,
                        cursor: "pointer",
                        border: isSelected ? "1px solid var(--primary, #8838DD)" : "1px solid var(--border-color, #344054)",
                        background: isSelected ? "linear-gradient(135deg, rgba(136, 56, 221, 0.25), rgba(154, 80, 233, 0.15))" : "rgba(255, 255, 255, 0.04)",
                        color: isSelected ? "#FFFFFF" : "var(--text-main, #FFFFFF)",
                        boxShadow: isSelected ? "0 2px 8px rgba(136, 56, 221, 0.25)" : "none",
                        transition: "all 0.15s ease",
                      }}
                    >
                      {p.name}
                    </button>
                  );
                })}
              </div>
              {activePreset && (
                <div
                  style={{
                    marginTop: "10px",
                    padding: "10px 14px",
                    borderRadius: "6px",
                    background: "rgba(136, 56, 221, 0.08)",
                    border: "1px solid rgba(136, 56, 221, 0.25)",
                    fontSize: "12px",
                    color: "var(--secondary-text-color-dark, #D8D8D8)",
                    lineHeight: 1.5,
                  }}
                >
                  <strong style={{ color: "var(--ext-link-color, #9A50E9)" }}>{activePreset.name} Setup Note:</strong> {activePreset.notes}
                </div>
              )}
            </div>

            {/* Inputs */}
            <div style={{ display: "grid", gridTemplateColumns: "1.6fr 1fr", gap: "10px" }}>
              <div>
                <label style={{ display: "block", fontSize: "11px", color: "var(--text-muted)", marginBottom: "4px" }}>
                  SMTP Host
                </label>
                <input
                  type="text"
                  placeholder="smtp.gmail.com"
                  value={settings?.smtp_host || ""}
                  onChange={(e) => setSettings((prev) => (prev ? { ...prev, smtp_host: e.target.value } : prev))}
                  style={{
                    width: "100%",
                    background: "var(--bg-input)",
                    border: "1px solid var(--border-subtle)",
                    borderRadius: "6px",
                    padding: "8px 12px",
                    color: "var(--text-main)",
                    fontSize: "13px",
                  }}
                />
              </div>

              <div>
                <label style={{ display: "block", fontSize: "11px", color: "var(--text-muted)", marginBottom: "4px" }}>
                  Port
                </label>
                <input
                  type="number"
                  placeholder="587 / 465"
                  value={settings?.smtp_port || 587}
                  onChange={(e) =>
                    setSettings((prev) =>
                      prev ? { ...prev, smtp_port: parseInt(e.target.value, 10) || 587 } : prev
                    )
                  }
                  style={{
                    width: "100%",
                    background: "var(--bg-input)",
                    border: "1px solid var(--border-subtle)",
                    borderRadius: "6px",
                    padding: "8px 12px",
                    color: "var(--text-main)",
                    fontSize: "13px",
                  }}
                />
              </div>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px" }}>
              <div>
                <label style={{ display: "block", fontSize: "11px", color: "var(--text-muted)", marginBottom: "4px" }}>
                  Username / Auth User
                </label>
                <input
                  type="text"
                  placeholder="alerts@company.com"
                  value={settings?.smtp_user || ""}
                  onChange={(e) => setSettings((prev) => (prev ? { ...prev, smtp_user: e.target.value } : prev))}
                  style={{
                    width: "100%",
                    background: "var(--bg-input)",
                    border: "1px solid var(--border-subtle)",
                    borderRadius: "6px",
                    padding: "8px 12px",
                    color: "var(--text-main)",
                    fontSize: "13px",
                  }}
                />
              </div>

              <div>
                <label style={{ display: "block", fontSize: "11px", color: "var(--text-muted)", marginBottom: "4px" }}>
                  Password / App Secret
                </label>
                <input
                  type="password"
                  placeholder="••••••••"
                  value={settings?.smtp_pass || ""}
                  onChange={(e) => setSettings((prev) => (prev ? { ...prev, smtp_pass: e.target.value } : prev))}
                  style={{
                    width: "100%",
                    background: "var(--bg-input)",
                    border: "1px solid var(--border-subtle)",
                    borderRadius: "6px",
                    padding: "8px 12px",
                    color: "var(--text-main)",
                    fontSize: "13px",
                  }}
                />
              </div>
            </div>

            <div>
              <label style={{ display: "block", fontSize: "11px", color: "var(--text-muted)", marginBottom: "4px" }}>
                From Sender Address
              </label>
              <input
                type="text"
                placeholder="alerts@brand-intelligence.local"
                value={settings?.alert_from || ""}
                onChange={(e) => setSettings((prev) => (prev ? { ...prev, alert_from: e.target.value } : prev))}
                style={{
                  width: "100%",
                  background: "var(--bg-input)",
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "6px",
                  padding: "8px 12px",
                  color: "var(--text-main)",
                  fontSize: "13px",
                }}
              />
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button
                className="btn btn-primary"
                onClick={handleSaveSettings}
                disabled={savingSettings}
                style={{ fontSize: "13px", padding: "8px 20px" }}
              >
                {savingSettings ? "Saving…" : "Save SMTP Settings"}
              </button>
            </div>

            {/* Test Email Box */}
            <div
              style={{
                background: "rgba(255, 255, 255, 0.02)",
                border: "1px solid var(--border-subtle)",
                borderRadius: "8px",
                padding: "14px",
                display: "flex",
                flexDirection: "column",
                gap: "10px",
                marginTop: "6px",
              }}
            >
              <div style={{ fontSize: "12px", fontWeight: 700, color: "var(--text-main)" }}>
                Test SMTP Dispatch & Deliverability
              </div>

              <div style={{ display: "flex", gap: "8px" }}>
                <input
                  type="email"
                  placeholder="Specific test email (optional)"
                  value={testEmailTarget}
                  onChange={(e) => setTestEmailTarget(e.target.value)}
                  style={{
                    flex: 1,
                    background: "var(--bg-input)",
                    border: "1px solid var(--border-subtle)",
                    borderRadius: "6px",
                    padding: "7px 10px",
                    color: "var(--text-main)",
                    fontSize: "12px",
                  }}
                />
                <button
                  className="btn btn-secondary"
                  onClick={handleSendTestEmail}
                  disabled={sendingTest}
                  style={{ fontSize: "12px", whiteSpace: "nowrap" }}
                >
                  {sendingTest ? "Sending…" : "Send Test Email"}
                </button>
              </div>

              {testResult && (
                <div
                  style={{
                    padding: "8px 12px",
                    borderRadius: "6px",
                    fontSize: "12px",
                    background: testResult.ok ? "rgba(52, 199, 89, 0.12)" : "rgba(255, 59, 48, 0.12)",
                    border: `1px solid ${testResult.ok ? "rgba(52, 199, 89, 0.3)" : "rgba(255, 59, 48, 0.3)"}`,
                    color: testResult.ok ? "#34C759" : "#FF3B30",
                  }}
                >
                  {testResult.ok ? "✓ " : "✕ "}
                  {testResult.message}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ─── TAB 4: Setup & Configuration Guide ─────────────────────── */}
      {subTab === "guide" && (
        <div
          style={{
            background: "var(--bg-card)",
            border: "1px solid var(--border-subtle)",
            borderRadius: "12px",
            padding: "28px",
            display: "flex",
            flexDirection: "column",
            gap: "24px",
            maxWidth: "960px",
            margin: "0 auto",
            lineHeight: 1.6,
          }}
        >
          <div>
            <h2 style={{ fontSize: "20px", fontWeight: 700, color: "#FFFFFF", margin: "0 0 6px 0" }}>
              How to Configure Alerts & SMTP Notifications
            </h2>
            <p style={{ fontSize: "13px", color: "var(--text-muted)", margin: 0 }}>
              Follow these simple steps to set up automated operational alerts for session outages and token expiration deadlines.
            </p>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
            {/* Step 1 */}
            <div
              style={{
                background: "var(--background-color-dark, #101828)",
                border: "1px solid var(--border-color, #344054)",
                borderRadius: "10px",
                padding: "18px 20px",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "8px" }}>
                <span
                  style={{
                    width: "26px",
                    height: "26px",
                    borderRadius: "50%",
                    background: "linear-gradient(135deg, rgba(136, 56, 221, 0.25), rgba(154, 80, 233, 0.15))",
                    color: "var(--ext-link-color, #9A50E9)",
                    border: "1px solid rgba(136, 56, 221, 0.4)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: "12px",
                    fontWeight: 700,
                  }}
                >
                  1
                </span>
                <span style={{ fontSize: "15px", fontWeight: 700, color: "var(--text-main, #FFFFFF)" }}>
                  Add Your Recipient Email Address
                </span>
              </div>
              <p style={{ fontSize: "13px", color: "var(--secondary-text-color-dark, #D8D8D8)", margin: "0 0 6px 36px" }}>
                Go to the <strong>Email Notifications & SMTP</strong> tab, type your email (e.g. <code>security@yourcompany.com</code>) into the <strong>Alert Recipient Emails</strong> box, and click <strong>Add</strong>. You can add multiple team members.
              </p>
            </div>

            {/* Step 2 */}
            <div
              style={{
                background: "var(--background-color-dark, #101828)",
                border: "1px solid var(--border-color, #344054)",
                borderRadius: "10px",
                padding: "18px 20px",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "8px" }}>
                <span
                  style={{
                    width: "26px",
                    height: "26px",
                    borderRadius: "50%",
                    background: "linear-gradient(135deg, rgba(136, 56, 221, 0.25), rgba(154, 80, 233, 0.15))",
                    color: "var(--ext-link-color, #9A50E9)",
                    border: "1px solid rgba(136, 56, 221, 0.4)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: "12px",
                    fontWeight: 700,
                  }}
                >
                  2
                </span>
                <span style={{ fontSize: "15px", fontWeight: 700, color: "var(--text-main, #FFFFFF)" }}>
                  Configure Your SMTP Mail Provider
                </span>
              </div>
              <div style={{ fontSize: "13px", color: "var(--secondary-text-color-dark, #D8D8D8)", margin: "0 0 6px 36px", display: "flex", flexDirection: "column", gap: "8px" }}>
                <div>
                  <strong>Using Gmail:</strong> Click the <strong>Gmail</strong> preset button. In your Google Account (with 2FA on), visit <code>myaccount.google.com/apppasswords</code>, generate an App Password named "Brand Intelligence", and paste the 16-letter password into the Password box.
                </div>
                <div>
                  <strong>Using Microsoft 365 / Outlook:</strong> Click <strong>Outlook</strong>. Enter your email and account App Password (port 587).
                </div>
                <div>
                  <strong>Using SendGrid / AWS SES:</strong> Use your provider's SMTP host (e.g. <code>smtp.sendgrid.net</code>), port 587, and your API Key.
                </div>
              </div>
            </div>

            {/* Step 3 */}
            <div
              style={{
                background: "var(--background-color-dark, #101828)",
                border: "1px solid var(--border-color, #344054)",
                borderRadius: "10px",
                padding: "18px 20px",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "8px" }}>
                <span
                  style={{
                    width: "26px",
                    height: "26px",
                    borderRadius: "50%",
                    background: "linear-gradient(135deg, rgba(136, 56, 221, 0.25), rgba(154, 80, 233, 0.15))",
                    color: "var(--ext-link-color, #9A50E9)",
                    border: "1px solid rgba(136, 56, 221, 0.4)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: "12px",
                    fontWeight: 700,
                  }}
                >
                  3
                </span>
                <span style={{ fontSize: "15px", fontWeight: 700, color: "var(--text-main, #FFFFFF)" }}>
                  Verify with a Test Email
                </span>
              </div>
              <p style={{ fontSize: "13px", color: "var(--secondary-text-color-dark, #D8D8D8)", margin: "0 0 6px 36px" }}>
                Click <strong>Save SMTP Settings</strong>, then click <strong>Send Test Email</strong>. You will receive an immediate verification email confirming the connection is working.
              </p>
            </div>

            {/* Step 4 */}
            <div
              style={{
                background: "var(--background-color-dark, #101828)",
                border: "1px solid var(--border-color, #344054)",
                borderRadius: "10px",
                padding: "18px 20px",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "8px" }}>
                <span
                  style={{
                    width: "26px",
                    height: "26px",
                    borderRadius: "50%",
                    background: "linear-gradient(135deg, rgba(136, 56, 221, 0.25), rgba(154, 80, 233, 0.15))",
                    color: "var(--ext-link-color, #9A50E9)",
                    border: "1px solid rgba(136, 56, 221, 0.4)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: "12px",
                    fontWeight: 700,
                  }}
                >
                  4
                </span>
                <span style={{ fontSize: "15px", fontWeight: 700, color: "var(--text-main, #FFFFFF)" }}>
                  How the Proactive Watchdog Works
                </span>
              </div>
              <p style={{ fontSize: "13px", color: "var(--secondary-text-color-dark, #D8D8D8)", margin: "0 0 6px 36px" }}>
                The canary automatically inspects cookie expiration deadlines in the background. If a session cookie for Facebook (<code>xs</code>, <code>c_user</code>), Instagram (<code>sessionid</code>), or Twitter (<code>auth_token</code>) has fewer than 24 hours remaining, an alert is sent automatically before sweeps fail.
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
