// The Clients Directory's actual home. POST/GET/DELETE /clients have no
// backend on the rebuilt API (only /discovery, /analysis, /sessions
// survive -- see HomeView.tsx's saveConfig), so a saved client used to
// live only in React state: gone the moment the page reloaded, which is
// why "Clients Directory" came back empty even for a client just created.
// This is the local, durable replacement -- the full Client record,
// keyed by client_id, in this browser's localStorage. Same pattern as
// clientKeywords.ts and recentClients.ts, just for the whole record
// instead of one derived slice of it.

import type { Client } from "../api/types";

const KEY = "bi_saved_clients";

function loadAll(): Record<string, Client> {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Record<string, Client>) : {};
  } catch {
    return {};
  }
}

function persist(all: Record<string, Client>): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch {
    // storage unavailable (private mode, quota) -- the client just won't
    // survive a reload, same degraded behaviour as before this existed
  }
}

export function listSavedClients(): Client[] {
  return Object.values(loadAll());
}

export function saveClientLocally(client: Client): void {
  const all = loadAll();
  all[client.client_id] = client;
  persist(all);
}

export function deleteClientLocally(clientId: string): void {
  const all = loadAll();
  delete all[clientId];
  persist(all);
}
