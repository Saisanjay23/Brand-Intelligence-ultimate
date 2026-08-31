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
  running: "var(--cyan)",
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
  if (p.status !== "running" || !job.keywords.length) return undefined;
  const tabsForPlatform = p.keywords_total / job.keywords.length || 1;
  return job.keywords[Math.floor(p.keywords_done / tabsForPlatform)];
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
        <DiscoverIcon size={10} color="var(--cyan)" />
        <span style={{ flex: 1 }} />
        <span>{p.keywords_done}/{p.keywords_total}</span>
      </div>
      {p.status === "running" && (
        <div style={{ height: "3px", background: "var(--bg-inner)", borderRadius: "999px", overflow: "hidden", marginTop: "3px" }}>
          <div style={{ height: "100%", width: `${pct || 4}%`, background: "linear-gradient(90deg, var(--cyan), var(--purple))" }} />
        </div>
      )}
    </div>
  );
}

export function LiveResultsView({
  clientId, clientName, platforms, job, running, cancelling, onCancel, onAnalyseStarted, refreshKey, resumeAnalysisJobId,
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
  }, [clientId, platforms, refreshKey]);

  if (!clientId) {
    return (
      <div style={{ padding: "60px 20px", textAlign: "center", color: "var(--text-dim)" }}>
        Select or create a client on the Clients tab first -- Live Results is scoped to one client's discovered profiles.
      </div>
    );
  }

  const jobPlatformById = new Map((job?.platforms || []).map((p) => [p.platform, p]));
  const totalDone = (job?.platforms || []).reduce((acc, p) => acc + p.keywords_done, 0);
  const totalUnits = (job?.platforms || []).reduce((acc, p) => acc + p.keywords_total, 0);

  return (
    <div style={{ animation: "fadeUp 0.4s ease" }}>
      <h2 style={{ fontSize: "18px", fontWeight: 700, marginBottom: "4px" }}>{clientName || clientId}</h2>
      <div style={{ fontSize: "12px", color: "var(--text-dim)", marginBottom: "16px" }}>Live discovery results</div>

      {/* Phase tabs -- Discovery / Analysis, same two-tile row the original
          app used (styles/styles.css's platform-rail-grid at 2 columns). */}
      <div className="platform-rail-grid" style={{ gridTemplateColumns: "repeat(2, 1fr)", marginBottom: "16px" }}>
        {(["discovery", "analysis"] as const).map((ph) => (
          <div key={ph} className={`platform-rail-item ${phase === ph ? "active" : ""}`} onClick={() => setPhase(ph)}>
            <div className="rail-card-head">
              <span style={{ display: "flex", alignItems: "center" }}>
                {ph === "discovery" ? <DiscoverIcon size={16} color="var(--cyan)" /> : <AnalyseIcon size={16} color="#7c5cff" />}
              </span>
              <span style={{ fontSize: "12px", fontWeight: 500, color: "var(--text-primary)" }}>
                {ph === "discovery" ? "Discovery" : "Analysis"}
              </span>
            </div>
          </div>
        ))}
      </div>

      {/* Platform rail -- readiness, discovered-profile count, and (while a
          sweep is live or was just run) a small live progress row, per
          platform. Clicking one scopes the grid below to just that
          platform; clicking the active one clears back to "All Platforms". */}
      <div className="platform-rail-grid" style={{ gridTemplateColumns: `repeat(${platforms.length || 1}, 1fr)` }}>
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

      {/* "Recent Discovery Status" -- same box as the original app's, kept
          on screen after the job finishes (not just while running), so
          the analyst can see what the last sweep actually did. */}
      {job && (
        <div
          className="dashboard-card-box"
          style={{ marginTop: "16px", borderLeft: "4px solid var(--cyan)", background: "rgba(0, 229, 255, 0.04)", padding: "16px 20px" }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "8px", flexWrap: "wrap", gap: "8px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <DiscoverIcon size={18} color="var(--cyan)" />
              <span style={{ fontWeight: 700, color: "var(--text-main)", fontSize: "14px" }}>
                {running ? "Live Discovery Sweep Progress" : "Recent Discovery Status"}
              </span>
              {running ? (
                <span className="rail-pill" style={{ background: "var(--cyan)", color: "#000", fontWeight: 700, animation: "pulse 1.5s infinite" }}>RUNNING</span>
              ) : (
                <span className="rail-pill" style={{ background: "rgba(54,181,160,0.2)", color: "var(--success)", fontWeight: 700 }}>{job.status.toUpperCase()}</span>
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
            <div style={{ fontFamily: "var(--font-mono)", fontSize: "13px", fontWeight: 700, color: "var(--cyan)" }}>
              {totalDone} / {totalUnits || "?"} Sweeps Completed
            </div>
          </div>
          <div style={{ height: "8px", background: "var(--bg-inner)", borderRadius: "4px", overflow: "hidden" }}>
            <div
              style={{
                height: "100%", width: `${Math.min(100, Math.round((totalDone / (totalUnits || 1)) * 100))}%`,
                background: "linear-gradient(90deg, var(--cyan), var(--purple))", transition: "width 0.4s ease",
              }}
            />
          </div>
          {job.message && <div style={{ fontSize: "12px", color: "var(--text-dim)", marginTop: "8px" }}>{job.message}</div>}
          <div style={{ display: "flex", gap: "10px", marginTop: "12px", flexWrap: "wrap" }}>
            {job.platforms.map((p) => {
              const kw = currentKeywordOf(job, p);
              return (
                <div
                  key={p.platform}
                  style={{
                    display: "flex", alignItems: "center", gap: "6px", fontSize: "12px",
                    background: "var(--bg-surface)", padding: "5px 12px", borderRadius: "16px", border: "1px solid var(--border-color)",
                  }}
                >
                  <PlatformIcon platform={p.platform} size={15} />
                  <span style={{ fontWeight: 600, textTransform: "capitalize", color: "var(--text-main)" }}>{p.display_name}:</span>
                  <span style={{ color: STATUS_COLOR[p.status], fontWeight: 700, display: "inline-flex", alignItems: "center", gap: "4px" }}>
                    <StatusDot status={p.status} /> {p.keywords_done}/{p.keywords_total}
                  </span>
                  {kw && <span style={{ fontSize: "11px", color: "var(--cyan)", marginLeft: "4px" }}>"{kw}"</span>}
                  {p.note && <span style={{ fontSize: "11px", color: "var(--text-dim)", marginLeft: "4px" }}>{p.note}</span>}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {phase === "discovery" && (
        <DiscoveryProfileGrid
          groupId={clientId}
          platform={platform || undefined}
          refreshKey={refreshKey}
          onAnalyseStarted={onAnalyseStarted}
        />
      )}

      {phase === "analysis" && (
        <div style={{ marginTop: "24px" }}>
          <AnalysisView resumeJobId={resumeAnalysisJobId} />
        </div>
      )}
    </div>
  );
}
