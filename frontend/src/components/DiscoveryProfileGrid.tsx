// The discovery triage grid -- three tabs: New Profiles (scraped within the
// last 24h), Old Profiles (still pending, aged past 24h -- moves here
// automatically, no action needed), and Validated Profiles (an analyst
// explicitly validated it, from either New or Old). There is no reject
// action in this UI -- validate is the only triage decision it exposes.
//
// New/Old are both just the `pending` triage status client-side split by
// age; the backend has no age-bucketed status of its own, so both tabs
// share ONE fetch (up to MAX_LIMIT pending rows) and are re-sliced
// client-side rather than each doing its own server-paginated fetch --
// necessary to get an honest count/split by age at all, at the cost of a
// hard cap on how many pending rows this can meaningfully page through.
// Validated keeps true server-side pagination (no age split needed there).
//
// Filters -- keyword+search are server params; match level, entity type,
// and Individual+Domain are applied client-side on whatever's loaded (see
// each one's own comment below for why the backend can't do them).
//
// Export reuses POST /analysis/export/xlsx (backend/api/analysis.py) --
// a generic "rows -> .xlsx" endpoint with no analysis-specific logic in
// it, so it works for discovery rows too. Delete Platform Data calls
// POST /discovery/profiles/delete (backend/api/discovery.py), added this
// session specifically for this button.
import { useCallback, useEffect, useRef, useState } from "react";
import toast from "react-hot-toast";
import { analysisApi } from "../api/analysisApi";
import { discoveryApi } from "../api/discoveryApi";
import type { DiscoveredProfile } from "../api/discoveryApi";
import { getClientKeywords } from "../services/clientKeywords";
import { listSavedClients } from "../services/savedClients";
import { confirmAction } from "../utils/confirmAction";
import { download, rowsToCsv } from "../utils/download";
import { PlatformIcon } from "./PlatformIcon";
import { CloneIcon, GlobeIcon, LayersIcon, TargetIcon, TrashIcon, VerifiedBadgeIcon, ZapIcon } from "./AppIcons";

interface Props {
  groupId: string;
  // Scope to one platform (set by the platform rail above this grid); ""
  // or omitted means every platform.
  platform?: string;
  // Bumped by the parent whenever a fresh sweep completes, to force a reload.
  refreshKey: number;
  onAnalyseStarted: (jobId: string) => void;
}

type Tab = "new" | "old" | "validated";

const PAGE_SIZE_OPTIONS = [25, 50, 100, 200] as const;
// High Match no longer thresholds name_score at all -- see matchLevelOf,
// which gates "high" on name_exact_run (a real contiguous letter-run)
// instead. Medium still does.
const MATCH_MEDIUM_THRESHOLD = 50;
const NEW_WINDOW_MS = 24 * 60 * 60 * 1000;
// How many pending rows the New/Old split is computed over -- the
// backend's own hard ceiling per request (backend/shared/pagination.py's
// MAX_LIMIT). A client with more pending rows than this at once will see
// New/Old counts capped here rather than a true total; Validated has no
// such cap, it pages the normal server-side way.
const PENDING_FETCH_CAP = 1000;

// High Match is gated on `name_exact_run` (a real contiguous letter-run of
// the keyword inside the name -- see backend/shared/text.py::
// contiguous_letters_match), NOT a name_score threshold. name_score is
// word-order-insensitive by design (so "Adani Gautam" scores identically
// to "Gautam Adani"), which means a threshold alone would call a reordered
// name a High Match -- exactly the case an analyst asking for "High Match"
// wants EXCLUDED, since the name doesn't actually read as the keyword.
// Medium/Low fall back to the existing name_score bands for everything
// that doesn't clear the exact-run bar, unchanged.
function matchLevelOf(p: { name_score: number | null; name_exact_run: boolean | null }): "high" | "medium" | "low" {
  if (p.name_exact_run) return "high";
  const score = p.name_score;
  if (score !== null && score >= MATCH_MEDIUM_THRESHOLD) return "medium";
  return "low";
}

function isProfileNew(p: DiscoveredProfile): boolean {
  if (!p.first_seen) return false;
  const t = new Date(p.first_seen).getTime();
  return !isNaN(t) && Date.now() - t < NEW_WINDOW_MS;
}

// Table-row equivalent of the card's badge stack: a validated profile
// that's still inside its 24h window reads "validated + new", not just
// "validated".
function statusLabel(p: DiscoveredProfile): string {
  if (p.status === "validated") return isProfileNew(p) ? "validated + new" : "validated";
  return isProfileNew(p) ? "new" : "old";
}

function exportRow(p: DiscoveredProfile): Record<string, unknown> {
  return {
    Platform: p.platform,
    "Display Name": p.display_name,
    Username: p.username,
    URL: p.url,
    Status: p.status,
    Verified: p.verified ? "Yes" : "No",
    Followers: p.followers ?? "",
    "Match Score": p.name_score ?? "",
    Keywords: p.keywords.join("; "),
    "First Seen": p.first_seen ?? "",
  };
}

function Avatar({ p }: { p: DiscoveredProfile }) {
  const [failed, setFailed] = useState(false);
  const label = p.display_name || p.username || "?";
  if (!p.profile_image_url || failed) {
    return (
      <span className="profile-avatar-circle" style={{ width: 64, height: 64, fontSize: 26, borderRadius: "50%" }}>
        {label.charAt(0).toUpperCase()}
      </span>
    );
  }
  return (
    <img
      src={p.profile_image_url}
      alt=""
      referrerPolicy="no-referrer"
      style={{ width: "100%", height: "100%", objectFit: "cover" }}
      onError={() => setFailed(true)}
    />
  );
}

