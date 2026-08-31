import { useState } from "react";
import type { Client } from "../api/types";
import type { RecentClient } from "../services/recentClients";
import {
  BrandLogoIcon,
  ClientsNavIcon,
  LiveResultsNavIcon,
  AdminNavIcon,
  BellAlertIcon,
  SearchIcon,
  SunIcon,
  MoonIcon,
} from "./AppIcons";

// "analysis" was a standalone top-nav page; removed -- the same
// paste-URLs-and-scrape tool is reachable from Live Results' own
// Discovery/Analysis toggle (see LiveResultsView.tsx), so it was a
// redundant second entry point to the same feature.
export type ViewPage = "home" | "results" | "admin";

interface Props {
  page: ViewPage;
  onPage: (page: ViewPage) => void;
  clientId: string;
  clientName: string;
  recentClients: RecentClient[];
  allClients?: Client[];
  onClient: (clientId: string, name: string) => void;
  onForgetClient: (clientId: string) => void;
  activeJobsCount: number;
  readySessionsCount: number;
  platformCount: number;
  liveResultsCount?: number;
  theme: "light" | "dark";
  onThemeToggle: () => void;
}

export function Header({
  page,
  onPage,
  clientId,
  clientName,
  recentClients,
  allClients = [],
  onClient,
  onForgetClient,
  activeJobsCount,
  readySessionsCount,
  platformCount,
  liveResultsCount = 0,
  theme,
  onThemeToggle,
}: Props) {
  const [openDropdown, setOpenDropdown] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");

  const label = clientName || clientId;
  const initial = label ? label.charAt(0).toUpperCase() : "C";

  return (
    <header className="top-header">
      <div className="brand-logo-area">
        <div style={{ display: "flex", alignItems: "center", gap: "10px", cursor: "pointer" }} onClick={() => onPage("home")}>
          <BrandLogoIcon size={30} />
          <div>
            <div className="brand-logo-title">Brand Intelligence</div>
            <div className="brand-logo-sub">
              Social Intelligence Suite
            </div>
          </div>
        </div>
      </div>

      <div className="nav-links-row">
        <button
          onClick={() => onPage("home")}
          className={`top-nav-btn ${page === "home" ? "active" : ""}`}
        >
          <ClientsNavIcon size={16} />
          <span>Clients</span>
        </button>

        <button
          onClick={() => onPage("results")}
          className={`top-nav-btn ${page === "results" ? "active" : ""}`}
        >
          <LiveResultsNavIcon size={16} />
          <span>Live Results</span>
          {liveResultsCount > 0 && <span className="top-nav-badge">{liveResultsCount}</span>}
        </button>

        <button
          onClick={() => onPage("admin")}
          className={`top-nav-btn ${page === "admin" ? "active" : ""}`}
        >
          <AdminNavIcon size={16} />
          <span>Admin</span>
          <span className="top-nav-badge">
            {readySessionsCount}/{platformCount}
          </span>
        </button>
      </div>

      <div className="header-right-actions">
        <div
          className="bell-btn"
          onClick={onThemeToggle}
          title={`Switch to ${theme === "dark" ? "Light" : "Dark"} Mode`}
        >
          {theme === "dark" ? <SunIcon size={16} /> : <MoonIcon size={16} />}
        </div>

        <div
          className="bell-btn"
          onClick={() => onPage("admin")}
          title={`${activeJobsCount} active discovery sweep(s)`}
        >
          <BellAlertIcon size={16} />
          {activeJobsCount > 0 && (
            <span className="bell-badge">{activeJobsCount}</span>
          )}
        </div>

        <div
          className="client-selector-chip"
          onClick={() => setOpenDropdown(!openDropdown)}
        >
          <span className="client-avatar-sm">{initial}</span>
          <span style={{ fontSize: "13px", fontWeight: 600 }}>
            {label || "Set Client"}
          </span>
          <span style={{ fontSize: "9px", color: "var(--text-dim)" }}>▼</span>
        </div>

        {openDropdown && (() => {
          // Combine allClients and recentClients without duplicates
          const clientMap = new Map<string, { client_id: string; name: string }>();
          for (const c of allClients) {
            clientMap.set(c.client_id, { client_id: c.client_id, name: c.name || c.client_id });
          }
          for (const c of recentClients) {
            if (!clientMap.has(c.client_id)) {
              clientMap.set(c.client_id, { client_id: c.client_id, name: c.name || c.client_id });
            }
          }
          const mergedClients = Array.from(clientMap.values());
          const filtered = mergedClients.filter((c) => {
            if (!searchQuery.trim()) return true;
            const q = searchQuery.toLowerCase();
            return c.name.toLowerCase().includes(q) || c.client_id.toLowerCase().includes(q);
          });

          return (
            <div className="client-dropdown" style={{ width: "320px" }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  fontSize: "11px",
                  color: "var(--text-dim)",
                  textTransform: "uppercase",
                  letterSpacing: "1px",
                  fontWeight: 700,
                  padding: "2px 4px 8px",
                }}
              >
                <span>Saved Clients ({mergedClients.length})</span>
              </div>

              {/* Search Bar */}
              {mergedClients.length > 3 && (
                <div style={{ marginBottom: "8px", position: "relative", display: "flex", alignItems: "center" }}>
                  <SearchIcon size={12} color="var(--text-muted, #98a2b3)" style={{ position: "absolute", left: "8px", pointerEvents: "none" }} />
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder="Search clients…"
                    style={{
                      width: "100%",
                      padding: "6px 10px 6px 26px",
                      fontSize: "12px",
                      background: "var(--bg-surface-3, #1D2939)",
                      border: "1px solid var(--border-color, #344054)",
                      borderRadius: "6px",
                      color: "var(--text-main, #fff)",
                      outline: "none",
                      boxSizing: "border-box",
                    }}
                    autoFocus
                  />
                </div>
              )}

              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: "3px",
                  maxHeight: "220px",
                  overflowY: "auto",
                }}
              >
                {!filtered.length && (
                  <div
                    style={{
                      padding: "12px 8px",
                      fontSize: "12px",
                      color: "var(--text-muted)",
                      textAlign: "center",
                    }}
                  >
                    {searchQuery ? `No clients match "${searchQuery}"` : "No saved clients yet."}
                  </div>
                )}
                {filtered.map((c) => (
                  <button
                    key={c.client_id}
                    onClick={() => {
                      onClient(c.client_id, c.name);
                      setOpenDropdown(false);
                      setSearchQuery("");
                    }}
                    className={`client-list-item ${c.client_id === clientId ? "selected" : ""}`}
                  >
                    <span
                      style={{
                        width: "22px",
                        height: "22px",
                        borderRadius: "50%",
                        background: c.client_id === clientId
                          ? "linear-gradient(135deg, var(--cyan, #8838DD), var(--purple, #7727CD))"
                          : "var(--bg-hover)",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        fontSize: "11px",
                        fontWeight: 700,
                        flexShrink: 0,
                      }}
                    >
                      {(c.name || c.client_id).charAt(0).toUpperCase()}
                    </span>
                    <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      <strong>{c.name || c.client_id}</strong>
                      {c.name && (
                        <span style={{ color: "var(--text-dim)", fontSize: "11px", marginLeft: "4px" }}>
                          ({c.client_id})
                        </span>
                      )}
                    </span>
                    {c.client_id === clientId && (
                      <span style={{ color: "var(--cyan)", fontWeight: 700 }}>✓</span>
                    )}
                  </button>
                ))}
              </div>

              <div
                style={{
                  fontSize: "11px",
                  color: "var(--text-dim)",
                  marginTop: "10px",
                  paddingTop: "10px",
                  borderTop: "1px solid var(--border-subtle)",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <span>Switch or create clients anytime</span>
                {clientId && (
                  <button
                    type="button"
                    style={{
                      background: "transparent",
                      border: "none",
                      color: "var(--danger, #E95053)",
                      fontSize: "11px",
                      cursor: "pointer",
                      padding: "2px 4px",
                    }}
                    onClick={(e) => {
                      e.stopPropagation();
                      onForgetClient(clientId);
                    }}
                    title="Clear current active client"
                  >
                    Clear Active
                  </button>
                )}
              </div>
            </div>
          );
        })()}
      </div>
    </header>
  );
}
