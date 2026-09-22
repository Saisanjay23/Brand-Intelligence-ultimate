// Live Results: the Discovery/Analysis phase toggle + platform rail +
// "Recent Discovery Status" bar, all pulled from the original app's
// ResultsGrid.tsx (see its git history) and adapted to the rebuilt
// discovery API's job/platform shape. What each phase shows underneath the
// rail is new: Discovery renders DiscoveryProfileGrid (this session's
// Pending/Validated/Rejected triage grid); Analysis renders the actual
// paste-URLs-and-scrape tool (AnalysisView) embedded right here, instead of
// the old per-client analysis-phase-of-profiles table that had no backend
// behind it any more (analysis is independent/memory-only now).
import { useEffect, useState } from "react";
import { discoveryApi } from "../api/discoveryApi";
import type { DiscoveryJobState, PlatformSweepState } from "../api/discoveryApi";
import type { PlatformState } from "../api/types";
import { DiscoveryProfileGrid } from "../components/DiscoveryProfileGrid";
import { PlatformIcon } from "../components/PlatformIcon";
import { DiscoverIcon, AnalyseIcon, StopIcon, AlertTriangleIcon } from "../components/AppIcons";
import { AnalysisView } from "./AnalysisView";
import { formatElapsed, useLiveTimer } from "../utils/timeFormat";

interface Props {
  clientId: string;
  clientName: string;
  platforms: PlatformState[];
  job: DiscoveryJobState | null;
  running: boolean;
  cancelling: boolean;
  onCancel: () => void;
  onAnalyseStarted: (jobId: string) => void;
  refreshKey: number;
  /** Bumped while a sweep is running and its counts move. */
  liveKey?: number;
  // Set by App.tsx after "Analyse Validated Profiles"/"Analyse Selected"
  // (or Home's own "Analyse" action) starts a job -- switches this page's
  // own toggle to Analysis and hands the id to the embedded AnalysisView so
  // the analyst lands on it already being watched. There is no standalone
  // Analysis page any more; this toggle is the only way to reach it.
  resumeAnalysisJobId: string | null;
}

type Phase = "discovery" | "analysis";

const STATUS_COLOR: Record<PlatformSweepState["status"], string> = {
  pending: "var(--text-dim)",
  running: "var(--purple, #9A50E9)",
  done: "var(--success)",
  partial: "var(--warn-yellow)",
  failed: "var(--danger)",
  skipped: "var(--warn-yellow)",
};

function StatusDot({ status }: { status: PlatformSweepState["status"] }) {
  if (status === "failed") return <AlertTriangleIcon size={10} color="var(--danger)" />;
  return (
    <span
      style={{
        width: 8, height: 8, borderRadius: "50%", display: "inline-block",
        background: STATUS_COLOR[status],
        boxShadow: status === "running" ? `0 0 6px ${STATUS_COLOR[status]}` : "none",
      }}
    />
  );
}

// keywords_total is keywords x tabs for this platform (see
// discovery/runner.py), so dividing it out of keywords_done recovers which
// KEYWORD (not sweep-unit) is in flight right now -- the API doesn't
// expose that directly, this derives it from what it does expose.
function currentKeywordOf(job: DiscoveryJobState, p: PlatformSweepState): string | undefined {
  if (p.current_keyword) return p.current_keyword;
  if (p.status !== "running" || !job.keywords.length) return undefined;
  const tabsForPlatform = p.keywords_total / job.keywords.length || 1;
  return job.keywords[Math.floor(p.keywords_done / tabsForPlatform)];
}

