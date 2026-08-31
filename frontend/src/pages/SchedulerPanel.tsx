// Scheduler: pick which clients to sweep, in what order, then run discovery
// for each of them one at a time.
//
// Two panes. LEFT is every saved client (the same directory the Clients page
// shows, see services/savedClients.ts). RIGHT is the run queue -- drag a
// client across to add it, drag entries within it to reorder, press Run and
// the engine works down the list, one client's discovery sweep at a time.
//
// The queue and the run loop itself live in services/scheduleRunner.ts, NOT
// in this component: a run outlives this panel (an analyst is expected to go
// watch Live Results while it works), so the state has to outlive it too.
// This file is a view over that store and nothing more.
//
// Everything the previous version of this file talked to -- schedulerApi,
// jobsApi, a server-side round-robin engine -- was deleted with the old
// backend. Sequencing happens here now, over the one route that survives:
// POST /discovery/jobs and its poll.
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { toast } from "react-hot-toast";
import { clientsApi } from "../api/clientsApi";
import type { Client } from "../api/types";
import { listSavedClients } from "../services/savedClients";
import {
  clearQueue,
  dequeue,
  enqueue,
  getSnapshot,
  keywordsOf,
  reorder,
  resetStatuses,
  start,
  stop,
  subscribe,
  type EntryStatus,
  type ScheduleEntry,
} from "../services/scheduleRunner";
import { PlayIcon, StopIcon, SearchIcon, AlertTriangleIcon } from "../components/AppIcons";

// Drag payloads are prefixed so one drop handler can tell "a new client from
// the left pane" from "an entry being reordered within the queue".
const DRAG_CLIENT = "client:";
const DRAG_ENTRY = "entry:";

const STATUS_LOOK: Record<EntryStatus, { color: string; label: string; dot: string }> = {
  pending: { color: "var(--text-dim, #667085)", label: "queued", dot: "○" },
  running: { color: "var(--accent, #7c5cff)", label: "running", dot: "●" },
  done: { color: "var(--success, #36b5a0)", label: "done", dot: "●" },
  failed: { color: "var(--danger, #e95053)", label: "failed", dot: "●" },
  skipped: { color: "var(--warn-yellow, #fdb71b)", label: "skipped", dot: "●" },
  // Cancelled from outside this scheduler (another tab, a direct API call).
  // Terminal on purpose -- see the comment in scheduleRunner.ts's poll loop.
  cancelled: { color: "var(--warn-yellow, #fdb71b)", label: "cancelled", dot: "●" },
};

const PANE: React.CSSProperties = {
  background: "var(--bg-surface)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "12px",
  padding: "14px 16px",
  display: "flex",
  flexDirection: "column",
  minHeight: "420px",
};

const PANE_TITLE: React.CSSProperties = {
  fontSize: "10px",
  color: "var(--text-dim)",
  textTransform: "uppercase",
  letterSpacing: "1px",
  fontWeight: 700,
  marginBottom: "10px",
};

const SMALL_BTN: React.CSSProperties = {
  padding: "3px 8px",
  borderRadius: "6px",
  border: "1px solid var(--border-color)",
  background: "var(--bg-inner)",
  color: "var(--text-muted)",
  fontSize: "11px",
  fontWeight: 600,
  cursor: "pointer",
  lineHeight: 1.6,
};

