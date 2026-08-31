// Which of a client's keywords are "individual" (executive/person names)
// vs "domain" (brand/product keywords) -- the distinction the old app's
// "Individual + Domain" filter read from a per-client keyword_groups
// record the backend used to persist (backend/shared/keywords.py). The
// rebuilt discovery API takes a flat keyword list with no category tag at
// all, and there is no /clients backend left to persist this against
// (see HomeView.tsx's saveConfig). So it's remembered here instead: local
// to this browser, keyed by client id, written whenever a client is saved
// (whether that save persisted server-side or fell back to local-only) and
// read by DiscoveryProfileGrid to classify each profile's own `keywords[]`
// against these two sets client-side.

const KEY = "bi_client_keyword_categories";

export interface KeywordCategories {
  individual: string[];
  domain: string[];
}

function loadAll(): Record<string, KeywordCategories> {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Record<string, KeywordCategories>) : {};
  } catch {
    return {};
  }
}

export function saveClientKeywords(clientId: string, individual: string[], domain: string[]): void {
  try {
    const all = loadAll();
    all[clientId] = { individual, domain };
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch {
    // storage unavailable, the filter just won't have data for this client
  }
}

export function getClientKeywords(clientId: string): KeywordCategories {
  return loadAll()[clientId] || { individual: [], domain: [] };
}
