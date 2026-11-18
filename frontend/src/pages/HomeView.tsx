import { useCallback, useEffect, useMemo, useRef, useState, Fragment } from "react";
import { toast } from "react-hot-toast";

import { discoveryApi } from "../api/discoveryApi";
import type { Client, ClientConfig, KeywordGroup, PlatformState } from "../api/types";
import {
  cleanKeyword,
  mergeGeneratedChildren,
  parseBulkKeywordGroups,
  mergeBulkKeywordGroups,
} from "../services/keywordGroups";
import {
  parseCapMap,
  parseSweepSeconds,
  parseTabCaps,
  sameCaps,
  sameKeywordGroups,
} from "../services/scrapeLimits";
import { PlatformIcon } from "../components/PlatformIcon";
import { KeywordLogoCell } from "../components/KeywordLogoCell";
import { logosApi, type ClientLogo } from "../api/logosApi";
import { confirmAction } from "../utils/confirmAction";
import {
  createClient as createDirectoryClient,
  deleteClient as deleteDirectoryClient,
  findClient,
  refresh as refreshDirectory,
  updateClient as updateDirectoryClient,
  useClientDirectory,
} from "../services/clientDirectory";
import {
  clearDraft,
  clearDraftTexts,
  editKey,
  isBlankDraft,
  loadDraft,
  loadUi,
  saveDraft,
  saveUi,
  useDraftText,
  type ClientFormDraft,
} from "../services/clientDraft";
import {
  DiscoverIcon,
  AnalyseIcon,
  CyberGlobeIcon,
  GlobeIcon,
  StopIcon,
  TargetIcon,
  UserIcon,
  TagIcon,
  SaveIcon,
  SparklesIcon,
  BuildingIcon,
  SearchIcon,
  PlusIcon,
  TrashIcon,
  EditIcon,
  CloneIcon,
  ZapIcon,
  SettingsGearIcon,
  AlertTriangleIcon,
  LayersIcon,
  ClockIcon,
} from "../components/AppIcons";

type KeywordTab = "names" | "domain";
type Mode = "select" | "create";
type WorkspaceTab = "overview" | "keywords" | "limits" | "settings";

type FacebookTab = "people" | "pages" | "groups";
const ALL_FB_TABS: FacebookTab[] = ["people", "pages", "groups"];

// The Run page's Facebook tab chips. Ordered as Facebook's own results are,
// which -- with keywords sweeping one at a time -- is also the order results
// come back in.
const FB_TAB_CHIPS: { id: FacebookTab; label: string; icon: string }[] = [
  { id: "people", label: "People", icon: "\u{1F464}" },
  { id: "pages", label: "Pages", icon: "\u{1F4C4}" },
  { id: "groups", label: "Groups", icon: "\u{1F465}" },
];
type FacebookTabLimits = Record<FacebookTab, { individual: string; domain: string }>;

const WORKSPACE_TABS: WorkspaceTab[] = ["overview", "keywords", "limits", "settings"];

// A stored draft's Facebook boxes -> all three tabs present, blanks filled.
function fbTabLimitsOf(stored?: Record<string, { individual: string; domain: string }>): FacebookTabLimits {
  const one = (t: FacebookTab) => ({
    individual: stored?.[t]?.individual ?? "",
    domain: stored?.[t]?.domain ?? "",
  });
  return { people: one("people"), pages: one("pages"), groups: one("groups") };
}

interface Props {
  clientId: string;
  clientName: string;
  platforms: PlatformState[];
  onClient: (clientId: string, name: string) => void;
  onForgetClient: (clientId: string) => void;
  busy: boolean;
  onStopDiscovery?: () => void;
  stoppingDiscovery?: boolean;
  // Discovery still runs as a trackable job (see useDiscoveryJobPoll) --
  // this just hands the new job's id to whoever's watching it (App.tsx),
  // no jobsApi.job()/event-log fetch first, the poller reads its own
  // snapshot.
  onDiscoveryStarted: (jobId: string) => void;
  // "Run Analysis" no longer re-scrapes a client's own profile rows in
  // place (there is no such concept any more -- analysis is independent,
  // memory-only, URL-driven). It analyses this client's currently
  // VALIDATED discovery profiles instead (same action as Live Results'
  // "Analyse Validated Profiles"), which starts an ordinary analysis job
  // this hands off to the Analysis tab to watch.
  onAnalyseStarted: (jobId: string) => void;
  onError: (m: string) => void;
  // Whether `platforms` has ever loaded, and why it could not be read.
  // Without these an empty list cannot tell "none registered" from "still
  // loading" from "backend unreachable" -- and all three said "none".
  platformsLoaded?: boolean;
  platformsError?: string;
}

function splitKeywordList(raw: string): string[] {
  return raw
    .split(/[,\n]/)
    .map((s) => cleanKeyword(s))
    .filter(Boolean);
}

function dedupeKeywordsCaseInsensitive(keywords: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const kw of keywords) {
    const key = kw.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(kw);
  }
  return out;
}

function ChipInput({
  chips,
  onAdd,
  onAddMany,
  onRemove,
  placeholder,
  disabled,
  reserved = [],
}: {
  chips: string[];
  // Terms that are already searched without being a chip here -- a child
  // equal to its own parent. The server drops such a child, so accepting it
  // only made the "N searches" count promise one more than ran.
  reserved?: string[];
  onAdd: (v: string) => void;
  // Adding several at once (paste, bulk import) needs ONE state update
  // carrying the whole batch, not N calls to onAdd -- see addMany's own
  // comment for why. Optional so a caller with nothing better falls back
  // to looping onAdd (still correct for a caller whose onAdd doesn't close
  // over a stale array, just not what this component's own two callers do).
  onAddMany?: (values: string[]) => void;
  onRemove: (i: number) => void;
  placeholder: string;
  disabled?: boolean;
}) {
  const [input, setInput] = useState("");
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkText, setBulkText] = useState("");

  const taken = (kw: string) => {
    const key = cleanKeyword(kw).toLowerCase();
    return reserved.some((r) => cleanKeyword(r).toLowerCase() === key);
  };

  const commit = () => {
    const trimmed = cleanKeyword(input);
    if (trimmed) {
      if (taken(trimmed) || chips.some((c) => cleanKeyword(c).toLowerCase() === trimmed.toLowerCase())) {
        toast(`⚠️ "${trimmed}" already exists`, { id: `dup-${trimmed.toLowerCase()}` });
      } else {
        onAdd(trimmed);
      }
      setInput("");
    }
  };

  // Dedupes the whole batch in ONE pass against `chips` (this render's
  // value, read once) and against each other, then commits it in ONE call.
  // NOT "call onAdd() once per item in a loop": every real onAdd()
  // implementation here is `(v) => onChange([...chips, v])`, closing over
  // the SAME `chips` from this render -- N synchronous calls each compute
  // "current chips + one new item" from that identical stale array, and
  // since React batches these into one re-render, only the LAST call's
  // result survives. A paste of 4 terms silently kept just the last one.
  const addMany = (items: string[]) => {
    const seen = new Set(chips.map((c) => cleanKeyword(c).toLowerCase()));
    const fresh: string[] = [];
    let dupCount = 0;
    for (const kw of items) {
      const key = kw.toLowerCase();
      if (seen.has(key) || taken(kw)) {
        dupCount++;
      } else {
        seen.add(key);
        fresh.push(kw);
      }
    }
    if (fresh.length) {
      if (onAddMany) onAddMany(fresh);
      else fresh.forEach(onAdd);
    }
    if (dupCount > 0) {
      toast(`⚠️ Skipped ${dupCount} duplicate keyword${dupCount === 1 ? "" : "s"}`);
    }
  };

  const commitBulk = () => {
    addMany(splitKeywordList(bulkText));
    setBulkText("");
    setBulkOpen(false);
  };

  return (
    <div>
      <div className="chips-input-container">
        {chips.map((kw, i) => (
          <span key={i} className="kw-chip">
            {kw}
            <span className="remove-chip" onClick={() => onRemove(i)}>
              ✕
            </span>
          </span>
        ))}
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              commit();
            }
          }}
          onPaste={(e) => {
            const text = e.clipboardData.getData("text");
            if (/[,\n]/.test(text)) {
              e.preventDefault();
              addMany(splitKeywordList(text));
            }
          }}
          onBlur={commit}
          placeholder={placeholder}
          className="chip-input"
          disabled={disabled}
        />
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: "6px" }}>
        <div className="kw-count-badge" style={{ margin: 0 }}>
          <strong>{chips.length}</strong> keyword{chips.length === 1 ? "" : "s"} configured
        </div>
        <button
          type="button"
          className="bulk-kw-toggle"
          onClick={() => setBulkOpen((v) => !v)}
          disabled={disabled}
        >
          {bulkOpen ? "▾ Close bulk paste" : (
            <span style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}>
              <CloneIcon size={12} /> Bulk import
            </span>
          )}
        </button>
      </div>
      {bulkOpen && (
        <div className="bulk-kw-panel">
          <textarea
            value={bulkText}
            onChange={(e) => setBulkText(e.target.value)}
            placeholder={"one per line, or comma-separated -- e.g.\ngautam adani\nkaran adani, jeet adani"}
            rows={3}
            disabled={disabled}
          />
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "4px" }}>
            <button
              type="button"
              className="btn-cyber-primary"
              style={{ width: "auto", padding: "6px 14px", fontSize: "11.5px", marginTop: 0 }}
              onClick={commitBulk}
              disabled={disabled || !bulkText.trim()}
            >
              Add Keywords
            </button>
          </div>
        </div>
      )}
    </div>
  );
}


// ── Rule-based keyword generator ──────────────────────────────────
// Instead of picking individual permutations from a 100+ item flat list,
// the analyst checks RULE CATEGORIES ("Prefix Impersonation", "Scam Lures",
// etc.) and the system generates all matching terms at once. The output
// format (parent → children[]) is identical to what the old modal produced,
// so mergeGeneratedChildren and the backend need zero changes.

interface GenerationRule {
  id: string;
  label: string;
  description: string;
  appliesTo: "names" | "domain" | "both";
  prefixes: string[];
  suffixes: string[];
}

const GENERATION_RULES: GenerationRule[] = [
  {
    id: "prefix_impersonation",
    label: "Prefix Impersonation",
    description: "official_, real_, the_real_",
    appliesTo: "both",
    prefixes: ["official_", "real_", "the_real_"],
    suffixes: [],
  },
  {
    id: "suffix_impersonation",
    label: "Suffix Impersonation",
    description: "_official, _real, _vip, _direct",
    appliesTo: "both",
    prefixes: [],
    suffixes: ["_official", "_real", "_vip", "_direct"],
  },
  {
    id: "fan_parody",
    label: "Fan / Parody Pages",
    description: "_fanpage, _fan, _parody, _tribute",
    appliesTo: "names",
    prefixes: [],
    suffixes: ["_fanpage", "_fan", "_parody", "_tribute"],
  },
  {
    id: "support_lures",
    label: "Customer Support Lures",
    description: "support_, help_ / _support, _helpdesk, _service",
    appliesTo: "domain",
    prefixes: ["support_", "help_"],
    suffixes: ["_support", "_helpdesk", "_service"],
  },
  {
    id: "scam_crypto",
    label: "Scam / Crypto / Giveaway Lures",
    description: "_crypto, _giveaway, _investment, _fund, _promo",
    appliesTo: "both",
    prefixes: [],
    suffixes: ["_crypto", "_giveaway", "_investment", "_fund", "_promo"],
  },
  {
    id: "hiring_jobs",
    label: "Hiring / Job Lures",
    description: "_careers, _jobs, _recruitment, _hiring",
    appliesTo: "domain",
    prefixes: [],
    suffixes: ["_careers", "_jobs", "_recruitment", "_hiring"],
  },
  {
    id: "app_service",
    label: "App / Service Impersonation",
    description: "_app, _pro, _finance, _pay",
    appliesTo: "domain",
    prefixes: [],
    suffixes: ["_app", "_pro", "_finance", "_pay"],
  },
];

interface CustomRule {
  prefix: string;
  suffix: string;
}

// How many preview terms to show per rule before collapsing.
const RULE_PREVIEW_CAP = 4;

