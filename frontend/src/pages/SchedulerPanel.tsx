// Scheduler: pick which clients to sweep, in what order, then run discovery
// for each of them one at a time.
//
// Two panes. LEFT is every saved client (the same directory the Clients page
// shows, see services/savedClients.ts). RIGHT is the run queue -- drag a
// client across to add it, drag entries within it to reorder, press Run and
// the engine works down the list, one client's discovery sweep at a time.
//
// The queue, the schedule and the run loop all live on the SERVER
// (backend/services/scheduler_service.py). This file is a view over them
// and nothing more -- services/scheduleRunner.ts is the client that polls
// it.
//
// THAT IS WHAT MAKES THE CLOCK WORK. The run loop used to be plain JS in
// this tab, which meant a run scheduled for two in the morning happened
// only if somebody left a browser open -- and if they had not, nothing ran
// and nothing said so. Now the tab is optional: closing it, reloading it,
// or shutting the laptop does not touch a run in progress.
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
  setDirectory,
  start,
  stop,
  subscribe,
  unfinishedPlatformsOf,
  type EntryStatus,
  type PlatformDetail,
  type PlatformOutcome,
  type ScheduleEntry,
} from "../services/scheduleRunner";
import { SchedulerSchedule } from "../components/SchedulerSchedule";
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
  // Terminal on purpose: re-queueing it would restart the very job that
  // was just cancelled, on every lap, burning a platform session each time.
  cancelled: { color: "var(--warn-yellow, #fdb71b)", label: "cancelled", dot: "●" },
  // An analyst pressed Stop while this client was being swept.
  stopped: { color: "var(--warn-yellow, #fdb71b)", label: "stopped", dot: "●" },
  // The backend restarted mid-sweep. Whatever this client had already
  // found was saved -- the run simply stopped watching it. Says so rather
  // than claiming either success or failure, because neither is true.
  interrupted: { color: "var(--warn-yellow, #fdb71b)", label: "interrupted", dot: "●" },
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
  minWidth: 0,
  maxWidth: "100%",
  boxSizing: "border-box",
  overflow: "hidden",
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

  // The queue lives on the server and knows only client ids. The store
  // needs the directory to put a name and a keyword count against each one.
  useEffect(() => {
    setDirectory(clients);
  }, [clients]);

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
      await start();
    } catch (e) {
      // A 409 here means a run is ALREADY going -- most likely the
      // scheduled one that just fired. Saying so is the difference
      // between a button that looks broken and one that is telling the
      // analyst something they need to know.
      toast.error((e as Error).message || "could not start the run");
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
    <div style={{ color: "var(--text-main, #f2f4f7)", width: "100%", maxWidth: "100%", margin: 0, padding: 0, boxSizing: "border-box" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "18px", flexWrap: "wrap", gap: "12px" }}>
        <div>
          <h1 style={{ fontSize: "22px", fontWeight: 700, color: "var(--text-primary, #fff)", margin: 0, letterSpacing: "-0.3px" }}>
            🔁 Scheduler
          </h1>
          <p style={{ fontSize: "13px", color: "var(--text-muted, #98a2b3)", margin: "4px 0 0 0", maxWidth: "960px" }}>
            Drag clients from the left into the run queue, then press Run — or set a time below and
            let it run itself. Discovery sweeps them one at a time, top to bottom — never two at
            once, so no two clients compete for the same platform session. The run happens on the
            server: closing this tab, reloading, or shutting the laptop does not stop it.
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

      {/* THE BACKEND IS THE SCHEDULER NOW, so losing it is not a cosmetic
          problem: nothing is going to fire while this is showing. Said
          plainly rather than leaving the last good queue on screen looking
          current -- a stale-but-happy queue is the lie an analyst believes.

          ABOVE the schedule card, deliberately. Below it, the warning did
          not cover the most reassuring thing on the page -- a green "Next
          run Fri, 02:00" line that is, at that moment, a stale promise
          nothing is going to keep. */}
      {state.error && (
        <div style={{
          display: "flex", gap: "8px", alignItems: "flex-start", marginBottom: "16px",
          padding: "9px 12px", background: "rgba(233,80,83,0.10)",
          border: "1px solid rgba(233,80,83,0.3)", borderRadius: "8px",
          color: "var(--danger)", fontSize: "12px", lineHeight: 1.55,
        }}>
          <span style={{ marginTop: "1px", flexShrink: 0 }}>
            <AlertTriangleIcon size={14} color="var(--danger)" />
          </span>
          <span>
            <strong>Cannot reach the backend.</strong> {state.error}. Nothing below is guaranteed to
            be current, and no scheduled run will fire until the tool is reachable again.
          </span>
        </div>
      )}

      {/* WHEN this queue runs by itself. Above the queue because it is the
          thing an analyst sets once and then relies on. */}
      <SchedulerSchedule
        schedule={state.schedule}
        nextRunAt={state.nextRunAt}
        upcoming={state.upcoming}
        lastFiredAt={state.lastFiredAt}
        wallClockShift={state.wallClockShift}
        catchUpGraceMinutes={state.catchUpGraceMinutes}
        lastRun={state.lastRun}
        running={state.running}
        disabled={state.running}
        stale={Boolean(state.error)}
      />

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

      <div className="scheduler-grid-layout">
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
            <div style={{ display: "flex", flexDirection: "column", gap: "8px", overflowY: "auto", maxHeight: "calc(100vh - 350px)", minHeight: "350px", width: "100%", minWidth: 0, maxWidth: "100%", boxSizing: "border-box" }}>
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
    <div style={{ display: "flex", flexDirection: "column", gap: "6px", marginTop: "6px", maxWidth: "100%", minWidth: 0, boxSizing: "border-box" }}>
      <div style={{ display: "flex", gap: "5px", flexWrap: "wrap", alignItems: "center", maxWidth: "100%" }}>
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
                display: "inline-flex", alignItems: "center", gap: "4px",
                background: look.bg, color: look.fg, borderRadius: "999px",
                padding: "2px 7px", fontSize: "10px", fontWeight: 700,
                flexShrink: 0,
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
        {/* Searches still owing */}
        {entry.owed > 0 ? (
          <span
            title="Keyword searches this client still owes. The Scheduler runs one gap-closing pass over these automatically after the queue finishes."
            style={{ fontSize: "10.5px", color: "var(--warn-yellow, #fdb71b)", fontWeight: 700, flexShrink: 0 }}
          >
            {entry.owed} search{entry.owed === 1 ? "" : "es"} still owing
          </span>
        ) : entry.owed < 0 && owing > 0 ? (
          <span
            title="Coverage could not be read, so what this client still owes is unknown."
            style={{ fontSize: "10.5px", color: "var(--warn-yellow, #fdb71b)", fontWeight: 700, flexShrink: 0 }}
          >
            {owing} platform{owing === 1 ? "" : "s"} unfinished
          </span>
        ) : null}
      </div>

      {/* Failure / partial notes: show why each non-done platform had issues */}
      {ids.some((pid) => {
        const outcome = entry.platforms[pid];
        return (outcome === "failed" || outcome === "partial") && details[pid]?.note;
      }) && (
        <div style={{
          display: "flex", flexDirection: "column", gap: "3px",
          marginTop: "3px", paddingLeft: "2px", maxWidth: "100%", minWidth: 0, boxSizing: "border-box",
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
                  display: "inline-flex", alignItems: "flex-start", gap: "5px",
                  lineHeight: 1.4,
                  wordBreak: "break-word",
                  overflowWrap: "anywhere",
                  maxWidth: "100%",
                }}
              >
                <span style={{ marginTop: "2px", flexShrink: 0 }}><PlatformIcon platform={pid} size={10} /></span>
                <span style={{ flex: 1, minWidth: 0, wordBreak: "break-word", overflowWrap: "anywhere" }}>{details[pid].note}</span>
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
        display: "flex",
        flexDirection: "column",
        gap: "6px",
        padding: "10px 12px",
        borderRadius: "8px",
        background: isCurrent ? "rgba(124, 92, 255, 0.08)" : "var(--bg-inner)",
        border: `1px solid ${
          over ? "var(--accent, #7c5cff)" : isCurrent ? "var(--accent, #7c5cff)" : "var(--border-subtle)"
        }`,
        cursor: isCurrent ? "default" : "grab",
        width: "100%",
        minWidth: 0,
        maxWidth: "100%",
        boxSizing: "border-box",
        position: "relative",
      }}
    >
      {/* ── CARD HEADER: Index, Client Title, Status Pill, Elapsed, and Pinned X Button ── */}
      <div style={{ display: "flex", alignItems: "center", gap: "8px", width: "100%", minWidth: 0, boxSizing: "border-box" }}>
        <span style={{ fontSize: "11px", color: "var(--text-dim)", fontFamily: "var(--font-mono)", minWidth: "18px", flexShrink: 0 }}>
          {index + 1}.
        </span>

        <div style={{ fontSize: "13px", fontWeight: 600, color: "var(--text-primary, #fff)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: "0 1 auto", minWidth: 0 }}>
          {entry.name}
        </div>

        {/* Status Pill */}
        <span
          style={{
            fontSize: "11px", fontWeight: 700, color: look.color, whiteSpace: "nowrap",
            display: "inline-flex", alignItems: "center", gap: "4px", flexShrink: 0,
            background: "rgba(255, 255, 255, 0.04)", padding: "1px 6px", borderRadius: "4px",
          }}
        >
          <span style={entry.status === "running" ? { animation: "pulse 1.2s ease-in-out infinite" } : undefined}>
            {look.dot}
          </span>
          {look.label}
        </span>

        {/* Elapsed Timer */}
        {elapsed && (
          <span style={{ fontSize: "11px", color: "var(--text-dim)", fontFamily: "var(--font-mono)", whiteSpace: "nowrap", flexShrink: 0 }}>
            ⏱️ {elapsed}
          </span>
        )}

        {/* Spacer pushes the X mark to the right */}
        <div style={{ flex: 1, minWidth: "6px" }} />

        {/* ── REMOVE / DEQUEUE BUTTON (✕) ──
            Always anchored at the top-right corner of the card,
            with flexShrink: 0 so it can NEVER be pushed out of bounds */}
        <button
          style={{
            ...SMALL_BTN,
            padding: "2px 8px",
            color: isCurrent ? "var(--text-dim)" : "#ff6b6b",
            borderColor: isCurrent ? "var(--border-color)" : "rgba(239,68,68,0.4)",
            cursor: isCurrent ? "not-allowed" : "pointer",
            opacity: isCurrent ? 0.4 : 1,
            flexShrink: 0,
            fontSize: "12px",
            lineHeight: 1.4,
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            marginLeft: "auto",
          }}
          disabled={isCurrent}
          onClick={() => dequeue(entry.client_id)}
          title={isCurrent ? "Stop the run before removing the client being swept" : "Remove from the queue"}
        >
          ✕
        </button>
      </div>

      {/* ── SUBTITLE / PROGRESS MESSAGE ── */}
      <div style={{ fontSize: "11px", color: "var(--text-dim)", overflowWrap: "anywhere", wordBreak: "break-word", paddingLeft: "26px", lineHeight: 1.4, minWidth: 0, maxWidth: "100%" }}>
        {pendingStop
          ? `${entry.message} — finishing the sweep in flight before stopping`
          : entry.message || `${entry.keywords.length} keyword${entry.keywords.length === 1 ? "" : "s"}`}
        {entry.status === "done" && entry.found > 0 && (
          <span style={{ color: "var(--success)", fontWeight: 600 }}> · {entry.found} found, {entry.new_profiles} new</span>
        )}
      </div>

      {/* ── PLATFORM SWEEP CHIPS & NOTES ── */}
      <div style={{ paddingLeft: "26px", width: "100%", maxWidth: "100%", minWidth: 0, boxSizing: "border-box" }}>
        <PlatformChips entry={entry} />
      </div>

      {/* ── CONFIGURABLE RUN SCOPE ── */}
      {client && (
        <div style={{ marginTop: "4px", paddingLeft: "26px", width: "100%", maxWidth: "100%", minWidth: 0, boxSizing: "border-box" }}>
          <SchedulerRunScope
            client={client}
            platforms={platforms}
            disabled={isCurrent}
          />
        </div>
      )}
    </div>
  );
}
