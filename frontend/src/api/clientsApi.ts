// The clients backend (backend/api/clients.py). One org record per client
// id, stored in Mongo -- keywords, per-platform caps and cron included.
//
// CREATE AND EDIT ARE DIFFERENT CALLS ON PURPOSE. Saving used to be a
// single upsert keyed on the org id, so entering a NEW client under an id
// that was already taken overwrote that client's whole configuration and
// reported success. `createClient` is refused with 409 when the id exists,
// and `updateClient` is refused with 404 when it does not, so neither can
// quietly become the other.
import { json, post, url } from "./httpClient";
import type { Client, ClientConfig } from "./types";

const jsonInit = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// The config half of a client, without the id -- an edit takes the id from
// the path, since an org id is chosen once and never changed (everything
// already discovered is filed under it).
export type ClientEdit = Omit<ClientConfig, "client_id">;

export const clientsApi = {
  listClients: () => fetch(url("/clients")).then(json<{ items: Client[] }>),

  getClient: (clientId: string) =>
    fetch(url(`/clients/${encodeURIComponent(clientId)}`)).then(json<Client>),

  // 409 when the org id is taken. The thrown Error carries the backend's
  // own `detail`, which names the clash -- show it, do not swallow it.
  createClient: (body: ClientConfig) => post("/clients", body).then(json<Client>),

  updateClient: (clientId: string, body: ClientEdit) =>
    fetch(url(`/clients/${encodeURIComponent(clientId)}`), jsonInit("PUT", body)).then(json<Client>),

  deleteClient: (clientId: string) =>
    fetch(url(`/clients/${encodeURIComponent(clientId)}`), { method: "DELETE" }).then(json<Client>),

  // What the Scheduler runs for this client. A NARROW write: it cannot
  // disturb keywords or caps, and saving the client from the Clients form
  // cannot reset it.
  setSchedulerPrefs: (
    clientId: string,
    prefs: {
      platforms?: string[];
      keyword_scope?: string;
      facebook_tabs?: string[];
      budget_minutes?: number;
    },
  ) =>
    fetch(url(`/clients/${encodeURIComponent(clientId)}/scheduler-prefs`),
          jsonInit("PUT", prefs)).then(json<Client>),

  // The full desired order, front to back.
  reorderClients: (clientIds: string[]) =>
    fetch(url("/clients/reorder"), jsonInit("PUT", { client_ids: clientIds })).then(
      json<{ items: Client[] }>,
    ),
};