function RuleBasedGeneratorModal({
  nameKeywords,
  domainKeywords,
  existingNameGroups,
  existingDomainGroups,
  onAddKeywords,
  onClose,
}: {
  nameKeywords: string[];
  domainKeywords: string[];
  existingNameGroups: KeywordGroup[];
  existingDomainGroups: KeywordGroup[];
  onAddKeywords: (type: "names" | "domain", byParent: Record<string, string[]>) => void;
  onClose: () => void;
}) {
  const [enabledRules, setEnabledRules] = useState<Set<string>>(new Set());
  const [scopeNames, setScopeNames] = useState(true);
  const [scopeDomain, setScopeDomain] = useState(true);
  const [customRules, setCustomRules] = useState<CustomRule[]>([]);
  const [customPrefixInput, setCustomPrefixInput] = useState("");
  const [customSuffixInput, setCustomSuffixInput] = useState("");

  const toggleRule = (id: string) => {
    setEnabledRules((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const selectAllRules = () => {
    const applicableRules = GENERATION_RULES.filter((r) => {
      if (r.appliesTo === "names") return scopeNames && nameKeywords.length > 0;
      if (r.appliesTo === "domain") return scopeDomain && domainKeywords.length > 0;
      return (scopeNames && nameKeywords.length > 0) || (scopeDomain && domainKeywords.length > 0);
    });
    if (enabledRules.size === applicableRules.length && customRules.length === 0) {
      setEnabledRules(new Set());
    } else {
      setEnabledRules(new Set(applicableRules.map((r) => r.id)));
    }
  };

  const addCustomRule = () => {
    const p = customPrefixInput.trim();
    const s = customSuffixInput.trim();
    if (!p && !s) return;
    // Ensure they end/start with underscore for readability
    const prefix = p ? (p.endsWith("_") ? p : p + "_") : "";
    const suffix = s ? (s.startsWith("_") ? s : "_" + s) : "";
    setCustomRules((prev) => [...prev, { prefix, suffix }]);
    setCustomPrefixInput("");
    setCustomSuffixInput("");
  };

  const removeCustomRule = (idx: number) => {
    setCustomRules((prev) => prev.filter((_, i) => i !== idx));
  };

  // Build the set of all existing children for fast duplicate detection.
  const existingChildrenByParent = useMemo(() => {
    const map: Record<string, Set<string>> = {};
    for (const g of existingNameGroups) {
      map[g.parent] = new Set(g.children.map((c) => c.toLowerCase()));
    }
    for (const g of existingDomainGroups) {
      map[g.parent] = new Set(g.children.map((c) => c.toLowerCase()));
    }
    return map;
  }, [existingNameGroups, existingDomainGroups]);

  // Compute all generated terms grouped by type and parent, plus duplicate counts.
  const generated = useMemo(() => {
    const nameResults: Record<string, string[]> = {};
    const domainResults: Record<string, string[]> = {};
    let newCount = 0;
    let dupCount = 0;

    const perRulePreview: Record<string, { terms: string[]; total: number }> = {};

    const applyRule = (
      rule: { prefixes: string[]; suffixes: string[] },
      ruleId: string,
      keywords: string[],
      kwType: "names" | "domain",
    ) => {
      const bucket = kwType === "names" ? nameResults : domainResults;
      const preview = perRulePreview[ruleId] || { terms: [], total: 0 };
      perRulePreview[ruleId] = preview;

      for (const kw of keywords) {
        const clean = kw.toLowerCase().replace(/\s+/g, "_");
        const existingSet = existingChildrenByParent[kw] || new Set<string>();
        if (!bucket[kw]) bucket[kw] = [];

        const generated: string[] = [];
        for (const pre of rule.prefixes) generated.push(`${pre}${clean}`);
        for (const suf of rule.suffixes) generated.push(`${clean}${suf}`);

        for (const term of generated) {
          const key = term.toLowerCase();
          if (existingSet.has(key) || bucket[kw].some((t) => t.toLowerCase() === key)) {
            dupCount++;
          } else {
            bucket[kw].push(term);
            newCount++;
            preview.total++;
            if (preview.terms.length < RULE_PREVIEW_CAP) preview.terms.push(term);
          }
        }
      }
    };

    // Apply built-in rules
    for (const rule of GENERATION_RULES) {
      if (!enabledRules.has(rule.id)) continue;
      if ((rule.appliesTo === "names" || rule.appliesTo === "both") && scopeNames) {
        applyRule(rule, rule.id, nameKeywords, "names");
      }
      if ((rule.appliesTo === "domain" || rule.appliesTo === "both") && scopeDomain) {
        applyRule(rule, rule.id, domainKeywords, "domain");
      }
    }

    // Apply custom rules
    for (let ci = 0; ci < customRules.length; ci++) {
      const cr = customRules[ci];
      const customRuleObj = {
        prefixes: cr.prefix ? [cr.prefix] : [],
        suffixes: cr.suffix ? [cr.suffix] : [],
      };
      const ruleId = `custom_${ci}`;
      if (scopeNames && nameKeywords.length) {
        applyRule(customRuleObj, ruleId, nameKeywords, "names");
      }
      if (scopeDomain && domainKeywords.length) {
        applyRule(customRuleObj, ruleId, domainKeywords, "domain");
      }
    }

    return { nameResults, domainResults, newCount, dupCount, perRulePreview };
  }, [enabledRules, scopeNames, scopeDomain, nameKeywords, domainKeywords, customRules, existingChildrenByParent]);

  const handleApply = () => {
    if (Object.keys(generated.nameResults).length) {
      onAddKeywords("names", generated.nameResults);
    }
    if (Object.keys(generated.domainResults).length) {
      onAddKeywords("domain", generated.domainResults);
    }
    toast.success(`Added ${generated.newCount} search variation${generated.newCount === 1 ? "" : "s"}!`, { icon: "✨" });
    onClose();
  };

  const hasKeywords = nameKeywords.length > 0 || domainKeywords.length > 0;

  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(8,15,30,0.8)",
        backdropFilter: "blur(8px)",
        zIndex: 10000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "20px",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="dashboard-card-box"
        style={{ width: "min(680px, 100%)", background: "var(--bg-card)", maxHeight: "85vh", display: "flex", flexDirection: "column" }}
      >
        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px", borderBottom: "1px solid var(--border-subtle)", paddingBottom: "10px", flexShrink: 0 }}>
          <div style={{ fontSize: "15px", fontWeight: 700, display: "flex", alignItems: "center", gap: "8px" }}>
            <SparklesIcon size={16} color="var(--cyan)" />
            <span>Keyword Rule Generator</span>
          </div>
          <button onClick={onClose} style={{ background: "transparent", border: "none", color: "var(--text-muted)", fontSize: "16px", cursor: "pointer" }}>✕</button>
        </div>

        <div style={{ fontSize: "12px", color: "var(--text-dim)", marginBottom: "14px", lineHeight: 1.5, flexShrink: 0 }}>
          Select generation rules below to auto-generate impersonation, scam, and
          fake-support keyword variations. Each generated term is filed as a{" "}
          <strong style={{ color: "var(--cyan)" }}>search term</strong> under its parent keyword.
        </div>

        {!hasKeywords ? (
          <div style={{ textAlign: "center", padding: "24px", color: "var(--text-dim)" }}>
            Please add at least one Individual Name or Domain Keyword first.
          </div>
        ) : (
          <>
            {/* Scope toggles */}
            <div style={{ display: "flex", gap: "10px", marginBottom: "14px", flexShrink: 0 }}>
              <label
                className="rule-scope-toggle"
                style={{
                  display: "inline-flex", alignItems: "center", gap: "6px",
                  padding: "6px 12px", borderRadius: "8px", cursor: nameKeywords.length ? "pointer" : "not-allowed",
                  fontSize: "12px", fontWeight: 600,
                  background: scopeNames && nameKeywords.length ? "rgba(136, 56, 221, 0.15)" : "transparent",
                  border: `1px solid ${scopeNames && nameKeywords.length ? "rgba(136, 56, 221, 0.4)" : "var(--border-subtle)"}`,
                  color: scopeNames && nameKeywords.length ? "var(--cyan)" : "var(--text-dim)",
                  opacity: nameKeywords.length ? 1 : 0.5,
                }}
              >
                <input
                  type="checkbox"
                  checked={scopeNames && nameKeywords.length > 0}
                  onChange={() => nameKeywords.length && setScopeNames((v) => !v)}
                  disabled={!nameKeywords.length}
                  style={{ accentColor: "var(--cyan)" }}
                />
                <UserIcon size={13} /> Individual Names ({nameKeywords.length})
              </label>
              <label
                className="rule-scope-toggle"
                style={{
                  display: "inline-flex", alignItems: "center", gap: "6px",
                  padding: "6px 12px", borderRadius: "8px", cursor: domainKeywords.length ? "pointer" : "not-allowed",
                  fontSize: "12px", fontWeight: 600,
                  background: scopeDomain && domainKeywords.length ? "rgba(119, 39, 205, 0.15)" : "transparent",
                  border: `1px solid ${scopeDomain && domainKeywords.length ? "rgba(119, 39, 205, 0.4)" : "var(--border-subtle)"}`,
                  color: scopeDomain && domainKeywords.length ? "var(--purple)" : "var(--text-dim)",
                  opacity: domainKeywords.length ? 1 : 0.5,
                }}
              >
                <input
                  type="checkbox"
                  checked={scopeDomain && domainKeywords.length > 0}
                  onChange={() => domainKeywords.length && setScopeDomain((v) => !v)}
                  disabled={!domainKeywords.length}
                  style={{ accentColor: "var(--purple)" }}
                />
                <TagIcon size={13} /> Domain Keywords ({domainKeywords.length})
              </label>

              <button
                type="button"
                onClick={selectAllRules}
                style={{ marginLeft: "auto", background: "none", border: "none", color: "var(--cyan)", fontSize: "11px", cursor: "pointer", textDecoration: "underline" }}
              >
                {enabledRules.size > 0 ? "Deselect All Rules" : "Select All Rules"}
              </button>
            </div>

            {/* Rules list -- scrollable */}
            <div style={{ overflowY: "auto", flex: 1, minHeight: 0, border: "1px solid var(--border-subtle)", borderRadius: "10px", padding: "6px" }}>
              {GENERATION_RULES.map((rule) => {
                const disabled =
                  (rule.appliesTo === "names" && (!scopeNames || !nameKeywords.length)) ||
                  (rule.appliesTo === "domain" && (!scopeDomain || !domainKeywords.length)) ||
                  (rule.appliesTo === "both" && ((!scopeNames || !nameKeywords.length) && (!scopeDomain || !domainKeywords.length)));

                const checked = enabledRules.has(rule.id);
                const preview = generated.perRulePreview[rule.id];

                const appliesToLabel =
                  rule.appliesTo === "names" ? "Individual" :
                  rule.appliesTo === "domain" ? "Domain" : "Both";
                const appliesToColor =
                  rule.appliesTo === "names" ? "var(--cyan)" :
                  rule.appliesTo === "domain" ? "var(--purple)" : "var(--text-dim)";

                return (
                  <div
                    key={rule.id}
                    style={{
                      padding: "10px 12px",
                      borderRadius: "8px",
                      marginBottom: "4px",
                      background: checked ? "rgba(136, 56, 221, 0.08)" : "transparent",
                      border: checked ? "1px solid rgba(136, 56, 221, 0.2)" : "1px solid transparent",
                      opacity: disabled ? 0.45 : 1,
                      transition: "all 0.15s ease",
                    }}
                  >
                    <label
                      style={{
                        display: "flex",
                        alignItems: "flex-start",
                        gap: "10px",
                        cursor: disabled ? "not-allowed" : "pointer",
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => !disabled && toggleRule(rule.id)}
                        disabled={disabled}
                        style={{ marginTop: "2px", accentColor: "var(--cyan)" }}
                      />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "3px" }}>
                          <span style={{ fontSize: "13px", fontWeight: 600, color: "var(--text-main)" }}>{rule.label}</span>
                          <span style={{
                            fontSize: "9.5px", fontWeight: 600, color: appliesToColor,
                            background: "rgba(255,255,255,0.05)", padding: "1px 7px",
                            borderRadius: "20px", textTransform: "uppercase", letterSpacing: "0.3px",
                          }}>
                            {appliesToLabel}
                          </span>
                        </div>
                        <div style={{ fontSize: "11px", color: "var(--text-dim)", fontFamily: "var(--font-mono, monospace)" }}>
                          {rule.description}
                        </div>
                        {/* Live preview of generated terms */}
                        {checked && preview && preview.total > 0 && (
                          <div style={{
                            marginTop: "6px", fontSize: "11px", color: "var(--cyan)",
                            display: "flex", flexWrap: "wrap", gap: "4px", alignItems: "center",
                          }}>
                            <span style={{ color: "var(--text-dim)", marginRight: "2px" }}>→ {preview.total} terms:</span>
                            {preview.terms.map((t, i) => (
                              <span
                                key={i}
                                style={{
                                  background: "rgba(136, 56, 221, 0.12)", padding: "1px 6px",
                                  borderRadius: "4px", fontSize: "10.5px", fontFamily: "var(--font-mono, monospace)",
                                  color: "var(--text-main)",
                                }}
                              >
                                {t}
                              </span>
                            ))}
                            {preview.total > RULE_PREVIEW_CAP && (
                              <span style={{ color: "var(--text-dim)", fontSize: "10px" }}>
                                +{preview.total - RULE_PREVIEW_CAP} more
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    </label>
                  </div>
                );
              })}

              {/* Custom rules already added */}
              {customRules.map((cr, ci) => {
                const ruleId = `custom_${ci}`;
                const preview = generated.perRulePreview[ruleId];
                const label = [cr.prefix && `${cr.prefix}…`, cr.suffix && `…${cr.suffix}`].filter(Boolean).join(" / ");
                return (
                  <div
                    key={ruleId}
                    style={{
                      padding: "10px 12px",
                      borderRadius: "8px",
                      marginBottom: "4px",
                      background: "rgba(154, 80, 233, 0.08)",
                      border: "1px solid rgba(154, 80, 233, 0.25)",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                      <ZapIcon size={14} color="var(--cyan)" />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "2px" }}>
                          <span style={{ fontSize: "13px", fontWeight: 600, color: "var(--text-main)" }}>Custom: {label}</span>
                          <span style={{
                            fontSize: "9.5px", fontWeight: 600, color: "var(--text-dim)",
                            background: "rgba(255,255,255,0.05)", padding: "1px 7px",
                            borderRadius: "20px", textTransform: "uppercase", letterSpacing: "0.3px",
                          }}>
                            Both
                          </span>
                        </div>
                        {preview && preview.total > 0 && (
                          <div style={{ fontSize: "11px", color: "var(--cyan)", display: "flex", flexWrap: "wrap", gap: "4px", alignItems: "center" }}>
                            <span style={{ color: "var(--text-dim)", marginRight: "2px" }}>→ {preview.total} terms:</span>
                            {preview.terms.map((t, i) => (
                              <span
                                key={i}
                                style={{
                                  background: "rgba(136, 56, 221, 0.12)", padding: "1px 6px",
                                  borderRadius: "4px", fontSize: "10.5px", fontFamily: "var(--font-mono, monospace)",
                                  color: "var(--text-main)",
                                }}
                              >
                                {t}
                              </span>
                            ))}
                            {preview.total > RULE_PREVIEW_CAP && (
                              <span style={{ color: "var(--text-dim)", fontSize: "10px" }}>+{preview.total - RULE_PREVIEW_CAP} more</span>
                            )}
                          </div>
                        )}
                      </div>
                      <button
                        type="button"
                        onClick={() => removeCustomRule(ci)}
                        style={{ background: "transparent", border: "none", color: "var(--danger, #e95053)", cursor: "pointer", fontSize: "13px", padding: "2px 6px", flexShrink: 0 }}
                        title="Remove custom rule"
                      >
                        ✕
                      </button>
                    </div>
                  </div>
                );
              })}

              {/* Custom rule input */}
              <div
                style={{
                  padding: "10px 12px",
                  borderRadius: "8px",
                  border: "1px dashed var(--border-subtle)",
                  marginTop: "4px",
                }}
              >
                <div style={{ fontSize: "12px", fontWeight: 600, color: "var(--text-dim)", marginBottom: "8px", display: "flex", alignItems: "center", gap: "6px" }}>
                  <PlusIcon size={13} /> Add Custom Rule
                </div>
                <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                  <div style={{ flex: 1 }}>
                    <label style={{ fontSize: "10px", color: "var(--text-dim)", display: "block", marginBottom: "2px" }}>Prefix</label>
                    <input
                      value={customPrefixInput}
                      onChange={(e) => setCustomPrefixInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addCustomRule(); } }}
                      placeholder="e.g. hq_"
                      style={{
                        width: "100%", background: "rgba(255,255,255,0.04)", border: "1px solid var(--border-subtle)",
                        borderRadius: "6px", padding: "5px 8px", color: "var(--text-main)", fontSize: "12px",
                        fontFamily: "var(--font-mono, monospace)", outline: "none",
                      }}
                    />
                  </div>
                  <div style={{ flex: 1 }}>
                    <label style={{ fontSize: "10px", color: "var(--text-dim)", display: "block", marginBottom: "2px" }}>Suffix</label>
                    <input
                      value={customSuffixInput}
                      onChange={(e) => setCustomSuffixInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addCustomRule(); } }}
                      placeholder="e.g. _global"
                      style={{
                        width: "100%", background: "rgba(255,255,255,0.04)", border: "1px solid var(--border-subtle)",
                        borderRadius: "6px", padding: "5px 8px", color: "var(--text-main)", fontSize: "12px",
                        fontFamily: "var(--font-mono, monospace)", outline: "none",
                      }}
                    />
                  </div>
                  <button
                    type="button"
                    onClick={addCustomRule}
                    disabled={!customPrefixInput.trim() && !customSuffixInput.trim()}
                    style={{
                      background: "rgba(136, 56, 221, 0.15)", border: "1px solid rgba(136, 56, 221, 0.3)",
                      color: "var(--cyan)", padding: "6px 12px", borderRadius: "6px", fontSize: "11px",
                      fontWeight: 600, cursor: "pointer", whiteSpace: "nowrap", marginTop: "14px",
                      opacity: (!customPrefixInput.trim() && !customSuffixInput.trim()) ? 0.5 : 1,
                    }}
                  >
                    + Add
                  </button>
                </div>
              </div>
            </div>

            {/* Summary bar */}
            <div style={{
              display: "flex", justifyContent: "space-between", alignItems: "center",
              marginTop: "14px", paddingTop: "10px", borderTop: "1px solid var(--border-subtle)", flexShrink: 0,
            }}>
              <div style={{ fontSize: "12px", color: "var(--text-dim)" }}>
                {generated.newCount > 0 ? (
                  <>
                    <strong style={{ color: "var(--cyan)" }}>{generated.newCount}</strong> new search term{generated.newCount === 1 ? "" : "s"} from{" "}
                    <strong>{(scopeNames ? nameKeywords.length : 0) + (scopeDomain ? domainKeywords.length : 0)}</strong> keyword{((scopeNames ? nameKeywords.length : 0) + (scopeDomain ? domainKeywords.length : 0)) === 1 ? "" : "s"}
                    {generated.dupCount > 0 && (
                      <span style={{ color: "var(--text-muted)", marginLeft: "6px" }}>
                        ({generated.dupCount} duplicate{generated.dupCount === 1 ? "" : "s"} skipped)
                      </span>
                    )}
                  </>
                ) : enabledRules.size > 0 || customRules.length > 0 ? (
                  <span>All generated terms already exist as search terms.</span>
                ) : (
                  <span>Select rules above to generate search terms.</span>
                )}
              </div>
            </div>

            {/* Action buttons */}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: "10px", marginTop: "12px", flexShrink: 0 }}>
              <button type="button" onClick={onClose} className="action-btn" style={{ fontSize: "12px" }}>
                Cancel
              </button>
              <button
                type="button"
                onClick={handleApply}
                disabled={!generated.newCount}
                className="btn-cyber-primary"
                style={{ width: "auto", padding: "7px 18px", fontSize: "12px", marginTop: 0, display: "inline-flex", alignItems: "center", gap: "6px" }}
              >
                <SparklesIcon size={14} /> Apply {generated.newCount} Keyword{generated.newCount === 1 ? "" : "s"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// Parent/child keyword editor -- SPREADSHEET / DATA GRID layout.
//
// The distinction it exists to express (see backend/shared/keywords.py):
//   PARENT   the real name being protected. It is what discovered profiles
//            are scored against and the bucket they are filed under.
//   CHILDREN the analyst's own permutations. These ARE what gets searched on
//            every platform, and are never scored against.
//
// The grid layout is Excel-like: analysts can enter Main Keyword and its
// Permutations in the bottom row, Tab between them, press Enter to commit,
// or paste single/multi-line lists (e.g. "Name: perm1, perm2").
function KeywordGroupEditor({
  groups,
  onChange,
  parentPlaceholder,
  childPlaceholder,
  accent,
  disabled,
  clientId,
  kind,
  logos,
  onLogosChanged,
}: {
  groups: KeywordGroup[];
  onChange: (next: KeywordGroup[]) => void;
  parentPlaceholder: string;
  childPlaceholder: string;
  accent: string;
  disabled?: boolean;
  // Optional brand mark per keyword -- see components/KeywordLogoCell.tsx.
  // Absent until the client is saved, since a logo is stored against a
  // client id server-side.
  clientId: string;
  kind: "individual" | "domain";
  logos: ClientLogo[];
  onLogosChanged: () => void;
}) {
  // Kept outside the component so a keyword typed but not yet added
  // survives switching tabs (see services/clientDraft.ts).
  const [parentInput, setParentInput] = useDraftText(`${clientId || "new"}.${kind}.parent`);
  const [childInput, setChildInput] = useDraftText(`${clientId || "new"}.${kind}.child`);

  const applyBulkText = (raw: string) => {
    const parsed = parseBulkKeywordGroups(raw);
    if (!parsed.length) return;
    const { next, addedCount, dupCount } = mergeBulkKeywordGroups(groups, parsed);
    onChange(next);
    if (dupCount > 0) {
      toast(`⚠️ Skipped ${dupCount} duplicate item${dupCount === 1 ? "" : "s"}`);
    }
    if (addedCount > 0) {
      toast.success(`Added ${addedCount} keyword group${addedCount === 1 ? "" : "s"}`);
    }
  };

  const handleAddRow = () => {
    const pTrimmed = cleanKeyword(parentInput.replace(/^\s*,+|,+\s*$/g, ""));
    const cTrimmed = childInput.trim();

    if (!pTrimmed && !cTrimmed) return;

    // If input contains bulk separator or multiple lines, apply bulk parser
    if (/[,\n]/.test(pTrimmed) || /:(?!\/\/)|->|=>/.test(pTrimmed)) {
      // NOT WITH PERMUTATIONS TYPED BELOW IT. The bulk parser only reads
      // the main-keyword box, so anything in the permutations box was
      // cleared below without ever being added -- typed keywords vanishing
      // with no word said. Refuse and keep both boxes as they are instead.
      if (cTrimmed) {
        toast.error(
          "The main keyword contains a list separator (, : -> =>), so it is read as a bulk list " +
            "and the permutations box would be ignored. Put the permutations inside the main " +
            'keyword box (e.g. "Name: perm1, perm2"), or remove the separator.',
          { duration: 8000 },
        );
        return;
      }
      applyBulkText(pTrimmed);
      setParentInput("");
      setChildInput("");
      return;
    }

    if (!pTrimmed) {
      toast.error("Please enter a main keyword first");
      return;
    }

    // Parse children from childInput (comma-separated or semicolon-separated)
    const newChildren = cTrimmed
      ? cTrimmed.split(/[,;\n]+/).map((s) => cleanKeyword(s)).filter(Boolean)
      : [];

    // Check if parent already exists (case-insensitive)
    const existingIdx = groups.findIndex((g) => g.parent.toLowerCase() === pTrimmed.toLowerCase());
    if (existingIdx >= 0) {
      if (newChildren.length > 0) {
        const existing = groups[existingIdx];
        const seen = new Set(existing.children.map((c) => c.toLowerCase()));
        seen.add(existing.parent.toLowerCase());
        const fresh = newChildren.filter((c) => !seen.has(c.toLowerCase()));
        if (fresh.length > 0) {
          const updated = [...groups];
          updated[existingIdx] = { ...existing, children: [...existing.children, ...fresh] };
          onChange(updated);
          toast.success(`Added ${fresh.length} variation(s) to "${existing.parent}"`);
        } else {
          toast(`⚠️ All variations already exist for "${existing.parent}"`);
        }
      } else {
        toast(`⚠️ "${pTrimmed}" already exists`, { id: `dup-parent-${pTrimmed.toLowerCase()}` });
      }
      setParentInput("");
      setChildInput("");
      return;
    }

    // Add new group
    onChange([...groups, { parent: pTrimmed, children: newChildren }]);
    setParentInput("");
    setChildInput("");
  };

  const removeParent = (idx: number) =>
    onChange(groups.filter((_, i) => i !== idx));

  const setChildren = (idx: number, children: string[]) =>
    onChange(groups.map((g, i) => (i === idx ? { ...g, children } : g)));

  // THE NUMBER THE BACKEND ACTUALLY RUNS: every parent and every permutation,
  // each distinct search once (case- and spacing-insensitive, as
  // backend/shared/keywords.py `build_plans` dedupes). A permutation listed
  // under two parents is one search there, but was counted twice here, so
  // the screen promised more searches than ran.
  const totalSearchTerms = new Set(
    groups.flatMap((g) => [g.parent, ...g.children].map((t) => cleanKeyword(t).toLowerCase()).filter(Boolean)),
  ).size;

  return (
    <div>
      {/* ── SPREADSHEET GRID ── */}
      <div style={{
        border: "1px solid var(--border-subtle)",
        borderRadius: "10px",
        overflow: "hidden",
        background: "var(--bg-card)",
      }}>
        <table style={{
          width: "100%",
          borderCollapse: "collapse",
          tableLayout: "fixed",
        }}>
          <thead>
            <tr style={{
              background: "rgba(255,255,255,0.03)",
              borderBottom: "1px solid var(--border-subtle)",
            }}>
              <th style={{
                width: "220px",
                padding: "8px 12px",
                fontSize: "11px",
                fontWeight: 700,
                color: accent,
                textAlign: "left",
                textTransform: "uppercase",
                letterSpacing: "0.5px",
                borderRight: "1px solid var(--border-subtle)",
              }}>
                Main Keyword
              </th>
              <th style={{
                padding: "8px 12px",
                fontSize: "11px",
                fontWeight: 700,
                color: "var(--text-dim)",
                textAlign: "left",
                textTransform: "uppercase",
                letterSpacing: "0.5px",
              }}>
                Search Permutations
              </th>
              <th style={{ width: "44px", padding: "8px 4px" }} />
            </tr>
          </thead>
          <tbody>
            {groups.map((g, idx) => (
              <tr
                key={`${g.parent}-${idx}`}
                style={{
                  borderBottom: "1px solid var(--border-subtle)",
                  background: "transparent",
                  transition: "background 0.1s ease",
                }}
                onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = "rgba(255,255,255,0.02)"; }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = "transparent"; }}
              >
                {/* Parent cell */}
                <td style={{
                  padding: "8px 12px",
                  verticalAlign: "top",
                  borderRight: "1px solid var(--border-subtle)",
                }}>
                  <div style={{
                    fontSize: "13px",
                    fontWeight: 600,
                    color: "var(--text-main)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                    lineHeight: "28px",
                  }}>
                    {g.parent}
                  </div>
                  {/* The brand mark for THIS keyword. Optional: a keyword
                      with none behaves exactly as it always has. */}
                  <div style={{ marginTop: "6px" }}>
                    <KeywordLogoCell
                      clientId={clientId}
                      keyword={g.parent}
                      kind={kind}
                      logos={logos}
                      onChanged={onLogosChanged}
                      disabled={disabled}
                    />
                  </div>
                  <div style={{
                    fontSize: "10px",
                    color: accent,
                    fontWeight: 500,
                    marginTop: "2px",
                    opacity: 0.85,
                  }}>
                    {g.children.length
                      ? `${g.children.length + 1} searches (${g.children.length} permutation${g.children.length === 1 ? "" : "s"})`
                      : "searches itself"}
                  </div>
                </td>

                {/* Children/permutations cell */}
                <td style={{ padding: "6px 10px", verticalAlign: "top" }}>
                  <ChipInput
                    chips={g.children}
                    reserved={[g.parent]}
                    onAdd={(v) => setChildren(idx, [...g.children, v])}
                    onAddMany={(vs) => setChildren(idx, [...g.children, ...vs])}
                    onRemove={(i) => setChildren(idx, g.children.filter((_, j) => j !== i))}
                    placeholder={childPlaceholder}
                    disabled={disabled}
                  />
                </td>

                {/* Delete cell */}
                <td style={{ padding: "8px 4px", verticalAlign: "top", textAlign: "center" }}>
                  <button
                    type="button"
                    onClick={() => removeParent(idx)}
                    disabled={disabled}
                    title="Remove this keyword and all its permutations"
                    style={{
                      background: "transparent",
                      border: "none",
                      color: "var(--danger, #e95053)",
                      cursor: "pointer",
                      fontSize: "13px",
                      padding: "4px",
                      opacity: 0.6,
                      transition: "opacity 0.15s",
                      lineHeight: "28px",
                    }}
                    onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.opacity = "1"; }}
                    onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.opacity = "0.6"; }}
                  >
                    <TrashIcon size={14} />
                  </button>
                </td>
              </tr>
            ))}

            {/* Add-new row -- dual inputs for Main Keyword & Permutations */}
            <tr style={{ background: "rgba(255,255,255,0.015)" }}>
              {/* Main Keyword Input */}
              <td style={{
                padding: "8px 12px",
                borderRight: "1px solid var(--border-subtle)",
                verticalAlign: "middle",
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                  <PlusIcon size={13} color={accent} style={{ flexShrink: 0, opacity: 0.7 }} />
                  <input
                    value={parentInput}
                    onChange={(e) => setParentInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        handleAddRow();
                      }
                    }}
                    onPaste={(e) => {
                      const text = e.clipboardData.getData("text");
                      if (/[,\n]/.test(text) || /:(?!\/\/)|->|=>/.test(text)) {
                        e.preventDefault();
                        applyBulkText(text);
                        setParentInput("");
                        setChildInput("");
                      }
                    }}
                    placeholder={parentPlaceholder}
                    disabled={disabled}
                    style={{
                      flex: 1,
                      minWidth: 0,
                      background: "transparent",
                      border: "none",
                      outline: "none",
                      color: "var(--text-main)",
                      fontSize: "12.5px",
                      padding: "4px 0",
                    }}
                  />
                </div>
              </td>

              {/* Permutations Input */}
              <td style={{ padding: "8px 10px", verticalAlign: "middle" }}>
                <input
                  value={childInput}
                  onChange={(e) => setChildInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      handleAddRow();
                    }
                  }}
                  onPaste={(e) => {
                    const text = e.clipboardData.getData("text");
                    // A parent is already typed: what lands in THIS box are
                    // its permutations. Pasted lines used to be read as a
                    // list of new parents instead -- each became its own
                    // group and the typed parent was thrown away. Only an
                    // explicit "parent: child, child" line is a bulk import.
                    const structured = /:(?!\/\/)|->|=>/.test(text);
                    if (cleanKeyword(parentInput) && !structured) {
                      if (/[,;\n]/.test(text)) {
                        e.preventDefault();
                        const pasted = text.split(/[,;\n]+/).map((s) => cleanKeyword(s)).filter(Boolean);
                        setChildInput((prev) => [cleanKeyword(prev), ...pasted].filter(Boolean).join(", "));
                      }
                      return;
                    }
                    if (structured || text.includes("\n")) {
                      e.preventDefault();
                      applyBulkText(text);
                      setParentInput("");
                      setChildInput("");
                    }
                  }}
                  placeholder="Permutations (e.g. official_name, real_name) -- press Enter to add"
                  disabled={disabled}
                  style={{
                    width: "100%",
                    background: "rgba(255,255,255,0.03)",
                    border: "1px dashed var(--border-subtle)",
                    borderRadius: "6px",
                    outline: "none",
                    color: "var(--text-main)",
                    fontSize: "12px",
                    padding: "5px 8px",
                  }}
                />
              </td>

              {/* Add Action Button */}
              <td style={{ padding: "8px 4px", verticalAlign: "middle", textAlign: "center" }}>
                <button
                  type="button"
                  onClick={handleAddRow}
                  disabled={disabled || (!parentInput.trim() && !childInput.trim())}
                  title="Add keyword"
                  style={{
                    background: (parentInput.trim() || childInput.trim()) ? "rgba(136, 56, 221, 0.25)" : "transparent",
                    border: `1px solid ${(parentInput.trim() || childInput.trim()) ? "rgba(136, 56, 221, 0.5)" : "transparent"}`,
                    color: (parentInput.trim() || childInput.trim()) ? "var(--cyan)" : "var(--text-muted)",
                    borderRadius: "6px",
                    cursor: (parentInput.trim() || childInput.trim()) ? "pointer" : "default",
                    fontSize: "12px",
                    padding: "4px 6px",
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    opacity: (parentInput.trim() || childInput.trim()) ? 1 : 0.4,
                    transition: "all 0.15s ease",
                  }}
                >
                  <PlusIcon size={14} />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      {/* Footer count */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: "8px" }}>
        <div className="kw-count-badge" style={{ margin: 0 }}>
          <strong>{groups.length}</strong> keyword{groups.length === 1 ? "" : "s"} · <strong style={{ color: accent }}>{totalSearchTerms}</strong> search{totalSearchTerms === 1 ? "" : "es"} per platform
        </div>
        <div style={{ fontSize: "10px", color: "var(--text-dim)" }}>
          Tip: Enter main keyword & permutations in the bottom row, or paste <code style={{ background: "rgba(255,255,255,0.06)", padding: "1px 4px", borderRadius: "3px" }}>Name: perm1, perm2</code>
        </div>
      </div>
    </div>
  );
}