function PlatformInFlightItem({
  p,
  job,
  running,
}: {
  p: PlatformSweepState;
  job: DiscoveryJobState;
  running: boolean;
}) {
  // SHARDED SWEEPS SHOW EVERY ACCOUNT, NOT A GUESS AT ONE. With several
  // workers the server deliberately stops filling current_keyword (there is
  // no single answer), so falling back to currentKeywordOf -- which derives
  // a keyword from keywords_done and assumes strict ordering -- would put a
  // confidently wrong keyword on screen. Slots are the truth in that case.
  const slots = (p.worker_slots ?? []).filter((s) => s.keyword);
  const sharded = slots.length > 1;
  const kw = sharded ? undefined : p.current_keyword || currentKeywordOf(job, p);
  const tab = !sharded && p.current_tab ? p.current_tab.toUpperCase() : undefined;
  const isRunning = p.status === "running";
  const itemElapsed = useLiveTimer(p.item_started_at_ts, isRunning);

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "8px",
        fontSize: "12px",
        background: isRunning ? "rgba(154, 80, 233, 0.12)" : "var(--bg-surface)",
        padding: "6px 14px",
        borderRadius: "16px",
        border: `1px solid ${isRunning ? "rgba(154, 80, 233, 0.45)" : "var(--border-color)"}`,
        boxShadow: isRunning ? "0 0 10px rgba(154, 80, 233, 0.25)" : "none",
        flexWrap: "wrap",
      }}
    >
      <PlatformIcon platform={p.platform} size={15} />
      <span style={{ fontWeight: 600, color: "var(--text-main)" }}>{p.display_name}:</span>
      <span style={{ color: STATUS_COLOR[p.status], fontWeight: 700, display: "inline-flex", alignItems: "center", gap: "4px" }}>
        <StatusDot status={p.status} /> {p.keywords_done}/{p.keywords_total}
      </span>
      {/* Only when the sweep was actually split -- a "1 session" badge on
          every chip would be noise, since one session is the norm. */}
      {(p.workers ?? 0) > 1 && (
        <span
          title={`Split across ${p.workers} sessions running in parallel`}
          style={{
            fontSize: "10px",
            fontWeight: 700,
            color: "var(--purple, #9A50E9)",
            border: "1px solid rgba(154, 80, 233, 0.35)",
            borderRadius: "4px",
            padding: "1px 5px",
          }}
        >
          x{p.workers}
        </span>
      )}
      {kw && (
        <span style={{ fontSize: "11.5px", color: "var(--purple, #9A50E9)", fontWeight: 600, background: "rgba(154, 80, 233, 0.12)", padding: "1px 7px", borderRadius: "10px" }}>
          "{kw}"
        </span>
      )}
      {/* One pill per account. Deliberately shows the ACCOUNT as well as the
          keyword: when three are sweeping at once, "which account is on
          this" is the question an operator actually has, and it is the only
          place in the UI that can answer it. */}
      {sharded &&
        slots.map((s, i) => (
          <span
            key={i}
            title={`${s.account || `Account ${i + 1}`}: ${s.step || "working"}`}
            style={{
              fontSize: "11px",
              color: "var(--purple, #9A50E9)",
              fontWeight: 600,
              background: "rgba(154, 80, 233, 0.12)",
              padding: "1px 7px",
              borderRadius: "10px",
              whiteSpace: "nowrap",
            }}
          >
            <span style={{ opacity: 0.65 }}>{s.account || `#${i + 1}`}</span>{" "}
            "{s.keyword}"
            {s.tab ? <span style={{ opacity: 0.65 }}> · {s.tab.toUpperCase()}</span> : null}
          </span>
        ))}
      {tab && isRunning && (
        <span
          style={{
            fontSize: "10px",
            fontWeight: 800,
            color: "#fff",
            background: "linear-gradient(135deg, #9A50E9, #7727CD)",
            padding: "1px 6px",
            borderRadius: "4px",
            boxShadow: "0 2px 6px rgba(154, 80, 233, 0.35)",
            letterSpacing: "0.04em",
          }}
        >
          [{tab}]
        </span>
      )}
      {isRunning && (
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "11px", color: "var(--purple, #9A50E9)", fontWeight: 700 }}>
          ⏱️ {formatElapsed(itemElapsed)}
        </span>
      )}
      {p.note && <span style={{ fontSize: "11px", color: "var(--text-dim)", marginLeft: "4px" }}>{p.note}</span>}
    </div>
  );
}

