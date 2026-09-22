// The Scheduler backend (backend/api/scheduler.py): when the client queue
// runs, who is in it, and how every run went.
//
// THE QUEUE AND THE RUN LOOP ARE THE SERVER'S NOW. They used to be this
// tab's localStorage and a `setTimeout` in this page, which meant a run
// scheduled for two in the morning happened only if somebody left a
// browser open -- and if they did not, nothing ran and nothing said so.
// This module is the client for the engine that replaced it.
//
// TIMES ON THE WIRE ARE UTC ISO STRINGS. The SCHEDULE is not: it is a wall
// clock (`at`) plus an IANA zone (`tz`), so "02:30" keeps meaning 02:30
// where the analyst sits even after the clocks change. Sending a UTC
// instant instead would drift the sweep an hour twice a year with nothing
// to explain it.
import { json, post, url } from "./httpClient";

export type ScheduleMode = "once" | "daily" | "weekly";

export interface Schedule {
  enabled: boolean;
  mode: ScheduleMode;
  // "HH:MM", 24-hour, local to `tz`.
  at: string;
  // "YYYY-MM-DD". Mode "once" only; ignored (not rejected) otherwise, so
  // switching mode in the form does not require clearing the other's field.
  on_date: string;
  // Monday=0 ... Sunday=6, matching Python's `date.weekday()`. Mode
  // "weekly" only.
  weekdays: number[];
  // IANA name. The browser's own zone is what this UI sends.
  tz: string;
}

// Exactly the per-platform sweep states the discovery job reports, carried
// through unchanged so the coverage view shows what the sweep itself said
// rather than a re-derivation of it.
export type PlatformOutcome =
  | "pending" | "running" | "done" | "partial" | "failed" | "skipped";

export interface PlatformDetail {
  found: number;
  new: number;
  note: string;
}

export type EntryStatus =
  | "pending" | "running" | "done" | "failed" | "skipped"
  | "cancelled" | "stopped" | "interrupted";

export interface RunEntry {
  client_id: string;
  name: string;
  status: EntryStatus;
  job_id: string;
  message: string;
  found: number;
  new_profiles: number;
  platforms: Record<string, PlatformOutcome>;
  platform_details: Record<string, PlatformDetail>;
  // Searches this client still owes on the durable coverage ledger after
  // its sweep settled.
  //
  // -1 MEANS NOT KNOWN, and is deliberately not 0: zero is the clean bill
  // of health, and a coverage read that failed must never be able to
  // impersonate one. That is the false all-clear that would be hardest of
  // all to notice, because it looks exactly like success.
  owed: number;
  closing_gaps: boolean;
  started_at: string | null;
  finished_at: string | null;
}

export type RunStatus =
  | "running" | "done" | "cancelled" | "failed" | "interrupted" | "missed";

export interface SchedulerRun {
  run_id: string;
  // How this run began. `missed` runs never began at all -- see below.
  trigger: "scheduled" | "catch_up" | "manual";
  status: RunStatus;
  message: string;
  entries: RunEntry[];
  current_id: string;
  stopping: boolean;
  started_at: string | null;
  finished_at: string | null;
  // What time this run was MEANT to fire, for a scheduled or catch-up run.
  due_at: string | null;
  late_seconds: number;
}

export interface WallClockShift {
  kind: "gap" | "overlap";
  requested: string;
  actual: string;
  on: string;
  note: string;
}

export interface SchedulerState {
  schedule: Schedule;
  // Ordered client ids. The order IS the sweep order.
  queue: string[];
  queue_names: Record<string, string>;
  running: boolean;
  // When the schedule next fires, UTC ISO.
  //
  // NULL MEANS NEVER -- the schedule is off, or it is a one-time schedule
  // whose moment has passed. Render that as "not scheduled". The reading
  // that must not happen is "not yet", which leaves an analyst waiting all
  // night for a run that was never coming.
  next_run_at: string | null;
  // The next few firing times, so a schedule can be confirmed as real
  // dates before anybody relies on it overnight.
  upcoming: string[];
  last_fired_at: string | null;
  // Set when the next run's local time does not exist, or exists twice,
  // because of a daylight-saving change.
  wall_clock_shift: WallClockShift | null;
  catch_up_grace_minutes: number;
  // The run in flight, or the most recent finished one when idle.
  run: SchedulerRun | null;
}

const jsonInit = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// The browser's own IANA zone, defaulting to India IST (Asia/Kolkata).
export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Kolkata";
  } catch {
    return "Asia/Kolkata";
  }
}

export const schedulerApi = {
  // One read for the whole page. Safe to poll.
  getState: () => fetch(url("/scheduler")).then(json<SchedulerState>),

  // Smart timezone detection based on VPN or IP egress location
  detectTimezone: () =>
    fetch(url("/scheduler/detect-timezone")).then(
      json<{ timezone: string; ip?: string; city?: string; country?: string; source: string }>
    ),

  // 422 on a schedule that could never fire -- a weekly with no days, a
  // one-time date already past, a bad timezone. The thrown Error carries
  // the backend's own reason; show it rather than swallowing it, because
  // "saved" and "will never run" must not look the same.
  setSchedule: (body: Schedule) =>
    fetch(url("/scheduler/schedule"), jsonInit("PUT", body)).then(json<SchedulerState>),

  // What a schedule WOULD do, without saving it -- for confirming
  // "weekly, Mon + Fri" as real dates while the analyst is still typing.
  preview: (body: Schedule) =>
    post("/scheduler/preview", body).then(
      json<{ upcoming: string[]; wall_clock_shift: WallClockShift | null; note: string }>),

  // The FULL list, in order, every time. 409 while a run is in flight.
  setQueue: (clientIds: string[]) =>
    fetch(url("/scheduler/queue"), jsonInit("PUT", { client_ids: clientIds }))
      .then(json<SchedulerState>),

  // The same code path a scheduled fire takes, labelled `manual`. 409 when
  // a run is already going -- declined rather than queued behind it.
  runNow: () => post("/scheduler/run", {}).then(json<{ run_id: string; message: string }>),

  stop: () => post("/scheduler/stop", {}).then(json<{ stopping: boolean; message: string }>),

  listRuns: (limit = 25) =>
    fetch(url(`/scheduler/runs?limit=${limit}`)).then(json<{ items: SchedulerRun[] }>),
};
