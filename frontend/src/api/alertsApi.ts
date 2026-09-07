/**
 * REST API client for Alerts, Live Incidents, Email notifications, and Session Canary.
 */

const API_BASE = "";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let err = "Request failed";
    try {
      const data = await res.json();
      err = data.detail || err;
    } catch {
      err = `${res.status} ${res.statusText}`;
    }
    throw new Error(err);
  }
  return res.json() as Promise<T>;
}

export interface AlertSettings {
  alert_emails: string[];
  smtp_host: string;
  smtp_port: number;
  smtp_user: string;
  smtp_pass: string;
  alert_from: string;
  smtp_ssl: boolean;
  alert_on_session_dead: boolean;
  alert_on_session_expiring: boolean;
  alert_on_critical_incident: boolean;
  session_expiry_warning_hours: number;
}

export interface Incident {
  id: string;
  platform: string;
  kind: string;
  scope: string;
  job_id: string;
  error_type: string;
  severity: "critical" | "warning" | "info" | string;
  message: string;
  cause?: string;
  fix?: string;
  ts: string;
}

export interface IncidentsResponse {
  incidents: Incident[];
  counts: Record<string, number>;
}

export interface PlatformCanarySummary {
  platform: string;
  status: "healthy" | "warning" | "error" | "unconfigured" | string;
  total: number;
  available: number;
  dead: number;
  warnings: Array<{
    platform: string;
    session_id: string;
    identifier: string;
    remaining_hours: number;
  }>;
  check_details: Record<string, any>;
}

export interface CanaryReport {
  last_run: string | null;
  overall_healthy: boolean;
  platforms: Record<string, PlatformCanarySummary>;
  warnings: string[];
  errors: string[];
}

export const alertsApi = {
  getSettings: () =>
    fetch(`${API_BASE}/alerts/settings`).then(json<AlertSettings>),

  saveSettings: (settings: Partial<AlertSettings>) =>
    fetch(`${API_BASE}/alerts/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    }).then(json<AlertSettings>),

  sendTestEmail: (toEmail?: string) =>
    fetch(`${API_BASE}/alerts/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to_email: toEmail || null }),
    }).then(json<{ ok: boolean; detail: string }>),

  getIncidents: (params?: { limit?: number; severity?: string; platform?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.severity) q.set("severity", params.severity);
    if (params?.platform) q.set("platform", params.platform);
    const qs = q.toString() ? `?${q.toString()}` : "";
    return fetch(`${API_BASE}/alerts/incidents${qs}`).then(json<IncidentsResponse>);
  },

  dismissIncident: (id: string) =>
    fetch(`${API_BASE}/alerts/incidents/${id}`, { method: "DELETE" }).then(
      json<{ ok: boolean }>
    ),

  clearAllIncidents: () =>
    fetch(`${API_BASE}/alerts/incidents`, { method: "DELETE" }).then(
      json<{ ok: boolean; cleared: number }>
    ),

  runCanarySweep: () =>
    fetch(`${API_BASE}/alerts/canary/run`, { method: "POST" }).then(
      json<CanaryReport>
    ),

  getCanaryStatus: () =>
    fetch(`${API_BASE}/alerts/canary/status`).then(json<CanaryReport>),
};
