// The client directory: every client, read from the database.
//
// THE STORE IS THE SERVER. Clients used to live in this browser's
// localStorage, which meant a client existed only on the machine that
// created it -- not shareable, not backed up, and invisible to the server
// that actually runs the sweeps. They are Mongo documents now
// (backend/api/clients.py), and this module is the one place the frontend
// reads or writes them.
//
// WHY THERE IS A CACHE AT ALL. Several callers need a client SYNCHRONOUSLY
// while rendering -- DiscoveryProfileGrid classifies each profile's
// keywords as it draws a row, Live Activity's coverage view lists clients
// that have never been queued. Those cannot await. So the list is fetched
// once, kept here, and handed out synchronously; anything that mutates it
// refreshes it. Components subscribe with useClientDirectory().
//
// EACH CLIENT IS ITS OWN RECORD. Nothing in here merges two clients or
// carries one client's keywords into another: every entry is one server
// document keyed by its own org id, and the two keyword lists (individual
// names vs domain/brand terms) stay separate exactly as curated.

import { useSyncExternalStore } from "react";
import { clientsApi, type ClientEdit } from "../api/clientsApi";
import type { Client, ClientConfig } from "../api/types";

export interface DirectoryState {
  clients: Client[];
  // False until the first load finishes, so a caller can tell "no clients"
  // apart from "not asked yet" -- an empty directory and an unreachable
  // database look identical otherwise.
  loaded: boolean;
  loading: boolean;
  // Set when the last load failed. The database is the only store now, so
  // this is a real outage the analyst has to see, not something to swallow.
  error: string;
}

let state: DirectoryState = { clients: [], loaded: false, loading: false, error: "" };
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

function setState(patch: Partial<DirectoryState>): void {
  state = { ...state, ...patch };
  emit();
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getSnapshot(): DirectoryState {
  return state;
}

// Synchronous read of the cache, for render paths that cannot await.
// Empty before the first refresh() resolves.
export function listClients(): Client[] {
  return state.clients;
}

export function findClient(clientId: string): Client | undefined {
  return state.clients.find((c) => c.client_id === clientId);
}

// Synchronously patch one client in the local cache, immediately emitting
// to all subscribers so the UI and runner reflect changes in real time.
export function patchClientLocal(clientId: string, patch: Partial<Client>): void {
  const next = state.clients.map((c) => (c.client_id === clientId ? { ...c, ...patch } : c));
  setState({ clients: next });
}

// A client's two curated keyword lists. These come off the client's own
// record now; they used to be mirrored into localStorage by whichever save
// happened last, which meant a browser that had never saved a client had no
// idea how to categorise its profiles.
export function keywordCategories(clientId: string): { individual: string[]; domain: string[] } {
  const client = findClient(clientId);
  return {
    individual: client?.name_keywords ?? [],
    domain: client?.domain_keywords ?? [],
  };
}

// ------------------------------------------------------------------ loading

let inFlight: Promise<void> | null = null;

export function refresh(): Promise<void> {
  // Four components mount at once on a page load and all want the list.
  // One request, shared.
  if (inFlight) return inFlight;
  setState({ loading: true });
  inFlight = clientsApi
    .listClients()
    .then((res) => setState({ clients: res.items, loaded: true, loading: false, error: "" }))
    .catch((e: Error) =>
      setState({
        loading: false,
        loaded: true,
        error: e.message || "could not reach the clients database",
      }),
    )
    .finally(() => {
      inFlight = null;
    });
  return inFlight;
}

// ------------------------------------------------------------------ writes

export async function createClient(config: ClientConfig): Promise<Client> {
  const created = await clientsApi.createClient(config);
  await refresh();
  return created;
}

export async function updateClient(clientId: string, config: ClientEdit): Promise<Client> {
  const saved = await clientsApi.updateClient(clientId, config);
  await refresh();
  return saved;
}

export async function deleteClient(clientId: string): Promise<void> {
  await clientsApi.deleteClient(clientId);
  await refresh();
}

// ---------------------------------------------------------------- migration

// Clients created before the database existed are sitting in this browser's
// localStorage. Move them once, then stop reading that key forever.
//
// Failures are per client and never fatal: a 409 means the server already
// has that org id (someone else migrated it, or it was created directly),
// which is a success for our purposes -- the client exists in the database,
// which is the whole goal.
const LEGACY_CLIENTS = "bi_saved_clients";
const LEGACY_KEYWORDS = "bi_client_keyword_categories";

export async function migrateLegacyLocalClients(): Promise<number> {
  let stored: Record<string, Client>;
  try {
    const raw = localStorage.getItem(LEGACY_CLIENTS);
    if (!raw) return 0;
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      localStorage.removeItem(LEGACY_CLIENTS);
      return 0;
    }
    stored = parsed as Record<string, Client>;
  } catch {
    return 0;
  }

  const local = Object.values(stored).filter((c) => c && c.client_id);
  if (!local.length) {
    localStorage.removeItem(LEGACY_CLIENTS);
    localStorage.removeItem(LEGACY_KEYWORDS);
    return 0;
  }

  let moved = 0;
  let hardFailure = false;
  for (const c of local) {
    try {
      await clientsApi.createClient({
        client_id: c.client_id,
        name: c.name || c.client_id,
        domain: c.domain || "",
        name_keywords: c.name_keywords || [],
        domain_keywords: c.domain_keywords || [],
        keyword_groups: c.keyword_groups || {},
        platform_limits_individual: c.platform_limits_individual || {},
        platform_limits_domain: c.platform_limits_domain || {},
        platform_tab_limits: c.platform_tab_limits || {},
        cron: c.cron ?? null,
      });
      moved += 1;
    } catch (e) {
      // "already taken" means it is in the database, which is what we
      // wanted. Anything else (server down, network) must NOT clear the
      // local copy -- that would delete the only copy there is.
      if (!/already taken/i.test((e as Error).message)) hardFailure = true;
    }
  }

  if (!hardFailure) {
    localStorage.removeItem(LEGACY_CLIENTS);
    localStorage.removeItem(LEGACY_KEYWORDS);
  }
  return moved;
}

// ---------------------------------------------------------------------- hook

export function useClientDirectory(): DirectoryState {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
