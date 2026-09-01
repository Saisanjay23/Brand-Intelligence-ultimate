// Consolidates the surviving operational/admin surfaces: Sessions, Proxies
// and Scheduler. Mail and Live Activity were dropped -- their backend route
// groups (/settings/mail, /jobs+/incidents) no longer exist on the rebuilt
// backend, only /discovery, /analysis, /sessions do.
//
// Proxies was previously listed here as dropped too, which was wrong:
// ProxyPanel.tsx was fully written and its backend (PUT
// /sessions/{platform}/{id}/proxy) has existed all along -- the component
// was simply never imported by anything, so 442 lines of working proxy UI
// were unreachable from the app. It is a tab again.
import { useState } from "react";
import type { SessionInfo } from "../api/types";
import { SchedulerPanel } from "./SchedulerPanel";
import { SessionPanel } from "./SessionPanel";
import { ProxyPanel } from "./ProxyPanel";
import { SessionsKeyIcon, SchedulerClockIcon, GlobeIcon } from "../components/AppIcons";

interface Props {
  sessions: SessionInfo[];
  onChanged: () => void;
}

type AdminTab = "sessions" | "proxies" | "scheduler";

const TABS: { id: AdminTab; label: string; icon: (active: boolean) => React.ReactNode }[] = [
  { id: "sessions", label: "Sessions", icon: (a) => <SessionsKeyIcon size={15} color={a ? "#00F0FF" : "currentColor"} /> },
  { id: "proxies", label: "Proxies", icon: (a) => <GlobeIcon size={15} color={a ? "#00F0FF" : "currentColor"} /> },
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
      {tab === "proxies" && <ProxyPanel sessions={sessions} onChanged={onChanged} />}
      {tab === "scheduler" && <SchedulerPanel />}
    </div>
  );
}
