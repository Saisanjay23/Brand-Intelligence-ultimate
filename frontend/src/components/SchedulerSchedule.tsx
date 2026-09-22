// The date/time card at the top of the Scheduler: when the queue runs by
// itself.
//
// WHAT THIS IS FOR. An analyst sets a time, walks away, and the queue
// sweeps every client in it one by one at that time. The run happens on the
// SERVER (backend/services/scheduler_service.py), so it does not need this
// page, this browser, or this machine to be awake -- only the tool itself
// to be running.
//
// THE THINGS THIS CARD REFUSES TO BE VAGUE ABOUT, because each one is a way
// for an analyst to believe a sweep is coming when it is not:
//
//   * NOT SCHEDULED is said in those words. A schedule that is off, or a
//     one-time one whose moment has passed, never renders as a countdown to
//     nothing.
//   * THE NEXT RUN IS SHOWN AS A REAL DATE, with the next few after it, so
//     "weekly, Mon + Fri" can be confirmed as actual days before anybody
//     relies on it overnight.
//   * THE TIMEZONE IS NAMED. "02:00" means 02:00 where the analyst is, and
//     the card says which zone that is, because the server may well be
//     somewhere else.
//   * A MISSED RUN IS SHOWN IN RED, not omitted. If the tool was off at
//     02:00 and came back too late to catch up, that is the single most
//     important thing on this page and it gets said outright.
//   * A DAYLIGHT-SAVING SHIFT IS EXPLAINED. Twice a year the time an
//     analyst typed does not exist, or exists twice; the card says what
//     will actually happen instead of quietly running an hour off.
import { useEffect, useMemo, useState } from "react";
import { toast } from "react-hot-toast";

import type { Schedule, ScheduleMode, SchedulerRun } from "../api/schedulerApi";
import { browserTimezone } from "../api/schedulerApi";
import { saveSchedule } from "../services/scheduleRunner";
import { AlertTriangleIcon } from "./AppIcons";

// Monday-first, matching Python's `date.weekday()` (Mon=0 ... Sun=6), which
// is what the API expects. Getting this convention wrong is a silent
// one-day error, so the two ends agree on it explicitly.
const WEEKDAYS = [
  { value: 0, label: "Mon" },
  { value: 1, label: "Tue" },
  { value: 2, label: "Wed" },
  { value: 3, label: "Thu" },
  { value: 4, label: "Fri" },
  { value: 5, label: "Sat" },
  { value: 6, label: "Sun" },
];

const MODES: { value: ScheduleMode; label: string; hint: string }[] = [
  { value: "once", label: "Once", hint: "One run, at a date and time you pick." },
  { value: "daily", label: "Daily", hint: "Every day at this time." },
  { value: "weekly", label: "Weekly", hint: "On the days you tick, at this time." },
];

const CARD: React.CSSProperties = {
  background: "var(--bg-surface)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "12px",
  padding: "14px 18px",
  marginBottom: "16px",
  boxSizing: "border-box",
};

const LABEL: React.CSSProperties = {
  fontSize: "10px",
  color: "var(--text-dim)",
  textTransform: "uppercase",
  letterSpacing: "1px",
  fontWeight: 700,
  marginBottom: "5px",
  display: "block",
};

const FIELD: React.CSSProperties = {
  background: "var(--bg-inner)",
  border: "1px solid var(--border-color)",
  borderRadius: "8px",
  padding: "7px 10px",
  color: "var(--text-main)",
  fontSize: "13px",
  outline: "none",
  boxSizing: "border-box",
  colorScheme: "dark",
};

function chip(active: boolean, disabled = false): React.CSSProperties {
  return {
    padding: "5px 11px",
    borderRadius: "999px",
    border: `1px solid ${active ? "var(--accent, #7c5cff)" : "var(--border-color)"}`,
    background: active ? "rgba(124,92,255,0.16)" : "var(--bg-inner)",
    color: active ? "var(--accent, #7c5cff)" : "var(--text-muted)",
    fontSize: "12px",
    fontWeight: 700,
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.5 : 1,
    lineHeight: 1.5,
  };
}

