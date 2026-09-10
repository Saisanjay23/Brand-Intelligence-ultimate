// What the Scheduler should run for one client: which platforms, keyword
// scopes, Facebook tabs, and sweep time budget.
//
// Matches the unified discovery runner aesthetics in HomeView.
// Choices update synchronously in memory in real time via patchClientLocal,
// and persist to MongoDB via PUT /clients/{id}/scheduler-prefs.

import { useEffect, useRef, useState } from "react";
import toast from "react-hot-toast";
import { clientsApi } from "../api/clientsApi";
import type { Client, PlatformState } from "../api/types";
import { patchClientLocal, refresh as refreshDirectory } from "../services/clientDirectory";
import { PlatformIcon } from "./PlatformIcon";
import {
  CyberGlobeIcon,
  LayersIcon,
  UserIcon,
  TagIcon,
  ClockIcon,
} from "./AppIcons";

type FacebookTab = "people" | "pages" | "groups";

const ALL_FB_TABS: FacebookTab[] = ["people", "pages", "groups"];

const FB_TAB_CHIPS: { id: FacebookTab; label: string; icon: string }[] = [
  { id: "people", label: "People", icon: "👤" },
  { id: "pages", label: "Pages", icon: "📄" },
  { id: "groups", label: "Groups", icon: "👥" },
];

const KEYWORD_SCOPES: { id: "individual" | "domain"; label: string; hint: string }[] = [
  { id: "individual", label: "Individual Names", hint: "Executive / person names only" },
  { id: "domain", label: "Domain Keywords", hint: "Brand / product keywords only" },
];

interface Props {
  client: Client;
  platforms: PlatformState[];
  disabled?: boolean;
}

