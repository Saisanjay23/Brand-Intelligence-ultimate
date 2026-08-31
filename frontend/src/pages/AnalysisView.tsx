import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import toast from "react-hot-toast";
import {
  analysisApi,
  type AnalysisItemData,
  type AnalysisJobResponse,
} from "../api/analysisApi";
import {
  ActivityWaveIcon,
  DownloadIcon,
  AnalysisNavIcon,
  SearchIcon,
  StopIcon,
  VerifiedBadgeIcon,
} from "../components/AppIcons";
import { PlatformIcon } from "../components/PlatformIcon";
import { download, downloadBlob, rowsToCsv, rowsToTsv } from "../utils/download";

const SAMPLE_URLS = [
  "https://www.facebook.com/zuck",
  "https://www.instagram.com/instagram",
  "https://twitter.com/elonmusk",
  "https://www.youtube.com/@Google",
  "https://t.me/durov",
  "https://www.tiktok.com/@tiktok",
].join("\n");

// A plain `<input>` with a transparent resting background reads, at a
// glance, as identical to an EMPTY cell -- an input's own value is a DOM
// property, not text content, so even automated inspection (get_page_text/
// innerText) can't see it either. Next to ToggleCell's bold colored pill
// two columns over, that made half a row's real data (asset name,
// followers, last post, location -- the actual identifying evidence) look
// like it hadn't been scraped, when it had. Two fixes: an always-visible
// filled background so a cell with a real value reads as "filled in," not
// as blank space waiting to be noticed; and a distinct, visibly different
// look for a value that's GENUINELY missing (dashed border, muted "—"),
// so an analyst can tell "no data" from "data present but easy to miss"
// without having to click into every cell to check.
const EditableCell = ({ value, onChange, placeholder = "", readOnly = false }: { value: string, onChange: (v: string) => void, placeholder?: string, readOnly?: boolean }) => {
  const filled = value.trim().length > 0;
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => !readOnly && onChange(e.target.value)}
      placeholder={placeholder || "—"}
      readOnly={readOnly}
      style={{
        background: filled ? "var(--bg-surface-3, rgba(255,255,255,0.05))" : "transparent",
        border: filled ? "1px solid var(--border-color, rgba(255,255,255,0.1))" : "1px dashed var(--border-color, rgba(255,255,255,0.18))",
        color: filled ? "inherit" : "var(--text-muted, #667085)",
        fontStyle: filled ? "normal" : "italic",
        width: "100%",
        // An <input> has no intrinsic width from its OWN value the way a
        // <span> would -- the table's auto-layout algorithm can't tell
        // "CyfirmaBreakingOfficial" needs more room than "nasa" just by
        // looking at this element, so minWidth is the only real signal it
        // gets. 60px (roughly 8 characters) was starving every text column
        // down near its floor regardless of the table now being allowed to
        // grow (see the table's own width:"max-content" fix) -- 130px
        // gives a typical handle/date/city room to sit unclipped. Whatever
        // still doesn't fit gets a clean "…", not a raw cut mid-word: the
        // value is still there in full, just click in to see/edit it.
        minWidth: "130px",
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
        padding: "6px 9px",
        borderRadius: "5px",
        outline: "none",
        transition: "background 0.15s ease, border-color 0.15s ease",
        fontSize: "inherit",
        fontFamily: "inherit",
        cursor: readOnly ? "default" : "text"
      }}
      onFocus={(e) => {
        if (!readOnly) {
          e.target.style.background = "rgba(0, 229, 255, 0.08)";
          e.target.style.border = "1px solid var(--cyan, #00E5FF)";
        }
      }}
      onBlur={(e) => {
        if (!readOnly) {
          e.target.style.background = filled ? "var(--bg-surface-3, rgba(255,255,255,0.05))" : "transparent";
          e.target.style.border = filled
            ? "1px solid var(--border-color, rgba(255,255,255,0.1))"
            : "1px dashed var(--border-color, rgba(255,255,255,0.18))";
        }
      }}
    />
  );
};

const ToggleCell = ({ value, onChange, defaultWhenEmpty = "—" }: { value: string, onChange: (v: string) => void, defaultWhenEmpty?: string }) => {
  const displayValue = value || defaultWhenEmpty;
  const isYes = displayValue === "Yes";
  const isNo = displayValue === "No";
  const color = isYes ? "#00E599" : isNo ? "#F26A6E" : "var(--text-muted, #667085)";
  return (
    <button
      type="button"
      onClick={() => onChange(isYes ? "No" : "Yes")}
      title="Click to toggle"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "5px",
        background: isYes ? "rgba(0, 229, 153, 0.12)" : isNo ? "rgba(242, 106, 110, 0.12)" : "var(--bg-surface-3, #344054)",
        border: `1.5px solid ${isYes ? "rgba(0, 229, 153, 0.5)" : isNo ? "rgba(242, 106, 110, 0.5)" : "var(--border-color, #475467)"}`,
        color,
        padding: "5px 12px",
        borderRadius: "999px",
        fontSize: "11px",
        fontWeight: 700,
        cursor: "pointer",
        minWidth: "60px",
        justifyContent: "center",
        transition: "transform 0.12s ease, box-shadow 0.12s ease, background 0.12s ease",
        boxShadow: isYes ? "0 0 10px rgba(0, 229, 153, 0.15)" : isNo ? "0 0 10px rgba(242, 106, 110, 0.15)" : "none",
      }}
      onMouseEnter={(e) => { e.currentTarget.style.transform = "translateY(-1px) scale(1.04)"; }}
      onMouseLeave={(e) => { e.currentTarget.style.transform = "none"; }}
    >
      <span style={{ fontSize: "10px" }}>{isYes ? "✓" : isNo ? "✕" : "•"}</span>
      {displayValue}
    </button>
  );
};

const computeDynamicRisk = (row: any, formatMode: "incident" | "legacy"): number => {
  const nameYes = formatMode === "incident" ? row["Name (Yes/No)"] : row["Name (Yes / No)"];
  const logoYes = formatMode === "incident" ? row["Logo (Yes/No)"] : row["Logo (Yes / No)"];
  const activeYes = formatMode === "incident" ? row["Active (Yes/No)"] : row["Active (Yes / No)"];
  const location = row["Location"];
  const lastPost = row["Last Post (DD-MM-YYYY) (Optional)"];

  const hasName = nameYes === "Yes" || nameYes === "" || nameYes === undefined;
  const hasLogo = logoYes === "Yes" || logoYes === "" || logoYes === undefined;
  const isActive = activeYes === "Yes";
  const hasLocation = Boolean(location && String(location).trim() !== "");
  const hasLastPost = Boolean(lastPost && String(lastPost).trim() !== "");
  
  let tier = "NONE";
  if (isActive) tier = "ACTIVE";
  else if (hasLastPost) tier = "DORMANT";
  
  if (!hasName) return 2;
  
  if (hasLogo) {
    if (tier === "ACTIVE") return hasLocation ? 9 : 8;
    if (hasLocation || tier === "DORMANT") return 7;
    return 6;
  }
  if (tier === "ACTIVE") return 5;
  if (tier === "DORMANT") return 4;
  return 3;
};