function ProfileCard({
  p, selected, onToggleSelected, onValidate, onUnvalidate, busy,
}: {
  p: DiscoveredProfile;
  selected: boolean;
  onToggleSelected: (id: string) => void;
  onValidate?: (id: string) => void;
  onUnvalidate?: (id: string) => void;
  busy?: boolean;
}) {
  // Clicking anywhere on the card selects it -- the avatar/name/platform
  // links stop propagation so opening the profile in a new tab doesn't
  // also toggle selection.
  const stop = (e: React.MouseEvent) => e.stopPropagation();
  return (
    <div
      className="profile-card"
      onClick={() => onToggleSelected(p.id)}
      style={{ cursor: "pointer", ...(selected ? { outline: "2px solid var(--cyan)", outlineOffset: "-2px" } : {}) }}
    >
      <div className="profile-card-header">
        <a href={p.url} target="_blank" rel="noreferrer" onClick={stop} style={{ display: "block", width: "100%", height: "100%" }}>
          <Avatar p={p} />
        </a>
        <div style={{ position: "absolute", top: 9, left: 9, display: "flex", alignItems: "center", gap: 5, zIndex: 2 }}>
          <span
            className="card-badge-top-left"
            style={{ position: "static", background: p.status === "validated" ? "rgba(0,193,77,0.85)" : "rgba(154,80,233,0.85)", color: "#fff" }}
          >
            {p.status === "validated" ? "validated" : isProfileNew(p) ? "new" : "old"}
          </span>
          {/* Validating doesn't stop a profile being newly-discovered -- it
              keeps its "new" badge (next to "validated") until the same 24h
              window from first_seen runs out, so an analyst can still tell
              a just-scraped validated profile from an old one. Validating
              never touches first_seen (it's $setOnInsert in
              backend/database/repositories/profile_repository.py), and the
              minute tick below re-renders this once the window lapses. */}
          {p.status === "validated" && isProfileNew(p) && (
            <span
              className="card-badge-top-left"
              style={{ position: "static", background: "rgba(154,80,233,0.85)", color: "#fff" }}
            >
              new
            </span>
          )}
        </div>
        {p.name_score != null && (() => {
          const level = matchLevelOf(p);
          const color = level === "high" ? "rgba(0,193,77,0.85)" : level === "medium" ? "rgba(255,171,0,0.85)" : "rgba(102,112,133,0.85)";
          return (
            <span className="card-badge-top-right" style={{ background: color, color: "#fff" }}>
              {level} match
            </span>
          );
        })()}
        <a href={p.url} target="_blank" rel="noreferrer" onClick={stop} className="card-badge-platform" style={{ textDecoration: "none" }}>
          <PlatformIcon platform={p.platform} size={14} />
          {p.platform}
        </a>
      </div>
      <div className="profile-card-body">
        <div style={{ display: "flex", alignItems: "center", gap: "5px" }}>
          <a href={p.url} target="_blank" rel="noreferrer" onClick={stop} className="profile-display-name" title={p.display_name || p.username}>
            {p.display_name || p.username || p.url}
          </a>
          {p.verified && <VerifiedBadgeIcon size={15} />}
        </div>
        {p.username && <div className="profile-handle">@{p.username}</div>}
        {p.followers != null && (
          <div style={{ fontSize: "12px", color: "var(--text-dim)" }}>{p.followers.toLocaleString()} followers</div>
        )}
        {!!p.keywords.length && (
          <div className="card-keyword-tags">
            {p.keywords.map((kw) => (
              <span key={kw} className="card-keyword-tag">🔑 {kw}</span>
            ))}
          </div>
        )}
        {onValidate && (
          <div className="card-actions-row" style={{ display: "flex", gap: "8px", marginTop: "8px" }}>
            <button className="btn-accept" disabled={busy} onClick={(e) => { stop(e); onValidate(p.id); }}>
              ✅ Validate
            </button>
          </div>
        )}
        {onUnvalidate && (
          <div className="card-actions-row" style={{ display: "flex", gap: "8px", marginTop: "8px" }}>
            <button
              className="btn-reject"
              disabled={busy}
              title="Move back to New/Old -- undo an accidental Validate"
              onClick={(e) => { stop(e); onUnvalidate(p.id); }}
            >
              ↩ Move to Old
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function ProfileTable({
  items, selected, onToggleSelected, onValidate, onUnvalidate, busyId,
}: {
  items: DiscoveredProfile[];
  selected: Set<string>;
  onToggleSelected: (id: string) => void;
  onValidate?: (id: string) => void;
  onUnvalidate?: (id: string) => void;
  busyId?: string | null;
}) {
  const stop = (e: React.MouseEvent) => e.stopPropagation();
  return (
    <div style={{ overflowX: "auto", marginTop: "16px" }}>
      <table className="core_table">
        <thead>
          <tr>
            <th>Platform</th>
            <th>Name</th>
            <th>Status</th>
            <th>Match</th>
            <th>Followers</th>
            <th>Keywords</th>
            {(onValidate || onUnvalidate) && <th>Actions</th>}
          </tr>
        </thead>
        <tbody>
          {items.map((p) => (
            <tr
              key={p.id}
              onClick={() => onToggleSelected(p.id)}
              style={{ cursor: "pointer", background: selected.has(p.id) ? "rgba(0, 229, 255, 0.06)" : undefined }}
            >
              <td><PlatformIcon platform={p.platform} size={16} /></td>
              <td>
                <a href={p.url} target="_blank" rel="noreferrer" onClick={stop} style={{ color: "var(--text-main)", display: "inline-flex", alignItems: "center", gap: 5 }}>
                  {p.display_name || p.username || p.url}
                  {p.verified && <VerifiedBadgeIcon size={13} />}
                </a>
              </td>
              <td>{statusLabel(p)}</td>
              <td>{p.name_score != null ? `${matchLevelOf(p)} (${p.name_score})` : "—"}</td>
              <td>{p.followers != null ? p.followers.toLocaleString() : "—"}</td>
              <td>{p.keywords.join(", ")}</td>
              {(onValidate || onUnvalidate) && (
                <td>
                  {onValidate && (
                    <button
                      className="btn-accept"
                      style={{ padding: "3px 8px", fontSize: 11 }}
                      disabled={busyId === p.id}
                      onClick={(e) => { stop(e); onValidate(p.id); }}
                    >
                      Validate
                    </button>
                  )}
                  {onUnvalidate && (
                    <button
                      className="btn-reject"
                      style={{ padding: "3px 8px", fontSize: 11 }}
                      disabled={busyId === p.id}
                      title="Move back to New/Old -- undo an accidental Validate"
                      onClick={(e) => { stop(e); onUnvalidate(p.id); }}
                    >
                      ↩ Move to Old
                    </button>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DiscoveryProfileGrid({ groupId, platform, refreshKey, onAnalyseStarted }: Props) {
  const [tab, setTab] = useState<Tab>("new");
  const [pendingItems, setPendingItems] = useState<DiscoveredProfile[] | null>(null);
  const [loadingPending, setLoadingPending] = useState(false);
  const [validatedPage, setValidatedPage] = useState<{ items: DiscoveredProfile[]; total: number } | null>(null);
  const [loadingValidated, setLoadingValidated] = useState(false);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busyId, setBusyId] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [analysing, setAnalysing] = useState<"all" | "selected" | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [copyingAll, setCopyingAll] = useState(false);

  const [keywordFilter, setKeywordFilter] = useState("");
  const [keywordMatchType, setKeywordMatchType] = useState<"" | "individual" | "domain">("");
  const [matchLevel, setMatchLevel] = useState<"" | "high" | "medium" | "low">("");
  const [entityType, setEntityType] = useState<"" | "profile" | "page" | "group">("");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [pageSize, setPageSize] = useState<number>(25);
  const [offset, setOffset] = useState(0);
  const [viewMode, setViewMode] = useState<"cards" | "table">("cards");

  const [copyMenuOpen, setCopyMenuOpen] = useState(false);
  const [exportMenuOpen, setExportMenuOpen] = useState(false);
  const copyMenuRef = useRef<HTMLDivElement>(null);
  const exportMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (copyMenuRef.current && !copyMenuRef.current.contains(e.target as Node)) setCopyMenuOpen(false);
      if (exportMenuRef.current && !exportMenuRef.current.contains(e.target as Node)) setExportMenuOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  // New/Old is a pure computed split of `pendingItems` by age (see
  // isProfileNew) -- it's already correct the instant this component
  // mounts or re-renders, no explicit "move" step needed. But nothing
  // forces a re-render purely from the clock ticking: if an analyst opens
  // this grid and leaves the tab sitting untouched (no filter change, no
  // refresh) for longer than 24h, a profile that crosses the boundary
  // stays visually stuck under New until *something* else triggers a
  // re-render. This tick exists only to be that trigger -- every minute is
  // plenty for a 24h-granularity split, cheap, and needs no server round
  // trip since pendingItems itself doesn't change.
  const [, forceTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => forceTick((n) => n + 1), 60_000);
    return () => clearInterval(id);
  }, []);

  // Debounce search -> server param, matching the old app's search box.
  useEffect(() => {
    const t = setTimeout(() => setSearch(searchInput.trim()), 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  // Any filter (or tab) changing resets to page 1.
  useEffect(() => {
    setOffset(0);
  }, [tab, keywordFilter, search, pageSize, platform]);

  const loadPending = useCallback(async () => {
    setLoadingPending(true);
    try {
      const res = await discoveryApi.listProfiles({
        group_id: groupId, platform: platform || undefined, status: "pending",
        keyword: keywordFilter || undefined, search: search || undefined,
        limit: PENDING_FETCH_CAP,
      });
      setPendingItems(res.items);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setLoadingPending(false);
    }
  }, [groupId, platform, keywordFilter, search]);

  const loadValidated = useCallback(async () => {
    setLoadingValidated(true);
    try {
      const res = await discoveryApi.listProfiles({
        group_id: groupId, platform: platform || undefined, status: "validated",
        keyword: keywordFilter || undefined, search: search || undefined,
        limit: pageSize, offset,
      });
      setValidatedPage(res);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setLoadingValidated(false);
    }
  }, [groupId, platform, keywordFilter, search, pageSize, offset]);

  // Imperative use only (e.g. after a bulk delete) -- NOT an effect
  // dependency anywhere, see the two load effects below for why: bundling
  // both loads behind one identity is what made a page-change also look
  // like "the dataset changed" and clear the selection.
  const reloadAll = useCallback(() => {
    void loadPending();
    void loadValidated();
  }, [loadPending, loadValidated]);

  // Each list reloads on its own actual dependencies -- notably,
  // loadValidated depends on `offset` (real server-side pagination), so a
  // combined "reload everything" effect keyed on both callbacks' identity
  // used to re-run on every page change too. That's what was clearing a
  // page-1 selection the instant an analyst clicked to page 2: this effect
  // doesn't touch `selected` at all, so paging within the same filtered
  // set now leaves it alone.
  useEffect(() => {
    void loadPending();
  }, [loadPending, refreshKey]);

  useEffect(() => {
    void loadValidated();
  }, [loadValidated, refreshKey]);

  // Selection IS reset here, but only on what actually changes the
  // dataset being browsed -- a different client/platform/keyword/search,
  // switching tabs, or a fresh sweep landing (refreshKey). Deliberately
  // excludes `offset`/`pageSize`: paging forward/back is still the same
  // dataset, just a different slice of it.
  useEffect(() => {
    setSelected(new Set());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupId, platform, keywordFilter, search, tab, refreshKey]);

  // Individual/Domain classification: no server-side data for this any
  // more -- read back whatever HomeView's saveConfig last remembered for
  // this client (see services/clientKeywords.ts) and match each profile's
  // own keywords[] against those two sets, case-insensitively.
  const { individual: individualKw, domain: domainKw } = getClientKeywords(groupId);
  const individualSet = new Set(individualKw.map((k) => k.toLowerCase()));
  const domainSet = new Set(domainKw.map((k) => k.toLowerCase()));
  const matchesCategory = (p: DiscoveredProfile, cat: "individual" | "domain"): boolean => {
    const set = cat === "individual" ? individualSet : domainSet;
    return p.keywords.some((kw) => set.has(kw.toLowerCase()));
  };

  // Client-side only -- the backend has no match-level/entity-type/
  // keyword-category filter param.
  const clientFilter = (p: DiscoveredProfile): boolean => {
    if (matchLevel && matchLevelOf(p) !== matchLevel) return false;
    if (entityType && p.entity_type !== entityType) return false;
    if (keywordMatchType && !matchesCategory(p, keywordMatchType)) return false;
    return true;
  };

  const newItems = (pendingItems || []).filter(isProfileNew).filter(clientFilter);
  const oldItems = (pendingItems || []).filter((p) => !isProfileNew(p)).filter(clientFilter);
  const validatedItemsAll = (validatedPage?.items || []).filter(clientFilter);

  // New/Old paginate client-side over the already-fetched+filtered set;
  // Validated keeps true server pagination.
  const displayed =
    tab === "new" ? newItems.slice(offset, offset + pageSize)
    : tab === "old" ? oldItems.slice(offset, offset + pageSize)
    : validatedItemsAll;

  const total = tab === "new" ? newItems.length : tab === "old" ? oldItems.length : (validatedPage?.total || 0);
  const loading = tab === "validated" ? loadingValidated : loadingPending;

  // Keyword dropdown options, counted from whatever's actually loaded right
  // now (no aggregate-across-all-pages endpoint exists) -- an honest
  // approximation, not a true global count.
  const keywordCounts = new Map<string, number>();
  for (const p of [...(pendingItems || []), ...(validatedPage?.items || [])]) {
    for (const kw of p.keywords) keywordCounts.set(kw, (keywordCounts.get(kw) || 0) + 1);
  }
  const keywordOptions = [...keywordCounts.entries()].sort((a, b) => b[1] - a[1]);

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  // Both validate() and unvalidate() below apply their removal to local
  // state FIRST, synchronously, before the network call -- a card an
  // analyst clicks Validate on disappears on that same render, not after
  // a round trip plus a full reload. The removed rows are held onto so a
  // failure (total or partial -- one bad id in a batch of 40 must not
  // undo the other 39) can put back exactly what the server says didn't
  // actually change, rather than forcing a full reload to recover.

  const validate = async (ids: string[]) => {
    const idSet = new Set(ids);
    const removed = (pendingItems || []).filter((p) => idSet.has(p.id));
    setPendingItems((prev) => (prev || []).filter((p) => !idSet.has(p.id)));
    setSelected((prev) => {
      const n = new Set(prev);
      ids.forEach((id) => n.delete(id));
      return n;
    });

    try {
      const res = await discoveryApi.setProfileStatus(ids, "validated");
      if (res.failed.length) {
        toast.error(`${res.failed.length} could not be validated`);
        const failedIds = new Set(res.failed.map((f) => f.value));
        const putBack = removed.filter((p) => failedIds.has(p.id));
        if (putBack.length) setPendingItems((prev) => [...putBack, ...(prev || [])]);
      }
      if (res.updated.length) toast.success(`${res.updated.length} validated -- moved to Validated Profiles`);
      // Refreshes the Validated tab's contents/count with what just moved
      // there. Not awaited: it must never delay the instant removal above.
      void loadValidated();
    } catch (e) {
      // Never reached the server -- none of it actually validated, so all
      // of it goes back.
      setPendingItems((prev) => [...removed, ...(prev || [])]);
      toast.error((e as Error).message);
    }
  };

  // The undo for an accidental Validate: sets status back to `pending`,
  // which the backend explicitly supports for exactly this ("`pending`
  // undoes either decision", see POST /discovery/profiles/status). The
  // profile reappears under New or Old automatically -- whichever
  // isProfileNew(p) computes from its untouched first_seen -- with no
  // need to guess which tab it belongs in.
  const unvalidate = async (ids: string[]) => {
    const idSet = new Set(ids);
    const removed = (validatedPage?.items || []).filter((p) => idSet.has(p.id));
    setValidatedPage((prev) =>
      prev ? { ...prev, items: prev.items.filter((p) => !idSet.has(p.id)), total: Math.max(0, prev.total - removed.length) } : prev,
    );
    setSelected((prev) => {
      const n = new Set(prev);
      ids.forEach((id) => n.delete(id));
      return n;
    });

    try {
      const res = await discoveryApi.setProfileStatus(ids, "pending");
      if (res.failed.length) {
        toast.error(`${res.failed.length} could not be moved back`);
        const failedIds = new Set(res.failed.map((f) => f.value));
        const putBack = removed.filter((p) => failedIds.has(p.id));
        if (putBack.length) {
          setValidatedPage((prev) =>
            prev ? { ...prev, items: [...putBack, ...prev.items], total: prev.total + putBack.length } : prev,
          );
        }
      }
      if (res.updated.length) {
        toast.success(`${res.updated.length} moved back${res.updated.length === 1 ? "" : ""} to New/Old`);
      }
      // Refreshes New/Old with the newly-un-validated profiles. Not
      // awaited, for the same reason as loadValidated() above.
      void loadPending();
    } catch (e) {
      setValidatedPage((prev) =>
        prev ? { ...prev, items: [...removed, ...prev.items], total: prev.total + removed.length } : prev,
      );
      toast.error((e as Error).message);
    }
  };

  const bulkValidate = async (ids: string[]) => {
    setBulkBusy(true);
    try {
      await validate(ids);
    } finally {
      setBulkBusy(false);
    }
  };

  const onValidateOne = async (id: string) => {
    setBusyId(id);
    try {
      await validate([id]);
    } finally {
      setBusyId(null);
    }
  };

  const bulkUnvalidate = async (ids: string[]) => {
    setBulkBusy(true);
    try {
      await unvalidate(ids);
    } finally {
      setBulkBusy(false);
    }
  };

  const onUnvalidateOne = async (id: string) => {
    setBusyId(id);
    try {
      await unvalidate([id]);
    } finally {
      setBusyId(null);
    }
  };

  const copyUrls = async (urls: string[], label: string) => {
    if (!urls.length) {
      toast.error(`No ${label} URLs to copy`);
      return;
    }
    try {
      await navigator.clipboard.writeText(urls.join("\n"));
      toast.success(`Copied ${urls.length} URL(s)`);
    } catch {
      toast.error("Clipboard unavailable -- select and copy manually");
    }
  };

  const scopedRows = () => (selected.size ? displayed.filter((p) => selected.has(p.id)) : displayed);

  const handleCopyUrls = () => {
    const rows = scopedRows();
    copyUrls(rows.map((p) => p.url), selected.size ? "selected" : "filtered");
    setCopyMenuOpen(false);
  };

  const handleCopyTable = async () => {
    const rows = scopedRows();
    if (!rows.length) {
      toast.error("Nothing to copy");
      setCopyMenuOpen(false);
      return;
    }
    const tsv = rowsToCsv(rows.map(exportRow)).replace(/,/g, "\t");
    try {
      await navigator.clipboard.writeText(tsv);
      toast.success(`Copied ${rows.length} row(s) as table data`);
    } catch {
      toast.error("Clipboard unavailable");
    }
    setCopyMenuOpen(false);
  };

  // Validated-tab-only pair, next to Analyse All Validated/Analyse
  // Selected -- same "all vs. selected" split, but for the clipboard
  // instead of a job. "Selected" reuses what's already loaded (selection
  // only ever covers rows on screen); "All" means every validated profile
  // matching the current filters, not just the current page, so it fetches
  // fresh rather than reusing `validatedPage` (which is paginated to
  // `pageSize`) -- same PENDING_FETCH_CAP-style ceiling loadPending()
  // already uses for its own "all pending rows" fetch.
  const handleCopyAllValidated = async () => {
    setCopyingAll(true);
    try {
      const res = await discoveryApi.listProfiles({
        group_id: groupId, platform: platform || undefined, status: "validated",
        keyword: keywordFilter || undefined, search: search || undefined,
        limit: PENDING_FETCH_CAP,
      });
      await copyUrls(res.items.map((p) => p.url), "validated");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setCopyingAll(false);
    }
  };

  const handleCopySelectedValidated = () => {
    const rows = (validatedPage?.items || []).filter((p) => selected.has(p.id));
    copyUrls(rows.map((p) => p.url), "selected");
  };

  const handleExport = async (fmt: "xlsx" | "csv" | "json") => {
    const rows = scopedRows();
    if (!rows.length) {
      toast.error("Nothing to export");
      setExportMenuOpen(false);
      return;
    }
    setExporting(true);
    try {
      const filename = `${groupId}-${tab}-${new Date().toISOString().slice(0, 10)}`;
      if (fmt === "xlsx") {
        const blob = await analysisApi.exportXlsx(filename, rows.map(exportRow));
        const href = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = href;
        a.download = `${filename}.xlsx`;
        a.click();
        URL.revokeObjectURL(href);
      } else if (fmt === "csv") {
        download(`${filename}.csv`, rowsToCsv(rows.map(exportRow)), "text/csv");
      } else {
        download(`${filename}.json`, JSON.stringify(rows.map(exportRow), null, 2), "application/json");
      }
      toast.success(`Exported ${rows.length} row(s)`);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setExporting(false);
      setExportMenuOpen(false);
    }
  };

  const handleDeletePlatformData = async () => {
    const dbStatus = tab === "validated" ? "validated" : "pending";
    const confirmed = await confirmAction(
      `Permanently delete every ${tab === "validated" ? "validated" : "new + old (pending)"} discovery profile for "${groupId}"`
      + `${platform ? ` on ${platform}` : " across every platform"}? This cannot be undone.`,
    );
    if (!confirmed) return;
    setDeleting(true);
    try {
      const res = await discoveryApi.deletePlatformData({ group_id: groupId, platform: platform || undefined, status: dbStatus });
      toast.success(`Deleted ${res.deleted} profile(s)`);
      setSelected(new Set());
      reloadAll();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setDeleting(false);
    }
  };

  const analyse = async (mode: "all" | "selected") => {
    const ids = mode === "selected" ? [...selected] : undefined;
    if (mode === "selected" && !ids?.length) {
      toast.error("Select at least one validated profile first");
      return;
    }
    setAnalysing(mode);
    try {
      // groupId IS the client_id by convention, but this component is only
      // ever handed the id, not the full client record -- look up the
      // saved client locally for its domain (see savedClients.ts; there is
      // no /clients backend to read it from server-side). undefined when
      // the client was deleted or this groupId was never a real client
      // (an ad-hoc/API-only group_id), which the backend already treats
      // as "no client behind this batch" -- same fallback as a pasted-URL
      // analysis job.
      const client = listSavedClients().find((c) => c.client_id === groupId);
      const res = await discoveryApi.analyseValidated({ group_id: groupId, ids, domain: client?.domain });
      toast.success(`Analysis started: ${res.accepted} profile(s)`);
      onAnalyseStarted(res.job_id);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setAnalysing(null);
    }
  };

  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const currentPage = Math.floor(offset / pageSize) + 1;

  // Arrow-key paging -- lets an analyst walk through a long list without
  // reaching for the mouse each time. Ignored while focus is in a text
  // field/select/textarea so it never hijacks normal typing (the search
  // box, a keyword filter, an editable cell). Same bounds as the Prev/Next
  // buttons themselves, so this is a no-op on the first/last page.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      if (e.key === "ArrowRight" && currentPage < pageCount) {
        setOffset(offset + pageSize);
      } else if (e.key === "ArrowLeft" && offset > 0) {
        setOffset(Math.max(0, offset - pageSize));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [offset, pageSize, currentPage, pageCount]);
  // No per-card validate on the Validated tab -- it's already validated;
  // that tab's actions are the selection-scoped Copy/Export/Analyse ones.
  const onValidateHandler = tab === "validated" ? undefined : onValidateOne;
  const onUnvalidateHandler = tab === "validated" ? onUnvalidateOne : undefined;

  return (
    <div style={{ marginTop: "24px" }}>
      {/* Tabs -- New Profiles / Old Profiles / Validated Profiles. New and
          Old are the same `pending` status, split client-side by
          first_seen age (24h); nothing moves them, that split is just
          re-evaluated live every time this loads. */}
      <div className="status-summary-row" style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
        {([
          ["new", "🆕 New Profiles", "var(--cyan-bright, var(--cyan))", newItems.length],
          ["old", "🕓 Old Profiles", "var(--purple)", oldItems.length],
          ["validated", "✅ Validated Profiles", "var(--success, #12B76A)", validatedPage?.total || 0],
        ] as const).map(([t, label, color, count]) => (
          <button
            key={t}
            className={`status-chip ${tab === t ? "on" : ""}`}
            onClick={() => setTab(t)}
            style={{
              display: "flex", alignItems: "center", gap: "8px", padding: "6px 14px", borderRadius: "20px",
              border: `1px solid ${tab === t ? color : "var(--border-color)"}`,
              background: tab === t ? "var(--bg-surface)" : "transparent",
              cursor: "pointer", fontSize: "12px", fontWeight: 600,
              color: tab === t ? color : "var(--text-muted)",
            }}
          >
            <span>{label}</span>
            <span style={{ background: tab === t ? color : "var(--bg-inner)", color: tab === t ? "#fff" : "var(--text-dim)", padding: "2px 8px", borderRadius: "12px", fontSize: "11px", fontWeight: 700 }}>
              {count}
            </span>
          </button>
        ))}
      </div>

      {/* Click a card/row to select it; validate whatever's selected.
          New/Old only -- Validated already has its own selected-scoped
          actions (Analyse Selected, Copy, Export) below. */}
      {tab !== "validated" && selected.size > 0 && (
        <div style={{ marginTop: "12px" }}>
          <button className="btn-accept" style={{ flex: "none", padding: "8px 16px" }} disabled={bulkBusy} onClick={() => bulkValidate([...selected])}>
            {bulkBusy ? "…" : `✅ Validate Selected (${selected.size})`}
          </button>
        </div>
      )}

      {/* The undo for a batch of accidental Validates -- moves the
          selection back to pending, which reappears under New or Old on
          its own (see unvalidate()'s comment). */}
      {tab === "validated" && selected.size > 0 && (
        <div style={{ marginTop: "12px" }}>
          <button className="btn-reject" style={{ flex: "none", padding: "8px 16px" }} disabled={bulkBusy} onClick={() => bulkUnvalidate([...selected])}>
            {bulkBusy ? "…" : `↩ Move Selected to Old (${selected.size})`}
          </button>
        </div>
      )}

      {/* Filter toolbar */}
      <div style={{ display: "flex", gap: "8px", marginTop: "12px", flexWrap: "wrap", alignItems: "center" }}>
        <select value={keywordFilter} onChange={(e) => setKeywordFilter(e.target.value)} className="select-filter" title="Only show profiles found by this exact keyword">
          <option value="">All Keywords</option>
          {keywordOptions.map(([kw, n]) => (
            <option key={kw} value={kw}>{kw} ({n})</option>
          ))}
        </select>
        <select
          value={keywordMatchType}
          onChange={(e) => setKeywordMatchType(e.target.value as typeof keywordMatchType)}
          className="select-filter"
          title="Filter to profiles matched via an Individual Name keyword vs a Domain keyword -- from this browser's own record of this client's keyword categories (see the Clients tab), not the server"
        >
          <option value="">Individual + Domain</option>
          <option value="individual">Individual Match Only</option>
          <option value="domain">Domain Match Only</option>
        </select>
        <select value={matchLevel} onChange={(e) => setMatchLevel(e.target.value as typeof matchLevel)} className="select-filter" title="How closely the scraped name matches the keyword that found it">
          <option value="">All Match Levels</option>
          <option value="high">High Match</option>
          <option value="medium">Medium Match</option>
          <option value="low">Low Match</option>
        </select>
        {platform === "facebook" && (
          <select value={entityType} onChange={(e) => setEntityType(e.target.value as typeof entityType)} className="select-filter" title="Facebook discovery distinguishes people, Pages, and Groups">
            <option value="">People + Pages + Groups</option>
            <option value="profile">People Only</option>
            <option value="page">Pages Only</option>
            <option value="group">Groups Only</option>
          </select>
        )}
        <div style={{ position: "relative", flex: 1, minWidth: "160px" }}>
          <input
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder="Search name / handle…"
            className="input-filter"
            style={{ width: "100%", boxSizing: "border-box" }}
          />
        </div>

        <div style={{ display: "flex", gap: "6px" }}>
          <button
            onClick={() => setViewMode("cards")}
            title="Card view"
            style={{
              background: viewMode === "cards" ? "rgba(136, 56, 221,0.12)" : "var(--bg-surface)",
              border: `1px solid ${viewMode === "cards" ? "var(--cyan)" : "var(--border-color)"}`,
              color: viewMode === "cards" ? "var(--text-main)" : "var(--text-muted)",
              borderRadius: "8px", padding: "7px 10px", cursor: "pointer",
              display: "inline-flex", alignItems: "center", gap: "5px", fontSize: "12px",
            }}
          >
            <LayersIcon size={12} /> <span>Cards</span>
          </button>
          <button
            onClick={() => setViewMode("table")}
            title="Table view"
            style={{
              background: viewMode === "table" ? "rgba(136, 56, 221,0.12)" : "var(--bg-surface)",
              border: `1px solid ${viewMode === "table" ? "var(--cyan)" : "var(--border-color)"}`,
              color: viewMode === "table" ? "var(--text-main)" : "var(--text-muted)",
              borderRadius: "8px", padding: "7px 10px", cursor: "pointer",
              display: "inline-flex", alignItems: "center", gap: "5px", fontSize: "12px",
            }}
          >
            <CloneIcon size={12} /> <span>Table</span>
          </button>
        </div>

        {/* Copy dropdown */}
        <div className="action-dropdown-container" ref={copyMenuRef} style={{ position: "relative" }}>
          <button
            className="btn-cyber-primary"
            style={{ padding: "7px 12px", fontSize: "11px", marginTop: 0, width: "auto", display: "inline-flex", alignItems: "center", gap: "5px" }}
            onClick={() => { setCopyMenuOpen(!copyMenuOpen); setExportMenuOpen(false); }}
            title="Copy profile URLs or table data to clipboard"
          >
            <CloneIcon size={12} /> {selected.size > 0 ? `Copy (${selected.size}) ▾` : "Copy ▾"}
          </button>
          {copyMenuOpen && (
            <div className="action-dropdown-menu">
              <div className="action-dropdown-header">Copy Options</div>
              {selected.size > 0 ? (
                <div className="action-dropdown-scope-badge">
                  <TargetIcon size={12} color="var(--cyan)" />
                  <span>{selected.size} Selected Row{selected.size > 1 ? "s" : ""}</span>
                </div>
              ) : (
                <div className="action-dropdown-scope-badge" style={{ background: "rgba(148, 163, 184, 0.12)", color: "var(--text-dim)" }}>
                  <GlobeIcon size={12} color="var(--text-dim)" />
                  <span>All Filtered ({displayed.length})</span>
                </div>
              )}
              <button className="action-dropdown-item" onClick={handleCopyUrls}>
                <div className="action-dropdown-item-left">
                  <span className="action-dropdown-item-icon">🔗</span>
                  <span>{selected.size > 0 ? `Copy Selected URLs (${selected.size})` : "Copy Profile URLs"}</span>
                </div>
                <span className="action-dropdown-item-badge">1-per-line</span>
              </button>
              <div className="action-dropdown-divider" />
              <button className="action-dropdown-item" onClick={handleCopyTable}>
                <div className="action-dropdown-item-left">
                  <span className="action-dropdown-item-icon">📊</span>
                  <span>Copy Table Data</span>
                </div>
                <span className="action-dropdown-item-badge">TSV</span>
              </button>
            </div>
          )}
        </div>

        {/* Export dropdown -- xlsx reuses POST /analysis/export/xlsx, csv/json are client-side */}
        <div className="action-dropdown-container" ref={exportMenuRef} style={{ position: "relative" }}>
          <button
            className="btn-cyber-primary"
            style={{ padding: "7px 12px", fontSize: "11px", marginTop: 0, width: "auto", display: "inline-flex", alignItems: "center", gap: "5px" }}
            onClick={() => { setExportMenuOpen(!exportMenuOpen); setCopyMenuOpen(false); }}
            disabled={exporting}
            title="Download table data as Excel (.xlsx), CSV, or JSON"
          >
            {exporting ? "⏳ Exporting…" : "📥 Export ▾"}
          </button>
          {exportMenuOpen && (
            <div className="action-dropdown-menu">
              <div className="action-dropdown-header">Export Data</div>
              {selected.size > 0 ? (
                <div className="action-dropdown-scope-badge">🎯 Exporting {selected.size} Selected</div>
              ) : (
                <div className="action-dropdown-scope-badge" style={{ background: "rgba(148, 163, 184, 0.12)", color: "var(--text-dim)" }}>
                  🌐 Exporting All Filtered ({displayed.length})
                </div>
              )}
              <button className="action-dropdown-item" onClick={() => handleExport("xlsx")}>
                <div className="action-dropdown-item-left"><span className="action-dropdown-item-icon">📗</span><span>Excel (.xlsx)</span></div>
              </button>
              <button className="action-dropdown-item" onClick={() => handleExport("csv")}>
                <div className="action-dropdown-item-left"><span className="action-dropdown-item-icon">📄</span><span>CSV</span></div>
              </button>
              <button className="action-dropdown-item" onClick={() => handleExport("json")}>
                <div className="action-dropdown-item-left"><span className="action-dropdown-item-icon">🗂️</span><span>JSON</span></div>
              </button>
            </div>
          )}
        </div>

        <button
          onClick={handleDeletePlatformData}
          disabled={deleting}
          title="Permanently delete every profile matching the current tab + platform filter"
          style={{
            background: "rgba(233, 80, 83, 0.12)", border: "1px solid var(--danger)", color: "var(--danger)",
            borderRadius: "8px", padding: "7px 12px", cursor: deleting ? "progress" : "pointer",
            display: "inline-flex", alignItems: "center", gap: "5px", fontSize: "11px", fontWeight: 700,
          }}
        >
          <TrashIcon size={12} /> {deleting ? "Deleting…" : "Delete Platform Data"}
        </button>
      </div>

      {tab === "validated" && (
        <div style={{ display: "flex", gap: "10px", marginTop: "14px", flexWrap: "wrap" }}>
          <button
            className="btn-cyber-primary"
            style={{ width: "auto", margin: 0, padding: "10px 18px" }}
            disabled={analysing !== null || !total}
            onClick={() => analyse("all")}
          >
            <ZapIcon size={14} /> {analysing === "all" ? "Starting…" : "Analyse All Validated"}
          </button>
          <button
            className="btn-cyber-primary"
            style={{ width: "auto", margin: 0, padding: "10px 18px" }}
            disabled={analysing !== null || !selected.size}
            onClick={() => analyse("selected")}
          >
            <ZapIcon size={14} /> {analysing === "selected" ? "Starting…" : "Analyse Selected"}
          </button>
          <button
            className="btn-cyber-primary"
            style={{ width: "auto", margin: 0, padding: "10px 18px" }}
            disabled={copyingAll || !total}
            onClick={handleCopyAllValidated}
          >
            <CloneIcon size={14} /> {copyingAll ? "Copying…" : "Copy All"}
          </button>
          <button
            className="btn-cyber-primary"
            style={{ width: "auto", margin: 0, padding: "10px 18px" }}
            disabled={!selected.size}
            onClick={handleCopySelectedValidated}
          >
            <CloneIcon size={14} /> Copy Selected
          </button>
        </div>
      )}

      {loading && !displayed.length && <div style={{ padding: "40px", textAlign: "center", color: "var(--text-dim)" }}>Loading…</div>}
      {!loading && !displayed.length && (
        <div style={{ padding: "40px", textAlign: "center", color: "var(--text-dim)" }}>
          No {tab === "new" ? "new" : tab === "old" ? "old" : "validated"} profiles match these filters.
        </div>
      )}

      {viewMode === "cards" ? (
        <div className="profile-grid-container" style={{ marginTop: "16px" }}>
          {displayed.map((p) => (
            <ProfileCard key={p.id} p={p} selected={selected.has(p.id)} onToggleSelected={toggle} onValidate={onValidateHandler} onUnvalidate={onUnvalidateHandler} busy={busyId === p.id} />
          ))}
        </div>
      ) : (
        !!displayed.length && (
          <ProfileTable items={displayed} selected={selected} onToggleSelected={toggle} onValidate={onValidateHandler} onUnvalidate={onUnvalidateHandler} busyId={busyId} />
        )
      )}

      {!loading && total > 0 && (
        <div style={{ display: "flex", justifyContent: "center", gap: "6px", alignItems: "center", marginTop: "16px", flexWrap: "wrap" }}>
          <label style={{ fontSize: "11px", color: "var(--text-dim)", display: "flex", alignItems: "center", gap: "5px" }}>
            Show
            <select value={pageSize} onChange={(e) => setPageSize(Number(e.target.value))} className="select-filter" style={{ padding: "4px 6px" }}>
              {PAGE_SIZE_OPTIONS.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
            per page
          </label>
          {total > pageSize && (
            <>
              <button disabled={offset === 0} onClick={() => setOffset(0)} className="btn-cyber-primary" style={{ width: "auto", padding: "6px 10px", marginTop: 0 }} title="First page">⏮</button>
              <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))} className="btn-cyber-primary" style={{ width: "auto", padding: "6px 12px", marginTop: 0 }} title="Previous page (← arrow key)">← Prev</button>
              <span style={{ fontSize: "12px", color: "var(--text-dim)" }}>Page {currentPage} of {pageCount} · {total} total</span>
              <button disabled={currentPage >= pageCount} onClick={() => setOffset(offset + pageSize)} className="btn-cyber-primary" style={{ width: "auto", padding: "6px 12px", marginTop: 0 }} title="Next page (→ arrow key)">Next →</button>
              <button disabled={currentPage >= pageCount} onClick={() => setOffset((pageCount - 1) * pageSize)} className="btn-cyber-primary" style={{ width: "auto", padding: "6px 10px", marginTop: 0 }} title="Last page">⏭</button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
