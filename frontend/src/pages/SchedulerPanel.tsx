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

import type { Client, PlatformState } from "../api/types";
import { refresh as refreshDirectory, useClientDirectory } from "../services/clientDirectory";
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
  unfinishedPlatformsOf,
  type EntryStatus,
  type PlatformDetail,
  type PlatformOutcome,
  type ScheduleEntry,
} from "../services/scheduleRunner";
import { PlayIcon, StopIcon, SearchIcon, AlertTriangleIcon } from "../components/AppIcons";
import { PlatformIcon } from "../components/PlatformIcon";
import { SchedulerRunScope } from "../components/SchedulerRunScope";

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

// A sweep's outcome is per platform, and the row's single word cannot say
// which. "done, 9 found" reads as a clean sweep even when Facebook lost its
// session a third of the way in and two thirds of the results are simply
// missing -- the most common partial outcome there is, and the one worth
// re-running. These chips are what say so.
const PLATFORM_LOOK: Record<PlatformOutcome, { fg: string; bg: string; label: string }> = {
  done: { fg: "var(--success, #36b5a0)", bg: "rgba(54,181,160,0.14)", label: "done" },
  partial: { fg: "var(--warn-yellow, #fdb71b)", bg: "rgba(253,183,27,0.15)", label: "partial" },
  failed: { fg: "var(--danger, #e95053)", bg: "rgba(233,80,83,0.14)", label: "failed" },
  // Not an error and not a result: this platform had no usable session, so
  // it was never swept at all.
  skipped: { fg: "var(--text-dim, #667085)", bg: "rgba(102,112,133,0.12)", label: "no session" },
  running: { fg: "var(--accent, #7c5cff)", bg: "rgba(124,92,255,0.14)", label: "running" },
  // Listed by the sweep but never reached, because it was stopped or
  // cancelled first.
  pending: { fg: "var(--text-dim, #667085)", bg: "rgba(102,112,133,0.12)", label: "not reached" },
};

