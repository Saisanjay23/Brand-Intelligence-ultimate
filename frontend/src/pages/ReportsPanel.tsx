// Reports, in one place: the all-clients digest, and any single client's.
//
// WHY BOTH LIVE HERE rather than beside the thing they describe. A report is
// an operator's job, not an analyst's: it is read on a schedule, forwarded,
// and filed. Putting the per-client one on the Clients page meant it sat in
// the middle of the screen used for CONFIGURING a client, where it was noise
// during the task that screen is actually for. Here, both are one click
// apart and neither interrupts anything.
//
// The client picker means the per-client report no longer depends on which
// client happens to be selected elsewhere in the app -- an operator writing
// up the week can move between them without changing the app's active
// client and disturbing whatever an analyst has open.
import { useEffect, useState } from "react";
import { ReportPanel } from "../components/ReportPanel";
import { listClients, refresh as refreshDirectory, useClientDirectory } from "../services/clientDirectory";

export function ReportsPanel() {
  const { clients, loaded } = useClientDirectory();
  const [clientId, setClientId] = useState("");

  useEffect(() => {
    void refreshDirectory();
  }, []);

  // Default to the first client so the panel is useful on arrival rather
  // than showing an empty picker the operator has to act on first.
  useEffect(() => {
    if (!clientId && clients.length) setClientId(clients[0].client_id);
  }, [clients, clientId]);

  const options = clients.length ? clients : listClients();

  return (
    <div style={{ padding: "24px", maxWidth: "1100px", margin: "0 auto",
                  color: "var(--text-main, #f2f4f7)" }}>
      <div style={{ marginBottom: "18px" }}>
        <h1 style={{ fontSize: "22px", fontWeight: 700, margin: 0, letterSpacing: "-0.3px",
                     color: "var(--text-primary, #fff)" }}>
          📄 Reports
        </h1>
        <p style={{ fontSize: "13px", color: "var(--text-muted, #98a2b3)",
                    margin: "4px 0 0 0", maxWidth: "760px" }}>
          Validated profiles per client and across all of them — <strong>New</strong> in the
          last 24 hours, <strong>Delta</strong> older than that, and the <strong>Total</strong>.
          Download the file or email it to the configured recipients.
        </p>
      </div>

      {/* Everything, first: the number an operator usually wants. */}
      <div style={{ marginBottom: "22px" }}>
        <ReportPanel />
      </div>

      <div style={{ marginBottom: "10px", display: "flex", alignItems: "center",
                    gap: "10px", flexWrap: "wrap" }}>
        <span style={{ fontSize: "11px", fontWeight: 700, letterSpacing: "1px",
                       textTransform: "uppercase", color: "var(--text-dim)" }}>
          Single client
        </span>
        <select
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          style={{
            padding: "6px 10px", borderRadius: "8px", fontSize: "12px", fontWeight: 600,
            background: "var(--bg-inner)", color: "var(--text-main)",
            border: "1px solid var(--border-color)", minWidth: "220px",
          }}
        >
          {options.map((c) => (
            <option key={c.client_id} value={c.client_id}>
              {c.name || c.client_id}
            </option>
          ))}
        </select>
      </div>

      {clientId ? (
        // Keyed on the client id so switching clients REMOUNTS the panel.
        // Without that it would keep the previous client's numbers on screen
        // until the new fetch resolved -- a report showing one client's name
        // over another's counts, which is the worst possible failure for a
        // document someone forwards.
        <ReportPanel key={clientId} clientId={clientId} />
      ) : (
        <div style={{ fontSize: "13px", color: "var(--text-dim)", padding: "16px",
                      background: "var(--bg-surface)", borderRadius: "12px",
                      border: "1px solid var(--border-subtle)" }}>
          {loaded ? "No clients configured yet." : "Loading clients…"}
        </div>
      )}
    </div>
  );
}