function KeywordTabs({
  activeTab,
  onTab,
  nameKeywords,
  domainKeywords,
  nameGroups,
  domainGroups,
  onNameGroups,
  onDomainGroups,
  platforms,
  disabled,
  clientId,
}: {
  activeTab: KeywordTab;
  onTab: (t: KeywordTab) => void;
  // Derived parent lists -- the tab counts, and the source the
  // threat-keyword generator builds its permutations FROM (it files each
  // one back under the parent it came from, as a child).
  nameKeywords: string[];
  domainKeywords: string[];
  nameGroups: KeywordGroup[];
  domainGroups: KeywordGroup[];
  onNameGroups: (next: KeywordGroup[]) => void;
  onDomainGroups: (next: KeywordGroup[]) => void;
  platforms: PlatformState[];
  disabled?: boolean;
  /** Empty until the client is saved -- logos are stored against a client id. */
  clientId: string;
}) {
  // Loaded once per client and handed to every keyword row, so a grid with
  // 40 keywords makes ONE request rather than one per row.
  const [logos, setLogos] = useState<ClientLogo[]>([]);
  // Why the logos could not be read, or "". A failed read used to become
  // `setLogos([])` -- the same picture as a client that simply has no
  // logos, so "unknown" looked like "none".
  const [logosError, setLogosError] = useState("");
  // The client the newest request was for. Switching client mid-load
  // used to let the OLD client's answer land last and paint its logos
  // onto the new client's keywords.
  const logosFor = useRef(clientId);
  const reloadLogos = useCallback(() => {
    logosFor.current = clientId;
    setLogosError("");
    if (!clientId) { setLogos([]); return; }
    logosApi.list(clientId)
      .then((r) => {
        if (logosFor.current === clientId) setLogos(r.items);
      })
      // Still no toast -- a client with no logos is the normal case and
      // most analysts never use the feature -- but a quiet line under the
      // tabs, so a failure is never mistaken for an empty list.
      .catch((e) => {
        if (logosFor.current !== clientId) return;
        setLogos([]);
        setLogosError((e as Error).message || "request failed");
      });
  }, [clientId]);
  useEffect(() => { reloadLogos(); }, [reloadLogos]);
  const [genOpen, setGenOpen] = useState(false);

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
        <div className="kw-tab-row" style={{ margin: 0 }}>
          <button className={`kw-tab-btn ${activeTab === "names" ? "active" : ""}`} onClick={() => onTab("names")}>
            <UserIcon size={14} style={{ marginRight: "6px" }} />
            Individual Names
            {nameKeywords.length > 0 && <span className="kw-tab-count">{nameKeywords.length}</span>}
          </button>
          <button className={`kw-tab-btn ${activeTab === "domain" ? "active" : ""}`} onClick={() => onTab("domain")}>
            <TagIcon size={14} style={{ marginRight: "6px" }} />
            Domain Keywords
            {domainKeywords.length > 0 && <span className="kw-tab-count">{domainKeywords.length}</span>}
          </button>
        </div>

        <button
          type="button"
          onClick={() => setGenOpen(true)}
          disabled={disabled || (!nameKeywords.length && !domainKeywords.length)}
          style={{
            background: "linear-gradient(135deg, rgba(154, 80, 233, 0.18), rgba(136, 56, 221, 0.22))",
            border: "1px solid rgba(154, 80, 233, 0.45)",
            color: "var(--purple, #9A50E9)",
            padding: "6px 12px",
            borderRadius: "8px",
            fontSize: "11.5px",
            fontWeight: 600,
            cursor: "pointer",
            display: "inline-flex",
            alignItems: "center",
            gap: "6px",
          }}
          title="Auto-generate threat actor and fake support keyword permutations"
        >
          <SparklesIcon size={14} /> Suggest Threat Keywords
        </button>
      </div>

      {logosError && (
        <div
          role="status"
          style={{
            margin: "0 0 10px", fontSize: "11.5px",
            color: "var(--warning, #F59E0B)",
            display: "flex", alignItems: "center", gap: "8px",
          }}
        >
          <AlertTriangleIcon size={12} />
          Logos could not be loaded ({logosError}) -- the logo column below may be incomplete.
          <button
            type="button"
            onClick={reloadLogos}
            style={{
              background: "transparent", border: "none", padding: 0,
              color: "inherit", textDecoration: "underline", cursor: "pointer", fontSize: "inherit",
            }}
          >
            Retry
          </button>
        </div>
      )}

      <div style={{ display: activeTab === "names" ? "block" : "none" }}>
        <KeywordGroupEditor
          groups={nameGroups}
          onChange={onNameGroups}
          parentPlaceholder="Type an executive/individual name and press Enter…"
          childPlaceholder="Add a search term for this name and press Enter…"
          accent="var(--purple, #9A50E9)"
          disabled={disabled}
          clientId={clientId}
          kind="individual"
          logos={logos}
          onLogosChanged={reloadLogos}
        />
      </div>

      <div style={{ display: activeTab === "domain" ? "block" : "none" }}>
        <KeywordGroupEditor
          groups={domainGroups}
          onChange={onDomainGroups}
          parentPlaceholder="Type a brand/product keyword and press Enter…"
          childPlaceholder="Add a search term for this keyword and press Enter…"
          accent="var(--purple, #8838DD)"
          disabled={disabled}
          clientId={clientId}
          kind="domain"
          logos={logos}
          onLogosChanged={reloadLogos}
        />
      </div>


      {genOpen && (
        <RuleBasedGeneratorModal
          nameKeywords={nameKeywords}
          domainKeywords={domainKeywords}
          existingNameGroups={nameGroups}
          existingDomainGroups={domainGroups}
          onAddKeywords={(type, byParent) => {
            // Attach the variations as CHILDREN of the parents they were
            // generated from, never as new parents (see
            // services/keywordGroups.ts for why, and for the merge rules).
            if (type === "names") onNameGroups(mergeGeneratedChildren(nameGroups, byParent));
            else onDomainGroups(mergeGeneratedChildren(domainGroups, byParent));
          }}
          onClose={() => setGenOpen(false)}
        />
      )}
    </div>
  );
}