const getPriorityFromRisk = (score: number) => {
  if (score >= 5) return "High";
  return "Low";
};

interface Props {
  // Set by HomeView's "Analyse" action or Live Results' "Analyse
  // Validated"/"Analyse Selected" buttons after they start a job, so the
  // analyst lands here already watching it instead of an empty paste box.
  // A bump (even to the same value re-sent) re-triggers the
  // watch effect below -- see its key.
  resumeJobId?: string | null;
}

export function AnalysisView({ resumeJobId }: Props = {}) {
  const [urlInput, setUrlInput] = useState("");

  // Job & Results state (in RAM only, lost on refresh)
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobData, setJobData] = useState<AnalysisJobResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  // Table view & filter state
  const [formatMode, setFormatMode] = useState<"incident" | "legacy">("incident");
  const [searchQuery, setSearchQuery] = useState("");
  const [platformFilter, setPlatformFilter] = useState<string>("all");
  const [riskFilter, setRiskFilter] = useState<string>("all");
  const [exporting, setExporting] = useState(false);

  // Screenshot modal state
  const [previewScreenshot, setPreviewScreenshot] = useState<{
    url: string;
    profileName: string;
  } | null>(null);

  // Inline edits state
  const [edits, setEdits] = useState<Record<string, Record<string, string>>>({});

  const handleEdit = (itemId: string, field: string, value: string) => {
    setEdits((prev) => {
      const itemEdits = { ...(prev[itemId] || {}) };
      itemEdits[field] = value;

      // Synchronize keys between Incident and Legacy formats so edits apply to both exports
      if (field === "Name (Yes/No)") itemEdits["Name (Yes / No)"] = value;
      if (field === "Name (Yes / No)") itemEdits["Name (Yes/No)"] = value;
      if (field === "Logo (Yes/No)") itemEdits["Logo (Yes / No)"] = value;
      if (field === "Logo (Yes / No)") itemEdits["Logo (Yes/No)"] = value;
      if (field === "Active (Yes/No)") itemEdits["Active (Yes / No)"] = value;
      if (field === "Active (Yes / No)") itemEdits["Active (Yes/No)"] = value;
      if (field === "Number of Followers") itemEdits["Followers"] = value;
      if (field === "Followers") itemEdits["Number of Followers"] = value;

      return {
        ...prev,
        [itemId]: itemEdits,
      };
    });
  };

  // Live URL breakdown computation
  const urlSummary = useMemo(() => {
    const lines = urlInput
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    const breakdown: Record<string, number> = {};
    let validCount = 0;

    for (const raw of lines) {
      const lower = raw.toLowerCase();
      let plat = "other";
      if (lower.includes("facebook.com") || lower.includes("fb.me") || lower.includes("fb.com")) plat = "facebook";
      else if (lower.includes("instagram.com")) plat = "instagram";
      else if (lower.includes("twitter.com") || lower.includes("x.com")) plat = "twitter";
      else if (lower.includes("youtube.com") || lower.includes("youtu.be")) plat = "youtube";
      else if (lower.includes("t.me") || lower.includes("telegram.me")) plat = "telegram";
      else if (lower.includes("tiktok.com")) plat = "tiktok";

      breakdown[plat] = (breakdown[plat] || 0) + 1;
      if (plat !== "other") validCount++;
    }

    return { totalLines: lines.length, validCount, breakdown };
  }, [urlInput]);

  // Polling interval reference
  const pollingRef = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, []);

  const pollJob = useCallback(async (id: string) => {
    try {
      const data = await analysisApi.getJob(id);
      setJobData(data);
      if (data.status === "done" || data.status === "cancelled" || data.status === "failed") {
        setLoading(false);
        setCancelling(false);
        stopPolling();
        if (data.status === "done") {
          toast.success(`Analysis completed for ${data.completed}/${data.total} URLs`);
        } else if (data.status === "cancelled") {
          toast.error("Analysis stopped by user");
        }
      }
    } catch (e) {
      stopPolling();
      setLoading(false);
      setCancelling(false);
      toast.error((e as Error).message || "Failed to update job status");
    }
  }, [stopPolling]);

  useEffect(() => {
    return () => {
      stopPolling();
    };
  }, [stopPolling]);

  useEffect(() => {
    if (!resumeJobId) return;
    setJobId(resumeJobId);
    setJobData(null);
    setLoading(true);
    void pollJob(resumeJobId);
    stopPolling();
    pollingRef.current = window.setInterval(() => {
      pollJob(resumeJobId);
    }, 1500);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resumeJobId]);

  const handleStart = async () => {
    const lines = urlInput
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter(Boolean);

    if (!lines.length) {
      toast.error("Please enter at least one URL to analyze");
      return;
    }

    try {
      setLoading(true);
      setJobData(null);
      const res = await analysisApi.start(lines, "", "");
      setJobId(res.job_id);

      if (res.skipped && res.skipped.length > 0) {
        toast(
          `Skipped ${res.skipped.length} invalid/duplicate URL(s)`,
          { icon: "ℹ️" }
        );
      }

      // Initial poll immediately
      await pollJob(res.job_id);

      // Start polling interval
      stopPolling();
      pollingRef.current = window.setInterval(() => {
        pollJob(res.job_id);
      }, 1500);
    } catch (e) {
      setLoading(false);
      toast.error((e as Error).message || "Failed to start analysis");
    }
  };

  const handleCancel = async () => {
    if (!jobId) return;
    try {
      setCancelling(true);
      await analysisApi.cancelJob(jobId);
      toast("Stopping analysis...", { icon: "⏳" });
    } catch (e) {
      setCancelling(false);
      toast.error((e as Error).message || "Failed to cancel");
    }
  };

  const handleClear = () => {
    setUrlInput("");
    setJobId(null);
    setJobData(null);
    setSearchQuery("");
    stopPolling();
    toast.success("Workspace reset");
  };

  // Filtered rows for the table
  const filteredItems = useMemo(() => {
    if (!jobData?.items) return [];
    return jobData.items.filter((it) => {
      // Platform filter
      if (platformFilter !== "all" && it.platform !== platformFilter) return false;

      // Risk score filter
      if (riskFilter === "high" && (it.risk_score || 0) < 5) return false;
      if (riskFilter === "low" && (it.risk_score || 0) >= 5) return false;

      // Text search
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        const matchUrl = it.url.toLowerCase().includes(q);
        const matchName = (it.profile_name || "").toLowerCase().includes(q);
        const matchLoc = (it.location || "").toLowerCase().includes(q);
        const matchBio = (it.bio || "").toLowerCase().includes(q);
        if (!matchUrl && !matchName && !matchLoc && !matchBio) return false;
      }

      return true;
    });
  }, [jobData?.items, platformFilter, riskFilter, searchQuery]);

  // Export handlers
  const handleExport = async (fmt: "xlsx" | "csv" | "json" | "tsv") => {
    if (!jobData || !filteredItems.length) {
      toast.error("No analyzed items to export");
      return;
    }

    const rows = filteredItems.map((it) => {
      const baseRow = formatMode === "incident" ? it.incident_row : it.legacy_row;
      const itemEdits = edits[it.id] || {};

      // Merge only keys that exist in the base row to prevent appending duplicate columns
      // (e.g. "Name (Yes/No)" vs "Name (Yes / No)")
      const mergedRow: Record<string, any> = { ...baseRow };
      for (const key of Object.keys(itemEdits)) {
        if (key in mergedRow) {
          mergedRow[key] = itemEdits[key];
        }
      }

      const riskScore = computeDynamicRisk(mergedRow, formatMode);
      const priority = getPriorityFromRisk(riskScore);
      
      // Update fields if they exist in the row structure
      if (formatMode === "legacy") {
        mergedRow["Risk Score"] = riskScore;
        if ("priority" in mergedRow) mergedRow["priority"] = priority;
      } else {
        mergedRow["RiskScore"] = riskScore;
      }
      return mergedRow;
    });

    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    const filenameStem = `Analysis-${formatMode === "incident" ? "Platform-Format" : "Legacy-Format"}-${stamp}`;

    try {
      setExporting(true);
      if (fmt === "csv") {
        download(`${filenameStem}.csv`, rowsToCsv(rows), "text/csv");
        toast.success(`Exported ${rows.length} rows to CSV`);
      } else if (fmt === "json") {
        download(`${filenameStem}.json`, JSON.stringify(rows, null, 2), "application/json");
        toast.success(`Exported ${rows.length} rows to JSON`);
      } else if (fmt === "tsv") {
        const tsvText = rowsToTsv(rows);
        await navigator.clipboard.writeText(tsvText);
        toast.success(`Copied ${rows.length} rows (TSV) to clipboard! Paste into Excel/Sheets.`);
      } else if (fmt === "xlsx") {
        const filename = `${filenameStem}.xlsx`;
        const blob = await analysisApi.exportXlsx(filename, rows);
        downloadBlob(filename, blob);
        toast.success(`Exported ${rows.length} rows to Excel (.xlsx)`);
      }
    } catch (e) {
      toast.error((e as Error).message || "Export failed");
    } finally {
      setExporting(false);
    }
  };

  const getRiskBadge = (score: number) => {
    if (score >= 8) {
      return (
        <span
          style={{
            background: "rgba(233, 80, 83, 0.2)",
            color: "var(--danger, #E95053)",
            border: "1px solid rgba(233, 80, 83, 0.4)",
            padding: "2px 8px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: 700,
            display: "inline-flex",
            alignItems: "center",
            gap: "4px",
          }}
        >
          ● High ({score})
        </span>
      );
    }
    if (score >= 4) {
      return (
        <span
          style={{
            background: "rgba(247, 144, 9, 0.2)",
            color: "var(--warning, #F79009)",
            border: "1px solid rgba(247, 144, 9, 0.4)",
            padding: "2px 8px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: 700,
            display: "inline-flex",
            alignItems: "center",
            gap: "4px",
          }}
        >
          ● Medium ({score})
        </span>
      );
    }
    return (
      <span
        style={{
          background: "rgba(18, 183, 106, 0.2)",
          color: "var(--success, #12B76A)",
          border: "1px solid rgba(18, 183, 106, 0.4)",
          padding: "2px 8px",
          borderRadius: "12px",
          fontSize: "11px",
          fontWeight: 700,
          display: "inline-flex",
          alignItems: "center",
          gap: "4px",
        }}
      >
        ● Low ({score})
      </span>
    );
  };

  return (
    <div style={{ animation: "fadeUp 0.4s ease", maxWidth: "1520px", margin: "0 auto", paddingBottom: "60px" }}>
      {/* ─── Ephemeral Memory Notification Banner ─── */}
      <div
        style={{
          background: "linear-gradient(90deg, rgba(136, 56, 221, 0.12) 0%, rgba(0, 240, 255, 0.05) 100%)",
          border: "1px solid rgba(136, 56, 221, 0.32)",
          borderRadius: "12px",
          padding: "14px 20px",
          marginBottom: "20px",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "16px",
          flexWrap: "wrap",
          backdropFilter: "blur(12px)",
          boxShadow: "0 4px 20px rgba(0, 0, 0, 0.25)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <div
            style={{
              width: "36px",
              height: "36px",
              borderRadius: "10px",
              background: "linear-gradient(135deg, var(--primary-color, #8838DD) 0%, var(--active-badge-background-color, #7727CD) 100%)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "#fff",
              flexShrink: 0,
              boxShadow: "0 2px 10px rgba(136, 56, 221, 0.4)",
            }}
          >
            <AnalysisNavIcon size={18} />
          </div>
          <div>
            <div style={{ fontSize: "14px", fontWeight: 700, color: "var(--text-main, #fff)", display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
              <span>Analysis — Multi-Platform Profile Scraper</span>
              <span
                style={{
                  background: "rgba(0, 240, 255, 0.12)",
                  color: "#00F0FF",
                  border: "1px solid rgba(0, 240, 255, 0.35)",
                  fontSize: "10px",
                  padding: "2px 8px",
                  borderRadius: "20px",
                  fontWeight: 700,
                  letterSpacing: "0.5px",
                  textTransform: "uppercase",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "5px",
                  boxShadow: "0 0 10px rgba(0, 240, 255, 0.15)",
                }}
              >
                <span style={{ width: "5px", height: "5px", borderRadius: "50%", background: "#00F0FF", boxShadow: "0 0 6px #00F0FF" }} />
                RAM Session
              </span>
            </div>
            <div style={{ fontSize: "12px", color: "var(--text-muted, #98a2b3)", marginTop: "3px" }}>
              Paste direct URLs across Facebook, Instagram, Twitter/X, YouTube, Telegram &amp; TikTok. Data lives in temporary memory only and is completely cleared on page refresh.
            </div>
          </div>
        </div>

        {jobData && (
          <button
            onClick={handleClear}
            style={{
              background: "rgba(255, 255, 255, 0.05)",
              border: "1px solid var(--border-color, #344054)",
              color: "var(--text-main, #fff)",
              padding: "7px 16px",
              borderRadius: "8px",
              fontSize: "12px",
              fontWeight: 600,
              cursor: "pointer",
              transition: "all 0.2s ease",
            }}
            onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = "rgba(255, 255, 255, 0.1)"; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = "rgba(255, 255, 255, 0.05)"; }}
            title="Reset form and clear in-memory state"
          >
            Reset Workspace
          </button>
        )}
      </div>

      {/* ─── Input & Configuration Card ─── */}
      <div
        className="home-card"
        style={{
          background: "linear-gradient(180deg, var(--bg-card, #1D2939) 0%, rgba(16, 24, 40, 0.95) 100%)",
          border: "1px solid var(--border-color, #344054)",
          borderRadius: "14px",
          padding: "24px",
          marginBottom: "24px",
          boxShadow: "0 10px 30px rgba(0, 0, 0, 0.35)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "16px", flexWrap: "wrap", gap: "10px" }}>
          <div>
            <div style={{ fontSize: "16px", fontWeight: 700, color: "var(--text-main, #fff)", display: "flex", alignItems: "center", gap: "8px" }}>
              <span style={{ color: "var(--primary-color, #8838DD)" }}>1.</span> Direct URLs to Analyze
            </div>
            <div style={{ fontSize: "12px", color: "var(--text-muted, #98a2b3)", marginTop: "2px" }}>
              Enter or paste URLs (one per line). Supported: Facebook, Instagram, X/Twitter, YouTube, Telegram, TikTok.
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <button
              type="button"
              onClick={() => setUrlInput(SAMPLE_URLS)}
              style={{
                background: "rgba(136, 56, 221, 0.12)",
                border: "1px solid rgba(136, 56, 221, 0.35)",
                color: "var(--cyan-bright, #9A50E9)",
                padding: "6px 14px",
                borderRadius: "8px",
                fontSize: "12px",
                cursor: "pointer",
                fontWeight: 600,
                transition: "all 0.15s ease",
              }}
              onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = "rgba(136, 56, 221, 0.22)"; }}
              onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = "rgba(136, 56, 221, 0.12)"; }}
            >
              Load Sample URLs
            </button>
            {urlInput && (
              <button
                type="button"
                onClick={() => setUrlInput("")}
                style={{
                  background: "transparent",
                  border: "none",
                  color: "var(--text-muted, #98a2b3)",
                  fontSize: "12px",
                  cursor: "pointer",
                  padding: "6px 8px",
                  transition: "color 0.15s ease",
                }}
                onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.color = "var(--danger, #E95053)"; }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.color = "var(--text-muted, #98a2b3)"; }}
              >
                Clear URLs
              </button>
            )}
          </div>
        </div>

        {/* Textarea */}
        <textarea
          value={urlInput}
          onChange={(e) => setUrlInput(e.target.value)}
          placeholder={`https://www.facebook.com/sample_account\nhttps://www.instagram.com/sample_profile\nhttps://x.com/sample_user\nhttps://www.youtube.com/@channel_name\nhttps://t.me/sample_channel\nhttps://www.tiktok.com/@sample_creator`}
          rows={6}
          disabled={loading}
          style={{
            width: "100%",
            background: "var(--bg-primary, #080F1E)",
            border: "1px solid var(--border-color, #344054)",
            borderRadius: "10px",
            color: "var(--text-main, #fff)",
            padding: "14px 16px",
            fontSize: "13px",
            fontFamily: "var(--font-mono, monospace)",
            lineHeight: "1.65",
            resize: "vertical",
            outline: "none",
            boxSizing: "border-box",
            marginBottom: "14px",
            boxShadow: "inset 0 2px 8px rgba(0, 0, 0, 0.4)",
            transition: "border-color 0.2s ease, box-shadow 0.2s ease",
          }}
          onFocus={(e) => {
            e.currentTarget.style.borderColor = "var(--primary-color, #8838DD)";
            e.currentTarget.style.boxShadow = "0 0 0 2px rgba(136, 56, 221, 0.25), inset 0 2px 8px rgba(0, 0, 0, 0.4)";
          }}
          onBlur={(e) => {
            e.currentTarget.style.borderColor = "var(--border-color, #344054)";
            e.currentTarget.style.boxShadow = "inset 0 2px 8px rgba(0, 0, 0, 0.4)";
          }}
        />

        {/* Live Detected Platforms Breakdown */}
        {urlSummary.totalLines > 0 && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              flexWrap: "wrap",
              marginBottom: "18px",
              padding: "10px 14px",
              background: "rgba(8, 15, 30, 0.6)",
              border: "1px solid rgba(255, 255, 255, 0.06)",
              borderRadius: "10px",
              fontSize: "12px",
            }}
          >
            <span style={{ fontWeight: 600, color: "var(--text-dim, #98a2b3)", marginRight: "4px" }}>
              Detected ({urlSummary.validCount}/{urlSummary.totalLines}):
            </span>
            {Object.entries(urlSummary.breakdown).map(([plat, count]) => {
              if (plat === "other") {
                return (
                  <span
                    key={plat}
                    style={{
                      background: "rgba(233, 80, 83, 0.15)",
                      color: "var(--danger, #E95053)",
                      border: "1px solid rgba(233, 80, 83, 0.35)",
                      padding: "3px 10px",
                      borderRadius: "20px",
                      fontSize: "11px",
                      fontWeight: 600,
                    }}
                  >
                    {count} Unsupported
                  </span>
                );
              }
              const platformAccents: Record<string, { bg: string; border: string; color: string }> = {
                facebook: { bg: "rgba(24, 119, 242, 0.14)", border: "rgba(24, 119, 242, 0.35)", color: "#6ea8fe" },
                instagram: { bg: "rgba(228, 64, 95, 0.14)", border: "rgba(228, 64, 95, 0.35)", color: "#ff758f" },
                twitter: { bg: "rgba(255, 255, 255, 0.08)", border: "rgba(255, 255, 255, 0.22)", color: "#ffffff" },
                youtube: { bg: "rgba(255, 0, 0, 0.14)", border: "rgba(255, 0, 0, 0.35)", color: "#ff6b6b" },
                telegram: { bg: "rgba(34, 158, 217, 0.14)", border: "rgba(34, 158, 217, 0.35)", color: "#5bc0eb" },
                tiktok: { bg: "rgba(254, 44, 85, 0.14)", border: "rgba(37, 244, 238, 0.35)", color: "#25f4ee" },
              };
              const accent = platformAccents[plat] || {
                bg: "rgba(136, 56, 221, 0.18)",
                border: "rgba(136, 56, 221, 0.4)",
                color: "var(--text-main, #fff)",
              };
              return (
                <span
                  key={plat}
                  style={{
                    background: accent.bg,
                    color: accent.color,
                    border: `1px solid ${accent.border}`,
                    padding: "3px 10px",
                    borderRadius: "20px",
                    fontSize: "11px",
                    fontWeight: 600,
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "6px",
                  }}
                >
                  <PlatformIcon platform={plat} size={13} />
                  <span>{plat.charAt(0).toUpperCase() + plat.slice(1)}</span>
                  <span
                    style={{
                      background: "rgba(255, 255, 255, 0.15)",
                      borderRadius: "10px",
                      padding: "0 5px",
                      fontSize: "10px",
                      fontWeight: 700,
                    }}
                  >
                    {count}
                  </span>
                </span>
              );
            })}
          </div>
        )}

        {/* Action Button Bar */}
        <div style={{ display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" }}>
          {!loading ? (
            <button
              type="button"
              onClick={handleStart}
              disabled={!urlSummary.validCount}
              style={{
                background: urlSummary.validCount
                  ? "linear-gradient(135deg, var(--primary-color, #8838DD) 0%, var(--active-badge-background-color, #7727CD) 100%)"
                  : "var(--bg-surface-3, #344054)",
                color: "#fff",
                border: "none",
                padding: "10px 24px",
                borderRadius: "8px",
                fontSize: "13.5px",
                fontWeight: 700,
                cursor: urlSummary.validCount ? "pointer" : "not-allowed",
                boxShadow: urlSummary.validCount ? "0 4px 18px rgba(136, 56, 221, 0.45)" : "none",
                display: "inline-flex",
                alignItems: "center",
                gap: "8px",
                transition: "all 0.2s ease",
              }}
            >
              <AnalysisNavIcon size={16} />
              <span>Start Analysis ({urlSummary.validCount || 0})</span>
            </button>
          ) : (
            <button
              type="button"
              onClick={handleCancel}
              disabled={cancelling}
              style={{
                background: "var(--danger, #E95053)",
                color: "#fff",
                border: "none",
                padding: "10px 24px",
                borderRadius: "8px",
                fontSize: "14px",
                fontWeight: 700,
                cursor: "pointer",
                display: "inline-flex",
                alignItems: "center",
                gap: "8px",
              }}
            >
              <StopIcon size={16} />
              <span>{cancelling ? "Stopping..." : "Stop Analysis"}</span>
            </button>
          )}
        </div>
      </div>

      {/* ─── Live Execution Progress Card ─── */}
      {jobData && (
        <div
          style={{
            background: "var(--bg-card, #1D2939)",
            border: "1px solid var(--border-color, #344054)",
            borderRadius: "12px",
            padding: "20px",
            marginBottom: "24px",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "12px", flexWrap: "wrap", gap: "10px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
              <div
                style={{
                  width: "10px",
                  height: "10px",
                  borderRadius: "50%",
                  background:
                    jobData.status === "running"
                      ? "var(--cyan, #00F0FF)"
                      : jobData.status === "done"
                      ? "var(--success, #12B76A)"
                      : "var(--danger, #E95053)",
                  boxShadow: jobData.status === "running" ? "0 0 10px var(--cyan, #00F0FF)" : "none",
                }}
              />
              <span style={{ fontSize: "14px", fontWeight: 700, color: "var(--text-main, #fff)" }}>
                Analysis Progress: {jobData.completed} / {jobData.total} Finished
              </span>
              <span
                style={{
                  fontSize: "11px",
                  color: "var(--text-muted, #98a2b3)",
                  background: "var(--bg-surface-3, #344054)",
                  padding: "2px 8px",
                  borderRadius: "4px",
                  textTransform: "uppercase",
                  fontWeight: 600,
                }}
              >
                {jobData.status}
              </span>
            </div>

            <div style={{ fontSize: "13px", fontWeight: 600, color: "var(--text-main, #fff)" }}>
              {Math.round((jobData.completed / (jobData.total || 1)) * 100)}%
            </div>
          </div>

          {/* Progress Bar */}
          <div
            style={{
              width: "100%",
              height: "6px",
              background: "var(--bg-primary, #080F1E)",
              borderRadius: "3px",
              overflow: "hidden",
              marginBottom: "16px",
            }}
          >
            <div
              style={{
                width: `${Math.min(100, Math.round((jobData.completed / (jobData.total || 1)) * 100))}%`,
                height: "100%",
                background: "linear-gradient(90deg, var(--cyan, #00F0FF), var(--primary-color, #8838DD))",
                transition: "width 0.3s ease",
              }}
            />
          </div>

          {/* Platform Status Chips */}
          <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
            {Object.entries(jobData.platform_progress || {}).map(([plat, prog]) => (
              <div
                key={plat}
                style={{
                  background: "var(--bg-surface-3, #344054)",
                  border: "1px solid var(--border-color, #475467)",
                  borderRadius: "8px",
                  padding: "6px 12px",
                  fontSize: "12px",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "8px",
                }}
              >
                <PlatformIcon platform={plat} size={16} />
                <span style={{ fontWeight: 600, color: "var(--text-main, #fff)" }}>{prog.display_name}:</span>
                <span style={{ color: "var(--text-dim, #98a2b3)" }}>
                  {prog.completed}/{prog.total}
                </span>
                <span
                  style={{
                    fontSize: "10px",
                    fontWeight: 700,
                    textTransform: "uppercase",
                    color:
                      prog.status === "done"
                        ? "var(--success, #12B76A)"
                        : prog.status === "running"
                        ? "var(--cyan, #00F0FF)"
                        : prog.status === "failed"
                        ? "var(--danger, #E95053)"
                        : "var(--text-muted)",
                  }}
                >
                  {prog.status}
                </span>
              </div>
            ))}
          </div>

          {/* Live per-URL status -- concurrency is 1 per platform, so at
              most one item is "running" for a given platform at a time;
              this shows exactly which one, not just an aggregate count.
              The full scraped data (editable, exportable) is in the table
              below once a URL finishes -- this is just "what's happening
              right now", not a second copy of the results. */}
          <div style={{ marginTop: "12px", display: "flex", flexDirection: "column", gap: "4px" }}>
            {jobData.items.map((it) => (
              <div
                key={it.id}
                style={{
                  display: "flex", alignItems: "center", gap: "8px", fontSize: "12px",
                  padding: "4px 8px", borderRadius: "6px",
                  background: it.status === "running" ? "rgba(0, 240, 255, 0.06)" : "transparent",
                }}
              >
                <PlatformIcon platform={it.platform} size={13} />
                <span
                  style={{
                    fontSize: "10px", fontWeight: 700, textTransform: "uppercase", width: "56px", flexShrink: 0,
                    color:
                      it.status === "done" ? "var(--success, #12B76A)"
                      : it.status === "running" ? "var(--cyan, #00F0FF)"
                      : it.status === "error" ? "var(--danger, #E95053)"
                      : "var(--text-muted)",
                  }}
                >
                  {it.status === "running" ? "⏳ live" : it.status}
                </span>
                <span style={{ color: "var(--text-dim, #98a2b3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={it.url}>
                  {it.url}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ─── Dual-Format Results & Export Section ─── */}
      {jobData && jobData.items.length > 0 && (
        <div
          className="home-card"
          style={{
            background: "var(--bg-card, #1D2939)",
            border: "1px solid var(--border-color, #344054)",
            borderRadius: "12px",
            padding: "24px",
            boxShadow: "0 8px 24px rgba(0, 0, 0, 0.25)",
          }}
        >
          {/* Format Toggle & Export Toolbar */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: "20px",
              flexWrap: "wrap",
              gap: "16px",
              paddingBottom: "16px",
              borderBottom: "1px solid var(--border-color, #344054)",
            }}
          >
            {/* Format Mode Tabs */}
            <div style={{ display: "flex", alignItems: "center", gap: "6px", background: "var(--bg-primary, #080F1E)", padding: "4px", borderRadius: "8px" }}>
              <button
                type="button"
                onClick={() => setFormatMode("incident")}
                style={{
                  background: formatMode === "incident" ? "var(--primary-color, #8838DD)" : "transparent",
                  color: "#fff",
                  border: "none",
                  padding: "7px 16px",
                  borderRadius: "6px",
                  fontSize: "13px",
                  fontWeight: formatMode === "incident" ? 700 : 500,
                  cursor: "pointer",
                  transition: "all 0.2s ease",
                }}
              >
                📋 Platform / Incident Format (Takedown)
              </button>

              <button
                type="button"
                onClick={() => setFormatMode("legacy")}
                style={{
                  background: formatMode === "legacy" ? "var(--primary-color, #8838DD)" : "transparent",
                  color: "#fff",
                  border: "none",
                  padding: "7px 16px",
                  borderRadius: "6px",
                  fontSize: "13px",
                  fontWeight: formatMode === "legacy" ? 700 : 500,
                  cursor: "pointer",
                  transition: "all 0.2s ease",
                }}
              >
                📊 Legacy Format (Raw Analysis)
              </button>
            </div>

            {/* Export Actions */}
            <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
              <button
                type="button"
                onClick={() => handleExport("xlsx")}
                disabled={exporting || !filteredItems.length}
                style={{
                  background: "var(--success, #12B76A)",
                  color: "#fff",
                  border: "none",
                  padding: "7px 14px",
                  borderRadius: "6px",
                  fontSize: "12px",
                  fontWeight: 700,
                  cursor: "pointer",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "6px",
                }}
              >
                <DownloadIcon size={14} />
                <span>Excel (.xlsx)</span>
              </button>

              <button
                type="button"
                onClick={() => handleExport("csv")}
                disabled={exporting || !filteredItems.length}
                style={{
                  background: "var(--bg-surface-3, #344054)",
                  border: "1px solid var(--border-color, #475467)",
                  color: "var(--text-main, #fff)",
                  padding: "7px 12px",
                  borderRadius: "6px",
                  fontSize: "12px",
                  fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                CSV
              </button>

              <button
                type="button"
                onClick={() => handleExport("json")}
                disabled={exporting || !filteredItems.length}
                style={{
                  background: "var(--bg-surface-3, #344054)",
                  border: "1px solid var(--border-color, #475467)",
                  color: "var(--text-main, #fff)",
                  padding: "7px 12px",
                  borderRadius: "6px",
                  fontSize: "12px",
                  fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                JSON
              </button>

              <button
                type="button"
                onClick={() => handleExport("tsv")}
                disabled={exporting || !filteredItems.length}
                style={{
                  background: "var(--bg-surface-3, #344054)",
                  border: "1px solid var(--border-color, #475467)",
                  color: "var(--text-main, #fff)",
                  padding: "7px 12px",
                  borderRadius: "6px",
                  fontSize: "12px",
                  fontWeight: 600,
                  cursor: "pointer",
                }}
                title="Copy formatted TSV to paste directly into Google Sheets or Excel"
              >
                Copy TSV
              </button>
            </div>
          </div>

          {/* Filter & Search Bar */}
          <div style={{ display: "flex", alignItems: "center", gap: "12px", marginBottom: "16px", flexWrap: "wrap" }}>
            {/* Search Box */}
            <div style={{ position: "relative", minWidth: "260px", flex: 1 }}>
              <SearchIcon size={14} style={{ position: "absolute", left: "10px", top: "10px", color: "var(--text-muted)" }} />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search username, url, bio, location..."
                style={{
                  width: "100%",
                  background: "var(--bg-primary, #080F1E)",
                  border: "1px solid var(--border-color, #344054)",
                  borderRadius: "6px",
                  color: "var(--text-main, #fff)",
                  padding: "7px 10px 7px 32px",
                  fontSize: "12px",
                  outline: "none",
                  boxSizing: "border-box",
                }}
              />
            </div>

            {/* Platform Filter */}
            <select
              value={platformFilter}
              onChange={(e) => setPlatformFilter(e.target.value)}
              style={{
                background: "var(--bg-primary, #080F1E)",
                border: "1px solid var(--border-color, #344054)",
                color: "var(--text-main, #fff)",
                padding: "7px 12px",
                borderRadius: "6px",
                fontSize: "12px",
                outline: "none",
                cursor: "pointer",
              }}
            >
              <option value="all">All Platforms</option>
              <option value="facebook">Facebook</option>
              <option value="instagram">Instagram</option>
              <option value="twitter">Twitter / X</option>
              <option value="youtube">YouTube</option>
              <option value="telegram">Telegram</option>
              <option value="tiktok">TikTok</option>
            </select>

            {/* Risk Filter */}
            <select
              value={riskFilter}
              onChange={(e) => setRiskFilter(e.target.value)}
              style={{
                background: "var(--bg-primary, #080F1E)",
                border: "1px solid var(--border-color, #344054)",
                color: "var(--text-main, #fff)",
                padding: "7px 12px",
                borderRadius: "6px",
                fontSize: "12px",
                outline: "none",
                cursor: "pointer",
              }}
            >
              <option value="all">All Risk Levels</option>
              <option value="high">High Risk (5-9)</option>
              <option value="low">Low Risk (0-4)</option>
            </select>

            <span style={{ fontSize: "12px", color: "var(--text-muted)", marginLeft: "auto" }}>
              Showing {filteredItems.length} of {jobData.items.length} items
            </span>
          </div>

          {/* ─── Interactive Table View ─── */}
          <div style={{ overflowX: "auto", border: "1px solid var(--border-color, #344054)", borderRadius: "10px", boxShadow: "0 4px 20px rgba(0,0,0,0.15)" }}>
            {/* NOT width:"100%" -- an 11-column table pinned to 100% of the
                scroll wrapper's width has nowhere to grow, so the browser
                squeezes every column down to fit instead, cutting long
                values off mid-word (that's what the wrapper's own
                overflowX:auto was supposed to prevent, and couldn't, because
                a width:100% table never actually exceeds its container in
                the first place -- there was nothing left TO scroll to).
                width:"max-content" lets the table size itself to what its
                columns actually need; the wrapper scrolls to it instead. */}
            <table style={{ width: "max-content", minWidth: "100%", borderCollapse: "collapse", fontSize: "12.5px", textAlign: "left" }}>
              <thead>
                <tr style={{ background: "var(--bg-primary, #080F1E)", color: "var(--text-dim, #98a2b3)", borderBottom: "2px solid var(--border-color, #344054)" }}>
                  <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", textAlign: "center" }}>Screenshot</th>
                  {formatMode === "incident" ? (
                    <>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Platform</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Profile / Account</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Risk Rating</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Asset Name</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Active</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Name Match</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Logo Match</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Followers</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Last Post</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Location</th>
                    </>
                  ) : (
                    <>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Original Name</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Original feed</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>IMPERSONATED</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Profile name</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Created Date</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Logo (Yes / No)</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Followers</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Active (Yes / No)</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Name (Yes / No)</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Location</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Last Post (DD-MM-YYYY) (Optional)</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Risk Score</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>priority</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Date</th>
                      <th style={{ padding: "12px 14px", fontWeight: 700, fontSize: "10.5px", letterSpacing: "0.06em", textTransform: "uppercase", whiteSpace: "nowrap" }}>Comments</th>
                    </>
                  )}
                </tr>
              </thead>
              <tbody>
                {!filteredItems.length ? (
                  <tr>
                    <td colSpan={12} style={{ padding: "32px", textAlign: "center", color: "var(--text-muted)" }}>
                      No matching records found.
                    </td>
                  </tr>
                ) : (
                  filteredItems.map((it) => {
                    const originalRow = formatMode === "incident" ? it.incident_row : it.legacy_row;
                    const row = { ...originalRow, ...(edits[it.id] || {}) };
                    const screenshotUrl = it.has_screenshot ? analysisApi.getScreenshotUrl(jobData.job_id, it.id) : null;
                    return (
                      <tr
                        key={it.id}
                        style={{
                          borderBottom: "1px solid var(--border-color, #293546)",
                          background: it.status === "error" ? "rgba(233, 80, 83, 0.05)" : "transparent",
                          transition: "background 0.15s ease",
                        }}
                        onMouseEnter={(e) => { if (it.status !== "error") e.currentTarget.style.background = "rgba(0, 229, 255, 0.03)"; }}
                        onMouseLeave={(e) => { if (it.status !== "error") e.currentTarget.style.background = "transparent"; }}
                      >
                        {/* Screenshot -- a large enough thumbnail to actually
                            read at a glance, full-size preview on hover
                            (click also works, for touch devices). */}
                        <td style={{ padding: "10px 14px", textAlign: "center" }}>
                          {screenshotUrl ? (
                            <img
                              src={screenshotUrl}
                              alt=""
                              onMouseEnter={() => setPreviewScreenshot({ url: screenshotUrl, profileName: it.profile_name || it.entity_id })}
                              onMouseLeave={() => setPreviewScreenshot(null)}
                              onClick={() => setPreviewScreenshot({ url: screenshotUrl, profileName: it.profile_name || it.entity_id })}
                              style={{
                                width: "128px", height: "96px", objectFit: "cover", objectPosition: "top",
                                borderRadius: "8px", cursor: "zoom-in", border: "1px solid var(--border-color, #344054)",
                                boxShadow: "0 2px 8px rgba(0,0,0,0.25)", transition: "transform 0.15s ease, box-shadow 0.15s ease",
                              }}
                              onMouseOver={(e) => { e.currentTarget.style.transform = "scale(1.04)"; e.currentTarget.style.boxShadow = "0 4px 16px rgba(0,229,255,0.25)"; }}
                              onMouseOut={(e) => { e.currentTarget.style.transform = "none"; e.currentTarget.style.boxShadow = "0 2px 8px rgba(0,0,0,0.25)"; }}
                            />
                          ) : (
                            <div style={{ width: "128px", height: "96px", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--bg-primary, #080F1E)", borderRadius: "8px", border: "1px dashed var(--border-color, #344054)", margin: "0 auto" }}>
                              <span style={{ color: "var(--text-muted)", fontSize: "11px" }}>No capture</span>
                            </div>
                          )}
                        </td>

                        {/* Mode Specific Columns */}
                        {formatMode === "incident" ? (
                          <>
                            {/* Platform */}
                            <td style={{ padding: "10px 14px", whiteSpace: "nowrap" }}>
                              <span style={{ display: "inline-flex", alignItems: "center", gap: "6px", fontWeight: 600, color: "var(--text-main, #fff)" }}>
                                <PlatformIcon platform={it.platform} size={16} />
                                {it.platform_name}
                              </span>
                            </td>

                            {/* Profile Info */}
                            <td style={{ padding: "10px 14px", minWidth: "220px" }}>
                              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                                {it.profile_image_url ? (
                                  <img
                                    src={it.profile_image_url}
                                    alt=""
                                    style={{ width: "26px", height: "26px", borderRadius: "50%", objectFit: "cover", flexShrink: 0 }}
                                    onError={(e) => {
                                      (e.target as HTMLElement).style.display = "none";
                                    }}
                                  />
                                ) : (
                                  <div
                                    style={{
                                      width: "26px",
                                      height: "26px",
                                      borderRadius: "50%",
                                      background: "var(--bg-surface-3, #344054)",
                                      display: "flex",
                                      alignItems: "center",
                                      justifyContent: "center",
                                      fontSize: "11px",
                                      fontWeight: 700,
                                      flexShrink: 0,
                                    }}
                                  >
                                    {(it.profile_name || it.entity_id || "?").charAt(0).toUpperCase()}
                                  </div>
                                )}

                                <div style={{ minWidth: 0 }}>
                                  <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
                                    <a
                                      href={it.url}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                      style={{ fontWeight: 600, color: "var(--cyan, #00F0FF)", textDecoration: "none", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                                    >
                                      {it.profile_name || it.entity_id || "Profile Link"}
                                    </a>
                                    {it.verified && <VerifiedBadgeIcon size={14} />}
                                  </div>
                                  {it.error ? (
                                    <div style={{ fontSize: "11px", color: "var(--danger, #E95053)" }}>
                                      {it.error}
                                    </div>
                                  ) : (
                                    <div style={{ fontSize: "11px", color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                      {it.entity_id}
                                    </div>
                                  )}
                                </div>
                              </div>
                            </td>

                            {/* Risk Rating */}
                            <td style={{ padding: "10px 14px", whiteSpace: "nowrap" }}>
                              {getRiskBadge(computeDynamicRisk(row, formatMode))}
                            </td>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["AssetName"] || "")} onChange={(v) => handleEdit(it.id, "AssetName", v)} /></td>
                            <td style={{ padding: "0", textAlign: "center" }}><ToggleCell value={String(row["Active (Yes/No)"] || "")} onChange={(v) => handleEdit(it.id, "Active (Yes/No)", v)} /></td>
                            <td style={{ padding: "0", textAlign: "center" }}><ToggleCell value={String(row["Name (Yes/No)"] || "")} defaultWhenEmpty="Yes" onChange={(v) => handleEdit(it.id, "Name (Yes/No)", v)} /></td>
                            <td style={{ padding: "0", textAlign: "center" }}><ToggleCell value={String(row["Logo (Yes/No)"] || "")} defaultWhenEmpty="Yes" onChange={(v) => handleEdit(it.id, "Logo (Yes/No)", v)} /></td>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["Number of Followers"] ?? "")} onChange={(v) => handleEdit(it.id, "Number of Followers", v)} /></td>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["Last Post (DD-MM-YYYY) (Optional)"] || "")} onChange={(v) => handleEdit(it.id, "Last Post (DD-MM-YYYY) (Optional)", v)} /></td>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["Location"] || "")} onChange={(v) => handleEdit(it.id, "Location", v)} /></td>
                          </>
                        ) : (
                          <>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["Original Name"] || "")} onChange={(v) => handleEdit(it.id, "Original Name", v)} /></td>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["Original feed"] || "")} onChange={(v) => handleEdit(it.id, "Original feed", v)} /></td>
                            <td style={{ padding: "10px 14px", maxWidth: "200px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              <a href={it.url} target="_blank" rel="noopener noreferrer" style={{ color: "var(--cyan, #00F0FF)", textDecoration: "none" }}>{it.url}</a>
                            </td>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["Profile name"] || "")} onChange={(v) => handleEdit(it.id, "Profile name", v)} /></td>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["Created Date"] || "")} onChange={(v) => handleEdit(it.id, "Created Date", v)} /></td>
                            <td style={{ padding: "0", textAlign: "center" }}><ToggleCell value={String(row["Logo (Yes / No)"] || "")} defaultWhenEmpty="Yes" onChange={(v) => handleEdit(it.id, "Logo (Yes / No)", v)} /></td>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["Followers"] ?? "")} onChange={(v) => handleEdit(it.id, "Followers", v)} /></td>
                            <td style={{ padding: "0", textAlign: "center" }}><ToggleCell value={String(row["Active (Yes / No)"] || "")} onChange={(v) => handleEdit(it.id, "Active (Yes / No)", v)} /></td>
                            <td style={{ padding: "0", textAlign: "center" }}><ToggleCell value={String(row["Name (Yes / No)"] || "")} defaultWhenEmpty="Yes" onChange={(v) => handleEdit(it.id, "Name (Yes / No)", v)} /></td>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["Location"] || "")} onChange={(v) => handleEdit(it.id, "Location", v)} /></td>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["Last Post (DD-MM-YYYY) (Optional)"] || "")} onChange={(v) => handleEdit(it.id, "Last Post (DD-MM-YYYY) (Optional)", v)} /></td>
                            <td style={{ padding: "10px 14px", whiteSpace: "nowrap" }}>
                              {getRiskBadge(computeDynamicRisk(row, formatMode))}
                            </td>
                            <td style={{ padding: "0" }}><EditableCell value={getPriorityFromRisk(computeDynamicRisk(row, formatMode))} onChange={() => {}} readOnly={true} /></td>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["Date"] || "")} onChange={(v) => handleEdit(it.id, "Date", v)} /></td>
                            <td style={{ padding: "0" }}><EditableCell value={String(row["Comments"] || "")} onChange={(v) => handleEdit(it.id, "Comments", v)} /></td>
                          </>
                        )}
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ─── Screenshot Modal Preview ─── */}
      {previewScreenshot && (
        <div
          onClick={() => setPreviewScreenshot(null)}
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            background: "rgba(0, 0, 0, 0.85)",
            backdropFilter: "blur(6px)",
            zIndex: 9999,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "20px",
            pointerEvents: "none",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: "var(--bg-card, #1D2939)",
              border: "1px solid var(--border-color, #344054)",
              borderRadius: "14px",
              padding: "18px",
              maxWidth: "96vw",
              maxHeight: "96vh",
              display: "flex",
              flexDirection: "column",
              boxShadow: "0 24px 60px rgba(0,0,0,0.6)",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "14px" }}>
              <span style={{ fontSize: "15px", fontWeight: 700, color: "var(--text-main, #fff)", display: "flex", alignItems: "center", gap: "8px" }}>
                🔍 Evidence Capture — {previewScreenshot.profileName}
              </span>
              <span style={{ fontSize: "11px", color: "var(--text-dim, #98a2b3)" }}>Move away to close</span>
            </div>
            <div style={{ overflow: "auto", maxHeight: "calc(96vh - 90px)", borderRadius: "8px" }}>
              <img
                src={previewScreenshot.url}
                alt="Profile Evidence Capture"
                style={{ maxWidth: "100%", maxHeight: "calc(96vh - 90px)", height: "auto", display: "block", borderRadius: "8px" }}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
