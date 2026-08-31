import { useEffect, useState } from "react";
import type { Client } from "../api/types";
import { GlobeIcon, TagIcon, SearchIcon } from "./AppIcons";

// Full-text client finder: name, id, domain, or any configured keyword --
// broader than CustomClientSelect's own name/id-only filter, since this is
// meant to answer "which client did I set up that keyword for" as much as
// "pick a client I already know by name".
export function GlobalSearchModal({
  clients,
  onSelectClient,
  onClose,
}: {
  clients: Client[];
  onSelectClient: (clientId: string, name: string) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);

  const filtered = clients.filter((c) => {
    if (!query.trim()) return true;
    const q = query.toLowerCase();
    const matchesName = (c.name || "").toLowerCase().includes(q);
    const matchesId = c.client_id.toLowerCase().includes(q);
    const matchesDomain = (c.domain || "").toLowerCase().includes(q);
    const matchesKeywords = [
      ...(c.name_keywords || []),
      ...(c.domain_keywords || []),
    ].some((kw) => kw.toLowerCase().includes(q));
    return matchesName || matchesId || matchesDomain || matchesKeywords;
  });

  useEffect(() => {
    setSelectedIndex(0);
  }, [query]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIndex((prev) => (prev < filtered.length - 1 ? prev + 1 : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIndex((prev) => (prev > 0 ? prev - 1 : filtered.length - 1));
    } else if (e.key === "Enter" && filtered[selectedIndex]) {
      e.preventDefault();
      const chosen = filtered[selectedIndex];
      onSelectClient(chosen.client_id, chosen.name || chosen.client_id);
      onClose();
    } else if (e.key === "Escape") {
      onClose();
    }
  };

  return (
    <div className="global-search-modal" onClick={onClose}>
      <div className="global-search-box" onClick={(e) => e.stopPropagation()}>
        <div className="global-search-header">
          <span style={{ fontSize: "16px" }}>🔍</span>
          <input
            autoFocus
            type="text"
            className="global-search-input"
            placeholder="Search clients by name, ID, domain, or keyword..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
          />
          <span className="kbd-badge">Esc</span>
        </div>
        <div className="global-search-results">
          {!filtered.length ? (
            <div style={{ padding: "24px", textAlign: "center", color: "var(--text-dim)", fontSize: "13px" }}>
              No clients or keywords match "{query}"
            </div>
          ) : (
            filtered.map((c, i) => (
              <div
                key={c.client_id}
                className={`global-search-item ${selectedIndex === i ? "selected" : ""}`}
                onClick={() => {
                  onSelectClient(c.client_id, c.name || c.client_id);
                  onClose();
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                  <span className="client-avatar-sm">{(c.name || c.client_id).charAt(0).toUpperCase()}</span>
                  <div>
                    <div style={{ fontSize: "13px", fontWeight: 600, color: "var(--text-main)" }}>
                      {c.name || c.client_id}
                    </div>
                    <div style={{ fontSize: "11px", color: "var(--text-dim)", display: "flex", alignItems: "center", gap: "6px" }}>
                      <span style={{ display: "inline-flex", alignItems: "center", gap: "3px" }}>
                        <TagIcon size={11} color="var(--cyan)" /> {c.client_id}
                      </span>
                      {c.domain && (
                        <span style={{ display: "inline-flex", alignItems: "center", gap: "3px" }}>
                          · <GlobeIcon size={11} color="var(--cyan)" /> {c.domain}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                  {(c.name_keywords?.length || 0) > 0 && (
                    <span style={{ fontSize: "10px", color: "var(--text-dim)", background: "var(--bg-inner)", padding: "2px 6px", borderRadius: "4px" }}>
                      {c.name_keywords.length} names
                    </span>
                  )}
                  <span style={{ fontSize: "11px", color: "var(--cyan)" }}>Jump to Results →</span>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
