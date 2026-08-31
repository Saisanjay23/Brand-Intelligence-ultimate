// Consolidates the surviving operational/admin surfaces: Sessions and
// Scheduler. Mail, Proxies and Live Activity were dropped -- their backend
// route groups (/settings/mail, /jobs+/incidents+/clients) no longer exist
// on the rebuilt backend, only /discovery, /analysis, /sessions do.
import { useState } from "react";
import type { SessionInfo } from "../api/types";
import { SchedulerPanel } from "./SchedulerPanel";
import { SessionPanel } from "./SessionPanel";
import { SessionsKeyIcon, SchedulerClockIcon } from "../components/AppIcons";

interface Props {
  sessions: SessionInfo[];
  onChanged: () => void;
}

type AdminTab = "sessions" | "scheduler";

const TABS: { id: AdminTab; label: string; icon: (active: boolean) => React.ReactNode }[] = [
  { id: "sessions", label: "Sessions", icon: (a) => <SessionsKeyIcon size={15} color={a ? "#00F0FF" : "currentColor"} /> },
  { id: "scheduler", label: "Scheduler", icon: (a) => <SchedulerClockIcon size={15} color={a ? "#00F0FF" : "currentColor"} /> },
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
      {tab === "scheduler" && <SchedulerPanel />}
    </div>
  );
}