function PlatformLimitsEditor({
  platforms,
  individualLimits,
  domainLimits,
  onIndividualChange,
  onDomainChange,
  facebookTabLimits,
  onFacebookTabChange,
  disabled,
  emptyText = "No platforms registered yet.",
}: {
  platforms: PlatformState[];
  individualLimits: Record<string, string>;
  domainLimits: Record<string, string>;
  onIndividualChange: (platform: string, value: string) => void;
  onDomainChange: (platform: string, value: string) => void;
  facebookTabLimits: FacebookTabLimits;
  onFacebookTabChange: (tab: FacebookTab, kwType: "individual" | "domain", value: string) => void;
  disabled?: boolean;
  // What an EMPTY table says. Only "No platforms registered yet." once the
  // list has actually loaded -- before then, or when it failed, an empty
  // table means "not known", and saying "none" there is a clean zero.
  emptyText?: string;
}) {
  // Open when any sub-tab cap is set: a collapsed row hid caps that were
  // really in force, so the table read "Unlimited" for a capped Facebook.
  const fbHasValues = Object.values(facebookTabLimits).some(
    (t) => t.individual.trim() !== "" || t.domain.trim() !== "");
  const [fbExpanded, setFbExpanded] = useState(fbHasValues);
  useEffect(() => {
    if (fbHasValues) setFbExpanded(true);
  }, [fbHasValues]);

  return (
    <div className="platform-limits-table-card">
      <div>
        <div style={{ fontSize: "14px", fontWeight: 700, color: "var(--text-main)", display: "flex", alignItems: "center", gap: "8px" }}>
          <TargetIcon size={18} color="var(--cyan)" />
          <span>Per-Platform Scrape Limits</span>
        </div>
        <div style={{ fontSize: "12px", color: "var(--text-dim)", marginTop: "4px" }}>
          Individual and Domain sweeps are capped independently. Leave empty or 0 for <strong>Unlimited</strong> scraping.
        </div>
      </div>

      <table className="platform-limits-modern-table">
        <thead>
          <tr>
            <th style={{ width: "35%" }}>Platform</th>
            <th style={{ width: "30%" }}>
              <span style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                <UserIcon size={13} color="var(--cyan)" /> Individual Cap
              </span>
            </th>
            <th style={{ width: "35%" }}>
              <span style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                <TagIcon size={13} color="var(--purple)" /> Domain Cap
              </span>
            </th>
          </tr>
        </thead>
        <tbody>
          {platforms.map((p) => {
            const isFacebook = p.platform === "facebook";
            return (
              <Fragment key={p.platform}>
                <tr className="limits-table-row">
                  <td>
                    <div style={{ display: "flex", alignItems: "center", gap: "8px", fontWeight: 600, fontSize: "13px" }}>
                      <PlatformIcon platform={p.platform} size={18} />
                      <span>{p.name}</span>
                      {isFacebook && (
                        <button
                          type="button"
                          onClick={() => setFbExpanded((v) => !v)}
                          style={{
                            background: "rgba(154, 80, 233, 0.14)",
                            border: "1px solid rgba(154, 80, 233, 0.35)",
                            color: "var(--purple, #9A50E9)",
                            fontSize: "10.5px",
                            padding: "2px 7px",
                            borderRadius: "6px",
                            cursor: "pointer",
                            marginLeft: "auto",
                          }}
                        >
                          {fbExpanded ? "▴ Tabs" : "▾ Sub-tabs"}
                        </button>
                      )}
                    </div>
                  </td>
                  <td>
                    <input
                      type="number"
                      min={0}
                      value={individualLimits[p.platform] ?? ""}
                      onChange={(e) => onIndividualChange(p.platform, e.target.value)}
                      placeholder="∞ Unlimited"
                      disabled={disabled}
                      className="limits-num-input"
                    />
                  </td>
                  <td>
                    <input
                      type="number"
                      min={0}
                      value={domainLimits[p.platform] ?? ""}
                      onChange={(e) => onDomainChange(p.platform, e.target.value)}
                      placeholder="∞ Unlimited"
                      disabled={disabled}
                      className="limits-num-input"
                    />
                  </td>
                </tr>
                {isFacebook && fbExpanded && (
                  (
                    [
                      ["people", "People Tab"],
                      ["pages", "Pages Tab"],
                      ["groups", "Groups Tab"],
                    ] as const
                  ).map(([tab, label]) => (
                    <tr key={tab} className="limits-table-row" style={{ background: "rgba(0,0,0,0.18)" }}>
                      <td style={{ paddingLeft: "32px", fontSize: "12px", color: "var(--text-muted)" }}>
                        ↳ {label}
                      </td>
                      <td>
                        <input
                          type="number"
                          min={0}
                          value={facebookTabLimits[tab].individual}
                          onChange={(e) => onFacebookTabChange(tab, "individual", e.target.value)}
                          placeholder="∞ Unlimited"
                          disabled={disabled}
                          className="limits-num-input"
                        />
                      </td>
                      <td>
                        <input
                          type="number"
                          min={0}
                          value={facebookTabLimits[tab].domain}
                          onChange={(e) => onFacebookTabChange(tab, "domain", e.target.value)}
                          placeholder="∞ Unlimited"
                          disabled={disabled}
                          className="limits-num-input"
                        />
                      </td>
                    </tr>
                  ))
                )}
              </Fragment>
            );
          })}
          {!platforms.length && (
            <tr>
              <td colSpan={3} style={{ textAlign: "center", padding: "20px", color: "var(--text-dim)" }}>
                {emptyText}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// A selection -> the request's platform scope. Empty means "every ready
// platform" (send nothing). One collapses to `platform`, which keeps the
// backend's tighter per-platform job locking and its coalescing; two or
// more go as `platforms`.
function platformScope(sel: Set<string>): { platform?: string; platforms?: string[] } {
  const ids = [...sel];
  if (ids.length === 0) return {};
  if (ids.length === 1) return { platform: ids[0] };
  return { platforms: ids };
}

const EMPTY_FORM = { id: "", name: "", domain: "", nameKw: [] as string[], domainKw: [] as string[], cron: "" };

// The keyword categories a discovery sweep can be narrowed to, rendered as
// toggle chips beside the platform chips. Mirrors backend/shared/keywords.py's
// own INDIVIDUAL/DOMAIN vocabulary -- the server scopes by these exact names
// (see discovery_controller._validated_keyword_type), so they are a contract,
// not display strings.
const KEYWORD_SCOPES = [
  { id: "individual", label: "Individual Names" },
  { id: "domain", label: "Domain Keywords" },
] as const;

// Narrowed rather than plain `string` so the value flowing into
// discoveryApi.discover's `keyword_type` is checked at compile time against
// the two categories the server actually accepts, instead of relying on a
// cast that would happily pass a typo through to a 400.
type KeywordScope = (typeof KEYWORD_SCOPES)[number]["id"];

// A loaded client's groups for one keyword type. The API always returns
// `keyword_groups` (synthesising childless parents from the flat lists for
// a client saved before the feature existed), but this falls back to the
// flat list anyway so the form still populates against an older API build
// or a partially-cached response rather than silently showing no keywords.
function groupsOf(c: Client | null, type: "individual" | "domain", flat: string[]): KeywordGroup[] {
  const fromApi = c?.keyword_groups?.[type];
  if (Array.isArray(fromApi) && fromApi.length) {
    return fromApi.map((g) => ({
      parent: g.parent,
      children: Array.isArray(g.children) ? [...g.children] : [],
    }));
  }
  return (flat || []).map((parent) => ({ parent, children: [] }));
}

// A saved client's Facebook sub-tab caps, as the text the limit boxes hold.
// Also reads the older flat shape (one number for both keyword types).
function tabLimitsFormOf(c: Client): FacebookTabLimits {
  const fbTabs = (c.platform_tab_limits?.facebook || {}) as Record<string, unknown>;
  const readTab = (v: unknown): { individual: string; domain: string } => {
    if (v && typeof v === "object") {
      const o = v as { individual?: number; domain?: number };
      return {
        individual: o.individual !== undefined ? String(o.individual) : "",
        domain: o.domain !== undefined ? String(o.domain) : "",
      };
    }
    const flat = v !== undefined && v !== null ? String(v) : "";
    return { individual: flat, domain: flat };
  };
  return {
    people: readTab(fbTabs.people),
    pages: readTab(fbTabs.pages),
    groups: readTab(fbTabs.groups),
  };
}

// Do two copies of ONE saved client hold the same setup -- everything a
// sweep searches and everything the form edits? Used to notice that the
// directory has a newer copy than the one this page opened (another
// analyst saved in between). Order- and spacing-insensitive where the
// helpers are, so two reads of an unchanged client always compare equal;
// the flat keyword lists, which are exactly what Discover sends, are also
// compared verbatim.
function sameSavedClient(a: Client, b: Client): boolean {
  const flat = (xs?: string[]) => JSON.stringify(xs || []);
  return (
    (a.name || "") === (b.name || "") &&
    (a.domain || "") === (b.domain || "") &&
    (a.cron || "") === (b.cron || "") &&
    flat(a.name_keywords) === flat(b.name_keywords) &&
    flat(a.domain_keywords) === flat(b.domain_keywords) &&
    sameKeywordGroups(groupsOf(a, "individual", a.name_keywords || []),
      groupsOf(b, "individual", b.name_keywords || [])) &&
    sameKeywordGroups(groupsOf(a, "domain", a.domain_keywords || []),
      groupsOf(b, "domain", b.domain_keywords || [])) &&
    sameCaps(a.platform_limits_individual, b.platform_limits_individual) &&
    sameCaps(a.platform_limits_domain, b.platform_limits_domain) &&
    sameCaps(a.platform_tab_limits, b.platform_tab_limits)
  );
}

export function HomeView({
  clientId,
  platforms,
  onClient,
  onForgetClient,
  busy,
  onStopDiscovery,
  stoppingDiscovery = false,
  onDiscoveryStarted,
  onAnalyseStarted,
  onError,
  platformsLoaded = true,
  platformsError = "",
}: Props) {
  // Subscribed to the directory rather than copied out of it: this used
  // to be a snapshot taken on mount, so a client that arrived afterwards
  // -- migrated from localStorage, created in another tab -- did not
  // appear until something happened to remount the page.
  const { clients, loaded: clientsLoaded, error: clientsError } = useClientDirectory();
  const [loadingClients, setLoadingClients] = useState(true);
  // WHERE THE PAGE WAS LEFT. Switching to Live Results or Admin unmounts
  // this page; without this an analyst half-way through a new client came
  // back to a blank form (or to whichever client was last open). The mode,
  // the workspace tab and the unsaved form itself are read back from
  // services/clientDraft.ts, so coming back lands exactly where they left.
  const [boot] = useState(() => {
    const ui = loadUi();
    const inCreate = ui?.mode === "create" || !clientId;
    const tab = WORKSPACE_TABS.includes(ui?.workspaceTab as WorkspaceTab)
      ? (ui!.workspaceTab as WorkspaceTab)
      : "overview";
    return { mode: (inCreate ? "create" : "select") as Mode, tab, draft: inCreate ? loadDraft("new") : null };
  });
  const [mode, setMode] = useState<Mode>(boot.mode);
  const [activeWorkspaceTab, setActiveWorkspaceTab] = useState<WorkspaceTab>(boot.tab);
  const [sidebarFilter, setSidebarFilter] = useState<"all" | "active" | "empty">("all");
  const [sidebarSearch, setSidebarSearch] = useState("");

  const [editing, setEditing] = useState(false);
  const [activeClient, setActiveClient] = useState<Client | null>(null);

  const [idInput, setIdInput] = useState(boot.draft?.id ?? EMPTY_FORM.id);
  const [nameInput, setNameInput] = useState(boot.draft?.name ?? EMPTY_FORM.name);
  const [domainInput, setDomainInput] = useState(boot.draft?.domain ?? EMPTY_FORM.domain);

  // Parent/child groups are the SOURCE OF TRUTH here; the flat parent
  // lists below are derived from them, never edited independently, so the
  // form physically cannot produce a client whose groups and flat keywords
  // disagree (the server re-derives them again on save for the same
  // reason). See backend/shared/keywords.py.
  const [nameGroups, setNameGroups] = useState<KeywordGroup[]>(boot.draft?.nameGroups ?? []);
  const [domainGroups, setDomainGroups] = useState<KeywordGroup[]>(boot.draft?.domainGroups ?? []);
  const nameKeywords = useMemo(() => nameGroups.map((g) => g.parent), [nameGroups]);
  const domainKeywords = useMemo(() => domainGroups.map((g) => g.parent), [domainGroups]);

  const [platformLimitsIndividual, setPlatformLimitsIndividual] = useState<Record<string, string>>(
    boot.draft?.limitsIndividual ?? {});
  const [platformLimitsDomain, setPlatformLimitsDomain] = useState<Record<string, string>>(
    boot.draft?.limitsDomain ?? {});
  const [facebookTabLimits, setFacebookTabLimits] = useState<FacebookTabLimits>(
    () => fbTabLimitsOf(boot.draft?.fbTabLimits));
  const [cron, setCron] = useState(boot.draft?.cron ?? EMPTY_FORM.cron);
  const [activeTab, setActiveTab] = useState<KeywordTab>("names");

  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // A Discover / Analyse request on its way to the server. Without it a
  // double-click (or an impatient second click while the request was slow)
  // started TWO sweeps or two analysis jobs of the same thing. The ref is
  // the real guard -- it is set synchronously, before the first await, so
  // a second click in the same frame already sees it; the state only
  // drives the disabled buttons.
  const startingRef = useRef(false);
  const [starting, setStarting] = useState<"" | "discover" | "analyse">("");
  const beginStart = (what: "discover" | "analyse"): boolean => {
    if (startingRef.current) return false;
    startingRef.current = true;
    setStarting(what);
    return true;
  };
  const endStart = () => {
    startingRef.current = false;
    setStarting("");
  };

  // A SET, not a single id: the Run hub lets an analyst pick any
  // combination of platforms. Empty means "every ready platform", which
  // is what the All Platforms chip selects and what the backend does when
  // no scope is sent -- so the previous behaviour is the empty case.
  const [sweepPlatforms, setSweepPlatforms] = useState<Set<string>>(new Set());
  const [analysisPlatforms, setAnalysisPlatforms] = useState<Set<string>>(new Set());
  // Per-sweep time safety net, minutes as typed -- blank means "use the
  // server default" (currently 15 min, see backend/config/settings.py's
  // discovery_max_seconds). This only ever matters for a keyword that is
  // STILL finding new results when the clock runs out; one that already
  // reached a real end (Facebook's own has_next=false, etc.) stops on its
  // own well before any of this, confirmed live -- see that setting's own
  // comment. Not persisted per-client on purpose: unlike the result caps
  // above it, this is a "how patient am I today" dial, not part of a
  // client's saved scraping profile.
  const [sweepMaxMinutes, setSweepMaxMinutes] = useState("");

  // Straight from the clients database, through the shared directory
  // (services/clientDirectory.ts). Each client is one server document
  // owning its own keywords and caps -- nothing here merges two of them.
  const refreshClients = useCallback(() => {
    setLoadingClients(true);
    void refreshDirectory()
      // No setClients: `clients` is the subscribed directory itself.
      .finally(() => setLoadingClients(false));
  }, []);

  useEffect(() => {
    refreshClients();
  }, [refreshClients]);

  const loadIntoForm = (c: Client) => {
    setIdInput(c.client_id);
    setNameInput(c.name);
    setDomainInput(c.domain || "");
    setNameGroups(groupsOf(c, "individual", c.name_keywords || []));
    setDomainGroups(groupsOf(c, "domain", c.domain_keywords || []));
    setPlatformLimitsIndividual(
      Object.fromEntries(Object.entries(c.platform_limits_individual || {}).map(([k, v]) => [k, String(v)])),
    );
    setPlatformLimitsDomain(
      Object.fromEntries(Object.entries(c.platform_limits_domain || {}).map(([k, v]) => [k, String(v)])),
    );
    setFacebookTabLimits(tabLimitsFormOf(c));
    setCron(c.cron || "");
  };

  const clearForm = () => {
    setIdInput(EMPTY_FORM.id);
    setNameInput(EMPTY_FORM.name);
    setDomainInput(EMPTY_FORM.domain);
    setNameGroups([]);
    setDomainGroups([]);
    setPlatformLimitsIndividual({});
    setPlatformLimitsDomain({});
    setFacebookTabLimits({
      people: { individual: "", domain: "" },
      pages: { individual: "", domain: "" },
      groups: { individual: "", domain: "" },
    });
    setCron(EMPTY_FORM.cron);
  };

  const applyDraft = (d: ClientFormDraft) => {
    setIdInput(d.id);
    setNameInput(d.name);
    setDomainInput(d.domain);
    setNameGroups(d.nameGroups);
    setDomainGroups(d.domainGroups);
    setPlatformLimitsIndividual(d.limitsIndividual);
    setPlatformLimitsDomain(d.limitsDomain);
    setFacebookTabLimits(fbTabLimitsOf(d.fbTabLimits));
    setCron(d.cron);
  };

  // What the form holds right now, in the shape a draft is stored in.
  const formDraft = useMemo<ClientFormDraft>(
    () => ({
      id: idInput,
      name: nameInput,
      domain: domainInput,
      nameGroups,
      domainGroups,
      limitsIndividual: platformLimitsIndividual,
      limitsDomain: platformLimitsDomain,
      fbTabLimits: facebookTabLimits,
      cron,
    }),
    [idInput, nameInput, domainInput, nameGroups, domainGroups,
      platformLimitsIndividual, platformLimitsDomain, facebookTabLimits, cron],
  );

  // Open a saved client, with any unsaved edits the analyst left on it.
  const openClient = (c: Client) => {
    setActiveClient(c);
    dirCopyRef.current = c;
    loadIntoForm(c);
    const d = loadDraft(editKey(c.client_id));
    if (d && d.id === c.client_id) {
      applyDraft(d);
      toast(`Restored your unsaved changes to "${c.name || c.client_id}".`, { icon: "📝", id: "draft-restored" });
    }
    setMode("select");
    setEditing(false);
  };

  // Which client id this page last acted on. Lets the effect below tell
  // "the analyst picked another client" (header switcher, Ctrl+K) apart
  // from "the client list was re-read in the background".
  const seenClientIdRef = useRef<string | null>(boot.mode === "create" ? clientId : null);
  // Mirrors `hasPending` (computed further down, after this effect is
  // declared) so the effect below can read the CURRENT answer.
  const hasPendingRef = useRef(false);
  // The directory's own copy of the open client, as last seen -- see the
  // effect below for why identity, not content, decides "newly delivered".
  const dirCopyRef = useRef<Client | null>(null);

  useEffect(() => {
    if (clientId && activeClient?.client_id === clientId) {
      seenClientIdRef.current = clientId;
      // THE SAME CLIENT, RE-READ. This used to stop here, so once a client
      // was open, `activeClient` and the form were never refreshed from the
      // directory again: another analyst's saved keywords never reached
      // this screen, and Save then wrote this screen's stale list over
      // theirs. With nothing unsaved here, take the newer copy. With
      // unsaved edits it is NOT swapped in (that would throw the edits
      // away) -- saveConfig() checks for exactly this before writing.
      //
      // Only a copy the directory has NEWLY delivered counts (a different
      // object from the last one seen). Content alone is not enough: right
      // after a save, the directory can still hold the pre-save copy for a
      // moment (a re-read that left before the save landed), and adopting
      // that would put the analyst's just-saved edits back to the old ones.
      const fresh = clients.find((c) => c.client_id === clientId);
      if (fresh && fresh !== dirCopyRef.current) {
        const seenBefore = dirCopyRef.current !== null;
        dirCopyRef.current = fresh;
        if (seenBefore && !sameSavedClient(fresh, activeClient) && !hasPendingRef.current) {
          setActiveClient(fresh);
          loadIntoForm(fresh);
        }
      }
      return;
    }
    if (!clientId) {
      seenClientIdRef.current = clientId;
      return;
    }
    // THE BUG THIS GUARDS. The client list is re-read every time the
    // browser tab regains focus. This effect used to then load the last
    // selected client into the form whenever none was open -- and while
    // creating a new client none is, so simply switching browser tabs
    // replaced everything typed into Create New Client with an old client.
    // In create mode only a real switch to a different client may take over.
    if (mode === "create" && seenClientIdRef.current === clientId) return;
    const existing = clients.find((c) => c.client_id === clientId);
    if (!existing) return; // not loaded yet -- runs again when it arrives
    seenClientIdRef.current = clientId;
    openClient(existing);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId, clients, mode, activeClient]);

  // Remember which mode and workspace tab the page is on.
  useEffect(() => {
    saveUi({ mode, workspaceTab: activeWorkspaceTab });
  }, [mode, activeWorkspaceTab]);

  const switchToCreate = (opts: { restore?: boolean } = {}) => {
    setMode("create");
    setActiveClient(null);
    setEditing(false);
    // "+ New" brings back an unfinished new client rather than wiping it.
    const d = opts.restore === false ? null : loadDraft("new");
    if (d && !isBlankDraft(d)) {
      applyDraft(d);
      toast("Restored the new client you were setting up.", { icon: "📝", id: "draft-restored" });
    } else {
      clearForm();
    }
    setSweepPlatforms(new Set());
    setAnalysisPlatforms(new Set());
    setActiveWorkspaceTab("overview");
  };

  const discardNewClient = async () => {
    if (!isBlankDraft(formDraft) &&
        !(await confirmAction("Discard this new client? Everything typed into the form will be lost."))) {
      return false;
    }
    clearDraft("new");
    clearDraftTexts("new.");
    clearForm();
    return true;
  };

  const cancelCreate = async () => {
    if (!(await discardNewClient())) return;
    // Back to the client that was open before, not just the first one.
    const back = clients.find((c) => c.client_id === clientId) || clients[0];
    if (back) {
      selectSavedClient(back.client_id);
    } else {
      setMode("select");
    }
  };

  const selectSavedClient = (id: string) => {
    setSweepPlatforms(new Set());
    setAnalysisPlatforms(new Set());
    if (!id) {
      setActiveClient(null);
      setEditing(false);
      clearForm();
      onClient("", "");
      return;
    }
    const c = clients.find((x) => x.client_id === id);
    if (!c) return;
    seenClientIdRef.current = c.client_id;
    openClient(c);
    onClient(c.client_id, c.name);
  };

  const startEditing = () => {
    if (!activeClient) return;
    // Not reloaded from the saved client: that silently threw away any
    // keyword or limit edits made on the other tabs.
    setEditing(true);
    setActiveWorkspaceTab("settings");
  };

  const discardChanges = async () => {
    if (!activeClient) return;
    if (!(await confirmAction(
      `Discard your unsaved changes to "${activeClient.name || activeClient.client_id}"? The form goes back to what is saved.`,
    ))) {
      return;
    }
    // Back to the NEWEST saved copy, which may be someone else's save
    // that arrived while these edits were held.
    const base = clients.find((c) => c.client_id === activeClient.client_id) ?? activeClient;
    setActiveClient(base);
    dirCopyRef.current = base;
    loadIntoForm(base);
    clearDraft(editKey(activeClient.client_id));
    clearDraftTexts(`${activeClient.client_id}.`);
    setEditing(false);
  };

  const cancelEditing = () => {
    void discardChanges();
  };

  const cloneClient = async (c: Client) => {
    const pending = loadDraft("new");
    if (pending && !isBlankDraft(pending) &&
        !(await confirmAction(
          `You have an unfinished new client "${pending.name || pending.id || "untitled"}". Replace it with a copy of "${c.name || c.client_id}"?`,
        ))) {
      return;
    }
    switchToCreate({ restore: false });
    setIdInput(`${c.client_id}-copy`);
    setNameInput(`${c.name || c.client_id} (Copy)`);
    setDomainInput(c.domain || "");
    setNameGroups(groupsOf(c, "individual", c.name_keywords || []));
    setDomainGroups(groupsOf(c, "domain", c.domain_keywords || []));
    setPlatformLimitsIndividual(
      Object.fromEntries(Object.entries(c.platform_limits_individual || {}).map(([k, v]) => [k, String(v)])),
    );
    setPlatformLimitsDomain(
      Object.fromEntries(Object.entries(c.platform_limits_domain || {}).map(([k, v]) => [k, String(v)])),
    );
    // switchToCreate() above blanked the Facebook sub-tab caps, and this
    // used to leave them blank: the clone silently ran Facebook's People /
    // Pages / Groups UNCAPPED where the original was capped.
    setFacebookTabLimits(tabLimitsFormOf(c));
    toast.success(`Cloned configuration from "${c.name || c.client_id}". Review and save!`, { icon: "📋" });
  };

  const activeIndividualCount = activeClient?.name_keywords?.length || 0;
  const activeDomainCount = activeClient?.domain_keywords?.length || 0;
  const activeKeywordCount = activeIndividualCount + activeDomainCount;

  // Which keyword categories a discovery sweep covers. A SET with the same
  // semantics as `sweepPlatforms` above: EMPTY means all of them, which is
  // the default and what every sweep did before this control existed.
  // Selecting both categories is the same thing as selecting neither, so
  // it collapses back to empty (see toggleKeywordType). Narrowing this
  // actually excludes the other type's keywords from the request now --
  // handleSearch sends individual_keywords/domain_keywords as two separate
  // lists (see POST /discovery/jobs), each swept under its own cap
  // (platform_limits_individual/_domain, platform_tab_limits) -- there is
  // no server-side category resolution to fall back on any more; discovery/
  // discovery_service.py, which used to do that, was deleted in the lean
  // backend rebuild.
  const [sweepKeywordTypes, setSweepKeywordTypes] = useState<Set<KeywordScope>>(new Set());

  // WHICH FACEBOOK TABS THIS RUN SWEEPS. Same convention as the two chip
  // rows above it: an EMPTY selection means all of them, which is the
  // default and what every sweep did before this control existed.
  //
  // Worth its own control because Facebook's three tabs cost very different
  // amounts. People is effectively unbounded for a common name and runs to
  // the sweep time budget; Pages and Groups are small finite sets that
  // exhaust on their own in seconds. With keywords sweeping strictly one at
  // a time, an analyst after impersonating PAGES was paying for a People
  // sweep on every keyword to get them -- which is most of the run.
  const [sweepFbTabs, setSweepFbTabs] = useState<Set<FacebookTab>>(new Set());

  // The scope persists across client switches (the platform chips do too),
  // which can strand it on a category the newly-selected client has none of
  // -- that chip is disabled, so the only way out would be noticing it and
  // clicking another. Dropping the empty category is the honest fallback.
  useEffect(() => {
    setSweepKeywordTypes((prev) => {
      const next = new Set(prev);
      if (!activeIndividualCount) next.delete("individual");
      if (!activeDomainCount) next.delete("domain");
      return next.size === prev.size ? prev : next;
    });
  }, [activeIndividualCount, activeDomainCount]);

  const saveConfig = async (): Promise<Client | null> => {
    const id = idInput.trim();
    const name = nameInput.trim() || id;
    // An EDIT is "the analyst had this client open and pressed save". Any
    // other save is a CREATE, and a create under a taken org id is refused
    // by the server rather than overwriting whoever is already there.
    const editingExisting = activeClient?.client_id === id;
    if (!id) {
      onError("Enter an org id first.");
      return null;
    }
    setSaving(true);
    setSaved(false);
    try {
      // A limit that is not a whole number is REFUSED, not saved as "no cap":
      // see services/scrapeLimits.ts. The throw lands in the catch below and
      // reaches the analyst through onError.
      const parsedLimitsIndividual = parseCapMap(
        platformLimitsIndividual, (p) => `${nameOf(p)} individual cap`);
      const parsedLimitsDomain = parseCapMap(
        platformLimitsDomain, (p) => `${nameOf(p)} domain cap`);
      const fbTabLimits = parseTabCaps(facebookTabLimits);
      const upsertBody: ClientConfig = {
        client_id: id,
        name,
        domain: domainInput.trim(),
        // Sent for older/other consumers, but the server treats
        // `keyword_groups` as authoritative and re-derives these from its
        // parents anyway -- see backend/shared/keywords.py.
        name_keywords: nameKeywords,
        domain_keywords: domainKeywords,
        keyword_groups: { individual: nameGroups, domain: domainGroups },
        platform_limits_individual: parsedLimitsIndividual,
        platform_limits_domain: parsedLimitsDomain,
        platform_tab_limits: Object.keys(fbTabLimits).length ? { facebook: fbTabLimits } : {},
        cron: cron.trim() || null,
      };
      // Saved to the database. CREATE and EDIT are deliberately different
      // calls: saving used to be one upsert keyed on the org id, so
      // entering a NEW client under an id that was already taken silently
      // overwrote that client's keywords, caps and cron and reported
      // success -- two customers, one record, the first one's setup gone.
      // `createClient` is refused with 409 when the id exists, and the
      // message says so, so the analyst picks another id or edits the
      // client that is genuinely there.
      //
      // `editingExisting` is the analyst's own intent (they opened a client
      // and pressed save), not a guess from whether the id happens to be
      // free -- guessing is exactly what turned an accidental collision
      // into a silent overwrite.
      const { client_id: _omit, ...config } = upsertBody;
      // SOMEONE ELSE SAVED THIS CLIENT SINCE IT WAS OPENED. An edit is a
      // whole-record write, so saving now replaces their keywords and caps
      // with this screen's -- silently, as it used to. Re-read first and,
      // if the saved copy moved under us, ask before overwriting it.
      if (editingExisting && activeClient) {
        await refreshDirectory({ afterWrite: true });
        const server = findClient(id);
        if (server && !sameSavedClient(server, activeClient) &&
            !(await confirmAction(
              `"${server.name || id}" was changed by someone else since you opened it. Saving now ` +
                "REPLACES their saved keywords, caps and details with what is on this screen. " +
                "Save anyway? (Cancel, then Discard Changes, to load their version instead.)",
            ))) {
          return null;
        }
      }
      const client = editingExisting
        ? await updateDirectoryClient(id, config)
        : await createDirectoryClient(upsertBody);
      if (!editingExisting) {
        // The new-client draft is now a real client.
        clearDraft("new");
        clearDraftTexts("new.");
      }
      seenClientIdRef.current = client.client_id;
      // The directory's copy as of now is the baseline: only a copy it
      // delivers AFTER this save may replace the form (see the effect).
      dirCopyRef.current = findClient(client.client_id) ?? null;
      setActiveClient(client);
      // Show what the server actually stored (it collapses spacing and drops
      // duplicates), so the form and the saved client agree and the
      // unsaved-changes check below has nothing to report.
      loadIntoForm(client);
      clearDraft(editKey(client.client_id));
      setMode("select");
      setEditing(false);
      onClient(client.client_id, client.name);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      toast.success(
        editingExisting ? `Client "${client.name}" updated.` : `Client "${client.name}" created.`,
        { icon: "💾" },
      );
      refreshClients();
      return client;
    } catch (e) {
      onError((e as Error).message);
      return null;
    } finally {
      setSaving(false);
    }
  };

  // What the form holds that the saved client does not. Empty when the form
  // is exactly the saved client, or when no saved client is open. A limit
  // box that cannot be parsed counts as changed: it differs from whatever
  // was saved, and saving will say why.
  const unsavedChanges = (): string[] => {
    if (!activeClient) return [];
    const out: string[] = [];
    if (
      !sameKeywordGroups(nameGroups, groupsOf(activeClient, "individual", activeClient.name_keywords || [])) ||
      !sameKeywordGroups(domainGroups, groupsOf(activeClient, "domain", activeClient.domain_keywords || []))
    ) {
      out.push("keyword");
    }
    let limitsChanged = false;
    try {
      limitsChanged =
        !sameCaps(parseCapMap(platformLimitsIndividual, String), activeClient.platform_limits_individual) ||
        !sameCaps(parseCapMap(platformLimitsDomain, String), activeClient.platform_limits_domain) ||
        !sameCaps(parseTabCaps(facebookTabLimits), parseTabCaps(tabLimitsFormOf(activeClient)));
    } catch {
      limitsChanged = true;
    }
    if (limitsChanged) out.push("scrape-limit");
    return out;
  };

  // Name / domain / cron edits. Kept apart from unsavedChanges(): those do
  // not change what a sweep searches, so they must not block Discover, but
  // they are still unsaved work that has to survive a tab switch.
  const detailsChanged = (): boolean => {
    if (!activeClient) return false;
    return (
      (nameInput.trim() || activeClient.client_id) !== (activeClient.name || activeClient.client_id) ||
      domainInput.trim() !== (activeClient.domain || "").trim() ||
      cron.trim() !== (activeClient.cron || "").trim()
    );
  };

  const pendingChanges = [...unsavedChanges(), ...(detailsChanged() ? ["client details"] : [])];

  // EVERY CHANGE IS KEPT. The new-client form, or unsaved edits to an open
  // client, are written to storage as they are typed, so leaving the page
  // (another app tab, a reload) and coming back finds them where they were.
  const hasPending = pendingChanges.length > 0;
  hasPendingRef.current = hasPending;
  useEffect(() => {
    if (mode === "create") {
      if (isBlankDraft(formDraft)) clearDraft("new");
      else saveDraft("new", formDraft);
      return;
    }
    if (!activeClient || formDraft.id !== activeClient.client_id) return;
    if (hasPending) saveDraft(editKey(activeClient.client_id), formDraft);
    else clearDraft(editKey(activeClient.client_id));
  }, [mode, activeClient, formDraft, hasPending]);

  const handleSearch = async () => {
    if (!activeClient) return;
    if (!beginStart("discover")) return;
    try {
      await startSearch(activeClient);
    } finally {
      endStart();
    }
  };

  const startSearch = async (opened: Client) => {
    // THE FRESHEST SAVED COPY, NOT THE ONE OPENED. `activeClient` is
    // whatever was loaded when the client was opened; if another analyst
    // has saved new keywords since, sweeping it searched the OLD list and
    // never the new keywords -- a missed keyword that looks exactly like a
    // found-nothing one. Re-read the directory and sweep what is saved
    // now. (A failed re-read leaves the cached copy, and the directory's
    // own error banner says why.)
    await refreshDirectory({ afterWrite: true });
    const saved = findClient(opened.client_id) ?? opened;
    const changedElsewhere = !sameSavedClient(saved, opened);
    const individualCount = saved.name_keywords?.length || 0;
    const domainCount = saved.domain_keywords?.length || 0;
    if (!individualCount && !domainCount) {
      onError("This client has no keywords yet — head to the Keywords tab to add executive names or brand keywords.");
      return;
    }
    // The server rejects this too (POST /discovery/jobs requires at least
    // one non-empty individual_keywords/domain_keywords entry rather than
    // sweeping nothing and reporting success), but catching it here means
    // the analyst finds out on click instead of via a failed job.
    if (sweepKeywordScope === "individual" && !individualCount) {
      onError("This client has no individual names configured — pick All Keywords or Domain, or add some in the Keywords tab.");
      return;
    }
    if (sweepKeywordScope === "domain" && !domainCount) {
      onError("This client has no domain keywords configured — pick All Keywords or Individual, or add some in the Keywords tab.");
      return;
    }
    // WHAT IS SWEPT IS WHAT WAS SAVED. The request below is built from the
    // saved client, not from the form, so a keyword added (or a cap changed)
    // and not yet saved was silently left out: the analyst saw their new
    // keyword on screen, pressed Discover, and it was never searched. That
    // is the one thing this tool must not do, so it is refused until saved.
    const unsaved = unsavedChanges();
    if (unsaved.length) {
      onError(
        `You have unsaved ${unsaved.join(" and ")} changes. Discover searches what is saved, ` +
          "so save them first (Save Keyword Changes / Save Scrape Limits)." +
          (changedElsewhere
            ? " Note: this client was also changed by someone else since you opened it."
            : ""));
      return;
    }
    if (changedElsewhere) {
      // No unsaved edits, so the open-client effect reloads the form with
      // this copy; say so, rather than sweep a list the screen did not show.
      toast(
        `"${saved.name || saved.client_id}" was updated by someone else since you opened it -- ` +
          "this sweep uses the latest saved keywords and caps, now shown on screen.",
        { icon: "🔄", duration: 8000 },
      );
    }
    let maxSeconds: number | undefined;
    try {
      maxSeconds = parseSweepSeconds(sweepMaxMinutes);
    } catch (e) {
      onError((e as Error).message);
      return;
    }
    try {
      const scope = platformScope(sweepPlatforms);
      // sweepKeywordScope narrows which TYPE actually gets swept -- sending
      // an empty list for the excluded type rather than filtering after the
      // fact, so the caps below (platform_limits_individual/_domain) only
      // ever apply to keywords that are genuinely part of this sweep.
      const res = await discoveryApi.startDiscovery({
        group_id: saved.client_id,
        individual_keywords: sweepKeywordScope === "domain"
          ? [] : dedupeKeywordsCaseInsensitive(saved.name_keywords || []),
        domain_keywords: sweepKeywordScope === "individual"
          ? [] : dedupeKeywordsCaseInsensitive(saved.domain_keywords || []),
        platforms: scope.platforms || (scope.platform ? [scope.platform] : undefined),
        platform_limits_individual: saved.platform_limits_individual,
        platform_limits_domain: saved.platform_limits_domain,
        platform_tab_limits: saved.platform_tab_limits,
        // Empty = all three, exactly as an omitted `platforms` means every
        // platform. Sent unconditionally: the backend ignores it for every
        // platform that has only one tab.
        facebook_tabs: [...sweepFbTabs],
        max_seconds: maxSeconds,
      });
      // Started FIRST, then the skipped list: onDiscoveryStarted clears the
      // app's error banner, so reporting the skips before it (as this used
      // to) had them wiped in the same render -- never seen.
      onDiscoveryStarted(res.job_id);
      if (res.skipped.length) {
        onError(`Skipped: ${res.skipped.map((s) => `${s.value} (${s.reason})`).join(", ")}`);
      }
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const nameOf = (id: string) =>
    platforms.find((p) => p.platform === id)?.name || id;
  // What the scrape-limits table says when it has no rows. "None
  // registered" only once the platform list has really answered.
  const platformsEmptyText = platforms.length
    ? ""
    : platformsError
      ? `Platforms unavailable: ${platformsError}`
      : !platformsLoaded
        ? "Loading platforms…"
        : "No platforms registered yet.";
  // "Facebook", "Facebook +2", or "" for all-platforms -- a button label
  // has to stay short, so past two the rest becomes a count.
  const scopeLabel = (sel: Set<string>) => {
    const ids = [...sel];
    if (ids.length === 0) return "";
    if (ids.length === 1) return nameOf(ids[0]);
    return `${nameOf(ids[0])} +${ids.length - 1}`;
  };
  const sweepPlatformName = scopeLabel(sweepPlatforms);
  const analysisPlatformName = scopeLabel(analysisPlatforms);

  const handleRunAnalysis = async () => {
    if (!activeClient) return;
    // Guarded from the first click, before the confirmation dialog: two
    // quick clicks used to open two dialogs and start two identical jobs.
    if (!beginStart("analyse")) return;
    try {
      await startAnalysis(activeClient);
    } finally {
      endStart();
    }
  };

  const startAnalysis = async (activeClient: Client) => {
    const scope = analysisPlatformName ? `on ${analysisPlatformName}` : "across every platform";
    if (
      !(await confirmAction(
        `Analyse every currently validated profile of "${activeClient.name || activeClient.client_id}" ${scope}? This re-scrapes each one again -- results are memory-only, shown on the Analysis tab.`,
      ))
    ) {
      return;
    }
    try {
      const res = await discoveryApi.analyseValidated({
        group_id: activeClient.client_id,
        // THE WHOLE SELECTION, not just a selection of exactly one.
        //
        // This used to be
        //   analysisPlatforms.size === 1 ? [...analysisPlatforms][0] : undefined
        // against an endpoint that only spoke a single `platform`. So two or
        // more platforms picked collapsed to "no filter", and "no filter"
        // means EVERY platform -- an analyst who ticked Facebook and Twitter
        // got all six scraped, each one a live-session page visit per
        // validated profile. The confirmation dialog above even named the
        // narrow scope ("on Twitter +1") while the request widened it.
        //
        // Empty stays empty: no chips ticked is the All Platforms state, and
        // the API reads an empty list the same way it reads an omitted one.
        platforms: [...analysisPlatforms],
        domain: activeClient.domain,
      });
      onAnalyseStarted(res.job_id);
      // WHAT WAS ACTUALLY STARTED, and what was not. This used to ignore
      // both: profiles over the per-call ceiling, or URLs the analysis
      // engine refused, were never mentioned, so an analyst read the job's
      // results as covering every validated profile. After
      // onAnalyseStarted, which clears the error banner.
      toast.success(
        `Analysis started for ${res.accepted} validated profile${res.accepted === 1 ? "" : "s"}.`,
      );
      if (res.skipped.length) {
        onError(
          `Analysis did not include ${res.skipped.length} item(s): ` +
            res.skipped.map((s) => `${s.value} (${s.reason})`).join(", "),
        );
      }
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const handleDelete = async () => {
    if (!activeClient) return;
    // Says what actually happens, and only that. It used to promise that
    // ALL associated discovery profiles, validated profiles, analyst tags
    // and incidents went with the client; nothing deletes those, and
    // overstating a deletion is the kind of wrong that gets someone to
    // click through expecting a clean slate.
    const confirmed = await confirmAction(
      `Delete client "${activeClient.name || activeClient.client_id}"? This removes its keywords, scrape caps and cron from the database, for everyone -- not just this browser. Discovery profiles already found under this org id are NOT deleted: they stay in the database and reappear if you re-create the client with the same org id.`,
    );
    if (!confirmed) return;
    setDeleting(true);
    const deletedId = activeClient.client_id;
    // Local only: DELETE /clients/{id} has no backend either, so attempting
    // it just cost a 404 before doing the local delete anyway. Any
    // discovery profiles already stored server-side under this group_id are
    // untouched -- no route exposes deleting those.
    try {
      await deleteDirectoryClient(deletedId);
    } catch (e) {
      // Used to leave the button stuck on "Deleting…" with no message.
      setDeleting(false);
      onError((e as Error).message);
      return;
    }
    clearDraft(editKey(deletedId));
    clearDraftTexts(`${deletedId}.`);
    onForgetClient(deletedId);
    setActiveClient(null);
    setEditing(false);
    clearForm();
    onClient("", "");
    setDeleting(false);
    toast(`Client "${deletedId}" deleted.`, { icon: "🗑️" });
  };

  const filteredClients = useMemo(() => {
    return clients.filter((c) => {
      const matchesSearch =
        !sidebarSearch.trim() ||
        c.name.toLowerCase().includes(sidebarSearch.toLowerCase()) ||
        c.client_id.toLowerCase().includes(sidebarSearch.toLowerCase()) ||
        (c.domain && c.domain.toLowerCase().includes(sidebarSearch.toLowerCase()));

      if (!matchesSearch) return false;

      const totalKw = (c.name_keywords?.length || 0) + (c.domain_keywords?.length || 0);
      if (sidebarFilter === "active") return totalKw > 0;
      if (sidebarFilter === "empty") return totalKw === 0;
      return true;
    });
  }, [clients, sidebarSearch, sidebarFilter]);

  // The chips drive BOTH buttons: an analyst picks a scope once and then
  // chooses what to run on it, rather than setting it twice.
  const targetPlatforms = sweepPlatforms;
  const togglePlatform = (id: string) => {
    const next = new Set(targetPlatforms);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSweepPlatforms(next);
    setAnalysisPlatforms(next);
  };
  const selectAllPlatforms = () => {
    setSweepPlatforms(new Set());
    setAnalysisPlatforms(new Set());
  };

  // Keyword-category chips, same interaction as the platform chips above:
  // toggle them on, and an EMPTY selection means all of them.
  const toggleKeywordType = (id: KeywordScope) => {
    const next = new Set(sweepKeywordTypes);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    // With only two categories, "both selected" IS "all" -- collapsing it
    // back to empty keeps one canonical representation of that state, so
    // the All chip lights up instead of two chips that mean the same thing.
    setSweepKeywordTypes(next.size === KEYWORD_SCOPES.length ? new Set() : next);
  };
  const selectAllKeywordTypes = () => setSweepKeywordTypes(new Set());

  // Facebook tab chips, the same interaction again: toggle on, empty means
  // all. Selecting all three collapses back to empty so the All chip lights
  // up rather than three chips that together mean the same thing.
  const toggleFbTab = (id: FacebookTab) => {
    const next = new Set(sweepFbTabs);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSweepFbTabs(next.size === ALL_FB_TABS.length ? new Set() : next);
  };
  const selectAllFbTabs = () => setSweepFbTabs(new Set());

  // Only shown when Facebook is actually in scope -- the row is inert for
  // every other platform, and a control that cannot affect the run it sits
  // above is a question the analyst has to answer for nothing.
  const facebookInScope =
    targetPlatforms.size === 0 || targetPlatforms.has("facebook");

  // The single category to send to the API, or "" for all. Only a
  // selection of exactly one narrows anything -- empty (and, by the
  // collapse above, both) sweeps everything, which is what omitting the
  // parameter already means server-side.
  const sweepKeywordScope: KeywordScope | "" =
    sweepKeywordTypes.size === 1 ? [...sweepKeywordTypes][0] : "";
  return (
    <div className="clients-workspace-layout">
      {/* LEFT SIDEBAR: Client Directory */}
      <div className="clients-sidebar-card">
        <div className="clients-sidebar-header">
          <div className="clients-sidebar-title">
            <BuildingIcon size={16} color="var(--cyan)" />
            <span>Clients Directory</span>
            <span style={{ fontSize: "11px", color: "var(--text-dim)", fontWeight: 500 }}>
              ({clients.length})
            </span>
          </div>
          <button
            type="button"
            className="btn-new-client-pill"
            onClick={() => switchToCreate()}
            title="Create a new client"
          >
            <PlusIcon size={13} style={{ marginRight: "3px" }} /> New
          </button>
        </div>

        <div className="client-search-box">
          <span className="client-search-icon">
            <SearchIcon size={14} color="var(--text-dim)" />
          </span>
          <input
            value={sidebarSearch}
            onChange={(e) => setSidebarSearch(e.target.value)}
            placeholder="Search clients..."
          />
          <span
            className="client-search-shortcut"
            style={{ cursor: "pointer" }}
            onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true }))}
            title="Search clients globally (Ctrl+K)"
          >
            Ctrl K
          </span>
        </div>

        <div className="client-filter-pills">
          <button
            type="button"
            className={`client-filter-pill-btn ${sidebarFilter === "all" ? "active" : ""}`}
            onClick={() => setSidebarFilter("all")}
          >
            All
          </button>
          <button
            type="button"
            className={`client-filter-pill-btn ${sidebarFilter === "active" ? "active" : ""}`}
            onClick={() => setSidebarFilter("active")}
          >
            Active ({clients.filter((c) => (c.name_keywords?.length || 0) + (c.domain_keywords?.length || 0) > 0).length})
          </button>
          <button
            type="button"
            className={`client-filter-pill-btn ${sidebarFilter === "empty" ? "active" : ""}`}
            onClick={() => setSidebarFilter("empty")}
          >
            Needs Setup
          </button>
        </div>

        <div className="client-directory-list">
          {/* "No clients configured." ONLY when the directory really
              answered with none. Before the first answer it is "loading",
              and after a failed one it is an outage -- both used to read as
              an empty directory, inviting the analyst to re-create clients
              that exist. */}
          {clientsError && clients.length > 0 && (
            <div style={{ padding: "8px 12px", color: "var(--warning, #F59E0B)", fontSize: "11.5px" }}>
              Could not refresh clients: {clientsError}. Showing the last list loaded.
            </div>
          )}
          {!clientsLoaded || (loadingClients && !clients.length && !clientsError) ? (
            <div style={{ textAlign: "center", padding: "24px", color: "var(--text-dim)", fontSize: "12px" }}>
              Loading clients...
            </div>
          ) : clientsError && !clients.length ? (
            <div style={{ textAlign: "center", padding: "24px", color: "var(--danger, #E95053)", fontSize: "12px" }}>
              Could not load clients: {clientsError}
            </div>
          ) : !filteredClients.length ? (
            <div style={{ textAlign: "center", padding: "24px", color: "var(--text-dim)", fontSize: "12px" }}>
              {sidebarSearch ? "No matching clients found." : "No clients configured."}
            </div>
          ) : (
            filteredClients.map((c) => {
              const isSelected = mode === "select" && activeClient?.client_id === c.client_id;
              const kwCount = (c.name_keywords?.length || 0) + (c.domain_keywords?.length || 0);
              return (
                <div
                  key={c.client_id}
                  className={`client-directory-item ${isSelected ? "active" : ""}`}
                  onClick={() => selectSavedClient(c.client_id)}
                >
                  <div className="client-dir-avatar">
                    {(c.name || c.client_id).charAt(0).toUpperCase()}
                  </div>
                  <div className="client-dir-info">
                    <div className="client-dir-name">{c.name || c.client_id}</div>
                    <div className="client-dir-meta">
                      <span>{c.domain || c.client_id}</span>
                    </div>
                  </div>
                  <span className="client-dir-badge" title={`${kwCount} total keywords`}>
                    {kwCount} kw
                  </span>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* RIGHT DETAIL WORKSPACE */}
      <div className="client-workspace-pane">
        {mode === "create" ? (
          /* CREATE CLIENT WORKSPACE */
          <div className="dashboard-card-box" style={{ background: "var(--bg-card)", padding: "24px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px", borderBottom: "1px solid var(--border-subtle)", paddingBottom: "12px" }}>
              <div style={{ fontSize: "18px", fontWeight: 800, color: "var(--text-main)", display: "flex", alignItems: "center", gap: "8px" }}>
                <SparklesIcon size={18} color="var(--cyan)" />
                <span>Create New Client</span>
              </div>
              <button
                type="button"
                className="text-link-btn"
                onClick={() => void cancelCreate()}
                title="Discard this new client"
              >
                ✕ Cancel
              </button>
            </div>

            <div style={{ fontSize: "12px", color: "var(--text-dim)", marginBottom: "16px" }}>
              Your progress is kept as you type — you can switch tabs or reload and
              come back to it. Nothing is created until you press <strong>Save &amp; Create Client</strong>.
            </div>

            <div style={{ marginBottom: "20px" }}>
              <label className="field-label">1. Organization Details</label>
              <div className="client-setup-box" style={{ flexWrap: "wrap", marginTop: "8px" }}>
                <input
                  value={idInput}
                  onChange={(e) => setIdInput(e.target.value)}
                  placeholder="org id (unique slug, e.g. acme-corp)…"
                  className="client-select-input"
                />
                <input
                  value={nameInput}
                  onChange={(e) => setNameInput(e.target.value)}
                  placeholder="organization / client display name…"
                  className="client-select-input"
                />
                <input
                  value={domainInput}
                  onChange={(e) => setDomainInput(e.target.value)}
                  placeholder="official website domain (e.g. acme.com)…"
                  className="client-select-input"
                />
              </div>
            </div>

            <div style={{ marginBottom: "20px" }}>
              <label className="field-label">2. Search Keywords</label>
              <div style={{ marginTop: "8px" }}>
                <KeywordTabs
                  activeTab={activeTab}
                  onTab={setActiveTab}
                  nameKeywords={nameKeywords}
                  domainKeywords={domainKeywords}
                  nameGroups={nameGroups}
                  domainGroups={domainGroups}
                  onNameGroups={setNameGroups}
                  onDomainGroups={setDomainGroups}
                  platforms={platforms}
                  clientId={activeClient?.client_id || ""}
                />
              </div>
            </div>

            <div style={{ marginBottom: "20px" }}>
              <PlatformLimitsEditor
                platforms={platforms}
                emptyText={platformsEmptyText}
                individualLimits={platformLimitsIndividual}
                domainLimits={platformLimitsDomain}
                onIndividualChange={(platform, value) => setPlatformLimitsIndividual((prev) => ({ ...prev, [platform]: value }))}
                onDomainChange={(platform, value) => setPlatformLimitsDomain((prev) => ({ ...prev, [platform]: value }))}
                facebookTabLimits={facebookTabLimits}
                onFacebookTabChange={(tab, kwType, value) =>
                  setFacebookTabLimits((prev) => ({ ...prev, [tab]: { ...prev[tab], [kwType]: value } }))
                }
              />
            </div>

            <button
              onClick={saveConfig}
              disabled={saving || !idInput.trim()}
              className="btn-cyber-primary"
              style={{ marginTop: "16px", display: "inline-flex", alignItems: "center", justifyContent: "center", gap: "8px" }}
            >
              {saving ? "Creating Client…" : (
                <>
                  <SaveIcon size={15} /> Save & Create Client
                </>
              )}
            </button>
          </div>
        ) : !activeClient ? (
          /* NO CLIENT SELECTED EMPTY STATE */
          <div
            className="dashboard-card-box"
            style={{
              background: "var(--bg-card)",
              padding: "60px 24px",
              textAlign: "center",
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: "12px",
            }}
          >
            <BuildingIcon size={48} color="var(--cyan)" />
            <div style={{ fontSize: "18px", fontWeight: 800, color: "var(--text-main)" }}>
              Select or Create a Client
            </div>
            <div style={{ fontSize: "13px", color: "var(--text-dim)", maxWidth: "420px" }}>
              Choose a client from the sidebar directory on the left or click <strong>+ New</strong> to set up monitoring for a new brand.
            </div>
            <button
              type="button"
              className="btn-cyber-primary"
              style={{ width: "auto", padding: "10px 24px", marginTop: "12px", display: "inline-flex", alignItems: "center", gap: "8px" }}
              onClick={() => switchToCreate()}
            >
              <PlusIcon size={14} /> Create New Client
            </button>
          </div>
        ) : (
          /* ACTIVE CLIENT DETAIL WORKSPACE */
          <>
            {/* HERO HEADER */}
            <div className="client-hero-header-card">
              <div className="client-hero-left">
                <div className="client-hero-avatar">
                  {(activeClient.name || activeClient.client_id).charAt(0).toUpperCase()}
                </div>
                <div className="client-hero-title-group">
                  <div className="client-hero-name">{activeClient.name || activeClient.client_id}</div>
                  <div className="client-hero-meta-row">
                    <span className="client-hero-id">{activeClient.client_id}</span>
                    {activeClient.domain && (
                      <span className="client-hero-domain" style={{ display: "inline-flex", alignItems: "center", gap: "4px" }}>
                        <GlobeIcon size={12} /> {activeClient.domain}
                      </span>
                    )}
                    {/* Not a blanket green "Active": a client with no
                        keywords has nothing any sweep can search. */}
                    {activeKeywordCount ? (
                      <span className="status-dot-badge">
                        <span className="status-dot" /> Configured
                      </span>
                    ) : (
                      <span
                        className="status-dot-badge"
                        style={{ color: "var(--warning, #F59E0B)", borderColor: "rgba(245, 158, 11, 0.4)", background: "rgba(245, 158, 11, 0.1)" }}
                        title="This client has no keywords yet -- add some on the Keywords tab"
                      >
                        <span className="status-dot" style={{ background: "var(--warning, #F59E0B)", boxShadow: "none" }} /> Needs setup
                      </span>
                    )}
                  </div>
                </div>
              </div>

              <div className="client-hero-actions">
                <button
                  type="button"
                  className="client-hero-btn"
                  onClick={startEditing}
                  title="Edit client configuration"
                  style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}
                >
                  <EditIcon size={13} /> Edit
                </button>
                <button
                  type="button"
                  className="client-hero-btn"
                  onClick={() => void cloneClient(activeClient)}
                  title="Duplicate configuration"
                  style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}
                >
                  <CloneIcon size={13} /> Clone
                </button>
                <button
                  type="button"
                  className="client-hero-btn danger"
                  onClick={handleDelete}
                  disabled={deleting}
                  title="Permanently delete client"
                >
                  {deleting ? "Deleting…" : <TrashIcon size={14} />}
                </button>
              </div>
            </div>

            {/* WORKSPACE TABS NAV */}
            <div className="client-workspace-nav">
              <button
                type="button"
                className={`client-workspace-tab-btn ${activeWorkspaceTab === "overview" ? "active" : ""}`}
                onClick={() => setActiveWorkspaceTab("overview")}
              >
                <ZapIcon size={15} />
                <span>Run & Overview</span>
              </button>

              <button
                type="button"
                className={`client-workspace-tab-btn ${activeWorkspaceTab === "keywords" ? "active" : ""}`}
                onClick={() => setActiveWorkspaceTab("keywords")}
              >
                <TagIcon size={15} />
                <span>Keywords & Assets</span>
                <span className="workspace-tab-counter">{activeKeywordCount}</span>
              </button>

              <button
                type="button"
                className={`client-workspace-tab-btn ${activeWorkspaceTab === "limits" ? "active" : ""}`}
                onClick={() => setActiveWorkspaceTab("limits")}
              >
                <TargetIcon size={15} />
                <span>Scraping Limits</span>
              </button>

              <button
                type="button"
                className={`client-workspace-tab-btn ${activeWorkspaceTab === "settings" ? "active" : ""}`}
                onClick={() => setActiveWorkspaceTab("settings")}
              >
                <SettingsGearIcon size={15} />
                <span>Client Settings</span>
              </button>
            </div>

            {/* UNSAVED CHANGES -- shown on every tab, so edits made on one
                tab are never forgotten from another. They are kept in
                storage until saved or discarded. */}
            {hasPending && (
              <div
                role="status"
                style={{
                  display: "flex", alignItems: "center", justifyContent: "space-between", gap: "12px",
                  flexWrap: "wrap", padding: "10px 14px", marginBottom: "12px", borderRadius: "8px",
                  background: "rgba(245, 158, 11, 0.10)", border: "1px solid rgba(245, 158, 11, 0.35)",
                  fontSize: "12px", color: "var(--text-main)",
                }}
              >
                <span style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                  <AlertTriangleIcon size={14} color="#F59E0B" />
                  Unsaved {pendingChanges.join(", ")} changes. They are kept if you switch tabs,
                  but Discover only searches what is saved.
                </span>
                <span style={{ display: "inline-flex", gap: "8px" }}>
                  <button
                    type="button"
                    className="action-btn"
                    onClick={() => void discardChanges()}
                    disabled={saving}
                    style={{
                      background: "rgba(255, 255, 255, 0.06)", color: "var(--text-main)",
                      border: "1px solid var(--border-subtle)", borderRadius: "6px", padding: "5px 12px",
                    }}
                  >
                    Discard
                  </button>
                  <button
                    type="button"
                    className="btn-cyber-primary"
                    onClick={() => void saveConfig()}
                    disabled={saving}
                    style={{ width: "auto", padding: "5px 14px", margin: 0, display: "inline-flex", alignItems: "center", gap: "6px" }}
                  >
                    <SaveIcon size={13} /> {saving ? "Saving…" : "Save all"}
                  </button>
                </span>
              </div>
            )}

            {/* TAB CONTENT 1: RUN & OVERVIEW */}
            {activeWorkspaceTab === "overview" && (
              <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
                {/* UNIFIED COMMAND RUNNER */}
                <div className="unified-runner-card">
                  <div className="unified-platform-selector">
                    <button
                      type="button"
                      className={`unified-platform-btn ${targetPlatforms.size === 0 ? "active" : ""}`}
                      onClick={selectAllPlatforms}
                      title="Run on every platform with a ready session"
                    >
                      <CyberGlobeIcon size={15} color={targetPlatforms.size === 0 ? "#7C5CFF" : "#94A3B8"} />
                      <span>All Platforms</span>
                    </button>
                    {platforms.map((p) => {
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
                          className={`unified-platform-btn ${targetPlatforms.has(p.platform) ? "active" : ""}`}
                          onClick={() => togglePlatform(p.platform)}
                          title={`${targetPlatforms.has(p.platform) ? "Click to remove" : "Click to add"} ${p.name} `
                            + `(Session: ${p.session_state}) -- pick as many as you like`}
                        >
                          <PlatformIcon platform={p.platform} size={15} />
                          <span>{p.name}</span>
                          <span className={`runner-session-dot ${dotClass}`} />
                        </button>
                      );
                    })}
                  </div>

                  {/* WHICH KEYWORDS to sweep -- the same interaction as the
                      platform chips above: toggle them on, and selecting
                      NONE means all of them. Scopes discovery only; analysis
                      re-reads whatever discovery already stored, so it has no
                      keyword scope of its own to set. */}
                  <div className="unified-platform-selector" style={{ marginTop: "8px" }}>
                    <button
                      type="button"
                      className={`unified-platform-btn ${sweepKeywordTypes.size === 0 ? "active" : ""}`}
                      onClick={selectAllKeywordTypes}
                      title="Search both individual names and domain keywords"
                    >
                      <LayersIcon size={15} color={sweepKeywordTypes.size === 0 ? "#7C5CFF" : "#94A3B8"} />
                      <span>All Keywords</span>
                      <span className="kw-tab-count">{activeKeywordCount}</span>
                    </button>
                    {KEYWORD_SCOPES.map((opt) => {
                      const count = opt.id === "individual" ? activeIndividualCount : activeDomainCount;
                      const on = sweepKeywordTypes.has(opt.id);
                      return (
                        <button
                          key={opt.id}
                          type="button"
                          className={`unified-platform-btn ${on ? "active" : ""}`}
                          onClick={() => toggleKeywordType(opt.id)}
                          disabled={count === 0}
                          title={count === 0
                            ? `This client has no ${opt.label.toLowerCase()} configured`
                            : `${on ? "Click to remove" : "Click to add"} ${opt.label} -- selecting none searches everything`}
                        >
                          {opt.id === "individual"
                            ? <UserIcon size={15} color={on ? "#7C5CFF" : "#94A3B8"} />
                            : <TagIcon size={15} color={on ? "#7C5CFF" : "#94A3B8"} />}
                          <span>{opt.label}</span>
                          <span className="kw-tab-count">{count}</span>
                        </button>
                      );
                    })}
                  </div>

                  {/* WHICH FACEBOOK TABS -- People, Pages, Groups. Same
                      interaction as the two rows above: toggle them on, and
                      selecting NONE means all of them. Hidden when Facebook
                      is not in scope, since it can change nothing there. */}
                  {facebookInScope && (
                    <div className="unified-platform-selector" style={{ marginTop: "8px" }}>
                      <button
                        type="button"
                        className={`unified-platform-btn ${sweepFbTabs.size === 0 ? "active" : ""}`}
                        onClick={selectAllFbTabs}
                        title="Sweep all three Facebook result tabs"
                      >
                        <PlatformIcon platform="facebook" size={15} />
                        <span>All FB Tabs</span>
                      </button>
                      {FB_TAB_CHIPS.map((opt) => {
                        const on = sweepFbTabs.has(opt.id);
                        return (
                          <button
                            key={opt.id}
                            type="button"
                            className={`unified-platform-btn ${on ? "active" : ""}`}
                            onClick={() => toggleFbTab(opt.id)}
                            title={`${on ? "Click to remove" : "Click to add"} the Facebook `
                              + `${opt.label} tab -- selecting none sweeps all three`}
                          >
                            <span aria-hidden style={{ fontSize: "13px" }}>{opt.icon}</span>
                            <span>{opt.label}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}

                  {/* Per-sweep time safety net -- only bites a keyword that
                      is still finding new results when the clock runs out;
                      one that already reached a real end stops on its own
                      well before this. Blank = server default (15 min). */}
                  <div style={{ display: "flex", alignItems: "center", gap: "8px", marginTop: "10px" }}>
                    <label
                      htmlFor="sweep-max-minutes"
                      style={{ fontSize: "12px", color: "var(--text-dim)", display: "flex", alignItems: "center", gap: "6px" }}
                      title="Per-sweep safety cap -- only matters for a keyword still finding new results when time runs out. One that already finished stops immediately regardless of this."
                    >
                      <ClockIcon size={13} color="var(--text-dim)" />
                      Sweep time budget (minutes)
                    </label>
                    <input
                      id="sweep-max-minutes"
                      type="number"
                      min={1}
                      step={1}
                      placeholder="15 (default)"
                      value={sweepMaxMinutes}
                      onChange={(e) => setSweepMaxMinutes(e.target.value)}
                      style={{
                        width: "110px", padding: "6px 10px", fontSize: "12px",
                        background: "var(--bg-input, rgba(255,255,255,0.04))",
                        border: "1px solid var(--border-subtle, rgba(255,255,255,0.12))",
                        borderRadius: "6px", color: "var(--text-main)",
                      }}
                    />
                  </div>

                  {/* Discover/Analyse stay available while something is
                      already in flight -- including a sweep the round-robin
                      scheduler started for this client, which the app adopts
                      and reports as `busy`. Replacing the action button with
                      Stop (as this used to) meant an operator simply could
                      not queue a manual run for as long as the engine held
                      the client, with no way to tell the two apart. The
                      backend serialises the two safely on its per-platform
                      locks, so the new run just queues behind the running
                      one -- which is what the hint below says. */}
                  <div className="runner-actions-grid">
                    <div className="runner-action-cell">
                      <button
                        type="button"
                        className="runner-btn-primary"
                        disabled={!activeKeywordCount || !!starting}
                        onClick={() => void handleSearch()}
                        title={
                          busy
                            ? "Queue another discovery sweep -- it starts when the running one finishes"
                            : "Run a discovery sweep now"
                        }
                      >
                        <DiscoverIcon size={17} color="#fff" />
                        <span>
                          {(() => {
                            const bits = [
                              sweepPlatformName,
                              sweepKeywordScope === "individual" ? "Individual"
                                : sweepKeywordScope === "domain" ? "Domain" : "",
                            ].filter(Boolean);
                            if (starting === "discover") return "Starting…";
                            return bits.length ? `Discover (${bits.join(" · ")})` : "Discover";
                          })()}
                        </span>
                      </button>
                      {busy && onStopDiscovery && (
                        <button
                          type="button"
                          className="runner-btn-stop"
                          onClick={onStopDiscovery}
                          disabled={stoppingDiscovery}
                          title="Abort the discovery sweep that is running now"
                        >
                          <StopIcon size={17} color="#fff" />
                          <span>{stoppingDiscovery ? "Stopping..." : "Stop Discovery"}</span>
                        </button>
                      )}
                    </div>

                    <div className="runner-action-cell">
                      <button
                        type="button"
                        className="runner-btn-secondary"
                        disabled={!!starting}
                        onClick={() => void handleRunAnalysis()}
                        title="Analyse this client's currently validated profiles now -- results are memory-only, shown on the Analysis tab"
                      >
                        <AnalyseIcon size={17} color="#9A50E9" />
                        <span>
                          {starting === "analyse"
                            ? "Starting…"
                            : analysisPlatformName
                              ? `Analyse (${analysisPlatformName})`
                              : "Analyse"}
                        </span>
                      </button>
                    </div>
                  </div>

                  {busy && (
                    <div className="runner-queue-hint">
                      A run is already in flight for this client (it may be the
                      scheduler&rsquo;s). Starting another queues it behind the
                      current one rather than running both at once.
                    </div>
                  )}
                </div>

                {/* QUICK STATS METRIC GRID */}
                <div className="client-quick-stats-grid">
                  <div className="client-quick-stat-box">
                    <span className="quick-stat-label">Executive Names</span>
                    <span className="quick-stat-value">{activeClient.name_keywords?.length || 0}</span>
                    <span className="quick-stat-sub">Individual keywords</span>
                  </div>

                  <div className="client-quick-stat-box">
                    <span className="quick-stat-label">Brand Domains</span>
                    <span className="quick-stat-value">{activeClient.domain_keywords?.length || 0}</span>
                    <span className="quick-stat-sub">Brand keywords</span>
                  </div>

                  <div className="client-quick-stat-box">
                    <span className="quick-stat-label">Active Limits</span>
                    <span className="quick-stat-value">
                      {new Set([
                        ...Object.keys(activeClient.platform_limits_individual || {}),
                        ...Object.keys(activeClient.platform_limits_domain || {}),
                      ]).size}
                    </span>
                    <span className="quick-stat-sub">Capped platforms</span>
                  </div>

                  {/* FROM THE CLIENT'S OWN DATA. This was a hard-coded green
                      "● Active / Continuous protection" on every client --
                      including one with no keywords, which no sweep can
                      search anything for. Nothing here knows whether a
                      schedule is really running, so a set-up client says
                      what is true (keywords saved), not "protected". */}
                  <div className="client-quick-stat-box">
                    <span className="quick-stat-label">Monitoring</span>
                    {activeKeywordCount ? (
                      <>
                        <span className="quick-stat-value" style={{ fontSize: "15px", marginTop: "4px", color: "var(--text-main)" }}>
                          ● Configured
                        </span>
                        <span className="quick-stat-sub">
                          {activeKeywordCount} keyword{activeKeywordCount === 1 ? "" : "s"} saved to search
                        </span>
                      </>
                    ) : (
                      <>
                        <span className="quick-stat-value" style={{ fontSize: "15px", marginTop: "4px", color: "var(--warning, #F59E0B)" }}>
                          ● Needs setup
                        </span>
                        <span className="quick-stat-sub">No keywords -- nothing can be searched</span>
                      </>
                    )}
                  </div>
                </div>
              </div>
            )}

            {/* TAB CONTENT 2: KEYWORDS & ASSETS */}
            {activeWorkspaceTab === "keywords" && (
              <div className="dashboard-card-box" style={{ background: "var(--bg-card)", padding: "20px" }}>
                <KeywordTabs
                  activeTab={activeTab}
                  onTab={setActiveTab}
                  nameKeywords={nameKeywords}
                  domainKeywords={domainKeywords}
                  nameGroups={nameGroups}
                  domainGroups={domainGroups}
                  onNameGroups={setNameGroups}
                  onDomainGroups={setDomainGroups}
                  platforms={platforms}
                  clientId={activeClient?.client_id || ""}
                />

                <div style={{ marginTop: "20px", display: "flex", justifyContent: "flex-end" }}>
                  <button
                    onClick={saveConfig}
                    disabled={saving}
                    className="btn-cyber-primary"
                    style={{ width: "auto", padding: "10px 24px", margin: 0, display: "inline-flex", alignItems: "center", gap: "6px" }}
                  >
                    {saving ? "Saving…" : saved ? "✓ Saved" : (
                      <>
                        <SaveIcon size={14} /> Save Keyword Changes
                      </>
                    )}
                  </button>
                </div>
              </div>
            )}

            {/* TAB CONTENT 3: SCRAPING LIMITS */}
            {activeWorkspaceTab === "limits" && (
              <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
                <PlatformLimitsEditor
                  platforms={platforms}
                  emptyText={platformsEmptyText}
                  individualLimits={platformLimitsIndividual}
                  domainLimits={platformLimitsDomain}
                  onIndividualChange={(platform, value) => setPlatformLimitsIndividual((prev) => ({ ...prev, [platform]: value }))}
                  onDomainChange={(platform, value) => setPlatformLimitsDomain((prev) => ({ ...prev, [platform]: value }))}
                  facebookTabLimits={facebookTabLimits}
                  onFacebookTabChange={(tab, kwType, value) =>
                    setFacebookTabLimits((prev) => ({ ...prev, [tab]: { ...prev[tab], [kwType]: value } }))
                  }
                />

                <div style={{ display: "flex", justifyContent: "flex-end" }}>
                  <button
                    onClick={saveConfig}
                    disabled={saving}
                    className="btn-cyber-primary"
                    style={{ width: "auto", padding: "10px 24px", margin: 0, display: "inline-flex", alignItems: "center", gap: "6px" }}
                  >
                    {saving ? "Saving…" : saved ? "✓ Saved" : (
                      <>
                        <SaveIcon size={14} /> Save Scrape Limits
                      </>
                    )}
                  </button>
                </div>
              </div>
            )}

            {/* TAB CONTENT 4: SETTINGS */}
            {activeWorkspaceTab === "settings" && (
              <div className="dashboard-card-box" style={{ background: "var(--bg-card)", padding: "24px", display: "flex", flexDirection: "column", gap: "20px" }}>
                <div>
                  <div style={{ fontSize: "15px", fontWeight: 700, color: "var(--text-main)", marginBottom: "4px", display: "flex", alignItems: "center", gap: "8px" }}>
                    <BuildingIcon size={16} color="var(--cyan)" />
                    <span>Client Information</span>
                  </div>
                  <div style={{ fontSize: "12px", color: "var(--text-dim)", marginBottom: "12px" }}>
                    Update organization display name, associated domain, and identifier.
                  </div>
                  <div className="client-setup-box" style={{ flexWrap: "wrap", margin: 0 }}>
                    <input
                      value={idInput}
                      onChange={(e) => setIdInput(e.target.value)}
                      placeholder="org id…"
                      disabled={true}
                      className="client-select-input"
                      style={{ opacity: 0.6 }}
                      title="Organization ID cannot be modified after creation"
                    />
                    <input
                      value={nameInput}
                      onChange={(e) => setNameInput(e.target.value)}
                      placeholder="organization name…"
                      className="client-select-input"
                    />
                    <input
                      value={domainInput}
                      onChange={(e) => setDomainInput(e.target.value)}
                      placeholder="domain, e.g. xyz.com…"
                      className="client-select-input"
                    />
                  </div>
                </div>

                <div style={{ display: "flex", justifyContent: "flex-end", gap: "10px", borderTop: "1px solid var(--border-subtle)", paddingTop: "16px" }}>
                  {editing && (
                    <button
                      type="button"
                      className="action-btn"
                      onClick={cancelEditing}
                      style={{
                        background: "rgba(255, 255, 255, 0.06)",
                        color: "var(--text-main)",
                        border: "1px solid var(--border-subtle)",
                        borderRadius: "8px",
                        padding: "8px 16px",
                      }}
                    >
                      Cancel
                    </button>
                  )}
                  <button
                    onClick={saveConfig}
                    disabled={saving}
                    className="btn-cyber-primary"
                    style={{ width: "auto", padding: "10px 24px", margin: 0, display: "inline-flex", alignItems: "center", gap: "6px" }}
                  >
                    {saving ? "Saving…" : saved ? "✓ Saved" : (
                      <>
                        <SaveIcon size={14} /> Save Changes
                      </>
                    )}
                  </button>
                </div>

                <div style={{ borderTop: "1px solid rgba(239, 68, 68, 0.2)", paddingTop: "18px", marginTop: "10px" }}>
                  <div style={{ fontSize: "14px", fontWeight: 700, color: "var(--danger)", marginBottom: "4px", display: "flex", alignItems: "center", gap: "6px" }}>
                    <AlertTriangleIcon size={16} color="var(--danger)" />
                    <span>Danger Zone</span>
                  </div>
                  <div style={{ fontSize: "12px", color: "var(--text-dim)", marginBottom: "12px" }}>
                    {/* Says what DELETE /clients/{id} actually does (backend/api/clients.py
                        delete_client). This used to promise a cascade through discovery
                        hits, validated profiles and incidents that nothing performs. */}
                    Permanently delete this organization&rsquo;s saved setup -- its keywords, scrape
                    caps, cron and reference logos -- for everyone. Discovery profiles already found
                    under this org id are NOT deleted: they stay in the database and reappear if the
                    client is re-created with the same org id.
                  </div>
                  <button
                    onClick={handleDelete}
                    disabled={deleting}
                    className="danger-link-btn"
                    style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}
                  >
                    {deleting ? "Deleting Organization…" : (
                      <>
                        <TrashIcon size={14} /> Delete Client
                      </>
                    )}
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