// "Tue 23 Sep 2026, 02:00" in the analyst's own zone -- the one they typed
// the time in.
function whenLabel(isoString: string): string {
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return isoString;
  return d.toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

// "in 9h 42m" / "12m ago". Plain words, because this is the line an analyst
// reads to decide whether to wait or to press Run.
function relativeLabel(isoString: string, now: number): string {
  const t = Date.parse(isoString);
  if (Number.isNaN(t)) return "";
  const secs = Math.round((t - now) / 1000);
  const ago = secs < 0;
  const a = Math.abs(secs);
  let body: string;
  if (a < 60) body = `${a}s`;
  else if (a < 3600) body = `${Math.floor(a / 60)}m`;
  else if (a < 86400) body = `${Math.floor(a / 3600)}h ${Math.floor((a % 3600) / 60)}m`;
  else body = `${Math.floor(a / 86400)}d ${Math.floor((a % 86400) / 3600)}h`;
  return ago ? `${body} ago` : `in ${body}`;
}

// Today in the browser's own zone, for the date field's floor. Built from
// the local calendar rather than `toISOString()`, which is UTC and would
// forbid today for anyone east of Greenwich after their evening.
function todayLocal(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function SchedulerSchedule({
  schedule,
  nextRunAt,
  upcoming,
  lastFiredAt,
  wallClockShift,
  catchUpGraceMinutes,
  lastRun,
  running,
  disabled,
  stale,
}: {
  schedule: Schedule;
  nextRunAt: string | null;
  upcoming: string[];
  lastFiredAt: string | null;
  wallClockShift: { kind: string; note: string } | null;
  catchUpGraceMinutes: number;
  lastRun: SchedulerRun | null;
  running: boolean;
  disabled: boolean;
  // The backend could not be reached on the last poll, so everything here
  // is a remembered answer rather than a current one.
  stale: boolean;
}) {
  // Local edit buffer. The saved schedule is the source of truth; this is
  // what is being typed, which is not the same thing until Save is pressed.
  const [draft, setDraft] = useState<Schedule>(schedule);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  // Re-sync when the server's copy changes and nothing local is pending,
  // so a schedule saved in another tab shows up here.
  useEffect(() => {
    if (!dirty) setDraft(schedule);
  }, [schedule, dirty]);

  // A one-second tick so "in 9h 42m" counts down on its own.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const tz = draft.tz || browserTimezone();
  const patch = (fields: Partial<Schedule>) => {
    setDraft((d) => ({ ...d, ...fields }));
    setDirty(true);
  };

  const toggleWeekday = (value: number) => {
    const has = draft.weekdays.includes(value);
    patch({
      weekdays: has
        ? draft.weekdays.filter((w) => w !== value)
        : [...draft.weekdays, value].sort((a, b) => a - b),
    });
  };

  // Caught here as well as server-side. The server is the one that refuses
  // to store it; this is so the analyst sees why before pressing Save.
  const localProblem = useMemo(() => {
    if (!draft.enabled) return "";
    if (draft.mode === "weekly" && draft.weekdays.length === 0) {
      return "Pick at least one day — with none selected this could never run.";
    }
    if (draft.mode === "once" && !draft.on_date) {
      return "Pick a date for the one-time run.";
    }
    return "";
  }, [draft]);

  const save = async () => {
    if (localProblem) {
      toast.error(localProblem);
      return;
    }
    setSaving(true);
    try {
      await saveSchedule({ ...draft, tz });
      setDirty(false);
      toast.success(
        draft.enabled ? "Schedule saved — the queue will run by itself."
          : "Automatic runs turned off.");
    } catch (e) {
      // The backend refuses a schedule that could never fire, and says
      // why. Showing that reason is the whole point: "saved" and "will
      // never run" must not look the same.
      toast.error((e as Error).message || "could not save the schedule");
    } finally {
      setSaving(false);
    }
  };

  // The most important thing on the page when it is true.
  const missed = lastRun?.status === "missed" ? lastRun : null;
  const interrupted = lastRun?.status === "interrupted" ? lastRun : null;

  return (
    <div style={CARD}>
      <div style={{ display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap", marginBottom: dirty || draft.enabled ? "12px" : 0 }}>
        <label style={{ display: "inline-flex", alignItems: "center", gap: "8px", cursor: disabled ? "not-allowed" : "pointer" }}>
          <input
            type="checkbox"
            checked={draft.enabled}
            disabled={disabled}
            onChange={(e) => patch({ enabled: e.target.checked })}
            style={{ width: "15px", height: "15px", accentColor: "var(--accent, #7c5cff)", cursor: "inherit" }}
          />
          <span style={{ fontSize: "13px", fontWeight: 700, color: "var(--text-primary, #fff)" }}>
            🕑 Run this queue automatically
          </span>
        </label>

        <div style={{ flex: 1, minWidth: "12px" }} />

        {/* The one-line answer to "is anything going to happen?" */}
        <div style={{ fontSize: "12px", color: "var(--text-muted)", textAlign: "right" }}>
          {running ? (
            <span style={{ color: "var(--accent, #7c5cff)", fontWeight: 700 }}>running now</span>
          ) : nextRunAt ? (
            <>
              <span style={{ color: "var(--text-dim)" }}>Next run </span>
              {/* Greyed while the backend is unreachable. A green "Next
                  run Fri, 02:00" reads as a promise, and at that moment
                  it is a remembered one that nothing is keeping. */}
              <span style={{ color: stale ? "var(--text-dim)" : "var(--success)", fontWeight: 700 }}>
                {whenLabel(nextRunAt)}
              </span>
              <span style={{ color: "var(--text-dim)" }}> · {relativeLabel(nextRunAt, now)}</span>
              {stale && (
                <span style={{ color: "var(--warn-yellow, #fdb71b)", fontWeight: 700 }}>
                  {" "}· last known
                </span>
              )}
            </>
          ) : (
            // NEVER a countdown to nothing. If it is not scheduled, it
            // says so in those words.
            <span style={{ color: "var(--warn-yellow, #fdb71b)", fontWeight: 700 }}>
              Not scheduled
            </span>
          )}
        </div>
      </div>

      {/* A run that was due while the tool was off, and was too late to
          honour. Loud on purpose: nothing swept last night. */}
      {missed && (
        <div style={{
          display: "flex", gap: "8px", alignItems: "flex-start", marginBottom: "12px",
          padding: "9px 12px", background: "rgba(233,80,83,0.10)",
          border: "1px solid rgba(233,80,83,0.3)", borderRadius: "8px",
          color: "var(--danger)", fontSize: "12px", lineHeight: 1.55,
        }}>
          <span style={{ marginTop: "1px", flexShrink: 0 }}>
            <AlertTriangleIcon size={14} color="var(--danger)" />
          </span>
          <span>
            <strong>A scheduled run was missed.</strong> {missed.message}
          </span>
        </div>
      )}

      {/* A run the backend restarted in the middle of. Its clients were
          partly swept; what they found was saved. */}
      {interrupted && (
        <div style={{
          marginBottom: "12px", padding: "9px 12px",
          background: "rgba(253,183,27,0.09)", border: "1px solid rgba(253,183,27,0.3)",
          borderRadius: "8px", color: "var(--warn-yellow, #fdb71b)",
          fontSize: "12px", lineHeight: 1.55,
        }}>
          <strong>The last run was interrupted.</strong> {interrupted.message}. Anything it
          found before then was saved — press Run to sweep whatever it did not reach.
        </div>
      )}

      {draft.enabled && (
        <>
          <div style={{ display: "flex", gap: "16px", flexWrap: "wrap", alignItems: "flex-start" }}>
            {/* repeat */}
            <div>
              <span style={LABEL}>Repeat</span>
              <div style={{ display: "flex", gap: "6px" }}>
                {MODES.map((m) => (
                  <button
                    key={m.value}
                    title={m.hint}
                    disabled={disabled}
                    onClick={() => patch({ mode: m.value })}
                    style={chip(draft.mode === m.value, disabled)}
                  >
                    {m.label}
                  </button>
                ))}
              </div>
            </div>

            {/* date -- one-time runs only */}
            {draft.mode === "once" && (
              <div>
                <span style={LABEL}>Date</span>
                <input
                  type="date"
                  value={draft.on_date}
                  min={todayLocal()}
                  disabled={disabled}
                  onChange={(e) => patch({ on_date: e.target.value })}
                  style={{ ...FIELD, width: "160px" }}
                />
              </div>
            )}

            {/* time -- always */}
            <div>
              <span style={LABEL}>Time</span>
              <input
                type="time"
                value={draft.at}
                disabled={disabled}
                onChange={(e) => patch({ at: e.target.value })}
                style={{ ...FIELD, width: "120px" }}
              />
            </div>

            {/* days -- weekly only */}
            {draft.mode === "weekly" && (
              <div>
                <span style={LABEL}>Days</span>
                <div style={{ display: "flex", gap: "5px", flexWrap: "wrap" }}>
                  {WEEKDAYS.map((d) => (
                    <button
                      key={d.value}
                      disabled={disabled}
                      onClick={() => toggleWeekday(d.value)}
                      style={{ ...chip(draft.weekdays.includes(d.value), disabled), padding: "5px 9px" }}
                    >
                      {d.label}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div style={{ flex: 1, minWidth: "8px" }} />

            <div style={{ display: "flex", alignItems: "flex-end", gap: "8px", paddingTop: "14px" }}>
              <button
                onClick={() => void save()}
                disabled={disabled || saving || !dirty}
                style={{
                  padding: "8px 16px", borderRadius: "9px",
                  background: dirty ? "rgba(124,92,255,0.14)" : "var(--bg-inner)",
                  border: `1px solid ${dirty ? "rgba(124,92,255,0.45)" : "var(--border-color)"}`,
                  color: dirty ? "var(--accent, #7c5cff)" : "var(--text-dim)",
                  fontSize: "12.5px", fontWeight: 700,
                  cursor: disabled || saving || !dirty ? "not-allowed" : "pointer",
                  whiteSpace: "nowrap",
                }}
                title={dirty ? "Save this schedule" : "No unsaved changes"}
              >
                {saving ? "Saving…" : dirty ? "Save schedule" : "Saved"}
              </button>
            </div>
          </div>

          {/* The timezone, said out loud. The server may be elsewhere. */}
          <div style={{ fontSize: "11.5px", color: "var(--text-dim)", marginTop: "10px", lineHeight: 1.6 }}>
            Times are <strong style={{ color: "var(--text-muted)" }}>{tz}</strong> — the zone this
            browser is in. The run happens on the server, so it does not need this page open; it
            needs the tool itself to be running.
          </div>

          {localProblem && (
            <div style={{ fontSize: "12px", color: "var(--warn-yellow, #fdb71b)", marginTop: "6px", fontWeight: 600 }}>
              {localProblem}
            </div>
          )}

          {/* Twice a year the time typed does not exist, or exists twice. */}
          {wallClockShift && (
            <div style={{
              fontSize: "11.5px", color: "var(--warn-yellow, #fdb71b)",
              marginTop: "8px", lineHeight: 1.6,
            }}>
              ⏱ Daylight saving: {wallClockShift.note}.
            </div>
          )}

          {/* Real dates, so "weekly, Mon + Fri" can be checked before
              anybody relies on it overnight. */}
          {!dirty && upcoming.length > 1 && (
            <div style={{ fontSize: "11.5px", color: "var(--text-dim)", marginTop: "8px", lineHeight: 1.7 }}>
              Then: {upcoming.slice(1, 4).map(whenLabel).join(" · ")}
            </div>
          )}

          {dirty && (
            <div style={{ fontSize: "11.5px", color: "var(--warn-yellow, #fdb71b)", marginTop: "8px", fontWeight: 600 }}>
              Unsaved — press Save schedule, or nothing will change.
            </div>
          )}

          {catchUpGraceMinutes > 0 && (
            <div style={{ fontSize: "11px", color: "var(--text-dim)", marginTop: "8px", lineHeight: 1.6 }}>
              If the tool is off at that time, the run still happens when it comes back, as long as
              that is within {Math.round(catchUpGraceMinutes / 60)} hours. Later than that it is
              recorded as missed and shown here, rather than run at an unexpected hour.
            </div>
          )}

          {lastFiredAt && (
            <div style={{ fontSize: "11px", color: "var(--text-dim)", marginTop: "4px" }}>
              Last fired {whenLabel(lastFiredAt)} · {relativeLabel(lastFiredAt, now)}
            </div>
          )}
        </>
      )}
    </div>
  );
}