function durationLabel(from: number | null, to: number | null): string {
  if (!from) return "";
  const secs = Math.max(0, Math.round(((to ?? Date.now()) - from) / 1000));
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

// A 1s tick so a running entry's elapsed time counts up on its own, without
// the store having to emit purely for the clock.
function useNowTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

export function SchedulerPanel() {
  const state = useSyncExternalStore(subscribe, getSnapshot);
  const [clients, setClients] = useState<Client[]>([]);
  const [filter, setFilter] = useState("");
  const [dragOverQueue, setDragOverQueue] = useState(false);
  const [busy, setBusy] = useState(false);
  const now = useNowTick(state.running);

  // Same merge the Clients page does: whatever the (currently backendless)
  // /clients route returns, plus this browser's own saved clients.
  const loadClients = useCallback(() => {
    clientsApi
      .listClients()
      .then((res) => res.items)
      .catch(() => [] as Client[])
      .then((server) => {
        const byId = new Map(server.map((c) => [c.client_id, c]));
        for (const c of listSavedClients()) if (!byId.has(c.client_id)) byId.set(c.client_id, c);
        setClients([...byId.values()]);
      });
  }, []);

  useEffect(() => {
    loadClients();
  }, [loadClients]);

  const queuedIds = useMemo(
    () => new Set(state.entries.map((e) => e.client_id)),
    [state.entries],
  );

  const available = useMemo(
    () =>
      clients.filter((c) => {
        if (queuedIds.has(c.client_id)) return false;
        const q = filter.trim().toLowerCase();
        if (!q) return true;
        return (
          (c.name || "").toLowerCase().includes(q) ||
          c.client_id.toLowerCase().includes(q)
        );
      }),
    [clients, queuedIds, filter],
  );

  const counts = useMemo(() => {
    const c = { pending: 0, done: 0, failed: 0, skipped: 0 };
    for (const e of state.entries) {
      if (e.status === "pending") c.pending += 1;
      else if (e.status === "done") c.done += 1;
      else if (e.status === "failed") c.failed += 1;
      else if (e.status === "skipped") c.skipped += 1;
    }
    return c;
  }, [state.entries]);

  const addClient = (clientId: string) => {
    const client = clients.find((c) => c.client_id === clientId);
    if (!client) return;
    enqueue(client);
    if (!keywordsOf(client).length) {
      toast(`"${client.name || client.client_id}" has no keywords -- it will be skipped.`, {
        icon: "⚠️",
      });
    }
  };

  const onQueueDrop = (e: React.DragEvent, targetEntryId?: string) => {
    e.preventDefault();
    e.stopPropagation();
    setDragOverQueue(false);
    const data = e.dataTransfer.getData("text/plain");
    if (data.startsWith(DRAG_CLIENT)) {
      addClient(data.slice(DRAG_CLIENT.length));
    } else if (data.startsWith(DRAG_ENTRY) && targetEntryId) {
      reorder(data.slice(DRAG_ENTRY.length), targetEntryId);
    }
  };

  const handleRun = async () => {
    setBusy(true);
    try {
      if (!state.entries.some((e) => e.status === "pending")) resetStatuses();
      await start();
    } finally {
      setBusy(false);
    }
  };

  const nothingPending = !state.entries.some((e) => e.status === "pending");
  const runLabel = state.entries.length === 0
    ? "Run queue"
    : nothingPending
    ? "Run again"
    : `Run queue (${counts.pending})`;

  return (
    <div style={{ padding: "24px", color: "var(--text-main, #f2f4f7)", maxWidth: "1200px", margin: "0 auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "18px", flexWrap: "wrap", gap: "12px" }}>
        <div>
          <h1 style={{ fontSize: "22px", fontWeight: 700, color: "var(--text-primary, #fff)", margin: 0, letterSpacing: "-0.3px" }}>
            🔁 Scheduler
          </h1>
          <p style={{ fontSize: "13px", color: "var(--text-muted, #98a2b3)", margin: "4px 0 0 0", maxWidth: "720px" }}>
            Drag clients from the left into the run queue, then press Run. Discovery sweeps them one
            at a time, top to bottom — never two at once, so no two clients compete for the same
            platform session. You can keep working elsewhere while it runs; leaving this tab does not
            stop it (a full page reload does).
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          {state.running ? (
            <button
              onClick={() => void stop()}
              disabled={state.stopping}
              style={{
                padding: "10px 18px", borderRadius: "10px",
                cursor: state.stopping ? "wait" : "pointer",
                background: "rgba(233,80,83,0.12)", border: "1px solid rgba(233,80,83,0.4)",
                color: "var(--danger)", fontSize: "13px", fontWeight: 700, whiteSpace: "nowrap",
                display: "inline-flex", alignItems: "center", gap: "6px",
              }}
            >
              <StopIcon size={13} color="var(--danger)" />
              {state.stopping ? "Stopping…" : "Stop"}
            </button>
          ) : (
            <button
              onClick={() => void handleRun()}
              disabled={busy || state.entries.length === 0}
              style={{
                padding: "10px 18px", borderRadius: "10px",
                cursor: state.entries.length === 0 ? "not-allowed" : "pointer",
                background: "rgba(54,181,160,0.12)", border: "1px solid rgba(54,181,160,0.4)",
                color: "var(--success)", fontSize: "13px", fontWeight: 700, whiteSpace: "nowrap",
                opacity: state.entries.length === 0 ? 0.5 : 1,
                display: "inline-flex", alignItems: "center", gap: "6px",
              }}
              title={state.entries.length === 0 ? "Add at least one client to the queue first" : ""}
            >
              <PlayIcon size={13} color="var(--success)" />
              {runLabel}
            </button>
          )}
        </div>
      </div>

      {/* Summary strip -- only once there is something to summarise. */}
      {state.entries.length > 0 && (
        <div style={{ display: "flex", gap: "12px", marginBottom: "16px", flexWrap: "wrap" }}>
          {[
            { label: "In queue", value: state.entries.length, color: "var(--text-main)" },
            { label: "Waiting", value: counts.pending, color: "var(--text-muted)" },
            { label: "Done", value: counts.done, color: "var(--success)" },
            { label: "Failed", value: counts.failed, color: counts.failed ? "var(--danger)" : "var(--text-dim)" },
            { label: "Skipped", value: counts.skipped, color: counts.skipped ? "var(--warn-yellow, #fdb71b)" : "var(--text-dim)" },
          ].map((s) => (
            <div key={s.label} style={{ flex: "1 1 120px", background: "var(--bg-surface)", border: "1px solid var(--border-subtle)", borderRadius: "12px", padding: "10px 14px" }}>
              <div style={{ fontSize: "10px", color: "var(--text-dim)", textTransform: "uppercase", letterSpacing: "1px" }}>{s.label}</div>
              <div style={{ fontSize: "17px", fontWeight: 700, color: s.color, marginTop: "2px" }}>{s.value}</div>
            </div>
          ))}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(240px, 1fr) minmax(320px, 1.4fr)", gap: "16px", alignItems: "start" }}>
        {/* ─────────────────────────── saved clients ─────────────────────── */}
        <div style={PANE}>
          <div style={PANE_TITLE}>Saved clients ({available.length})</div>

          <div style={{ position: "relative", display: "flex", alignItems: "center", marginBottom: "10px" }}>
            <SearchIcon size={13} color="var(--text-muted, #98a2b3)" style={{ position: "absolute", left: "10px", pointerEvents: "none" }} />
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter clients…"
              style={{
                width: "100%", background: "var(--bg-inner)", border: "1px solid var(--border-color)",
                borderRadius: "8px", padding: "7px 10px 7px 30px", color: "var(--text-main)",
                fontSize: "12px", outline: "none", boxSizing: "border-box",
              }}
            />
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "6px", overflowY: "auto", maxHeight: "460px" }}>
            {available.length === 0 && (
              <div style={{ fontSize: "12px", color: "var(--text-dim)", padding: "16px 4px", textAlign: "center", lineHeight: 1.6 }}>
                {clients.length === 0
                  ? "No saved clients yet. Create one on the Clients page first."
                  : filter.trim()
                  ? `No clients match "${filter.trim()}".`
                  : "Every saved client is already in the queue."}
              </div>
            )}
            {available.map((c) => {
              const kwCount = keywordsOf(c).length;
              return (
                <div
                  key={c.client_id}
                  draggable
                  onDragStart={(e) => {
                    e.dataTransfer.setData("text/plain", DRAG_CLIENT + c.client_id);
                    e.dataTransfer.effectAllowed = "copy";
                  }}
                  onDoubleClick={() => addClient(c.client_id)}
                  title="Drag into the queue (or double-click, or press +)"
                  style={{
                    display: "flex", alignItems: "center", gap: "9px", padding: "8px 10px",
                    background: "var(--bg-inner)", border: "1px solid var(--border-subtle)",
                    borderRadius: "8px", cursor: "grab",
                  }}
                >
                  <span style={{ color: "var(--text-dim)", fontSize: "13px", lineHeight: 1 }}>⠿</span>
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: "13px", fontWeight: 600, color: "var(--text-main)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {c.name || c.client_id}
                    </div>
                    <div style={{ fontSize: "11px", color: kwCount ? "var(--text-dim)" : "var(--warn-yellow, #fdb71b)" }}>
                      {kwCount ? `${kwCount} keyword${kwCount === 1 ? "" : "s"}` : "no keywords"}
                    </div>
                  </span>
                  <button
                    style={{ ...SMALL_BTN, padding: "2px 7px" }}
                    onClick={() => addClient(c.client_id)}
                    title="Add to the run queue"
                  >
                    ＋
                  </button>
                </div>
              );
            })}
          </div>
        </div>

        {/* ──────────────────────────── run queue ────────────────────────── */}
        <div
          style={{
            ...PANE,
            borderColor: dragOverQueue ? "var(--accent, #7c5cff)" : "var(--border-subtle)",
            background: dragOverQueue ? "rgba(124, 92, 255, 0.06)" : "var(--bg-surface)",
            transition: "background 0.15s ease, border-color 0.15s ease",
          }}
          onDragOver={(e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = "copy";
            setDragOverQueue(true);
          }}
          onDragLeave={() => setDragOverQueue(false)}
          onDrop={(e) => onQueueDrop(e)}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px" }}>
            <div style={PANE_TITLE}>Run queue ({state.entries.length})</div>
            {state.entries.length > 0 && !state.running && (
              <div style={{ display: "flex", gap: "6px", marginBottom: "10px" }}>
                <button style={SMALL_BTN} onClick={() => resetStatuses()} title="Put every entry back to queued">
                  Reset
                </button>
                <button
                  style={{ ...SMALL_BTN, color: "#ff6b6b", borderColor: "rgba(239,68,68,0.4)" }}
                  onClick={() => {
                    clearQueue();
                    toast("Queue cleared");
                  }}
                  title="Empty the queue"
                >
                  Clear
                </button>
              </div>
            )}
          </div>

          {state.entries.length === 0 ? (
            <div
              style={{
                flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
                border: "1.5px dashed var(--border-color)", borderRadius: "10px",
                color: "var(--text-dim)", fontSize: "13px", textAlign: "center",
                padding: "24px", lineHeight: 1.7, minHeight: "260px",
              }}
            >
              Drag clients here to schedule them.
              <br />
              <span style={{ fontSize: "12px" }}>They will be swept in the order you drop them.</span>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "6px", overflowY: "auto", maxHeight: "460px" }}>
              {state.entries.map((entry, i) => (
                <QueueRow
                  key={entry.client_id}
                  entry={entry}
                  index={i}
                  isCurrent={state.currentId === entry.client_id}
                  stopping={state.stopping}
                  now={now}
                  onDrop={onQueueDrop}
                />
              ))}
            </div>
          )}

          {counts.failed > 0 && !state.running && (
            <div style={{
              marginTop: "12px", padding: "8px 12px", background: "rgba(233,80,83,0.08)",
              border: "1px solid rgba(233,80,83,0.25)", borderRadius: "8px",
              color: "var(--danger)", fontSize: "12px", display: "flex", alignItems: "center", gap: "8px",
            }}>
              <AlertTriangleIcon size={14} color="var(--danger)" />
              <span>
                {counts.failed} client{counts.failed === 1 ? "" : "s"} failed. Reset puts them back in
                the queue to retry.
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function QueueRow({
  entry,
  index,
  isCurrent,
  stopping,
  now,
  onDrop,
}: {
  entry: ScheduleEntry;
  index: number;
  isCurrent: boolean;
  stopping: boolean;
  now: number;
  onDrop: (e: React.DragEvent, targetEntryId?: string) => void;
}) {
  const [over, setOver] = useState(false);
  // A cancel is only honoured BETWEEN sweeps server-side (see
  // backend/discovery/runner.py -- a sweep already in flight runs to
  // completion), so Stop can take a while to land on a slow platform.
  // Saying "stopping" rather than "running" is the difference between that
  // looking like a wind-down and looking like Stop was ignored.
  const pendingStop = isCurrent && stopping && entry.status === "running";
  const look = pendingStop
    ? { color: "var(--warn-yellow, #fdb71b)", label: "stopping", dot: "●" }
    : STATUS_LOOK[entry.status];
  const elapsed =
    entry.status === "running"
      ? durationLabel(entry.started_at, now)
      : durationLabel(entry.started_at, entry.finished_at);

  return (
    <div
      draggable={!isCurrent}
      onDragStart={(e) => {
        e.dataTransfer.setData("text/plain", DRAG_ENTRY + entry.client_id);
        e.dataTransfer.effectAllowed = "move";
      }}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        setOver(false);
        onDrop(e, entry.client_id);
      }}
      style={{
        display: "flex", alignItems: "center", gap: "10px", padding: "9px 11px",
        borderRadius: "8px",
        background: isCurrent ? "rgba(124, 92, 255, 0.08)" : "var(--bg-inner)",
        border: `1px solid ${
          over ? "var(--accent, #7c5cff)" : isCurrent ? "var(--accent, #7c5cff)" : "var(--border-subtle)"
        }`,
        cursor: isCurrent ? "default" : "grab",
      }}
    >
      <span style={{ fontSize: "11px", color: "var(--text-dim)", fontFamily: "var(--font-mono)", minWidth: "18px" }}>
        {index + 1}.
      </span>

      <span style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: "13px", fontWeight: 600, color: "var(--text-primary, #fff)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {entry.name}
        </div>
        <div style={{ fontSize: "11px", color: "var(--text-dim)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {pendingStop
            ? `${entry.message} — finishing the sweep in flight before stopping`
            : entry.message || `${entry.keywords.length} keyword${entry.keywords.length === 1 ? "" : "s"}`}
          {entry.status === "done" && entry.found > 0 && (
            <span style={{ color: "var(--success)" }}> · {entry.found} found, {entry.new_profiles} new</span>
          )}
        </div>
      </span>

      {elapsed && (
        <span style={{ fontSize: "11px", color: "var(--text-dim)", fontFamily: "var(--font-mono)", whiteSpace: "nowrap" }}>
          {elapsed}
        </span>
      )}

      <span
        style={{
          fontSize: "11px", fontWeight: 700, color: look.color, whiteSpace: "nowrap",
          display: "inline-flex", alignItems: "center", gap: "5px", minWidth: "68px",
        }}
      >
        <span style={entry.status === "running" ? { animation: "pulse 1.2s ease-in-out infinite" } : undefined}>
          {look.dot}
        </span>
        {look.label}
      </span>

      <button
        style={{
          ...SMALL_BTN,
          padding: "2px 7px",
          color: isCurrent ? "var(--text-dim)" : "#ff6b6b",
          borderColor: isCurrent ? "var(--border-color)" : "rgba(239,68,68,0.4)",
          cursor: isCurrent ? "not-allowed" : "pointer",
          opacity: isCurrent ? 0.4 : 1,
        }}
        disabled={isCurrent}
        onClick={() => dequeue(entry.client_id)}
        title={isCurrent ? "Stop the run before removing the client being swept" : "Remove from the queue"}
      >
        ✕
      </button>
    </div>
  );
}
