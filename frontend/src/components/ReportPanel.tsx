// The sweep report for one client, or for all of them: the three counts,
// with download and email.
//
// WHY THE COUNTS ARE SHOWN HERE AND NOT JUST DOWNLOADED. An analyst wants
// the number far more often than the file. Rendering it inline means the
// common case costs one click and no download, and the file is there for
// the times it has to be attached to something.
//
// DOWNLOAD IS A PLAIN LINK TO THE SERVER, not a blob built in the browser.
// The server renders the same HTML the email carries, so a report saved to
// disk and a report sitting in somebody's inbox are byte-identical -- which
// matters the moment one is forwarded as evidence.
import { useCallback, useEffect, useState } from "react";
import toast from "react-hot-toast";
import {
  clientReportHtmlUrl,
  combinedReportHtmlUrl,
  reportsApi,
  type ClientReport,
  type CombinedReport,
} from "../api/reportsApi";

interface Props {
  /** Omit for the combined, all-clients digest. */
  clientId?: string;
}

function Card({ label, value, colour }: { label: string; value: number; colour: string }) {
  return (
    <div style={{
      flex: 1, minWidth: 92, padding: "12px 10px", textAlign: "center",
      background: "var(--bg-inner)", border: "1px solid var(--border-subtle)",
      borderRadius: "10px",
    }}>
      <div style={{ color: colour, fontSize: "24px", fontWeight: 800, lineHeight: 1.1 }}>
        {value}
      </div>
      <div style={{
        color: "var(--text-dim)", fontSize: "10px", letterSpacing: "1px",
        textTransform: "uppercase", marginTop: "4px", fontWeight: 700,
      }}>
        {label}
      </div>
    </div>
  );
}

export function ReportPanel({ clientId }: Props) {
  const combined = !clientId;
  const [report, setReport] = useState<ClientReport | CombinedReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    const p = combined ? reportsApi.combined() : reportsApi.client(clientId!);
    p.then(setReport)
      .catch((e) => toast.error((e as Error).message || "Could not build the report"))
      .finally(() => setLoading(false));
  }, [clientId, combined]);

  useEffect(() => {
    if (combined || clientId) load();
  }, [load, combined, clientId]);

  const counts = report
    ? (combined ? (report as CombinedReport).totals : (report as ClientReport).validated)
    : null;
  const logoMatches = report
    ? (combined ? (report as CombinedReport).totals.logo_matches
                : (report as ClientReport).logo_matches)
    : 0;
  const pending = report
    ? (combined ? (report as CombinedReport).totals.pending
                : (report as ClientReport).pending.total)
    : 0;

  const send = async () => {
    setSending(true);
    try {
      const res = combined
        ? await reportsApi.sendCombined()
        : await reportsApi.sendClient(clientId!);
      if (res.sent) toast.success("Report emailed");
      // Not an error toast: "no recipients configured" is the normal state
      // until an operator opts in, and the message says exactly what to fix.
      else toast(res.detail, { icon: "✉️" });
    } catch (e) {
      toast.error((e as Error).message || "Could not send the report");
    } finally {
      setSending(false);
    }
  };

  const href = combined ? combinedReportHtmlUrl() : clientReportHtmlUrl(clientId!);

  return (
    <div style={{
      background: "var(--bg-surface)", border: "1px solid var(--border-subtle)",
      borderRadius: "12px", padding: "16px",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
        <div>
          <div style={{ fontSize: "13px", fontWeight: 700, color: "var(--text-main)" }}>
            {combined ? "All clients — combined report" : "Sweep report"}
          </div>
          <div style={{ fontSize: "11px", color: "var(--text-dim)", marginTop: 2 }}>
            Validated profiles: new in the last 24h, older (delta), and the total.
          </div>
        </div>
        <button
          onClick={load}
          disabled={loading}
          title="Recalculate from the current data"
          style={{
            padding: "4px 10px", borderRadius: "6px", cursor: "pointer", fontSize: "11px",
            border: "1px solid var(--border-color)", background: "var(--bg-inner)",
            color: "var(--text-muted)", fontWeight: 600,
          }}
        >
          {loading ? "…" : "↻ Refresh"}
        </button>
      </div>

      {counts && (
        <div style={{ display: "flex", gap: "8px", marginTop: "14px", flexWrap: "wrap" }}>
          <Card label="New" value={counts.new} colour="var(--success, #36B5A0)" />
          <Card label="Delta" value={counts.delta} colour="var(--purple, #8838DD)" />
          <Card label="Total" value={counts.total} colour="var(--text-main)" />
        </div>
      )}

      {report && (
        <div style={{ fontSize: "11px", color: "var(--text-dim)", marginTop: "10px" }}>
          {pending} awaiting triage
          {logoMatches > 0 && (
            <> &middot; <span style={{ color: "var(--warn-yellow, #fdb71b)", fontWeight: 700 }}>
              {logoMatches} logo match{logoMatches === 1 ? "" : "es"}
            </span></>
          )}
          {combined && <> &middot; {(report as CombinedReport).clients.length} clients</>}
        </div>
      )}

      {/* "New: 0" is the NORMAL state right after a sweep -- validating is a
          manual step that happens afterwards. Saying so stops a zero reading
          like the sweep found nothing. */}
      {counts?.new === 0 && counts.total > 0 && (
        <div style={{
          fontSize: "11px", color: "var(--text-dim)", marginTop: "8px",
          padding: "8px 10px", background: "var(--bg-inner)", borderRadius: "8px",
          border: "1px solid var(--border-subtle)",
        }}>
          Nothing validated in the last 24 hours. Validating is a manual step, so this
          is expected when a sweep has just finished and the results are not triaged yet.
        </div>
      )}

      <div style={{ display: "flex", gap: "8px", marginTop: "14px", flexWrap: "wrap" }}>
        <a
          href={href}
          // The server sets Content-Disposition: attachment, so this saves
          // the file rather than navigating away from the app.
          style={{
            padding: "7px 14px", borderRadius: "8px", textDecoration: "none",
            border: "1px solid var(--border-color)", background: "var(--bg-inner)",
            color: "var(--text-main)", fontSize: "12px", fontWeight: 600,
          }}
        >
          ⬇ Download report
        </a>
        <a
          href={href}
          target="_blank"
          rel="noreferrer"
          style={{
            padding: "7px 14px", borderRadius: "8px", textDecoration: "none",
            border: "1px solid var(--border-color)", background: "transparent",
            color: "var(--text-muted)", fontSize: "12px", fontWeight: 600,
          }}
        >
          ↗ Preview
        </a>
        <button
          onClick={() => void send()}
          disabled={sending}
          title="Send this report to the configured alert recipients"
          style={{
            padding: "7px 14px", borderRadius: "8px", cursor: "pointer",
            border: "1px solid var(--accent, #7c5cff)", background: "transparent",
            color: "var(--accent, #7c5cff)", fontSize: "12px", fontWeight: 600,
          }}
        >
          {sending ? "Sending…" : "✉ Email report"}
        </button>
      </div>
    </div>
  );
}