export function SchedulerRunScope({ client, platforms, disabled }: Props) {
  const [busy, setBusy] = useState(false);

  const savedPlatforms = client.scheduler_platforms || [];
  const savedScope = client.scheduler_keyword_scope || "";
  const savedFbTabs = client.scheduler_facebook_tabs || [];
  const savedBudget = client.scheduler_budget_minutes || 0;

  // Local intent ahead of server response
  const intended = useRef<{
    platforms: string[];
    scope: string;
    facebook_tabs: string[];
    budget_minutes: number;
  } | null>(null);

  const [, forceRender] = useState(0);
  const timer = useRef<number | null>(null);

  // Sync back when server caught up or client prop changed
  useEffect(() => {
    const i = intended.current;
    if (!i) return;
    const samePlatforms =
      i.platforms.length === savedPlatforms.length &&
      i.platforms.every((p) => savedPlatforms.includes(p));
    const sameFbTabs =
      i.facebook_tabs.length === savedFbTabs.length &&
      i.facebook_tabs.every((t) => savedFbTabs.includes(t));
    if (
      samePlatforms &&
      sameFbTabs &&
      i.scope === savedScope &&
      i.budget_minutes === savedBudget
    ) {
      intended.current = null;
      forceRender((n) => n + 1);
    }
  }, [savedPlatforms, savedScope, savedFbTabs, savedBudget]);

  const current = () =>
    intended.current ?? {
      platforms: [...savedPlatforms],
      scope: savedScope,
      facebook_tabs: [...savedFbTabs],
      budget_minutes: savedBudget,
    };

  const selectedPlatforms = new Set(current().platforms);
  const scope = current().scope;
  const selectedFbTabs = new Set(current().facebook_tabs as FacebookTab[]);
  const budgetMinutes = current().budget_minutes;

  const activeIndividualCount = client.name_keywords?.length || 0;
  const activeDomainCount = client.domain_keywords?.length || 0;
  const activeKeywordCount = activeIndividualCount + activeDomainCount;

  // Apply synchronously in-memory + schedule debounced persistence to server
  const apply = (next: {
    platforms?: string[];
    scope?: string;
    facebook_tabs?: string[];
    budget_minutes?: number;
  }) => {
    const now = current();
    const updated = {
      platforms: next.platforms !== undefined ? next.platforms : now.platforms,
      scope: next.scope !== undefined ? next.scope : now.scope,
      facebook_tabs: next.facebook_tabs !== undefined ? next.facebook_tabs : now.facebook_tabs,
      budget_minutes: next.budget_minutes !== undefined ? next.budget_minutes : now.budget_minutes,
    };
    intended.current = updated;

    // REALTIME: immediately patch local client store so any consumer sees updated config
    patchClientLocal(client.client_id, {
      scheduler_platforms: updated.platforms,
      scheduler_keyword_scope: updated.scope,
      scheduler_facebook_tabs: updated.facebook_tabs,
      scheduler_budget_minutes: updated.budget_minutes,
    });
    forceRender((n) => n + 1);

    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      timer.current = null;
      const send = intended.current;
      if (!send) return;
      setBusy(true);
      clientsApi
        .setSchedulerPrefs(client.client_id, {
          platforms: send.platforms,
          keyword_scope: send.scope,
          facebook_tabs: send.facebook_tabs,
          budget_minutes: send.budget_minutes,
        })
        .then(() => refreshDirectory())
        .catch((e) => {
          intended.current = null;
          forceRender((n) => n + 1);
          toast.error((e as Error).message || "Could not save the run scope");
        })
        .finally(() => setBusy(false));
    }, 300);
  };

  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);

  // Platform selection toggling (exact selected only)
  const togglePlatform = (id: string) => {
    if (disabled) return;
    const next = new Set(selectedPlatforms);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    // If all enabled platforms are selected, collapse to empty (all)
    const allPlatformsCount = platforms.length;
    apply({
      platforms: next.size === allPlatformsCount || next.size === 0 ? [] : [...next],
    });
  };

  const selectAllPlatforms = () => {
    if (disabled) return;
    apply({ platforms: [] });
  };

  // Keyword scope toggling
  const toggleKeywordType = (id: "individual" | "domain") => {
    if (disabled) return;
    if (scope === id) {
      apply({ scope: "" }); // toggle off back to All Keywords
    } else {
      apply({ scope: id }); // exact selected only
    }
  };

  const selectAllKeywordTypes = () => {
    if (disabled) return;
    apply({ scope: "" });
  };

  // Facebook tabs toggling
  const toggleFbTab = (id: FacebookTab) => {
    if (disabled) return;
    const next = new Set(selectedFbTabs);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    // If all three tabs are selected or none, collapse to empty (all FB tabs)
    apply({
      facebook_tabs: next.size === ALL_FB_TABS.length || next.size === 0 ? [] : [...next],
    });
  };

  const selectAllFbTabs = () => {
    if (disabled) return;
    apply({ facebook_tabs: [] });
  };

  const facebookInScope =
    selectedPlatforms.size === 0 || selectedPlatforms.has("facebook");

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "8px",
        marginTop: "6px",
        opacity: busy ? 0.75 : 1,
        transition: "opacity 0.15s ease",
      }}
    >
      {/* ROW 1: PLATFORMS */}
      <div className="unified-platform-selector">
        <button
          type="button"
          disabled={disabled}
          className={`unified-platform-btn ${selectedPlatforms.size === 0 ? "active" : ""}`}
          onClick={selectAllPlatforms}
          title="Run on every platform with a ready session"
        >
          <CyberGlobeIcon
            size={14}
            color={selectedPlatforms.size === 0 ? "#7C5CFF" : "#94A3B8"}
          />
          <span>All Platforms</span>
        </button>
        {platforms.map((p) => {
          const isSelected = selectedPlatforms.has(p.platform);
          const dotClass =
            p.session_state === "ready"
              ? "ready"
              : p.session_state === "incomplete"
              ? "warn"
              : "error";
          return (
            <button
              key={p.platform}
              type="button"
              disabled={disabled}
              className={`unified-platform-btn ${isSelected ? "active" : ""}`}
              onClick={() => togglePlatform(p.platform)}
              title={`${isSelected ? "Click to remove" : "Click to add"} ${p.name} (Session: ${p.session_state})`}
            >
              <PlatformIcon platform={p.platform} size={14} />
              <span>{p.name}</span>
              <span className={`runner-session-dot ${dotClass}`} />
            </button>
          );
        })}
      </div>

      {/* ROW 2: KEYWORDS */}
      <div className="unified-platform-selector">
        <button
          type="button"
          disabled={disabled}
          className={`unified-platform-btn ${scope === "" ? "active" : ""}`}
          onClick={selectAllKeywordTypes}
          title="Search both individual names and domain keywords"
        >
          <LayersIcon
            size={14}
            color={scope === "" ? "#7C5CFF" : "#94A3B8"}
          />
          <span>All Keywords</span>
          <span className="kw-tab-count">{activeKeywordCount}</span>
        </button>

        {KEYWORD_SCOPES.map((opt) => {
          const count =
            opt.id === "individual" ? activeIndividualCount : activeDomainCount;
          const on = scope === opt.id;
          return (
            <button
              key={opt.id}
              type="button"
              disabled={disabled || count === 0}
              className={`unified-platform-btn ${on ? "active" : ""}`}
              onClick={() => toggleKeywordType(opt.id)}
              title={
                count === 0
                  ? `This client has no ${opt.label.toLowerCase()} configured`
                  : `${on ? "Click to remove" : "Click to select"} ${opt.label}`
              }
            >
              {opt.id === "individual" ? (
                <UserIcon size={14} color={on ? "#7C5CFF" : "#94A3B8"} />
              ) : (
                <TagIcon size={14} color={on ? "#7C5CFF" : "#94A3B8"} />
              )}
              <span>{opt.label}</span>
              <span className="kw-tab-count">{count}</span>
            </button>
          );
        })}
      </div>

      {/* ROW 3: FACEBOOK TABS (Shown only when Facebook is in scope) */}
      {facebookInScope && (
        <div className="unified-platform-selector">
          <button
            type="button"
            disabled={disabled}
            className={`unified-platform-btn ${selectedFbTabs.size === 0 ? "active" : ""}`}
            onClick={selectAllFbTabs}
            title="Sweep all three Facebook result tabs"
          >
            <PlatformIcon platform="facebook" size={14} />
            <span>All FB Tabs</span>
          </button>
          {FB_TAB_CHIPS.map((opt) => {
            const on = selectedFbTabs.has(opt.id);
            return (
              <button
                key={opt.id}
                type="button"
                disabled={disabled}
                className={`unified-platform-btn ${on ? "active" : ""}`}
                onClick={() => toggleFbTab(opt.id)}
                title={`${on ? "Click to remove" : "Click to add"} the Facebook ${opt.label} tab`}
              >
                <span aria-hidden style={{ fontSize: "12px" }}>{opt.icon}</span>
                <span>{opt.label}</span>
              </button>
            );
          })}
        </div>
      )}

      {/* ROW 4: SWEEP TIME BUDGET */}
      <div style={{ display: "flex", alignItems: "center", gap: "8px", marginTop: "2px" }}>
        <label
          style={{
            fontSize: "12px",
            color: "var(--text-dim)",
            display: "flex",
            alignItems: "center",
            gap: "6px",
          }}
          title="Per-sweep safety cap -- only matters for a keyword still finding new results when time runs out. One that already finished stops immediately regardless of this."
        >
          <ClockIcon size={13} color="var(--text-dim)" />
          Sweep time budget (minutes)
        </label>
        <input
          type="number"
          min={1}
          step={1}
          disabled={disabled}
          placeholder="15 (default)"
          value={budgetMinutes > 0 ? budgetMinutes : ""}
          onChange={(e) => {
            const val = e.target.value.trim();
            const num = val === "" ? 0 : parseInt(val, 10);
            apply({ budget_minutes: isNaN(num) ? 0 : Math.max(0, num) });
          }}
          style={{
            width: "105px",
            padding: "5px 9px",
            fontSize: "12px",
            background: "var(--bg-input, rgba(255,255,255,0.04))",
            border: "1px solid var(--border-color, rgba(255,255,255,0.1))",
            borderRadius: "6px",
            color: "var(--text-main)",
            outline: "none",
          }}
        />
      </div>
    </div>
  );
}
