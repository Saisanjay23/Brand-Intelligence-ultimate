// Live Activity: Unified Operations Dashboard & Cockpit
//
// 1) ⚡ In-Flight Sweeps   – live job cards with platform chips, progress, and telemetry
// 2) 📜 Job History         – searchable table of completed sweeps with slide-over drawer logs
// 3) 🗄 Data Triage & Retries – unified Retry Queue and Record Browser
//
// Features:
// - Slide-Over Cyber Terminal Drawer (no inline bloat)
// - Slide-Over Incidents Drawer (clean operational diagnostics)
// - Real-time pipeline health telemetry header
//
// Data comes from:
// - GET /jobs, GET /jobs/{id}/events, POST /jobs/{id}/cancel
// - GET /incidents
// - GET /clients
// - GET/POST /profiles, GET/POST /profiles/retry-queue

import React, { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { incidentsApi, type Incident } from "../api/incidentsApi";

import { jobsApi } from "../api/jobsApi";
import { profilesApi } from "../api/profilesApi";
import type { Client, Job, JobEvent, PlatformProgress, Profile } from "../api/types";
import { confirmAction } from "../utils/confirmAction";
import { download } from "../utils/download";
import { PlatformIcon } from "../components/PlatformIcon";
import {
  listClients as directoryClients,
  refresh as refreshDirectory,
  useClientDirectory,
} from "../services/clientDirectory";
import {
  getSnapshot,
  subscribe,
  unfinishedPlatformsOf,
  type ScheduleEntry,
} from "../services/scheduleRunner";
import {
  ZapIcon,
  DiscoverIcon,
  AnalyseIcon,
  ClockIcon,
  LockIcon,
  UnlockIcon,
  SearchIcon,
  TrashIcon,
  DatabaseIcon,
  AlertTriangleIcon,
  DownloadIcon,
  RefreshIcon,
  StopIcon,
  PlayIcon,
  ActivityWaveIcon,
  FilterIcon,
} from "../components/AppIcons";

const JOBS_REFRESH_MS = 4_000;
const INCIDENTS_REFRESH_MS = 15_000;
const LOG_REFRESH_MS = 2_000;
const BROWSE_PAGE_SIZE = 50;

// ─── Helpers ─────────────────────────────────────────────────────────────────

function useNowTick(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

function durationLabel(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return mins < 60 ? `${mins}m ${secs}s` : `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

function exactTime(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
}

const PLATFORM_COLOR: Record<string, string> = {
  facebook: "#1877f2",
  instagram: "#e1306c",
  twitter: "#1da1f2",
  youtube: "#ff0000",
  telegram: "#0088cc",
  tiktok: "#69c9d0",
};

const PLAT_STATUS_LOOK: Record<string, { bg: string; fg: string }> = {
  pending: { bg: "rgba(102,112,133,0.15)", fg: "var(--text-dim, #667085)" },
  running: { bg: "rgba(124,92,255,0.15)", fg: "var(--accent, #7c5cff)" },
  done: { bg: "rgba(54,181,160,0.15)", fg: "var(--success, #36b5a0)" },
  partial: { bg: "rgba(253,183,27,0.15)", fg: "var(--warn-yellow, #fdb71b)" },
  failed: { bg: "rgba(233,80,83,0.15)", fg: "var(--danger, #e95053)" },
  skipped: { bg: "rgba(102,112,133,0.1)", fg: "var(--text-dim, #667085)" },
  // Amber, not red: an interrupted platform is unfinished work the engine
  // will resume by itself on the next lap, not an error to investigate.
  interrupted: { bg: "rgba(253,183,27,0.18)", fg: "var(--warn-yellow, #fdb71b)" },
};

const JOB_STATUS_COLOR: Record<string, string> = {
  queued: "var(--text-dim, #667085)",
  running: "var(--accent, #7c5cff)",
  done: "var(--success, #36b5a0)",
  failed: "var(--danger, #e95053)",
  cancelled: "var(--warn-yellow, #fdb71b)",
};

const PROFILE_STATUS_BADGE: Record<string, string> = {
  pending: "var(--warn-yellow, #fdb71b)",
  approved: "var(--success, #36b5a0)",
  rejected: "var(--danger, #e95053)",
};

const RETRY_STATE_LOOK: Record<string, { color: string; label: string }> = {
  eligible: { color: "var(--accent, #7c5cff)", label: "Eligible" },
  exhausted: { color: "var(--warn-yellow, #fdb71b)", label: "Exhausted" },
  stopped: { color: "var(--danger, #e95053)", label: "Stopped" },
};

const SEVERITY_LOOK: Record<string, { color: string; label: string; bg: string }> = {
  critical: { color: "#ef4444", label: "CRITICAL", bg: "rgba(239, 68, 68, 0.15)" },
  warning: { color: "#fdb71b", label: "WARNING", bg: "rgba(253, 183, 27, 0.15)" },
  info: { color: "#00e5ff", label: "INFO", bg: "rgba(0, 229, 255, 0.15)" },
};

const LOG_TYPE_COLOR: Record<string, string> = {
  info: "var(--text-dim, #667085)",
  debug: "var(--text-dim, #667085)",
  warning: "var(--warn-yellow, #fdb71b)",
  warn: "var(--warn-yellow, #fdb71b)",
  error: "var(--danger, #e95053)",
  failed: "var(--danger, #e95053)",
  discovery: "#2ee9d6",
  analysis: "var(--accent, #7c5cff)",
  success: "var(--success, #36b5a0)",
  hit: "var(--success, #36b5a0)",
};

// ─── Embedded Styles ─────────────────────────────────────────────────────────

const PANEL_STYLES = `
@keyframes radarPulse {
  0%,100% { transform: scale(1); opacity: 0.9; }
  50% { transform: scale(1.4); opacity: 0.25; }
}
@keyframes drawerSlideIn {
  from { transform: translateX(100%); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}
@keyframes backdropFadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}
.la-tab {
  transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}
.la-tab:hover {
  background: rgba(255,255,255,0.06) !important;
  color: var(--text-primary, #fff) !important;
}
.la-tab.active {
  background: var(--bg-surface, #1e2837) !important;
  color: var(--accent, #7c5cff) !important;
  border-color: rgba(124,92,255,0.4) !important;
}
.la-job-card {
  transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
}
.la-job-card:hover {
  border-color: rgba(124,92,255,0.45) !important;
  transform: translateY(-2px);
}
.la-action-btn {
  transition: all 0.18s ease;
}
.la-action-btn:hover {
  background: var(--bg-surface-3, #344054) !important;
  border-color: rgba(255,255,255,0.2) !important;
}
.la-terminal-line:hover {
  background: rgba(255,255,255,0.04);
  border-radius: 4px;
}
.la-health-tile {
  transition: transform 0.18s ease, border-color 0.18s ease;
}
.la-health-tile:hover {
  border-color: rgba(0, 229, 255, 0.4) !important;
  transform: translateY(-1px);
}
`;

// ─── UI Atoms ────────────────────────────────────────────────────────────────

function StatusDot({ color, pulse = true }: { color: string; pulse?: boolean }) {
  return (
    <span style={{ position: "relative", display: "inline-flex", alignItems: "center", justifyContent: "center", width: 10, height: 10, flexShrink: 0 }}>
      <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, display: "block" }} />
      {pulse && (
        <span
          style={{
            position: "absolute",
            width: 14,
            height: 14,
            borderRadius: "50%",
            background: color,
            opacity: 0.3,
            animation: "radarPulse 2s ease-in-out infinite",
          }}
        />
      )}
    </span>
  );
}

function Badge({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        color,
        fontWeight: 700,
        fontSize: "11px",
        textTransform: "uppercase",
        letterSpacing: "0.4px",
        background: `${color}18`,
        padding: "2px 8px",
        borderRadius: "999px",
        border: `1px solid ${color}33`,
      }}
    >
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: color, flexShrink: 0 }} />
      {children}
    </span>
  );
}

function EmptyState({ icon, title, text, action }: { icon: React.ReactNode; title: string; text: string; action?: React.ReactNode }) {
  return (
    <div
      style={{
        padding: "48px 24px",
        textAlign: "center",
        background: "var(--bg-surface, #1e2837)",
        border: "1px dashed rgba(255,255,255,0.12)",
        borderRadius: 14,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 12,
      }}
    >
      <div style={{ width: 56, height: 56, borderRadius: "50%", background: "rgba(124,92,255,0.1)", display: "flex", alignItems: "center", justifyContent: "center" }}>
        {icon}
      </div>
      <div>
        <h4 style={{ margin: "0 0 6px 0", fontSize: 15, fontWeight: 700, color: "var(--text-primary, #fff)" }}>{title}</h4>
        <p style={{ margin: 0, fontSize: 13, color: "var(--text-dim, #667085)", maxWidth: 420 }}>{text}</p>
      </div>
      {action && <div style={{ marginTop: 8 }}>{action}</div>}
    </div>
  );
}

// ─── Slide-Over Cyber Terminal Drawer ────────────────────────────────────────

const LOG_FILTER_PILLS = [
  { id: "", label: "All Logs" },
  { id: "hit,discovery,analysis,success", label: "🎯 Hits & Matches" },
  { id: "error,failed,warning,warn", label: "⚠ Errors & Warnings" },
  { id: "discovery", label: "Discovery" },
  { id: "analysis", label: "Analysis" },
];

function CyberTerminalDrawer({
  jobId,
  clientName,
  jobKind,
  jobStatus,
  onClose,
}: {
  jobId: string | null;
  clientName: string;
  jobKind?: string;
  jobStatus?: string;
  onClose: () => void;
}) {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [filterText, setFilterText] = useState("");
  const [filterPill, setFilterPill] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const lastSeq = useRef(0);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    setEvents([]);
    lastSeq.current = 0;

    const poll = () => {
      jobsApi
        .jobEvents(jobId, lastSeq.current)
        .then((r) => {
          if (cancelled || !r.items.length) return;
          lastSeq.current = r.items[r.items.length - 1].seq;
          setEvents((prev) => [...prev, ...r.items].slice(-800));
        })
        .catch(() => {});
    };

    poll();
    const t = setInterval(poll, LOG_REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [jobId]);

  useEffect(() => {
    if (autoScroll && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [events, autoScroll]);

  // Keyboard shortcut ESC to dismiss
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && jobId) onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [jobId, onClose]);

  const filteredEvents = useMemo(() => {
    let list = events;
    if (filterPill) {
      const types = new Set(filterPill.split(","));
      list = list.filter((e) => types.has(e.type.toLowerCase()));
    }
    if (filterText.trim()) {
      const q = filterText.toLowerCase();
      list = list.filter((e) => e.message.toLowerCase().includes(q) || e.type.toLowerCase().includes(q));
    }
    return list;
  }, [events, filterText, filterPill]);

  const handleDownload = () => {
    if (!jobId) return;
    const text = events
      .map((e) => `[${e.ts || new Date().toISOString()}] [${e.type.toUpperCase()}] ${e.message}`)
      .join("\n");
    download(`job-${jobId}-log.txt`, text, "text/plain");
  };

  if (!jobId) return null;

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 9999,
        display: "flex",
        justifyContent: "flex-end",
        animation: "backdropFadeIn 0.2s ease-out",
      }}
    >
      {/* Dim backdrop */}
      <div
        onClick={onClose}
        style={{
          position: "absolute",
          inset: 0,
          background: "rgba(4, 8, 16, 0.65)",
          backdropFilter: "blur(4px)",
        }}
      />

      {/* Drawer content */}
      <div
        style={{
          position: "relative",
          width: "min(720px, 94vw)",
          height: "100%",
          background: "#080f1e",
          borderLeft: "1px solid rgba(124,92,255,0.35)",
          boxShadow: "-12px 0 40px rgba(0,0,0,0.6)",
          display: "flex",
          flexDirection: "column",
          animation: "drawerSlideIn 0.24s cubic-bezier(0.16, 1, 0.3, 1)",
          zIndex: 2,
        }}
      >
        {/* Drawer Header */}
        <div
          style={{
            padding: "16px 20px",
            background: "rgba(124,92,255,0.08)",
            borderBottom: "1px solid rgba(124,92,255,0.2)",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: 12,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 10, overflow: "hidden" }}>
            <div style={{ width: 34, height: 34, borderRadius: 8, background: "rgba(124,92,255,0.2)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
              <ActivityWaveIcon size={18} color="var(--cyan)" />
            </div>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <h3 style={{ margin: 0, fontSize: 16, fontWeight: 700, color: "var(--text-primary, #fff)" }}>
                  Cyber Terminal
                </h3>
                {jobKind && (
                  <span style={{ fontSize: 10, padding: "2px 6px", borderRadius: 4, background: "rgba(255,255,255,0.08)", color: "var(--text-dim)", textTransform: "uppercase" }}>
                    {jobKind}
                  </span>
                )}
                {jobStatus && <Badge color={JOB_STATUS_COLOR[jobStatus] ?? "var(--text-dim)"}>{jobStatus}</Badge>}
              </div>
              <div style={{ fontSize: 12, color: "var(--text-dim)", fontFamily: "var(--font-mono)", marginTop: 2 }}>
                {clientName} · job/{jobId.slice(0, 10)}…
              </div>
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <button
              type="button"
              onClick={() => setAutoScroll((v) => !v)}
              style={{
                fontSize: 11,
                padding: "6px 10px",
                borderRadius: 6,
                background: autoScroll ? "rgba(54,181,160,0.15)" : "rgba(255,255,255,0.06)",
                border: `1px solid ${autoScroll ? "var(--success, #36b5a0)" : "rgba(255,255,255,0.12)"}`,
                color: autoScroll ? "var(--success, #36b5a0)" : "var(--text-dim)",
                cursor: "pointer",
                fontWeight: 600,
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
              }}
            >
              {autoScroll ? <><LockIcon size={12} /> Auto-Scroll ON</> : <><UnlockIcon size={12} /> Free Scroll</>}
            </button>
            <button
              type="button"
              onClick={handleDownload}
              disabled={!events.length}
              style={{
                fontSize: 11,
                padding: "6px 10px",
                borderRadius: 6,
                background: "rgba(255,255,255,0.06)",
                border: "1px solid rgba(255,255,255,0.12)",
                color: "var(--text-primary, #fff)",
                cursor: events.length ? "pointer" : "not-allowed",
                fontWeight: 600,
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
              }}
            >
              <DownloadIcon size={13} /> Export
            </button>
            <button
              type="button"
              onClick={onClose}
              style={{
                width: 32,
                height: 32,
                borderRadius: 8,
                background: "rgba(255,255,255,0.06)",
                border: "1px solid rgba(255,255,255,0.1)",
                color: "var(--text-muted)",
                cursor: "pointer",
                fontWeight: 700,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              ✕
            </button>
          </div>
        </div>

        {/* Filter bar */}
        <div
          style={{
            padding: "10px 16px",
            borderBottom: "1px solid rgba(255,255,255,0.06)",
            background: "rgba(0,0,0,0.2)",
            display: "flex",
            flexDirection: "column",
            gap: 8,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
            {LOG_FILTER_PILLS.map((pill) => (
              <button
                key={pill.id}
                type="button"
                onClick={() => setFilterPill(pill.id)}
                style={{
                  fontSize: 11,
                  padding: "4px 10px",
                  borderRadius: 999,
                  cursor: "pointer",
                  fontWeight: 600,
                  background: filterPill === pill.id ? "rgba(124,92,255,0.25)" : "transparent",
                  border: `1px solid ${filterPill === pill.id ? "var(--accent, #7c5cff)" : "rgba(255,255,255,0.1)"}`,
                  color: filterPill === pill.id ? "var(--accent, #7c5cff)" : "var(--text-dim)",
                }}
              >
                {pill.label}
              </button>
            ))}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div style={{ position: "relative", flex: 1, display: "flex", alignItems: "center" }}>
              <SearchIcon size={13} color="var(--text-dim)" style={{ position: "absolute", left: 10 }} />
              <input
                type="text"
                value={filterText}
                onChange={(e) => setFilterText(e.target.value)}
                placeholder="Search raw console output, URL, or JSON payload…"
                style={{
                  width: "100%",
                  fontSize: 12,
                  padding: "6px 10px 6px 30px",
                  borderRadius: 6,
                  background: "rgba(255,255,255,0.04)",
                  border: "1px solid rgba(255,255,255,0.1)",
                  color: "var(--text-main)",
                  outline: "none",
                  fontFamily: "var(--font-mono)",
                }}
              />
            </div>
            <span style={{ fontSize: 11, color: "var(--text-dim)", whiteSpace: "nowrap", fontFamily: "var(--font-mono)" }}>
              {filteredEvents.length}/{events.length} lines
            </span>
          </div>
        </div>

        {/* Live Stream Terminal Box */}
        <div
          ref={boxRef}
          style={{
            flex: 1,
            overflowY: "auto",
            padding: "12px 16px",
            fontFamily: "var(--font-mono)",
            fontSize: 11.5,
            lineHeight: "1.7",
            background: "#050914",
          }}
        >
          {filteredEvents.length === 0 ? (
            <div style={{ color: "var(--text-dim)", textAlign: "center", paddingTop: 80 }}>
              {events.length === 0 ? (
                <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10 }}>
                  <StatusDot color="var(--accent, #7c5cff)" />
                  <span>Waiting for stdout logs from worker engine…</span>
                </div>
              ) : (
                "No lines match the current filter query."
              )}
            </div>
          ) : (
            filteredEvents.map((e) => {
              const t = e.type.toLowerCase();
              const col = LOG_TYPE_COLOR[t] ?? "var(--text-dim)";
              return (
                <div key={e.seq} className="la-terminal-line" style={{ display: "flex", gap: 10, padding: "2px 6px" }}>
                  <span style={{ color: "rgba(255,255,255,0.25)", flexShrink: 0, userSelect: "none" }}>
                    {e.ts ? new Date(e.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : ""}
                  </span>
                  <span style={{ color: col, flexShrink: 0, fontWeight: 700, minWidth: 84, textTransform: "uppercase", letterSpacing: "0.3px" }}>
                    [{e.type}]
                  </span>
                  <span style={{ color: col !== "var(--text-dim, #667085)" ? "rgba(255,255,255,0.9)" : "rgba(255,255,255,0.6)", wordBreak: "break-word" }}>
                    {e.message}
                  </span>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Slide-Over Incidents Drawer ─────────────────────────────────────────────

function IncidentsDrawer({
  isOpen,
  onClose,
  activeSeverity,
  onSelectSeverity,
}: {
  isOpen: boolean;
  onClose: () => void;
  activeSeverity: string;
  onSelectSeverity: (s: string) => void;
}) {
  const [items, setItems] = useState<Incident[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [expanded, setExpanded] = useState<string>("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    setLoading(true);

    const load = () => {
      incidentsApi
        .list(60, activeSeverity)
        .then((r) => {
          if (cancelled) return;
          setItems(r.items);
          setCounts(r.counts);
          setError("");
        })
        .catch((e) => !cancelled && setError((e as Error).message))
        .finally(() => !cancelled && setLoading(false));
    };

    load();
    const t = setInterval(load, INCIDENTS_REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [isOpen, activeSeverity]);

  // ESC key handler
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isOpen) onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const pill = (value: string, label: string, color: string) => (
    <button
      key={value || "all"}
      type="button"
      onClick={() => onSelectSeverity(value)}
      style={{
        padding: "4px 12px",
        borderRadius: "999px",
        fontSize: "11px",
        fontWeight: 700,
        cursor: "pointer",
        border: `1px solid ${activeSeverity === value ? color : "rgba(255,255,255,0.1)"}`,
        background: activeSeverity === value ? `${color}25` : "transparent",
        color: activeSeverity === value ? color : "var(--text-muted)",
      }}
    >
      {label}
      {counts[value] != null ? ` (${counts[value]})` : ""}
    </button>
  );

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 9999,
        display: "flex",
        justifyContent: "flex-end",
        animation: "backdropFadeIn 0.2s ease-out",
      }}
    >
      {/* Dim backdrop */}
      <div
        onClick={onClose}
        style={{
          position: "absolute",
          inset: 0,
          background: "rgba(4, 8, 16, 0.65)",
          backdropFilter: "blur(4px)",
        }}
      />

      {/* Drawer */}
      <div
        style={{
          position: "relative",
          width: "min(680px, 94vw)",
          height: "100%",
          background: "var(--bg-surface, #1e2837)",
          borderLeft: "1px solid rgba(255,255,255,0.12)",
          boxShadow: "-12px 0 40px rgba(0,0,0,0.6)",
          display: "flex",
          flexDirection: "column",
          animation: "drawerSlideIn 0.24s cubic-bezier(0.16, 1, 0.3, 1)",
          zIndex: 2,
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: "16px 20px",
            borderBottom: "1px solid rgba(255,255,255,0.08)",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            background: "rgba(0,0,0,0.15)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ width: 34, height: 34, borderRadius: 8, background: "rgba(253,183,27,0.15)", display: "flex", alignItems: "center", justifyContent: "center" }}>
              <AlertTriangleIcon size={18} color="var(--warn-yellow)" />
            </div>
            <div>
              <h3 style={{ margin: 0, fontSize: 16, fontWeight: 700, color: "var(--text-primary, #fff)" }}>
                Pipeline Incidents & Health
              </h3>
              <p style={{ margin: "2px 0 0 0", fontSize: 12, color: "var(--text-dim)" }}>
                Operational failures, checkpoint locks, and parser errors
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            style={{
              width: 32,
              height: 32,
              borderRadius: 8,
              background: "rgba(255,255,255,0.06)",
              border: "1px solid rgba(255,255,255,0.1)",
              color: "var(--text-muted)",
              cursor: "pointer",
              fontWeight: 700,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            ✕
          </button>
        </div>

        {/* Severity Filter Pills */}
        <div
          style={{
            padding: "10px 18px",
            borderBottom: "1px solid rgba(255,255,255,0.06)",
            display: "flex",
            alignItems: "center",
            gap: 8,
            flexWrap: "wrap",
            background: "rgba(0,0,0,0.1)",
          }}
        >
          {pill("", "All Severities", "var(--cyan, #00e5ff)")}
          {pill("critical", "Critical", SEVERITY_LOOK.critical.color)}
          {pill("warning", "Warning", SEVERITY_LOOK.warning.color)}
          {pill("info", "Info", SEVERITY_LOOK.info.color)}
        </div>

        {/* Incident List */}
        <div style={{ flex: 1, overflowY: "auto", padding: "14px 18px", display: "flex", flexDirection: "column", gap: 10 }}>
          {error && <div style={{ fontSize: 13, color: "var(--danger)" }}>{error}</div>}

          {!error && items.length === 0 && (
            <div style={{ textAlign: "center", padding: "60px 20px", color: "var(--text-dim)" }}>
              <div style={{ fontSize: 32, marginBottom: 12 }}>🛡️</div>
              <h4 style={{ margin: "0 0 4px 0", color: "var(--text-primary, #fff)" }}>Pipeline is Healthy</h4>
              <p style={{ margin: 0, fontSize: 13 }}>
                No active incidents recorded{activeSeverity ? ` at '${activeSeverity}' severity` : ""}.
              </p>
            </div>
          )}

          {items.map((i) => {
            const look = SEVERITY_LOOK[i.severity] ?? { color: "var(--text-dim)", label: i.severity || "?", bg: "rgba(255,255,255,0.05)" };
            const open = expanded === i.id;
            return (
              <div
                key={i.id}
                onClick={() => setExpanded(open ? "" : i.id)}
                style={{
                  padding: "12px 14px",
                  borderRadius: 10,
                  background: "var(--bg-app, #101828)",
                  border: `1px solid ${open ? look.color + "55" : "rgba(255,255,255,0.08)"}`,
                  borderLeft: `4px solid ${look.color}`,
                  cursor: "pointer",
                  transition: "all 0.15s ease",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <span
                    style={{
                      fontSize: 10,
                      fontWeight: 800,
                      padding: "2px 7px",
                      borderRadius: 4,
                      background: look.bg,
                      color: look.color,
                      letterSpacing: "0.5px",
                    }}
                  >
                    {look.label}
                  </span>
                  <strong style={{ fontSize: 13, color: "var(--text-primary, #fff)" }}>
                    {i.platform}/{i.kind}
                  </strong>
                  <span style={{ fontSize: 12, color: "var(--text-dim)" }}>{i.error_type}</span>
                  <span style={{ flex: 1 }} />
                  <span style={{ fontSize: 11, color: "var(--text-dim)" }}>{relativeTime(i.ts)}</span>
                </div>

                <div
                  style={{
                    fontSize: 12.5,
                    color: "var(--text-muted, #98a2b3)",
                    marginTop: 6,
                    ...(open ? {} : { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }),
                  }}
                >
                  {i.message}
                </div>

                {open && (
                  <div style={{ marginTop: 12, paddingTop: 10, borderTop: "1px dashed rgba(255,255,255,0.1)", fontSize: 12, color: "var(--text-dim)", lineHeight: 1.6 }}>
                    {i.cause && (
                      <div style={{ marginBottom: 6 }}>
                        <strong style={{ color: "var(--text-primary, #fff)" }}>Cause:</strong> {i.cause}
                      </div>
                    )}
                    {i.fix && (
                      <div style={{ marginBottom: 6 }}>
                        <strong style={{ color: "var(--success, #36b5a0)" }}>Recommended Fix:</strong> {i.fix}
                      </div>
                    )}
                    {i.where && (
                      <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, background: "rgba(0,0,0,0.3)", padding: "8px 10px", borderRadius: 6, marginTop: 6, whiteSpace: "pre-wrap", color: "var(--text-dim)" }}>
                        {i.where}
                      </div>
                    )}
                    <div style={{ marginTop: 8, fontSize: 11, color: "var(--text-dim)" }}>
                      {exactTime(i.ts)} {i.job_id ? `· job ${i.job_id}` : ""} {i.scope ? `· scope: ${i.scope}` : ""}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ─── Platform Progress Chip ──────────────────────────────────────────────────

function PlatformChip({ pid, p, now }: { pid: string; p: PlatformProgress; now: number }) {
  const look = PLAT_STATUS_LOOK[p.status] ?? { bg: "rgba(102,112,133,0.1)", fg: "var(--text-dim)" };
  const pct = p.total > 0 ? Math.min(100, Math.round((p.processed / p.total) * 100)) : 0;
  const liveElapsed = p.started
    ? p.status === "running"
      ? Math.floor((now - p.started * 1000) / 1000)
      : p.elapsed_seconds
    : null;
  const accentColor = PLATFORM_COLOR[pid] ?? "#7c5cff";
  const isRunning = p.status === "running";

  return (
    <div
      style={{
        background: "var(--bg-app, #101828)",
        border: `1px solid ${isRunning ? accentColor + "66" : "rgba(255,255,255,0.08)"}`,
        borderRadius: 10,
        padding: "10px 14px",
        minWidth: 160,
        flex: "1 1 160px",
        display: "flex",
        flexDirection: "column",
        gap: 6,
        boxShadow: isRunning ? `0 0 12px ${accentColor}18` : "none",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <PlatformIcon platform={pid} size={18} />
          <span style={{ fontSize: 12, fontWeight: 700, color: "var(--text-primary, #fff)", textTransform: "capitalize" }}>
            {pid}
          </span>
        </div>
        <span
          style={{
            fontSize: 9,
            fontWeight: 700,
            padding: "2px 7px",
            borderRadius: 999,
            background: look.bg,
            color: look.fg,
            border: `1px solid ${look.fg}55`,
            textTransform: "uppercase",
            letterSpacing: "0.4px",
          }}
        >
          {p.status}
        </span>
      </div>

      {p.total > 0 && (
        <div>
          <div style={{ height: 4, borderRadius: 2, background: "rgba(255,255,255,0.08)", overflow: "hidden" }}>
            <div
              style={{
                height: "100%",
                width: `${pct}%`,
                background: isRunning ? `linear-gradient(90deg, ${accentColor}bb, ${accentColor})` : look.fg,
                transition: "width 0.5s ease",
              }}
            />
          </div>
          <div style={{ fontSize: 10, color: "var(--text-dim, #667085)", marginTop: 4, display: "flex", justifyContent: "space-between" }}>
            <span>
              {p.processed}/{p.total} ({pct}%)
            </span>
            {liveElapsed !== null && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
                <ClockIcon size={11} /> {durationLabel(liveElapsed)}
              </span>
            )}
          </div>
        </div>
      )}

      {isRunning && p.done_items && p.done_items.length > 0 && (
        <div style={{ fontSize: 10, color: "var(--text-dim)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
          <span style={{ color: look.fg, fontWeight: 600 }}>Just:</span> {p.done_items[p.done_items.length - 1]}
        </div>
      )}
      {isRunning && p.eta_seconds != null && (
        <div style={{ fontSize: 10, color: look.fg, fontWeight: 600 }}>ETA ~{durationLabel(p.eta_seconds)}</div>
      )}
    </div>
  );
}

// ─── Modern In-Flight Job Card ───────────────────────────────────────────────

function InFlightJobCard({
  job,
  clientName,
  now,
  onStop,
  onOpenTerminal,
  onBrowseRecords,
  stopping,
}: {
  job: Job;
  clientName: string;
  now: number;
  onStop: (id: string) => void;
  onOpenTerminal: () => void;
  onBrowseRecords: () => void;
  stopping: boolean;
}) {
  const statusColor = JOB_STATUS_COLOR[job.status] ?? "var(--text-dim)";
  const elapsed = job.started ? Math.floor((now - new Date(job.started).getTime()) / 1000) : null;
  const platformIds = Object.keys(job.platforms || {});
  const canStop = job.status === "queued" || job.status === "running";
  const isRunning = job.status === "running";
  const totalDone = platformIds.reduce((s, pid) => s + (job.platforms[pid]?.processed ?? 0), 0);
  const throughput = elapsed && elapsed > 30 ? Math.round((totalDone / elapsed) * 60) : null;

  return (
    <div
      className="la-job-card"
      style={{
        background: "var(--bg-surface, #1e2837)",
        border: `1px solid ${isRunning ? "rgba(124,92,255,0.3)" : "rgba(255,255,255,0.08)"}`,
        borderRadius: 14,
        padding: "16px 18px",
        boxShadow: isRunning ? "0 4px 24px rgba(124,92,255,0.12)" : "none",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
        {/* Left: Job Meta */}
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            {job.kind === "discovery" ? <DiscoverIcon size={18} color="var(--cyan)" /> : <AnalyseIcon size={18} color="#7c5cff" />}
            <h4 style={{ margin: 0, fontSize: 16, fontWeight: 700, color: "var(--text-primary, #fff)" }}>
              {clientName}
            </h4>
            <span
              style={{
                fontSize: 11,
                color: "var(--text-dim)",
                textTransform: "capitalize",
                background: "rgba(255,255,255,0.06)",
                padding: "2px 8px",
                borderRadius: 999,
                fontWeight: 600,
              }}
            >
              {job.kind}
            </span>
            {isRunning ? <StatusDot color="var(--accent, #7c5cff)" /> : <Badge color={statusColor}>{job.status}</Badge>}
          </div>

          <div style={{ fontSize: 12, color: "var(--text-dim, #98a2b3)", display: "flex", flexWrap: "wrap", gap: "6px 16px" }}>
            <span title={exactTime(job.started)}>Started {relativeTime(job.started)}</span>
            {elapsed !== null && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                <ClockIcon size={12} /> {durationLabel(elapsed)}
              </span>
            )}
            {throughput !== null && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 4, color: "var(--success, #36b5a0)" }}>
                <ZapIcon size={12} color="var(--success, #36b5a0)" /> ~{throughput} items/min
              </span>
            )}
            {job.blocked_by && (
              <span style={{ color: "var(--warn-yellow, #fdb71b)" }}>
                · waiting on {job.blocked_by.client_id}'s {job.blocked_by.kind}
              </span>
            )}
          </div>
        </div>

        {/* Right: Actions */}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button
            type="button"
            className="la-action-btn"
            onClick={onOpenTerminal}
            style={{
              padding: "7px 13px",
              borderRadius: 8,
              background: "rgba(124,92,255,0.15)",
              border: "1px solid rgba(124,92,255,0.4)",
              color: "var(--accent, #7c5cff)",
              fontSize: 12,
              fontWeight: 700,
              cursor: "pointer",
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <ActivityWaveIcon size={14} color="var(--accent, #7c5cff)" /> Terminal & Logs
          </button>
          <button
            type="button"
            className="la-action-btn"
            onClick={onBrowseRecords}
            style={{
              padding: "7px 13px",
              borderRadius: 8,
              background: "var(--bg-surface-3, #1d2939)",
              border: "1px solid rgba(255,255,255,0.12)",
              color: "var(--text-body, #fff)",
              fontSize: 12,
              fontWeight: 600,
              cursor: "pointer",
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <DatabaseIcon size={13} /> Records
          </button>
          {canStop && (
            <button
              type="button"
              onClick={() => onStop(job.id)}
              disabled={stopping}
              style={{
                padding: "7px 13px",
                borderRadius: 8,
                background: "rgba(233,80,83,0.12)",
                border: "1px solid rgba(233,80,83,0.4)",
                color: "var(--danger, #e95053)",
                fontSize: 12,
                fontWeight: 700,
                cursor: stopping ? "wait" : "pointer",
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
              }}
            >
              <StopIcon size={12} color="var(--danger)" /> {stopping ? "Stopping…" : "Stop"}
            </button>
          )}
        </div>
      </div>

      {/* Platform Chips Grid */}
      {platformIds.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 16 }}>
          {platformIds.map((pid) => (
            <PlatformChip key={pid} pid={pid} p={job.platforms[pid]} now={now} />
          ))}
        </div>
      )}

      {job.message && (
        <div
          style={{
            marginTop: 12,
            padding: "6px 10px",
            background: "rgba(0,0,0,0.2)",
            borderRadius: 6,
            fontSize: 11,
            color: "var(--text-dim)",
            fontFamily: "var(--font-mono)",
          }}
        >
          {job.message}
        </div>
      )}
    </div>
  );
}

// ─── Main Panel Component ───────────────────────────────────────────────────

const LA_SELECT_STYLE: React.CSSProperties = {
  background: "var(--bg-inner, #0b1220)",
  border: "1px solid var(--border-color, #344054)",
  borderRadius: 8,
  padding: "8px 12px",
  color: "var(--text-main)",
  fontSize: 12,
  outline: "none",
};

// ═════════════════════════════════════════════════════════════════════════
// CLIENT COVERAGE
//
// Answers the one question no other view can: for THIS client, which
// platforms finished their last sweep and which still owe work? An in-flight
// job card shows one run in progress; the Scheduler tab shows one aggregate
// word per client. Neither can express "Instagram and X are done, Facebook
// lost its session halfway" -- the most common partial outcome, and the only
// one that needs following up.
//
// Fed by THE SCHEDULER'S OWN RECORD (services/scheduleRunner.ts), not a
// server route. This tab used to poll GET /scheduler/status, written after
// every turn by a server-side round-robin engine -- and that engine went
// with the old backend. The rebuilt API has no /scheduler at all, so every
// poll 404'd, this tab was a permanent "Could not load coverage", and its
// badge sat at zero however much work was owing. The scheduler now runs in
// the browser, and its loop records each sweep's own per-platform breakdown
// as the job reports it: the same data the deleted engine used to persist.
//
// SCOPE, therefore: what THIS browser's scheduler has run. A sweep started
// by hand from the Discovery page, or by someone on another machine, is not
// in here -- which is why every label below says "scheduled sweep" rather
// than "sweep".
// ═════════════════════════════════════════════════════════════════════════

// Only the saved-client half of the list needs polling at all now; the
// scheduler's own record arrives by subscription.
const COVERAGE_REFRESH_MS = 6_000;

const OUTCOME_LABEL: Record<string, string> = {
  done: "done",
  partial: "partial",
  failed: "failed",
  skipped: "no session",
  running: "running",
  // A platform the sweep listed but never got to, because it was cancelled
  // or stopped first. Not an error, just unswept.
  pending: "not reached",
};

// One row of the list: a scheduler queue entry where there is one, and a
// saved client that has never been through the queue where there is not.
interface CoverageRow {
  client_id: string;
  name: string;
  outcomes: Record<string, string>;
  unfinished: string[];
  neverRun: boolean;
  running: boolean;
  finished_at: number | null;
  note: string;
}

function coverageRows(entries: ScheduleEntry[], currentId: string): CoverageRow[] {
  const rows: CoverageRow[] = entries.map((e) => ({
    client_id: e.client_id,
    name: e.name,
    outcomes: e.platforms ?? {},
    unfinished: unfinishedPlatformsOf(e),
    neverRun: Object.keys(e.platforms ?? {}).length === 0,
    // `currentId` is set only while the run loop is on this client's turn.
    running: Boolean(currentId) && e.client_id === currentId,
    finished_at: e.finished_at,
    note: e.message,
  }));

  // A saved client that has never been near the queue is the biggest
  // coverage gap there is, so it is listed rather than quietly left out --
  // which is exactly what showing only the queue would do.
  const queued = new Set(rows.map((r) => r.client_id));
  for (const c of directoryClients()) {
    if (queued.has(c.client_id)) continue;
    rows.push({
      client_id: c.client_id,
      name: c.name || c.client_id,
      outcomes: {},
      unfinished: [],
      neverRun: true,
      running: false,
      finished_at: null,
      note: "",
    });
  }
  return rows;
}

// "Open" = not fully covered, either because a platform did not finish on
// the last scheduled sweep or because there has never been one. Both are
// work owing; only the wording differs.
const isOpen = (r: CoverageRow): boolean => r.neverRun || r.unfinished.length > 0;

function ClientCoverage({ onCount }: { onCount: (n: number) => void }) {
  const state = useSyncExternalStore(subscribe, getSnapshot);
  const [onlyOpen, setOnlyOpen] = useState(false);

  // The scheduler half of this list is a real subscription and updates the
  // instant a sweep reports in. The never-swept half is not: saved clients
  // live in localStorage, which emits nothing to subscribe to, so a client
  // created while this tab is open would otherwise never appear. Hence a
  // slow tick, purely to re-read them -- `tick` is a `useMemo` dependency
  // and nothing else.
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), COVERAGE_REFRESH_MS);
    return () => clearInterval(t);
  }, []);

  const rows = useMemo(
    () => coverageRows(state.entries, state.currentId),
    [state.entries, state.currentId, tick],
  );
  const openCount = rows.filter(isOpen).length;

  // In an effect, not in render: the badge this feeds is the PARENT's state,
  // and setting parent state during a child's render is both a React warning
  // and a re-render loop.
  useEffect(() => {
    onCount(openCount);
  }, [onCount, openCount]);

  if (!rows.length) {
    return (
      <EmptyState
        icon={<DatabaseIcon size={26} color="var(--cyan)" />}
        title="No clients yet"
        text="Create a client, then queue it on the Scheduler tab. Coverage is recorded as each scheduled sweep reports in."
      />
    );
  }

  const shown = onlyOpen ? rows.filter(isOpen) : rows;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <span style={{ fontSize: 12.5, color: "var(--text-muted, #98a2b3)" }}>
          {openCount > 0
            ? `${openCount} of ${rows.length} client(s) are not fully covered — a platform that did not finish, or no scheduled sweep at all.`
            : `All ${rows.length} client(s) completed every platform on their last scheduled sweep.`}
        </span>
        <button
          type="button"
          onClick={() => setOnlyOpen((v) => !v)}
          style={{
            background: onlyOpen ? "rgba(253,183,27,0.15)" : "transparent",
            border: "1px solid var(--border-subtle, #344054)", borderRadius: 8,
            color: onlyOpen ? "var(--warn-yellow, #fdb71b)" : "var(--text-muted, #98a2b3)",
            fontSize: 11.5, fontWeight: 700, padding: "5px 11px", cursor: "pointer",
            whiteSpace: "nowrap",
          }}
          title="Show only clients with platforms still owing work"
        >
          {onlyOpen ? "Showing unfinished only" : "Show unfinished only"}
        </button>
      </div>

      {shown.map((c) => {
        const ids = Object.keys(c.outcomes).sort();
        const open = isOpen(c);
        return (
          <div
            key={c.client_id}
            style={{
              border: `1px solid ${open ? "rgba(253,183,27,0.35)" : "var(--border-subtle, #344054)"}`,
              borderRadius: 12, padding: "12px 14px",
              background: open ? "rgba(253,183,27,0.05)" : "var(--bg-surface, #1e2837)",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 9, flexWrap: "wrap" }}>
              <strong style={{ fontSize: 13.5 }}>{c.name}</strong>
              {c.running && <Badge color="var(--accent, #7c5cff)">sweeping now</Badge>}
              {c.neverRun && !c.running && <Badge color="var(--text-dim, #667085)">never swept</Badge>}
              {open && !c.neverRun && !c.running && (
                <Badge color="var(--warn-yellow, #fdb71b)">{c.unfinished.length} still owing</Badge>
              )}
              <span style={{ marginLeft: "auto", fontSize: 11.5, color: "var(--text-dim, #667085)" }}>
                {c.running
                  ? "in progress"
                  : c.finished_at
                  ? relativeTime(new Date(c.finished_at).toISOString())
                  : "never run"}
              </span>
            </div>

            {ids.length === 0 ? (
              <div style={{ fontSize: 11.5, color: "var(--text-dim, #667085)", marginTop: 7 }}>
                No per-platform record yet — this client has not completed a scheduled sweep.
                Queue it on the Scheduler tab.
              </div>
            ) : (
              <div style={{ display: "flex", gap: 7, flexWrap: "wrap", marginTop: 9 }}>
                {ids.map((pid) => {
                  const st = c.outcomes[pid];
                  const look = PLAT_STATUS_LOOK[st] ?? PLAT_STATUS_LOOK.skipped;
                  return (
                    <span
                      key={pid}
                      title={`${pid}: ${OUTCOME_LABEL[st] ?? st}`}
                      style={{
                        display: "inline-flex", alignItems: "center", gap: 6,
                        background: look.bg, color: look.fg,
                        borderRadius: 999, padding: "4px 10px",
                        fontSize: 11.5, fontWeight: 700,
                      }}
                    >
                      <PlatformIcon platform={pid} size={14} />
                      <span>{pid}</span>
                      <span style={{ opacity: 0.85, fontWeight: 500 }}>{OUTCOME_LABEL[st] ?? st}</span>
                    </span>
                  );
                })}
              </div>
            )}

            {c.note && (
              <div style={{ fontSize: 11.5, color: "var(--text-muted, #98a2b3)", marginTop: 8 }}>
                {c.note}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function LiveActivityPanel() {
  const [activeTab, setActiveTab] = useState<"live" | "coverage" | "history" | "triage">("live");
  // Clients with platforms still owing work. Lifted out of ClientCoverage
  // so the tab badge stays accurate while another tab is open.
  const [unfinishedClients, setUnfinishedClients] = useState(0);
  const [triageSubTab, setTriageSubTab] = useState<"retry" | "records">("retry");
  const [jobs, setJobs] = useState<Job[]>([]);
  // Subscribed, not copied -- see SchedulerPanel's note.
  const { clients } = useClientDirectory();
  const [error, setError] = useState("");
  const [stoppingId, setStoppingId] = useState("");
  const now = useNowTick();

  // Slide-Over Drawer States
  const [terminalJobId, setTerminalJobId] = useState<string | null>(null);
  const [isIncidentsOpen, setIsIncidentsOpen] = useState(false);
  const [incidentSeverity, setIncidentSeverity] = useState("");
  const [incidentCounts, setIncidentCounts] = useState<Record<string, number>>({});

  // History Tab Filters
  const [historySearch, setHistorySearch] = useState("");
  const [historyClient, setHistoryClient] = useState("");

  // Shared Triage State (Single Unified Client Selector)
  const [triageClientId, setTriageClientId] = useState("");
  const [triagePlatform, setTriagePlatform] = useState("");

  // Record Manager State
  const [browseStatus, setBrowseStatus] = useState("");
  const [browseSearch, setBrowseSearch] = useState("");
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profilesTotal, setProfilesTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [browseLoading, setBrowseLoading] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // Retry Queue State
  const [retryStateFilter, setRetryStateFilter] = useState<"" | "eligible" | "exhausted" | "stopped">("");
  const [retryItems, setRetryItems] = useState<Profile[]>([]);
  const [retryCounts, setRetryCounts] = useState({ eligible: 0, exhausted: 0, stopped: 0 });
  const [retryLoading, setRetryLoading] = useState(false);
  const [retrySelected, setRetrySelected] = useState<Set<string>>(new Set());
  const [retryActingId, setRetryActingId] = useState("");
  const [retryBulkBusy, setRetryBulkBusy] = useState(false);

  // The client directory, from the database. Refreshed on mount rather
  // than trusted from cache, since this panel is where an analyst comes to
  // check coverage across every client there is.
  useEffect(() => {
    void refreshDirectory();
  }, []);

  // Poll Jobs & Incidents Header Counts
  useEffect(() => {
    let cancelled = false;
    const load = () => {
      jobsApi
        .jobs("", 100)
        .then((r) => {
          if (!cancelled) setJobs(r.items);
        })
        .catch((e) => !cancelled && setError((e as Error).message));

      incidentsApi
        .list(20, "")
        .then((r) => {
          if (!cancelled) setIncidentCounts(r.counts || {});
        })
        .catch(() => {});
    };

    load();
    const t = setInterval(load, JOBS_REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const clientName = useMemo(() => {
    const m = new Map(clients.map((c) => [c.client_id, c.name || c.client_id]));
    return (id: string) => m.get(id) || id;
  }, [clients]);

  const activeJobs = jobs.filter((j) => j.status === "queued" || j.status === "running");
  const terminalJobs = jobs.filter((j) => j.status === "done" || j.status === "failed" || j.status === "cancelled");

  // Selected job for terminal drawer meta
  const selectedTerminalJob = useMemo(() => {
    if (!terminalJobId) return null;
    return jobs.find((j) => j.id === terminalJobId) || null;
  }, [terminalJobId, jobs]);

  // Telemetry Calculations
  const totalProcessed = activeJobs.reduce(
    (s, j) => s + Object.values(j.platforms || {}).reduce((ps, p) => ps + (p.processed ?? 0), 0),
    0,
  );
  const totalElapsedSecs = activeJobs.reduce(
    (s, j) => s + (j.started ? Math.floor((now - new Date(j.started).getTime()) / 1000) : 0),
    0,
  );
  const throughputPerMin = totalElapsedSecs > 30 ? Math.round((totalProcessed / totalElapsedSecs) * 60) : null;

  // Stop job
  const stop = async (jobId: string) => {
    if (!(await confirmAction("Stop this job? Whatever hasn't been scraped/analysed yet will be left as-is."))) return;
    setStoppingId(jobId);
    try {
      await jobsApi.cancelJob(jobId);
      const r = await jobsApi.jobs("", 100);
      setJobs(r.items);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStoppingId("");
    }
  };

  // Jump from Job Card to Record Manager
  const browseTo = (clientId: string, platform: string | null) => {
    setTriageClientId(clientId);
    setTriagePlatform(platform || "");
    setOffset(0);
    setTriageSubTab("records");
    setActiveTab("triage");
  };

  // Load Records
  const loadProfiles = () => {
    if (!triageClientId) {
      setProfiles([]);
      setProfilesTotal(0);
      return;
    }
    setBrowseLoading(true);
    profilesApi
      .profiles({
        client_id: triageClientId,
        platform: triagePlatform || undefined,
        status: browseStatus || undefined,
        search: browseSearch || undefined,
        limit: BROWSE_PAGE_SIZE,
        offset,
      })
      .then((r) => {
        setProfiles(r.items);
        setProfilesTotal(r.total);
        setSelected(new Set());
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setBrowseLoading(false));
  };

  useEffect(() => {
    if (activeTab === "triage" && triageSubTab === "records") {
      loadProfiles();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, triageSubTab, triageClientId, triagePlatform, browseStatus, offset]);

  const deleteSelected = async () => {
    if (selected.size === 0) return;
    if (!(await confirmAction(`Permanently delete ${selected.size} profile record(s)? This cannot be undone.`))) return;
    setDeleting(true);
    try {
      await profilesApi.deleteProfiles(Array.from(selected));
      loadProfiles();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDeleting(false);
    }
  };

  const toggleOne = (id: string) =>
    setSelected((prev) => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  const toggleAll = () =>
    setSelected((prev) => (prev.size === profiles.length ? new Set() : new Set(profiles.map((p) => p.id))));

  // Load Retry Queue
  const loadRetryQueue = () => {
    if (!triageClientId) {
      setRetryItems([]);
      setRetryCounts({ eligible: 0, exhausted: 0, stopped: 0 });
      return;
    }
    setRetryLoading(true);
    profilesApi
      .retryQueue(triageClientId, triagePlatform || undefined)
      .then((r) => {
        setRetryItems(r.items);
        setRetryCounts(r.counts);
        setRetrySelected((prev) => {
          const stillPresent = new Set(r.items.map((i) => i.id));
          const next = new Set<string>();
          prev.forEach((id) => {
            if (stillPresent.has(id)) next.add(id);
          });
          return next;
        });
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setRetryLoading(false));
  };

  useEffect(() => {
    if (activeTab !== "triage" || triageSubTab !== "retry" || !triageClientId) return;
    loadRetryQueue();
    const t = setInterval(loadRetryQueue, JOBS_REFRESH_MS);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, triageSubTab, triageClientId, triagePlatform]);

  const visibleRetryItems = retryItems.filter(
    (i) => !retryStateFilter || i.retry_state === retryStateFilter,
  );

  const stopOne = async (id: string) => {
    setRetryActingId(id);
    try {
      await profilesApi.stopRetry(id);
      loadRetryQueue();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRetryActingId("");
    }
  };

  const resumeOne = async (id: string) => {
    setRetryActingId(id);
    try {
      await profilesApi.resumeRetry(id);
      loadRetryQueue();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRetryActingId("");
    }
  };

  const toggleRetrySelected = (id: string) =>
    setRetrySelected((prev) => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  const toggleRetrySelectedAll = () =>
    setRetrySelected((prev) =>
      prev.size === visibleRetryItems.length ? new Set() : new Set(visibleRetryItems.map((i) => i.id)),
    );

  const stopSelectedRetries = async () => {
    if (retrySelected.size === 0) return;
    if (
      !(await confirmAction(
        `Stop automatic retry for ${retrySelected.size} profile(s)? They can be Resumed later, but no future sweep will revisit them until you do.`,
      ))
    )
      return;
    setRetryBulkBusy(true);
    try {
      const res = await profilesApi.bulkStopRetry(Array.from(retrySelected));
      if (res.failed.length) setError(`${res.failed.length} profile(s) could not be stopped.`);
      loadRetryQueue();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRetryBulkBusy(false);
    }
  };

  // Filtered Terminal History Jobs
  const filteredHistoryJobs = useMemo(() => {
    return terminalJobs.filter((job) => {
      if (historyClient && job.client_id !== historyClient) return false;
      if (historySearch.trim()) {
        const q = historySearch.toLowerCase();
        const cName = clientName(job.client_id).toLowerCase();
        if (!cName.includes(q) && !job.id.toLowerCase().includes(q) && !job.kind.toLowerCase().includes(q)) {
          return false;
        }
      }
      return true;
    });
  }, [terminalJobs, historyClient, historySearch, clientName]);

  const criticalIncidentCount = incidentCounts.critical ?? 0;
  const warningIncidentCount = incidentCounts.warning ?? 0;

  const TABS: Array<{ id: "live" | "coverage" | "history" | "triage"; label: string; icon: React.ReactNode; badge?: number }> = [
    { id: "live", label: "In-Flight Sweeps", icon: <ZapIcon size={15} />, badge: activeJobs.length || undefined },
    // Badged with the number of clients still owing platform work -- the
    // one number an operator wants at a glance: how much coverage is
    // currently incomplete.
    { id: "coverage", label: "Client Coverage", icon: <DiscoverIcon size={15} />, badge: unfinishedClients || undefined },
    { id: "history", label: "Job History & Logs", icon: <ClockIcon size={15} />, badge: terminalJobs.length || undefined },
    {
      id: "triage",
      label: "Data Triage & Retries",
      icon: <DatabaseIcon size={15} />,
      badge: retryCounts.exhausted + retryCounts.stopped || undefined,
    },
  ];

  return (
    <div style={{ color: "var(--text-main, #f2f4f7)", paddingBottom: 40 }}>
      <style>{PANEL_STYLES}</style>

      {/* Page Title & Global Action Bar */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 18, flexWrap: "wrap", gap: 12 }}>
        <div>
          <h2 style={{ fontSize: 22, fontWeight: 800, color: "var(--text-primary, #fff)", margin: 0, display: "flex", alignItems: "center", gap: 10 }}>
            <ActivityWaveIcon size={24} color="var(--cyan)" />
            <span>Live Activity & Operations</span>
          </h2>
          <p style={{ fontSize: 13, color: "var(--text-muted, #98a2b3)", margin: "4px 0 0 0" }}>
            Real-time multi-platform scraper telemetry, live terminal inspection, and data triage.
          </p>
        </div>

        {/* Incidents Quick Drawer Trigger */}
        <button
          type="button"
          onClick={() => setIsIncidentsOpen(true)}
          style={{
            padding: "8px 14px",
            borderRadius: 10,
            background: criticalIncidentCount > 0 ? "rgba(239,68,68,0.18)" : warningIncidentCount > 0 ? "rgba(253,183,27,0.18)" : "var(--bg-surface, #1e2837)",
            border: `1px solid ${criticalIncidentCount > 0 ? "rgba(239,68,68,0.5)" : warningIncidentCount > 0 ? "rgba(253,183,27,0.5)" : "rgba(255,255,255,0.1)"}`,
            color: criticalIncidentCount > 0 ? "#ef4444" : warningIncidentCount > 0 ? "var(--warn-yellow, #fdb71b)" : "var(--text-main)",
            fontSize: 12,
            fontWeight: 700,
            cursor: "pointer",
            display: "inline-flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          <AlertTriangleIcon size={15} color={criticalIncidentCount > 0 ? "#ef4444" : warningIncidentCount > 0 ? "#fdb71b" : "var(--cyan)"} />
          <span>
            {criticalIncidentCount > 0
              ? `${criticalIncidentCount} Critical Incident(s)`
              : warningIncidentCount > 0
                ? `${warningIncidentCount} Pipeline Warning(s)`
                : "Pipeline Incidents"}
          </span>
          <span style={{ fontSize: 10, background: "rgba(255,255,255,0.1)", padding: "1px 6px", borderRadius: 999 }}>
            View
          </span>
        </button>
      </div>

      {/* Global Telemetry Strip */}
      <div
        style={{
          display: "flex",
          gap: 12,
          flexWrap: "wrap",
          marginBottom: 22,
          padding: "14px 18px",
          background: "var(--bg-surface, #1e2837)",
          border: "1px solid rgba(124,92,255,0.22)",
          borderRadius: 14,
          boxShadow: "0 4px 20px rgba(0,0,0,0.25)",
        }}
      >
        {/* Pipeline Health */}
        <div
          className="la-health-tile"
          onClick={() => setIsIncidentsOpen(true)}
          style={{ flex: "1 1 150px", display: "flex", flexDirection: "column", gap: 3, cursor: "pointer" }}
        >
          <span style={{ fontSize: 10, color: "var(--text-dim)", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.5px" }}>
            Pipeline Health
          </span>
          <span style={{ fontSize: 16, fontWeight: 800, color: criticalIncidentCount > 0 ? "var(--danger, #ef4444)" : warningIncidentCount > 0 ? "var(--warn-yellow, #fdb71b)" : "var(--success, #36b5a0)", display: "flex", alignItems: "center", gap: 6 }}>
            <StatusDot color={criticalIncidentCount > 0 ? "#ef4444" : warningIncidentCount > 0 ? "#fdb71b" : "#36b5a0"} />
            {criticalIncidentCount > 0 ? "Critical Issues" : warningIncidentCount > 0 ? "Degraded" : "Healthy"}
          </span>
        </div>

        {/* Active Sweeps */}
        <div style={{ flex: "1 1 130px", display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ fontSize: 10, color: "var(--text-dim)", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.5px" }}>
            Active Engines
          </span>
          <span style={{ fontSize: 16, fontWeight: 800, color: activeJobs.length ? "var(--accent, #7c5cff)" : "var(--text-dim)", display: "flex", alignItems: "center", gap: 6 }}>
            {activeJobs.length ? `${activeJobs.length} Running` : "Idle"}
          </span>
        </div>

        {/* Queued */}
        <div style={{ flex: "1 1 120px", display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ fontSize: 10, color: "var(--text-dim)", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.5px" }}>
            Queued
          </span>
          <span style={{ fontSize: 16, fontWeight: 800, color: "var(--warn-yellow, #fdb71b)" }}>
            {activeJobs.filter((j) => j.status === "queued").length} Pending
          </span>
        </div>

        {/* Throughput */}
        <div style={{ flex: "1 1 140px", display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ fontSize: 10, color: "var(--text-dim)", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.5px" }}>
            Live Throughput
          </span>
          <span style={{ fontSize: 16, fontWeight: 800, color: "var(--success, #36b5a0)" }}>
            {throughputPerMin !== null ? `~${throughputPerMin}/min` : "—"}
          </span>
        </div>

        {/* Jobs Done */}
        <div style={{ flex: "1 1 120px", display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ fontSize: 10, color: "var(--text-dim)", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.5px" }}>
            Completed (24h)
          </span>
          <span style={{ fontSize: 16, fontWeight: 800, color: "var(--text-primary, #fff)" }}>
            {terminalJobs.length} Sweeps
          </span>
        </div>
      </div>

      {/* Error Banner */}
      {error && (
        <div
          style={{
            padding: "10px 16px",
            background: "rgba(233,80,83,0.12)",
            border: "1px solid rgba(233,80,83,0.3)",
            color: "var(--danger, #e95053)",
            borderRadius: 10,
            marginBottom: 16,
            fontSize: 13,
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <AlertTriangleIcon size={15} color="var(--danger)" /> {error}
          </span>
          <button
            type="button"
            onClick={() => setError("")}
            style={{ background: "transparent", border: "none", color: "var(--danger, #e95053)", cursor: "pointer", fontWeight: 700, fontSize: 14 }}
          >
            ✕
          </button>
        </div>
      )}

      {/* 3 Main View Tabs */}
      <div
        style={{
          display: "flex",
          gap: 6,
          background: "var(--bg-app, #101828)",
          padding: 5,
          borderRadius: 12,
          border: "1px solid var(--border-color, #344054)",
          marginBottom: 20,
        }}
      >
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={`la-tab${activeTab === tab.id ? " active" : ""}`}
            onClick={() => setActiveTab(tab.id)}
            style={{
              flex: 1,
              padding: "10px 14px",
              borderRadius: 8,
              border: "1px solid transparent",
              background: activeTab === tab.id ? "var(--bg-surface, #1e2837)" : "transparent",
              color: activeTab === tab.id ? "var(--accent, #7c5cff)" : "var(--text-muted, #98a2b3)",
              fontSize: 13,
              fontWeight: activeTab === tab.id ? 700 : 500,
              cursor: "pointer",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 8,
            }}
          >
            {tab.icon}
            <span>{tab.label}</span>
            {tab.badge !== undefined && (
              <span
                style={{
                  padding: "1px 7px",
                  borderRadius: 999,
                  fontSize: 10,
                  fontWeight: 800,
                  background: activeTab === tab.id ? "var(--accent, #7c5cff)" : "var(--bg-surface-3, #344054)",
                  color: "#fff",
                }}
              >
                {tab.badge}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* ════════════════════════════════════════════════════════════════════════
          TAB 1: IN-FLIGHT RUNS
          ════════════════════════════════════════════════════════════════════════ */}
      {activeTab === "live" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {activeJobs.length === 0 ? (
            <EmptyState
              icon={<ZapIcon size={26} color="var(--cyan)" />}
              title="No Active Sweeps Running"
              text="Launch a Discovery Sweep or Re-run Analysis from the Clients panel, or wait for the background round-robin engine."
            />
          ) : (
            activeJobs.map((job) => (
              <InFlightJobCard
                key={job.id}
                job={job}
                clientName={clientName(job.client_id)}
                now={now}
                onStop={stop}
                onOpenTerminal={() => setTerminalJobId(job.id)}
                onBrowseRecords={() => browseTo(job.client_id, job.platform)}
                stopping={stoppingId === job.id}
              />
            ))
          )}
        </div>
      )}

      {activeTab === "coverage" && <ClientCoverage onCount={setUnfinishedClients} />}

      {/* ════════════════════════════════════════════════════════════════════════
          TAB 2: JOB HISTORY & LOGS
          ════════════════════════════════════════════════════════════════════════ */}
      {activeTab === "history" && (
        <div>
          {/* History Search & Filter Bar */}
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 16 }}>
            <div style={{ position: "relative", flex: "1 1 240px", display: "flex", alignItems: "center" }}>
              <SearchIcon size={14} color="var(--text-dim)" style={{ position: "absolute", left: 10 }} />
              <input
                value={historySearch}
                onChange={(e) => setHistorySearch(e.target.value)}
                placeholder="Search history by client, job ID, or kind…"
                style={{ ...LA_SELECT_STYLE, width: "100%", paddingLeft: 30 }}
              />
            </div>
            <select
              value={historyClient}
              onChange={(e) => setHistoryClient(e.target.value)}
              style={LA_SELECT_STYLE}
            >
              <option value="">All Clients</option>
              {clients.map((c) => (
                <option key={c.client_id} value={c.client_id}>
                  {c.name || c.client_id}
                </option>
              ))}
            </select>
          </div>

          {filteredHistoryJobs.length === 0 ? (
            <EmptyState
              icon={<ClockIcon size={26} color="var(--accent)" />}
              title="No Completed Jobs Found"
              text="No past job runs match the search or filter criteria."
            />
          ) : (
            <div style={{ overflowX: "auto", background: "var(--bg-surface, #1e2837)", borderRadius: 12, border: "1px solid rgba(255,255,255,0.08)" }}>
              <table className="core_table" style={{ minWidth: 720 }}>
                <thead>
                  <tr>
                    <th>Client Name</th>
                    <th>Type</th>
                    <th>Status</th>
                    <th>Duration</th>
                    <th>Finished</th>
                    <th style={{ textAlign: "right" }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredHistoryJobs.map((job) => {
                    const took =
                      job.started && job.finished
                        ? Math.round((new Date(job.finished).getTime() - new Date(job.started).getTime()) / 1000)
                        : null;
                    return (
                      <tr key={job.id}>
                        <td style={{ fontWeight: 700, color: "var(--text-primary, #fff)" }}>
                          {clientName(job.client_id)}
                        </td>
                        <td>
                          <span
                            style={{
                              fontSize: 11,
                              background: "rgba(255,255,255,0.06)",
                              padding: "3px 8px",
                              borderRadius: 999,
                              display: "inline-flex",
                              alignItems: "center",
                              gap: 4,
                            }}
                          >
                            {job.kind === "discovery" ? <DiscoverIcon size={12} color="var(--cyan)" /> : <AnalyseIcon size={12} color="#7c5cff" />}
                            {job.kind}
                          </span>
                        </td>
                        <td>
                          <Badge color={JOB_STATUS_COLOR[job.status] ?? "var(--text-dim)"}>{job.status}</Badge>
                        </td>
                        <td style={{ fontSize: 12, color: "var(--text-dim)" }}>{durationLabel(took)}</td>
                        <td style={{ fontSize: 12, color: "var(--text-dim)" }} title={exactTime(job.finished)}>
                          {relativeTime(job.finished)}
                        </td>
                        <td style={{ textAlign: "right" }}>
                          <div style={{ display: "inline-flex", gap: 6 }}>
                            <button
                              type="button"
                              onClick={() => browseTo(job.client_id, job.platform)}
                              style={{
                                fontSize: 11,
                                padding: "4px 10px",
                                borderRadius: 6,
                                cursor: "pointer",
                                background: "rgba(54,181,160,0.1)",
                                border: "1px solid rgba(54,181,160,0.3)",
                                color: "var(--success, #36b5a0)",
                                fontWeight: 600,
                                display: "inline-flex",
                                alignItems: "center",
                                gap: 4,
                              }}
                            >
                              <DatabaseIcon size={11} /> Records
                            </button>
                            <button
                              type="button"
                              onClick={() => setTerminalJobId(job.id)}
                              style={{
                                fontSize: 11,
                                padding: "4px 10px",
                                borderRadius: 6,
                                cursor: "pointer",
                                background: "rgba(124,92,255,0.15)",
                                border: "1px solid rgba(124,92,255,0.35)",
                                color: "var(--accent, #7c5cff)",
                                fontWeight: 600,
                                display: "inline-flex",
                                alignItems: "center",
                                gap: 4,
                              }}
                            >
                              <ActivityWaveIcon size={11} color="var(--accent)" /> View Logs
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ════════════════════════════════════════════════════════════════════════
          TAB 3: DATA TRIAGE & RETRIES
          ════════════════════════════════════════════════════════════════════════ */}
      {activeTab === "triage" && (
        <div>
          {/* Top Triage Unified Controls */}
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              flexWrap: "wrap",
              gap: 12,
              marginBottom: 16,
              padding: "12px 16px",
              background: "var(--bg-surface, #1e2837)",
              borderRadius: 12,
              border: "1px solid rgba(255,255,255,0.08)",
            }}
          >
            {/* Sub-view Switcher */}
            <div style={{ display: "flex", gap: 4, background: "var(--bg-app, #101828)", padding: 3, borderRadius: 8 }}>
              <button
                type="button"
                onClick={() => setTriageSubTab("retry")}
                style={{
                  padding: "6px 14px",
                  borderRadius: 6,
                  border: "none",
                  background: triageSubTab === "retry" ? "var(--bg-surface, #1e2837)" : "transparent",
                  color: triageSubTab === "retry" ? "var(--cyan, #00e5ff)" : "var(--text-dim)",
                  fontWeight: triageSubTab === "retry" ? 700 : 500,
                  fontSize: 12,
                  cursor: "pointer",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <RefreshIcon size={12} color={triageSubTab === "retry" ? "var(--cyan)" : "currentColor"} />
                <span>Retry Queue</span>
                {retryCounts.exhausted + retryCounts.stopped > 0 && (
                  <span style={{ fontSize: 10, background: "rgba(253,183,27,0.2)", color: "var(--warn-yellow)", padding: "1px 5px", borderRadius: 999 }}>
                    {retryCounts.exhausted + retryCounts.stopped}
                  </span>
                )}
              </button>

              <button
                type="button"
                onClick={() => setTriageSubTab("records")}
                style={{
                  padding: "6px 14px",
                  borderRadius: 6,
                  border: "none",
                  background: triageSubTab === "records" ? "var(--bg-surface, #1e2837)" : "transparent",
                  color: triageSubTab === "records" ? "var(--cyan, #00e5ff)" : "var(--text-dim)",
                  fontWeight: triageSubTab === "records" ? 700 : 500,
                  fontSize: 12,
                  cursor: "pointer",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <DatabaseIcon size={12} color={triageSubTab === "records" ? "var(--cyan)" : "currentColor"} />
                <span>Record Browser</span>
              </button>
            </div>

            {/* Client & Platform Filter (Persisted across sub-views) */}
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <select
                value={triageClientId}
                onChange={(e) => {
                  setTriageClientId(e.target.value);
                  setOffset(0);
                }}
                style={LA_SELECT_STYLE}
              >
                <option value="">Select a Client…</option>
                {clients.map((c) => (
                  <option key={c.client_id} value={c.client_id}>
                    {c.name || c.client_id}
                  </option>
                ))}
              </select>

              <select
                value={triagePlatform}
                onChange={(e) => {
                  setTriagePlatform(e.target.value);
                  setOffset(0);
                }}
                style={LA_SELECT_STYLE}
              >
                <option value="">All Platforms</option>
                {["facebook", "instagram", "twitter", "youtube", "telegram", "tiktok"].map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {!triageClientId ? (
            <EmptyState
              icon={<SearchIcon size={26} color="var(--cyan)" />}
              title="Select a Client to Begin Triage"
              text="Pick a client from the dropdown above to inspect failed profile analysis retries or manage discovered records."
            />
          ) : triageSubTab === "retry" ? (
            /* ── SUB-VIEW: RETRY QUEUE ── */
            <div>
              {/* Counts Banner */}
              <div
                style={{
                  display: "flex",
                  gap: 12,
                  flexWrap: "wrap",
                  marginBottom: 14,
                  padding: "12px 16px",
                  background: "var(--bg-surface, #1e2837)",
                  border: "1px solid rgba(124,92,255,0.2)",
                  borderRadius: 12,
                }}
              >
                {([
                  { key: "eligible" as const, label: "Eligible (auto-retrying)" },
                  { key: "exhausted" as const, label: "Exhausted (needs resume)" },
                  { key: "stopped" as const, label: "Stopped (by analyst)" },
                ] as const).map((stat) => (
                  <div key={stat.key} style={{ flex: "1 1 160px", display: "flex", flexDirection: "column", gap: 2 }}>
                    <span style={{ fontSize: 10, color: "var(--text-dim)", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.5px" }}>
                      {stat.label}
                    </span>
                    <span style={{ fontSize: 17, fontWeight: 800, color: RETRY_STATE_LOOK[stat.key].color }}>
                      {retryCounts[stat.key]}
                    </span>
                  </div>
                ))}
              </div>

              {/* Action and Filter Controls */}
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10, flexWrap: "wrap", gap: 8 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <select
                    value={retryStateFilter}
                    onChange={(e) => setRetryStateFilter(e.target.value as typeof retryStateFilter)}
                    style={LA_SELECT_STYLE}
                  >
                    <option value="">Every State</option>
                    <option value="eligible">Eligible only</option>
                    <option value="exhausted">Exhausted only</option>
                    <option value="stopped">Stopped only</option>
                  </select>
                  <button
                    type="button"
                    onClick={loadRetryQueue}
                    disabled={retryLoading}
                    style={{ ...LA_SELECT_STYLE, cursor: "pointer", fontWeight: 700, display: "inline-flex", alignItems: "center", gap: 6 }}
                  >
                    <RefreshIcon size={12} /> Refresh
                  </button>
                </div>

                <button
                  type="button"
                  onClick={stopSelectedRetries}
                  disabled={retrySelected.size === 0 || retryBulkBusy}
                  style={{
                    padding: "6px 14px",
                    borderRadius: 8,
                    background: retrySelected.size ? "rgba(233,80,83,0.15)" : "var(--bg-surface-3, #1d2939)",
                    border: `1px solid ${retrySelected.size ? "rgba(233,80,83,0.4)" : "rgba(255,255,255,0.1)"}`,
                    color: retrySelected.size ? "var(--danger, #e95053)" : "var(--text-dim)",
                    fontSize: 12,
                    fontWeight: 700,
                    cursor: retrySelected.size ? "pointer" : "not-allowed",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 5,
                  }}
                >
                  <StopIcon size={13} /> Stop Selected {retrySelected.size > 0 ? `(${retrySelected.size})` : ""}
                </button>
              </div>

              {/* Retry Table */}
              <div style={{ overflowX: "auto", background: "var(--bg-surface, #1e2837)", borderRadius: 12, border: "1px solid rgba(255,255,255,0.08)" }}>
                <table className="core_table">
                  <thead>
                    <tr>
                      <th style={{ width: 36 }}>
                        <input
                          type="checkbox"
                          checked={visibleRetryItems.length > 0 && retrySelected.size === visibleRetryItems.length}
                          onChange={toggleRetrySelectedAll}
                        />
                      </th>
                      <th>Platform</th>
                      <th>Profile Name</th>
                      <th>URL</th>
                      <th>State</th>
                      <th>Reason</th>
                      <th>Attempts</th>
                      <th>Last Attempt</th>
                      <th style={{ textAlign: "right" }}>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleRetryItems.length === 0 ? (
                      <tr>
                        <td colSpan={9} style={{ textAlign: "center", padding: 32, color: "var(--text-dim)" }}>
                          {retryLoading
                            ? "Loading retry queue…"
                            : "Nothing in the retry queue for this client — every approved profile is either fully read or on track."}
                        </td>
                      </tr>
                    ) : (
                      visibleRetryItems.map((p) => {
                        const look = RETRY_STATE_LOOK[p.retry_state ?? "eligible"];
                        const acting = retryActingId === p.id;
                        return (
                          <tr key={p.id}>
                            <td>
                              <input
                                type="checkbox"
                                checked={retrySelected.has(p.id)}
                                onChange={() => toggleRetrySelected(p.id)}
                              />
                            </td>
                            <td style={{ textTransform: "capitalize" }}>
                              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                                <PlatformIcon platform={p.platform} size={15} />
                                {p.platform}
                              </span>
                            </td>
                            <td style={{ maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontWeight: 600 }}>
                              {p.profile_name || "—"}
                            </td>
                            <td style={{ maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              <a href={p.url} target="_blank" rel="noreferrer" style={{ color: "var(--accent, #7c5cff)" }}>
                                {p.url}
                              </a>
                            </td>
                            <td>
                              <Badge color={look.color}>{look.label}</Badge>
                            </td>
                            <td style={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 12, color: "var(--text-dim)" }} title={p.retry_reason}>
                              {p.retry_reason || "—"}
                            </td>
                            <td style={{ fontSize: 12, color: "var(--text-dim)" }}>{p.analysis_attempts ?? 0}</td>
                            <td style={{ fontSize: 12, color: "var(--text-dim)" }} title={exactTime(p.analysed_at)}>
                              {relativeTime(p.analysed_at) !== "—" ? relativeTime(p.analysed_at) : "—"}
                            </td>
                            <td style={{ textAlign: "right" }}>
                              {p.retry_state === "stopped" ? (
                                <button
                                  type="button"
                                  onClick={() => resumeOne(p.id)}
                                  disabled={acting}
                                  style={{
                                    fontSize: 11,
                                    padding: "4px 10px",
                                    borderRadius: 6,
                                    cursor: acting ? "wait" : "pointer",
                                    background: "rgba(54,181,160,0.15)",
                                    border: "1px solid rgba(54,181,160,0.35)",
                                    color: "var(--success, #36b5a0)",
                                    fontWeight: 700,
                                    display: "inline-flex",
                                    alignItems: "center",
                                    gap: 4,
                                  }}
                                >
                                  <PlayIcon size={11} /> {acting ? "…" : "Resume"}
                                </button>
                              ) : (
                                <button
                                  type="button"
                                  onClick={() => stopOne(p.id)}
                                  disabled={acting}
                                  style={{
                                    fontSize: 11,
                                    padding: "4px 10px",
                                    borderRadius: 6,
                                    cursor: acting ? "wait" : "pointer",
                                    background: "rgba(233,80,83,0.12)",
                                    border: "1px solid rgba(233,80,83,0.35)",
                                    color: "var(--danger, #e95053)",
                                    fontWeight: 700,
                                    display: "inline-flex",
                                    alignItems: "center",
                                    gap: 4,
                                  }}
                                >
                                  <StopIcon size={11} /> {acting ? "…" : "Stop"}
                                </button>
                              )}
                            </td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            /* ── SUB-VIEW: RECORD BROWSER ── */
            <div>
              {/* Record Filters Bar */}
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
                <select
                  value={browseStatus}
                  onChange={(e) => {
                    setBrowseStatus(e.target.value);
                    setOffset(0);
                  }}
                  style={LA_SELECT_STYLE}
                >
                  <option value="">All Statuses</option>
                  <option value="pending">Pending</option>
                  <option value="approved">Approved</option>
                  <option value="rejected">Rejected</option>
                </select>

                <div style={{ position: "relative", flex: "1 1 220px", display: "flex", alignItems: "center" }}>
                  <SearchIcon size={13} color="var(--text-dim)" style={{ position: "absolute", left: 10 }} />
                  <input
                    value={browseSearch}
                    onChange={(e) => setBrowseSearch(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && (setOffset(0), loadProfiles())}
                    placeholder="Search profile name or URL…"
                    style={{ ...LA_SELECT_STYLE, width: "100%", paddingLeft: 28 }}
                  />
                </div>

                <button
                  type="button"
                  onClick={() => {
                    setOffset(0);
                    loadProfiles();
                  }}
                  style={{ ...LA_SELECT_STYLE, cursor: "pointer", fontWeight: 700 }}
                >
                  Search
                </button>

                <button
                  type="button"
                  onClick={deleteSelected}
                  disabled={selected.size === 0 || deleting}
                  style={{
                    padding: "6px 14px",
                    borderRadius: 8,
                    background: selected.size ? "rgba(233,80,83,0.15)" : "var(--bg-surface-3, #1d2939)",
                    border: `1px solid ${selected.size ? "rgba(233,80,83,0.4)" : "rgba(255,255,255,0.1)"}`,
                    color: selected.size ? "var(--danger, #e95053)" : "var(--text-dim)",
                    fontSize: 12,
                    fontWeight: 700,
                    cursor: selected.size ? "pointer" : "not-allowed",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 5,
                    marginLeft: "auto",
                  }}
                >
                  <TrashIcon size={13} /> Delete Selected {selected.size > 0 ? `(${selected.size})` : ""}
                </button>
              </div>

              {/* Record Table */}
              <div style={{ overflowX: "auto", background: "var(--bg-surface, #1e2837)", borderRadius: 12, border: "1px solid rgba(255,255,255,0.08)" }}>
                <table className="core_table">
                  <thead>
                    <tr>
                      <th style={{ width: 36 }}>
                        <input
                          type="checkbox"
                          checked={profiles.length > 0 && selected.size === profiles.length}
                          onChange={toggleAll}
                        />
                      </th>
                      <th>Platform</th>
                      <th>Name</th>
                      <th>URL</th>
                      <th>Status</th>
                      <th>Risk</th>
                      <th>Phase</th>
                      <th>Last Seen</th>
                    </tr>
                  </thead>
                  <tbody>
                    {profiles.length === 0 ? (
                      <tr>
                        <td colSpan={8} style={{ textAlign: "center", padding: 32, color: "var(--text-dim)" }}>
                          {browseLoading ? "Loading records…" : "No database records match these filters."}
                        </td>
                      </tr>
                    ) : (
                      profiles.map((p) => (
                        <tr key={p.id}>
                          <td>
                            <input
                              type="checkbox"
                              checked={selected.has(p.id)}
                              onChange={() => toggleOne(p.id)}
                            />
                          </td>
                          <td style={{ textTransform: "capitalize" }}>
                            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                              <PlatformIcon platform={p.platform} size={15} />
                              {p.platform}
                            </span>
                          </td>
                          <td style={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontWeight: 600 }}>
                            {p.profile_name || "—"}
                          </td>
                          <td style={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            <a href={p.url} target="_blank" rel="noreferrer" style={{ color: "var(--accent, #7c5cff)" }}>
                              {p.url}
                            </a>
                          </td>
                          <td>
                            <Badge color={PROFILE_STATUS_BADGE[p.status] ?? "var(--text-dim)"}>{p.status}</Badge>
                          </td>
                          <td>{p.risk_score ?? "—"}</td>
                          <td style={{ textTransform: "capitalize" }}>{p.phase || "—"}</td>
                          <td style={{ fontSize: 12, color: "var(--text-dim)" }} title={exactTime(p.analysed_at)}>
                            {relativeTime(p.analysed_at) !== "—" ? relativeTime(p.analysed_at) : "—"}
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>

              {/* Pagination */}
              <div style={{ display: "flex", justifyContent: "center", gap: 12, alignItems: "center", marginTop: 14 }}>
                <button
                  type="button"
                  onClick={() => setOffset(Math.max(0, offset - BROWSE_PAGE_SIZE))}
                  disabled={offset === 0}
                  style={{ ...LA_SELECT_STYLE, cursor: offset === 0 ? "not-allowed" : "pointer" }}
                >
                  ← Prev
                </button>
                <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
                  {offset + 1}–{Math.min(offset + BROWSE_PAGE_SIZE, profilesTotal)} of {profilesTotal}
                </span>
                <button
                  type="button"
                  onClick={() => setOffset(offset + BROWSE_PAGE_SIZE)}
                  disabled={offset + BROWSE_PAGE_SIZE >= profilesTotal}
                  style={{ ...LA_SELECT_STYLE, cursor: offset + BROWSE_PAGE_SIZE >= profilesTotal ? "not-allowed" : "pointer" }}
                >
                  Next →
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* ════════════════════════════════════════════════════════════════════════
          SLIDE-OVER DRAWERS
          ════════════════════════════════════════════════════════════════════════ */}

      {/* Cyber Terminal Slide-Over Drawer */}
      <CyberTerminalDrawer
        jobId={terminalJobId}
        clientName={selectedTerminalJob ? clientName(selectedTerminalJob.client_id) : ""}
        jobKind={selectedTerminalJob?.kind}
        jobStatus={selectedTerminalJob?.status}
        onClose={() => setTerminalJobId(null)}
      />

      {/* Incidents Slide-Over Drawer */}
      <IncidentsDrawer
        isOpen={isIncidentsOpen}
        onClose={() => setIsIncidentsOpen(false)}
        activeSeverity={incidentSeverity}
        onSelectSeverity={(s) => setIncidentSeverity(s)}
      />
    </div>
  );
}