const PANE: React.CSSProperties = {
  background: "var(--bg-surface)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "12px",
  padding: "16px 18px",
  display: "flex",
  flexDirection: "column",
  minHeight: "calc(100vh - 270px)",
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

export function SchedulerPanel({ platforms }: { platforms: PlatformState[] }) {
  const state = useSyncExternalStore(subscribe, getSnapshot);
  // Subscribed, not copied: a one-time copy taken on mount goes stale the
  // moment a client is created anywhere else, and never catches up.
  const { clients } = useClientDirectory();
  const [filter, setFilter] = useState("");
  const [dragOverQueue, setDragOverQueue] = useState(false);
  const [busy, setBusy] = useState(false);
  const now = useNowTick(state.running);

  // Straight from the clients database, via the shared directory cache
  // (services/clientDirectory.ts). Refreshed on mount so a client created
  // on another machine -- or in another tab -- shows up here.
  const loadClients = useCallback(() => {
    void refreshDirectory();
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
      // start() now auto-resets all entries to a clean slate, so there is
      // no need to call resetStatuses() separately.
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
    <div style={{ color: "var(--text-main, #f2f4f7)", width: "100%", margin: 0, padding: 0 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "18px", flexWrap: "wrap", gap: "12px" }}>
        <div>
          <h1 style={{ fontSize: "22px", fontWeight: 700, color: "var(--text-primary, #fff)", margin: 0, letterSpacing: "-0.3px" }}>
            🔁 Scheduler
          </h1>
          <p style={{ fontSize: "13px", color: "var(--text-muted, #98a2b3)", margin: "4px 0 0 0", maxWidth: "960px" }}>
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

      <div style={{ display: "grid", gridTemplateColumns: "320px 1fr", gap: "18px", alignItems: "start", width: "100%" }}>
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

          <div style={{ display: "flex", flexDirection: "column", gap: "6px", overflowY: "auto", maxHeight: "calc(100vh - 350px)", minHeight: "350px" }}>
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
            <div style={{ display: "flex", flexDirection: "column", gap: "8px", overflowY: "auto", maxHeight: "calc(100vh - 350px)", minHeight: "350px" }}>
              {state.entries.map((entry, i) => (
                <QueueRow
                  key={entry.client_id}
                  entry={entry}
                  index={i}
                  isCurrent={state.currentId === entry.client_id}
                  stopping={state.stopping}
                  now={now}
                  onDrop={onQueueDrop}
                  platforms={platforms}
                  client={clients.find((c) => c.client_id === entry.client_id)}
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

// Empty until this entry has swept once, so a queued client stays as quiet
// as it was before this existed.
function PlatformChips({ entry }: { entry: ScheduleEntry }) {
  const ids = Object.keys(entry.platforms ?? {}).sort();
  if (!ids.length) return null;
  const owing = unfinishedPlatformsOf(entry).length;
  const details = entry.platform_details ?? {};

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "4px", marginTop: "6px" }}>
      <div style={{ display: "flex", gap: "5px", flexWrap: "wrap", alignItems: "center" }}>
        {ids.map((pid) => {
          const outcome = entry.platforms[pid];
          const look = PLATFORM_LOOK[outcome] ?? PLATFORM_LOOK.skipped;
          const detail: PlatformDetail | undefined = details[pid];
          const hasCounts = detail && (detail.found > 0 || detail.new > 0);
          return (
            <span
              key={pid}
              title={
                detail?.note
                  ? `${pid}: ${look.label} — ${detail.note}`
                  : `${pid}: ${look.label}${hasCounts ? ` (${detail!.found} found, ${detail!.new} new)` : ""}`
              }
              style={{
                display: "inline-flex", alignItems: "center", gap: "5px",
                background: look.bg, color: look.fg, borderRadius: "999px",
                padding: "2px 8px", fontSize: "10.5px", fontWeight: 700,
              }}
            >
              <PlatformIcon platform={pid} size={11} />
              {hasCounts ? (
                <span style={{ fontWeight: 600 }}>
                  {detail!.found}<span style={{ fontWeight: 400, opacity: 0.7 }}> found</span>
                  {detail!.new > 0 && (
                    <span style={{ marginLeft: "3px", fontWeight: 400, opacity: 0.7 }}>
                      · {detail!.new} new
                    </span>
                  )}
                </span>
              ) : (
                <span style={{ fontWeight: 500, opacity: 0.9 }}>{look.label}</span>
              )}
            </span>
          );
        })}
        {owing > 0 && (
          <span style={{ fontSize: "10.5px", color: "var(--warn-yellow, #fdb71b)", fontWeight: 700 }}>
            {owing} still owing
          </span>
        )}
      </div>

      {/* Failure / partial notes: show why each non-done platform had issues */}
      {ids.some((pid) => {
        const outcome = entry.platforms[pid];
        return (outcome === "failed" || outcome === "partial") && details[pid]?.note;
      }) && (
        <div style={{
          display: "flex", flexDirection: "column", gap: "2px",
          marginTop: "2px", paddingLeft: "2px",
        }}>
          {ids
            .filter((pid) => {
              const outcome = entry.platforms[pid];
              return (outcome === "failed" || outcome === "partial") && details[pid]?.note;
            })
            .map((pid) => (
              <span
                key={pid}
                style={{
                  fontSize: "10px",
                  color: entry.platforms[pid] === "failed"
                    ? "var(--danger, #e95053)"
                    : "var(--warn-yellow, #fdb71b)",
                  display: "inline-flex", alignItems: "center", gap: "4px",
                  lineHeight: 1.5,
                }}
              >
                <PlatformIcon platform={pid} size={10} />
                {details[pid].note}
              </span>
            ))}
        </div>
      )}
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
  platforms,
  client,
}: {
  entry: ScheduleEntry;
  index: number;
  isCurrent: boolean;
  stopping: boolean;
  now: number;
  onDrop: (e: React.DragEvent, targetEntryId?: string) => void;
  platforms: PlatformState[];
  // undefined when the client was deleted from the directory after being
  // queued -- the row still renders, it just has no scope to configure.
  client?: Client;
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
        <PlatformChips entry={entry} />

        {/* What this client will actually sweep. Editable while it sits in
            the queue AND while a run is in progress -- a change lands on the
            client record, and the runner re-reads that record when the
            client's turn comes up, so an edit made mid-queue still applies.
            Disabled only for the entry being swept right now, whose request
            has already gone. */}
        {client && (
          <div style={{ marginTop: "8px" }}>
            <SchedulerRunScope
              client={client}
              platforms={platforms}
              disabled={isCurrent}
            />
          </div>
        )}
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
