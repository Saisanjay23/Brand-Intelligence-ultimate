import { useCallback, useEffect, useMemo, useState, Fragment } from "react";
import { toast } from "react-hot-toast";
import { clientsApi } from "../api/clientsApi";
import { discoveryApi } from "../api/discoveryApi";
import type { Client, KeywordGroup, PlatformState } from "../api/types";
import {
  mergeGeneratedChildren,
  parseBulkKeywordGroups,
  mergeBulkKeywordGroups,
} from "../services/keywordGroups";
import { PlatformIcon } from "../components/PlatformIcon";
import { GlobalSearchModal } from "../components/GlobalSearchModal";
import { confirmAction } from "../utils/confirmAction";
import { saveClientKeywords } from "../services/clientKeywords";
import { listSavedClients, saveClientLocally, deleteClientLocally } from "../services/savedClients";
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
} from "../components/AppIcons";

type KeywordTab = "names" | "domain";
type Mode = "select" | "create";
type WorkspaceTab = "overview" | "keywords" | "limits" | "settings";

type FacebookTab = "people" | "pages" | "groups";
type FacebookTabLimits = Record<FacebookTab, { individual: string; domain: string }>;

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
}

function splitKeywordList(raw: string): string[] {
  return raw
    .split(/[,\n]/)
    .map((s) => s.trim())
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
}: {
  chips: string[];
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

  const commit = () => {
    const trimmed = input.trim();
    if (trimmed) {
      if (chips.some((c) => c.toLowerCase() === trimmed.toLowerCase())) {
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
    const seen = new Set(chips.map((c) => c.toLowerCase()));
    const fresh: string[] = [];
    let dupCount = 0;
    for (const kw of items) {
      const key = kw.toLowerCase();
      if (seen.has(key)) {
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
                      background: "rgba(0, 229, 255, 0.06)",
                      border: "1px solid rgba(0, 229, 255, 0.15)",
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
}: {
  groups: KeywordGroup[];
  onChange: (next: KeywordGroup[]) => void;
  parentPlaceholder: string;
  childPlaceholder: string;
  accent: string;
  disabled?: boolean;
}) {
  const [parentInput, setParentInput] = useState("");
  const [childInput, setChildInput] = useState("");

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
    const pTrimmed = parentInput.trim().replace(/^,+|,+$/g, "");
    const cTrimmed = childInput.trim();

    if (!pTrimmed && !cTrimmed) return;

    // If input contains bulk separator or multiple lines, apply bulk parser
    if (/[,\n]/.test(pTrimmed) || pTrimmed.includes(":") || pTrimmed.includes("->") || pTrimmed.includes("=>")) {
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
      ? cTrimmed.split(/[,;\n]+/).map((s) => s.trim()).filter(Boolean)
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

  const totalSearchTerms = groups.reduce(
    (n, g) => n + (g.children.length ? g.children.length + 1 : 1), 0,
  );

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
                      if (/[,\n]/.test(text) || text.includes(":") || text.includes("->")) {
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
                    if (text.includes(":") || text.includes("->") || text.includes("\n")) {
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
}) {
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
            background: "linear-gradient(135deg, rgba(0, 229, 255, 0.15), rgba(136, 56, 221, 0.15))",
            border: "1px solid rgba(0, 229, 255, 0.4)",
            color: "var(--cyan, #00E5FF)",
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

      <div style={{ display: activeTab === "names" ? "block" : "none" }}>
        <KeywordGroupEditor
          groups={nameGroups}
          onChange={onNameGroups}
          parentPlaceholder="Type an executive/individual name and press Enter…"
          childPlaceholder="Add a search term for this name and press Enter…"
          accent="var(--cyan, #00E5FF)"
          disabled={disabled}
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
}: {
  platforms: PlatformState[];
  individualLimits: Record<string, string>;
  domainLimits: Record<string, string>;
  onIndividualChange: (platform: string, value: string) => void;
  onDomainChange: (platform: string, value: string) => void;
  facebookTabLimits: FacebookTabLimits;
  onFacebookTabChange: (tab: FacebookTab, kwType: "individual" | "domain", value: string) => void;
  disabled?: boolean;
}) {
  const [fbExpanded, setFbExpanded] = useState(false);

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
                            background: "rgba(0, 229, 255, 0.12)",
                            border: "1px solid rgba(0, 229, 255, 0.3)",
                            color: "var(--cyan)",
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
                No platforms registered yet.
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
}: Props) {
  const [clients, setClients] = useState<Client[]>([]);
  const [loadingClients, setLoadingClients] = useState(true);
  const [mode, setMode] = useState<Mode>(clientId ? "select" : "create");
  const [activeWorkspaceTab, setActiveWorkspaceTab] = useState<WorkspaceTab>("overview");
  const [globalSearchOpen, setGlobalSearchOpen] = useState(false);
  const [sidebarFilter, setSidebarFilter] = useState<"all" | "active" | "empty">("all");
  const [sidebarSearch, setSidebarSearch] = useState("");

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setGlobalSearchOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const [editing, setEditing] = useState(false);
  const [activeClient, setActiveClient] = useState<Client | null>(null);

  const [idInput, setIdInput] = useState(EMPTY_FORM.id);
  const [nameInput, setNameInput] = useState(EMPTY_FORM.name);
  const [domainInput, setDomainInput] = useState(EMPTY_FORM.domain);

  // Parent/child groups are the SOURCE OF TRUTH here; the flat parent
  // lists below are derived from them, never edited independently, so the
  // form physically cannot produce a client whose groups and flat keywords
  // disagree (the server re-derives them again on save for the same
  // reason). See backend/shared/keywords.py.
  const [nameGroups, setNameGroups] = useState<KeywordGroup[]>([]);
  const [domainGroups, setDomainGroups] = useState<KeywordGroup[]>([]);
  const nameKeywords = useMemo(() => nameGroups.map((g) => g.parent), [nameGroups]);
  const domainKeywords = useMemo(() => domainGroups.map((g) => g.parent), [domainGroups]);

  const [platformLimitsIndividual, setPlatformLimitsIndividual] = useState<Record<string, string>>({});
  const [platformLimitsDomain, setPlatformLimitsDomain] = useState<Record<string, string>>({});
  const [facebookTabLimits, setFacebookTabLimits] = useState<FacebookTabLimits>({
    people: { individual: "", domain: "" },
    pages: { individual: "", domain: "" },
    groups: { individual: "", domain: "" },
  });
  const [cron, setCron] = useState(EMPTY_FORM.cron);
  const [activeTab, setActiveTab] = useState<KeywordTab>("names");

  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // A SET, not a single id: the Run hub lets an analyst pick any
  // combination of platforms. Empty means "every ready platform", which
  // is what the All Platforms chip selects and what the backend does when
  // no scope is sent -- so the previous behaviour is the empty case.
  const [sweepPlatforms, setSweepPlatforms] = useState<Set<string>>(new Set());
  const [analysisPlatforms, setAnalysisPlatforms] = useState<Set<string>>(new Set());

  const refreshClients = useCallback(() => {
    setLoadingClients(true);
    clientsApi
      .listClients()
      // GET /clients has no backend on the rebuilt API -- this always
      // 404s in that configuration, which is expected (see saveConfig's
      // local-only fallback). Locally-saved clients (savedClients.ts) are
      // what actually populates the directory today; merged in regardless
      // of whether the API call above succeeded, so a real future backend
      // and this browser's own saved list both show up rather than one
      // silently hiding the other.
      .then((res) => res.items)
      .catch(() => [] as Client[])
      .then((serverClients) => {
        const local = listSavedClients();
        const byId = new Map(serverClients.map((c) => [c.client_id, c]));
        for (const c of local) if (!byId.has(c.client_id)) byId.set(c.client_id, c);
        setClients([...byId.values()]);
      })
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
    const fbTabs = c.platform_tab_limits?.facebook || {};
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
    setFacebookTabLimits({
      people: readTab(fbTabs.people),
      pages: readTab(fbTabs.pages),
      groups: readTab(fbTabs.groups),
    });
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

  useEffect(() => {
    if (!clientId || activeClient || !clients.length) return;
    const existing = clients.find((c) => c.client_id === clientId);
    if (existing) {
      setActiveClient(existing);
      loadIntoForm(existing);
      setMode("select");
      setEditing(false);
    }
  }, [clientId, clients]);

  const switchToCreate = () => {
    setMode("create");
    setActiveClient(null);
    setEditing(false);
    clearForm();
    setSweepPlatforms(new Set());
    setAnalysisPlatforms(new Set());
    setActiveWorkspaceTab("overview");
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
    setActiveClient(c);
    loadIntoForm(c);
    setMode("select");
    setEditing(false);
    onClient(c.client_id, c.name);
  };

  const startEditing = () => {
    if (!activeClient) return;
    loadIntoForm(activeClient);
    setEditing(true);
    setActiveWorkspaceTab("settings");
  };

  const cancelEditing = () => {
    if (activeClient) loadIntoForm(activeClient);
    setEditing(false);
  };

  const cloneClient = (c: Client) => {
    switchToCreate();
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
    if (!id) {
      onError("Enter an org id first.");
      return null;
    }
    setSaving(true);
    setSaved(false);
    try {
      const parseLimits = (raws: Record<string, string>): Record<string, number> => {
        const out: Record<string, number> = {};
        for (const [platform, raw] of Object.entries(raws)) {
          const n = Number(raw);
          if (raw.trim() && Number.isFinite(n) && n > 0) out[platform] = Math.floor(n);
        }
        return out;
      };
      const parsedLimitsIndividual = parseLimits(platformLimitsIndividual);
      const parsedLimitsDomain = parseLimits(platformLimitsDomain);
      const fbTabLimits: Record<string, Record<string, number>> = {};
      for (const [tab, byType] of Object.entries(facebookTabLimits)) {
        const perType: Record<string, number> = {};
        for (const [kwType, raw] of Object.entries(byType)) {
          const n = Number(raw);
          if (raw.trim() && Number.isFinite(n) && n > 0) perType[kwType] = Math.floor(n);
        }
        if (Object.keys(perType).length) fbTabLimits[tab] = perType;
      }
      const upsertBody: Parameters<typeof clientsApi.upsertClient>[0] = {
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
      let client: Client;
      let persisted = true;
      try {
        client = await clientsApi.upsertClient(upsertBody);
      } catch {
        // POST /clients has no backend on the rebuilt API (only
        // /discovery, /analysis, /sessions survive) -- fall back to a
        // browser-local client so the actual point of this form (giving
        // Discover something with a group id + keywords to sweep with)
        // still works. Persisted to localStorage (savedClients.ts), not
        // just this render's state, so it survives a page reload instead
        // of vanishing the moment "Clients Directory" refetches.
        persisted = false;
        client = { ...upsertBody, domain: upsertBody.domain || "" } as Client;
        saveClientLocally(client);
        // GET /clients will keep coming back empty (no backend), so the
        // sidebar list would otherwise never reflect what's actually
        // usable right now -- merge this local client into it directly.
        setClients((prev) => [client, ...prev.filter((c) => c.client_id !== client.client_id)]);
      }
      setActiveClient(client);
      // The "Individual + Domain" filter on Live Results reads this back
      // to classify each profile's own keywords[] -- see clientKeywords.ts.
      saveClientKeywords(client.client_id, nameKeywords, domainKeywords);
      setMode("select");
      setEditing(false);
      onClient(client.client_id, client.name);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      if (persisted) {
        toast.success(`Client "${client.name}" saved!`, { icon: "💾" });
      } else {
        toast(`"${client.name}" is local-only for this session -- no /clients backend to persist it to.`, { icon: "⚠️" });
      }
      refreshClients();
      return client;
    } catch (e) {
      onError((e as Error).message);
      return null;
    } finally {
      setSaving(false);
    }
  };

  const handleSearch = async () => {
    if (!activeClient) return;
    if (!activeKeywordCount) {
      onError("This client has no keywords yet — head to the Keywords tab to add executive names or brand keywords.");
      return;
    }
    // The server rejects this too (POST /discovery/jobs requires at least
    // one non-empty individual_keywords/domain_keywords entry rather than
    // sweeping nothing and reporting success), but catching it here means
    // the analyst finds out on click instead of via a failed job.
    if (sweepKeywordScope === "individual" && !activeIndividualCount) {
      onError("This client has no individual names configured — pick All Keywords or Domain, or add some in the Keywords tab.");
      return;
    }
    if (sweepKeywordScope === "domain" && !activeDomainCount) {
      onError("This client has no domain keywords configured — pick All Keywords or Individual, or add some in the Keywords tab.");
      return;
    }
    try {
      const scope = platformScope(sweepPlatforms);
      // sweepKeywordScope narrows which TYPE actually gets swept -- sending
      // an empty list for the excluded type rather than filtering after the
      // fact, so the caps below (platform_limits_individual/_domain) only
      // ever apply to keywords that are genuinely part of this sweep.
      const res = await discoveryApi.startDiscovery({
        group_id: activeClient.client_id,
        individual_keywords: sweepKeywordScope === "domain"
          ? [] : dedupeKeywordsCaseInsensitive(activeClient.name_keywords || []),
        domain_keywords: sweepKeywordScope === "individual"
          ? [] : dedupeKeywordsCaseInsensitive(activeClient.domain_keywords || []),
        platforms: scope.platforms || (scope.platform ? [scope.platform] : undefined),
        platform_limits_individual: activeClient.platform_limits_individual,
        platform_limits_domain: activeClient.platform_limits_domain,
        platform_tab_limits: activeClient.platform_tab_limits,
      });
      if (res.skipped.length) {
        onError(`Skipped: ${res.skipped.map((s) => `${s.value} (${s.reason})`).join(", ")}`);
      }
      onDiscoveryStarted(res.job_id);
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const nameOf = (id: string) =>
    platforms.find((p) => p.platform === id)?.name || id;
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
    const scope = analysisPlatformName ? `on ${analysisPlatformName}` : "across every platform";
    if (
      !(await confirmAction(
        `Analyse every currently validated profile of "${activeClient.name || activeClient.client_id}" ${scope}? This re-scrapes each one again -- results are memory-only, shown on the Analysis tab.`,
      ))
    ) {
      return;
    }
    try {
      const platformFilter = analysisPlatforms.size === 1 ? [...analysisPlatforms][0] : undefined;
      const res = await discoveryApi.analyseValidated({
        group_id: activeClient.client_id,
        platform: platformFilter,
        domain: activeClient.domain,
      });
      onAnalyseStarted(res.job_id);
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const handleDelete = async () => {
    if (!activeClient) return;
    const confirmed = await confirmAction(
      `Permanently delete client "${activeClient.name || activeClient.client_id}"? This will delete ALL associated discovery profiles, validated profiles, analyst tags, and incidents. This cannot be undone.`,
    );
    if (!confirmed) return;
    setDeleting(true);
    const deletedId = activeClient.client_id;
    let persisted = true;
    try {
      await clientsApi.deleteClient(deletedId);
    } catch {
      // DELETE /clients/{id} has no backend either -- remove it from this
      // browser's local list regardless, so the UI doesn't get stuck.
      // Any discovery profiles already saved server-side under this
      // group_id are untouched (no route exposes deleting them).
      persisted = false;
    }
    deleteClientLocally(deletedId);
    setClients((prev) => prev.filter((c) => c.client_id !== deletedId));
    onForgetClient(deletedId);
    setActiveClient(null);
    setEditing(false);
    clearForm();
    onClient("", "");
    setDeleting(false);
    toast(persisted ? `Client "${deletedId}" deleted.` : `Removed "${deletedId}" locally -- no /clients backend to delete it from.`, {
      icon: persisted ? "🗑️" : "⚠️",
    });
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

  // The single category to send to the API, or "" for all. Only a
  // selection of exactly one narrows anything -- empty (and, by the
  // collapse above, both) sweeps everything, which is what omitting the
  // parameter already means server-side.
  const sweepKeywordScope: KeywordScope | "" =
    sweepKeywordTypes.size === 1 ? [...sweepKeywordTypes][0] : "";
  return (
    <div className="clients-workspace-layout">
      {globalSearchOpen && (
        <GlobalSearchModal
          clients={clients}
          onSelectClient={(id) => selectSavedClient(id)}
          onClose={() => setGlobalSearchOpen(false)}
        />
      )}

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
            onClick={switchToCreate}
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
          <span className="client-search-shortcut">Ctrl K</span>
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
          {loadingClients ? (
            <div style={{ textAlign: "center", padding: "24px", color: "var(--text-dim)", fontSize: "12px" }}>
              Loading clients...
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
                onClick={() => {
                  if (clients.length) {
                    selectSavedClient(clients[0].client_id);
                  } else {
                    setMode("select");
                  }
                }}
              >
                ✕ Cancel
              </button>
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
                  disabled={busy}
                />
              </div>
            </div>

            <div style={{ marginBottom: "20px" }}>
              <PlatformLimitsEditor
                platforms={platforms}
                individualLimits={platformLimitsIndividual}
                domainLimits={platformLimitsDomain}
                onIndividualChange={(platform, value) => setPlatformLimitsIndividual((prev) => ({ ...prev, [platform]: value }))}
                onDomainChange={(platform, value) => setPlatformLimitsDomain((prev) => ({ ...prev, [platform]: value }))}
                facebookTabLimits={facebookTabLimits}
                onFacebookTabChange={(tab, kwType, value) =>
                  setFacebookTabLimits((prev) => ({ ...prev, [tab]: { ...prev[tab], [kwType]: value } }))
                }
                disabled={busy}
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
              onClick={switchToCreate}
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
                    <span className="status-dot-badge">
                      <span className="status-dot" /> Active
                    </span>
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
                  onClick={() => cloneClient(activeClient)}
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
                        disabled={!activeKeywordCount}
                        onClick={handleSearch}
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
                        onClick={handleRunAnalysis}
                        title="Analyse this client's currently validated profiles now -- results are memory-only, shown on the Analysis tab"
                      >
                        <AnalyseIcon size={17} color="#00F0FF" />
                        <span>
                          {analysisPlatformName
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

                  <div className="client-quick-stat-box">
                    <span className="quick-stat-label">Monitoring</span>
                    <span className="quick-stat-value" style={{ fontSize: "15px", marginTop: "4px", color: "var(--success)" }}>
                      ● Active
                    </span>
                    <span className="quick-stat-sub">Continuous protection</span>
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
                  disabled={busy}
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
                  individualLimits={platformLimitsIndividual}
                  domainLimits={platformLimitsDomain}
                  onIndividualChange={(platform, value) => setPlatformLimitsIndividual((prev) => ({ ...prev, [platform]: value }))}
                  onDomainChange={(platform, value) => setPlatformLimitsDomain((prev) => ({ ...prev, [platform]: value }))}
                  facebookTabLimits={facebookTabLimits}
                  onFacebookTabChange={(tab, kwType, value) =>
                    setFacebookTabLimits((prev) => ({ ...prev, [tab]: { ...prev[tab], [kwType]: value } }))
                  }
                  disabled={busy}
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
                    Permanently delete this organization and cascade remove all associated discovery hits, validated profiles, and incident tickets.
                  </div>
                  <button
                    onClick={handleDelete}
                    disabled={deleting}
                    className="danger-link-btn"
                    style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}
                  >
                    {deleting ? "Deleting Organization…" : (
                      <>
                        <TrashIcon size={14} /> Delete Organization & All Associated Data
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