function DiscoveryBannerBox({
  job,
  running,
  cancelling,
  onCancel,
}: {
  job: DiscoveryJobState;
  running: boolean;
  cancelling: boolean;
  onCancel: () => void;
}) {
  const [showHistory, setShowHistory] = useState(false);
  const totalDone = (job.platforms || []).reduce((acc, p) => acc + p.keywords_done, 0);
  const totalUnits = (job.platforms || []).reduce((acc, p) => acc + p.keywords_total, 0);
  const pct = totalUnits > 0 ? Math.min(100, Math.round((totalDone / totalUnits) * 100)) : 0;

  const elapsedSec = useLiveTimer(job.started_at_ts, running, job.elapsed_seconds);
  const history = job.history || [];

  return (
    <div
      className="dashboard-card-box"
      style={{
        marginTop: "16px",
        borderLeft: "4px solid var(--purple, #9A50E9)",
        background: "rgba(154, 80, 233, 0.06)",
        padding: "16px 20px",
        transition: "all 0.3s ease",
      }}
    >
      {/* Top Header Row with Timers, ETA, & Progress */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "10px", flexWrap: "wrap", gap: "10px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
          <DiscoverIcon size={18} color="var(--purple, #9A50E9)" />
          <span style={{ fontWeight: 700, color: "var(--text-main)", fontSize: "14px" }}>
            {running ? "Live Discovery Sweep Progress" : "Recent Discovery Status"}
          </span>
          {running ? (
            <span className="rail-pill" style={{ background: "var(--purple, #9A50E9)", color: "#fff", fontWeight: 700, animation: "pulse 1.5s infinite" }}>
              RUNNING
            </span>
          ) : (
            <span className="rail-pill" style={{ background: "rgba(54,181,160,0.2)", color: "var(--success)", fontWeight: 700 }}>
              {job.status.toUpperCase()}
            </span>
          )}
          {running && (
            <button
              type="button"
              onClick={onCancel}
              disabled={cancelling}
              style={{
                background: "linear-gradient(135deg, rgba(239,68,68,0.25), rgba(220,38,38,0.35))",
                color: "#ff6b6b", border: "1px solid rgba(239,68,68,0.5)", padding: "3px 10px",
                borderRadius: "12px", fontSize: "11px", fontWeight: 700,
                cursor: cancelling ? "progress" : "pointer", opacity: cancelling ? 0.6 : 1,
                display: "inline-flex", alignItems: "center", gap: "5px",
              }}
            >
              <StopIcon size={11} color="#ff6b6b" /> {cancelling ? "Stopping..." : "Stop Sweep"}
            </button>
          )}
        </div>

        {/* Real-time Telemetry Metrics Pill Bar */}
        <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap", fontFamily: "var(--font-mono)", fontSize: "12px" }}>
          <span
            style={{
              background: "rgba(154, 80, 233, 0.12)",
              border: "1px solid rgba(154, 80, 233, 0.3)",
              color: "var(--purple, #9A50E9)",
              padding: "3px 9px",
              borderRadius: "6px",
              fontWeight: 700,
              display: "inline-flex",
              alignItems: "center",
              gap: "4px",
            }}
          >
            ⏱️ {formatElapsed(elapsedSec)}
          </span>

          {running && job.estimated_remaining_seconds !== undefined && job.estimated_remaining_seconds !== null && (
            <span
              style={{
                background: "rgba(124, 92, 255, 0.12)",
                border: "1px solid rgba(124, 92, 255, 0.3)",
                color: "var(--purple, #a78bfa)",
                padding: "3px 9px",
                borderRadius: "6px",
                fontWeight: 600,
                display: "inline-flex",
                alignItems: "center",
                gap: "4px",
              }}
              title="Estimated time remaining based on active sweep speeds"
            >
              ⏳ Est: ~{formatElapsed(job.estimated_remaining_seconds)}
            </span>
          )}

          <span style={{ fontWeight: 700, color: "var(--text-main)" }}>
            {totalDone} / {totalUnits || "?"} Sweeps ({pct}%)
          </span>

          {job.found > 0 && (
            <span
              style={{
                background: "rgba(18, 183, 106, 0.15)",
                color: "var(--success, #12b76a)",
                border: "1px solid rgba(18, 183, 106, 0.35)",
                padding: "3px 9px",
                borderRadius: "6px",
                fontWeight: 700,
              }}
            >
              🎯 {job.found} Found {job.new > 0 ? `(+${job.new} new)` : ""}
            </span>
          )}
        </div>
      </div>

      {/* Progress Bar */}
      <div style={{ height: "8px", background: "var(--bg-inner)", borderRadius: "4px", overflow: "hidden", marginBottom: "10px" }}>
        <div
          style={{
            height: "100%", width: `${pct}%`,
            background: "linear-gradient(90deg, #9A50E9, #7727CD)",
            boxShadow: running ? "0 0 10px rgba(154, 80, 233, 0.5)" : "none",
            transition: "width 0.4s ease",
          }}
        />
      </div>

      {job.message && (
        <div style={{ fontSize: "12px", color: "var(--text-dim)", marginBottom: "10px" }}>
          {job.message}
        </div>
      )}

      {/* Active Platform & Keyword In-Flight Chips */}
      <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
        {job.platforms.map((p) => (
          <PlatformInFlightItem key={p.platform} p={p} job={job} running={running} />
        ))}
      </div>

      {/* Collapsible Completed Sweeps Timings & Audit Log */}
      {history.length > 0 && (
        <div style={{ marginTop: "12px", borderTop: "1px solid rgba(255, 255, 255, 0.08)", paddingTop: "8px" }}>
          <button
            type="button"
            onClick={() => setShowHistory((v) => !v)}
            style={{
              background: "transparent",
              border: "none",
              color: "var(--purple, #9A50E9)",
              fontSize: "11.5px",
              fontWeight: 600,
              cursor: "pointer",
              padding: "2px 0",
              display: "inline-flex",
              alignItems: "center",
              gap: "6px",
            }}
          >
            <span>{showHistory ? "▾ Hide Completed Sweeps Log" : `▸ Show Completed Sweeps Timings (${history.length} completed)`}</span>
          </button>

          {showHistory && (
            <div
              style={{
                marginTop: "8px",
                maxHeight: "180px",
                overflowY: "auto",
                background: "rgba(0, 0, 0, 0.25)",
                border: "1px solid rgba(255, 255, 255, 0.06)",
                borderRadius: "8px",
                padding: "8px 12px",
                display: "flex",
                flexDirection: "column",
                gap: "5px",
                fontFamily: "var(--font-mono)",
                fontSize: "11px",
              }}
            >
              {[...history].reverse().map((h, i) => (
                <div
                  key={i}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "8px",
                    color: "var(--text-dim)",
                    padding: "3px 0",
                    borderBottom: i !== history.length - 1 ? "1px solid rgba(255, 255, 255, 0.03)" : "none",
                    flexWrap: "wrap",
                  }}
                >
                  <span style={{ color: "var(--success)" }}>✅</span>
                  <span style={{ color: "var(--text-muted)", fontSize: "10px" }}>{h.timestamp}</span>
                  <PlatformIcon platform={h.platform} size={12} />
                  <span style={{ fontWeight: 600, color: "var(--text-main)" }}>{h.display_name}</span>
                  <span style={{ color: "var(--purple, #9A50E9)" }}>"{h.keyword}"</span>
                  <span
                    style={{
                      fontSize: "9.5px",
                      background: "rgba(255, 255, 255, 0.08)",
                      padding: "1px 5px",
                      borderRadius: "3px",
                      textTransform: "uppercase",
                      color: "#fff",
                    }}
                  >
                    [{h.tab}]
                  </span>
                  <span style={{ color: "var(--purple, #a78bfa)", fontWeight: 700 }}>⏱️ {h.duration_seconds.toFixed(1)}s</span>
                  <span style={{ marginLeft: "auto", color: h.hits_found > 0 ? "var(--success)" : "var(--text-muted)" }}>
                    {h.hits_found} found {h.hits_new > 0 ? `(+${h.hits_new} new)` : ""}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Small per-tile progress row -- same recipe as the original
// PlatformProgressRow (icon + status dot + processed/total + a thin bar
// while running), adapted to keywords_done/keywords_total.
function TileProgressRow({ p }: { p: PlatformSweepState }) {
  const pct = p.keywords_total > 0 ? Math.min(100, Math.round((p.keywords_done / p.keywords_total) * 100)) : 0;
  return (
    <div style={{ marginTop: "4px", width: "100%" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "5px", fontSize: "10px", fontFamily: "var(--font-mono)", color: STATUS_COLOR[p.status] }}>
        <StatusDot status={p.status} />
        <DiscoverIcon size={10} color="var(--purple, #9A50E9)" />
        <span style={{ flex: 1 }} />
        <span>{p.keywords_done}/{p.keywords_total}</span>
      </div>
      {p.status === "running" && (
        <div style={{ height: "3px", background: "var(--bg-inner)", borderRadius: "999px", overflow: "hidden", marginTop: "3px" }}>
          <div style={{ height: "100%", width: `${pct || 4}%`, background: "linear-gradient(90deg, #9A50E9, #7727CD)" }} />
        </div>
      )}
    </div>
  );
}

export function LiveResultsView({
  clientId, clientName, platforms, job, running, cancelling, onCancel, onAnalyseStarted, refreshKey, liveKey, resumeAnalysisJobId,
}: Props) {
  const [phase, setPhase] = useState<Phase>("discovery");
  const [platform, setPlatform] = useState<string>("");
  const [counts, setCounts] = useState<Record<string, number>>({});

  // A new analysis job id arriving (from this page's own "Analyse
  // Validated"/"Analyse Selected", or from Home's "Analyse" action)
  // switches straight to the Analysis toggle so the analyst doesn't have
  // to click it themselves.
  useEffect(() => {
    if (resumeAnalysisJobId) setPhase("analysis");
  }, [resumeAnalysisJobId]);

  // KEYED ON `liveKey` TOO, NOT JUST `refreshKey`. `refreshKey` moves when
  // a sweep FINISHES; `liveKey` moves every time its counts do. Without the
  // second one these tiles were the one part of this page that did not
  // update while a sweep ran -- the grid below filled in with new rows
  // while the per-platform "N results" above it sat on the number it had
  // when the sweep started, which reads as the count being broken rather
  // than as it being refreshed on a different schedule.
  useEffect(() => {
    if (!clientId) return;
    let cancelled = false;
    Promise.all(
      platforms.map((p) =>
        discoveryApi
          .listProfiles({ group_id: clientId, platform: p.platform, limit: 1 })
          .then((res) => [p.platform, res.total] as const)
          .catch(() => [p.platform, 0] as const),
      ),
    ).then((pairs) => {
      if (!cancelled) setCounts(Object.fromEntries(pairs));
    });
    return () => {
      cancelled = true;
    };
  }, [clientId, platforms, refreshKey, liveKey]);

  const jobPlatformById = new Map((job?.platforms || []).map((p) => [p.platform, p]));

  return (
    <div style={{ animation: "fadeUp 0.4s ease" }}>
      <h2 style={{ fontSize: "18px", fontWeight: 700, marginBottom: "4px" }}>
        {clientId ? (clientName || clientId) : "Live Results & Direct Analysis"}
      </h2>
      <div style={{ fontSize: "12px", color: "var(--text-dim)", marginBottom: "16px" }}>
        {clientId ? "Live discovery & profile analysis" : "Select a client for Discovery sweeps, or scrape and analyze URLs directly below."}
      </div>

      {/* Phase tabs -- Discovery / Analysis */}
      <div className="platform-rail-grid" style={{ gridTemplateColumns: "repeat(2, 1fr)", marginBottom: "16px" }}>
        {(["discovery", "analysis"] as const).map((ph) => (
          <div key={ph} className={`platform-rail-item ${phase === ph ? "active" : ""}`} onClick={() => setPhase(ph)}>
            <div className="rail-card-head">
              <span style={{ display: "flex", alignItems: "center" }}>
                {ph === "discovery" ? <DiscoverIcon size={16} color="var(--purple, #9A50E9)" /> : <AnalyseIcon size={16} color="var(--purple, #9A50E9)" />}
              </span>
              <span style={{ fontSize: "12px", fontWeight: 500, color: "var(--text-primary)" }}>
                {ph === "discovery" ? "Discovery" : "Analysis"}
              </span>
            </div>
          </div>
        ))}
      </div>

      {/* Platform rail -- only when a client is selected */}
      {clientId && (
        <div className="platform-rail-grid">
          {platforms.map((p) => {
            const count = counts[p.platform] || 0;
            const sweep = jobPlatformById.get(p.platform);
            return (
              <div
                key={p.platform}
                className={`platform-rail-item ${platform === p.platform ? "active" : ""}`}
                onClick={() => setPlatform((prev) => (prev === p.platform ? "" : p.platform))}
                title={platform === p.platform ? "Click to clear filter -- show every platform" : `Filter Discovery to ${p.name} only`}
              >
              <div className="rail-card-head">
                <PlatformIcon platform={p.platform} size={18} />
                <span style={{ fontSize: "12px", fontWeight: 500 }}>{p.name}</span>
              </div>
              <div className="rail-card-foot" style={{ display: "flex", justifyContent: "space-between", width: "100%" }}>
                <span className="rail-pill" style={{ color: p.session_state === "ready" ? "var(--success)" : "var(--text-dim)" }}>
                  {p.session_state}
                </span>
                <span className="rail-pill" style={{ color: count > 0 ? "var(--text-main)" : "var(--text-dim)", fontWeight: count > 0 ? 700 : 400 }}>
                  {count} {count === 1 ? "result" : "results"}
                </span>
              </div>
              {sweep && sweep.status !== "pending" && <TileProgressRow p={sweep} />}
              </div>
            );
          })}
        </div>
      )}

      {/* "Recent Discovery Status" Banner Box */}
      {job && (
        <DiscoveryBannerBox
          job={job}
          running={running}
          cancelling={cancelling}
          onCancel={onCancel}
        />
      )}

      {phase === "discovery" && (
        clientId ? (
          <DiscoveryProfileGrid
            groupId={clientId}
            platform={platform || undefined}
            refreshKey={refreshKey}
            liveKey={liveKey}
            onAnalyseStarted={onAnalyseStarted}
          />
        ) : (
          <div style={{ padding: "50px 20px", textAlign: "center", color: "var(--text-dim)", background: "var(--bg-surface)", border: "1px solid var(--border-color)", borderRadius: "12px", marginTop: "16px" }}>
            <p style={{ fontSize: "14px", color: "var(--text-main)", marginBottom: "10px", fontWeight: 600 }}>
              Select a client on the Clients tab to run or view Discovery sweeps.
            </p>
            <p style={{ fontSize: "12px", color: "var(--text-muted)", marginBottom: "16px" }}>
              To scrape and analyze suspicious profiles immediately without selecting a client:
            </p>
            <button
              type="button"
              onClick={() => setPhase("analysis")}
              className="btn-cyber-primary"
              style={{ display: "inline-flex", alignItems: "center", gap: "8px", margin: "0 auto", padding: "8px 18px", fontSize: "13px" }}
            >
              <AnalyseIcon size={14} color="#fff" /> Open Direct URL Analysis →
            </button>
          </div>
        )
      )}

      {phase === "analysis" && (
        <div style={{ marginTop: "24px" }}>
          <AnalysisView resumeJobId={resumeAnalysisJobId} clientId={clientId} />
        </div>
      )}
    </div>
  );
}
