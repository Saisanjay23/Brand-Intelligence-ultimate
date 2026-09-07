import { useState } from "react";
import type { SessionInfo } from "../api/types";
import { SchedulerPanel } from "./SchedulerPanel";
import { SessionPanel } from "./SessionPanel";
import { AlertsIncidentsPanel } from "./AlertsIncidentsPanel";
import { SessionsKeyIcon, SchedulerClockIcon, AlertBellIcon } from "../components/AppIcons";

interface Props {
  sessions: SessionInfo[];
  onChanged: () => void;
}

type AdminTab = "sessions" | "alerts" | "scheduler";

const TABS: { id: AdminTab; label: string; icon: (active: boolean) => React.ReactNode }[] = [
  { id: "sessions", label: "Sessions", icon: (a) => <SessionsKeyIcon size={15} color={a ? "#8838DD" : "currentColor"} /> },
  { id: "alerts", label: "Alerts & Incidents", icon: (a) => <AlertBellIcon size={15} color={a ? "#8838DD" : "currentColor"} /> },
  { id: "scheduler", label: "Scheduler", icon: (a) => <SchedulerClockIcon size={15} color={a ? "#8838DD" : "currentColor"} /> },
];

export function AdminPanel({ sessions, onChanged }: Props) {
  const [tab, setTab] = useState<AdminTab>("sessions");

  return (
    <div style={{ animation: "fadeUp 0.4s ease" }}>
      <div className="mode-tab-row" style={{ marginBottom: "20px" }}>
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`mode-tab-btn ${tab === t.id ? "active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            <span>{t.icon(tab === t.id)}</span>
            <span>{t.label}</span>
          </button>
        ))}
      </div>

      {tab === "sessions" && <SessionPanel sessions={sessions} onChanged={onChanged} />}
      {tab === "alerts" && <AlertsIncidentsPanel />}
      {tab === "scheduler" && <SchedulerPanel />}
    </div>
  );
}
