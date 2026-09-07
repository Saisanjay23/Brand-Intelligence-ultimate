/**
 * Clients live in the database, and each one is its own record.
 *
 * THE DEFECTS THESE GUARD
 *   1. CLIENTS MERGED. Saving went through one upsert keyed on the org id,
 *      so entering a NEW client under an id that was already taken did not
 *      fail -- it overwrote that client's keywords, caps and cron with the
 *      new one's and reported success. Two customers, one record, the
 *      first one's configuration gone. Create and edit are separate calls
 *      now; the server refuses a create on a taken id.
 *
 *   2. THE MIGRATION COULD EAT THE ONLY COPY. Clients created before the
 *      database existed sit in localStorage. Moving them and then clearing
 *      that key is only safe if every client actually landed -- clearing
 *      after a failed POST would delete the sole copy.
 *
 *   3. ONE CLIENT'S KEYWORDS SHOWING UNDER ANOTHER. `keywordCategories`
 *      feeds the Individual/Domain filter; it must read the record for the
 *      client asked about and no other.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const listClients = vi.fn();
const createClient = vi.fn();
const updateClient = vi.fn();
const deleteClient = vi.fn();

vi.mock("../api/clientsApi", () => ({
  clientsApi: {
    listClients: (...a: unknown[]) => listClients(...a),
    createClient: (...a: unknown[]) => createClient(...a),
    updateClient: (...a: unknown[]) => updateClient(...a),
    deleteClient: (...a: unknown[]) => deleteClient(...a),
  },
}));

function client(id: string, over: Record<string, unknown> = {}) {
  return {
    client_id: id,
    name: id,
    domain: "",
    name_keywords: [`${id} ceo`],
    domain_keywords: [id],
    keyword_groups: {},
    platform_limits_individual: {},
    platform_limits_domain: {},
    platform_tab_limits: {},
    order: 0,
    cron: null,
    ...over,
  };
}

async function loadStore() {
  vi.resetModules();
  return await import("./clientDirectory");
}

beforeEach(() => {
  localStorage.clear();
  listClients.mockReset();
  createClient.mockReset();
  updateClient.mockReset();
  deleteClient.mockReset();
});

afterEach(() => {
  localStorage.clear();
});

describe("each client is its own record", () => {
  it("keeps one client's keywords out of another's", async () => {
    const store = await loadStore();
    listClients.mockResolvedValue({
      items: [
        client("alpha", { name_keywords: ["alpha ceo"], domain_keywords: ["alpha"] }),
        client("beta", { name_keywords: ["beta ceo"], domain_keywords: ["beta"] }),
      ],
    });
    await store.refresh();

    expect(store.keywordCategories("alpha")).toEqual({
      individual: ["alpha ceo"],
      domain: ["alpha"],
    });
    expect(store.keywordCategories("beta")).toEqual({
      individual: ["beta ceo"],
      domain: ["beta"],
    });
    // A client that is not in the directory contributes nothing rather
    // than borrowing whichever record happens to be first.
    expect(store.keywordCategories("nobody")).toEqual({ individual: [], domain: [] });
  });

  it("surfaces the server's refusal to overwrite a taken org id", async () => {
    const store = await loadStore();
    listClients.mockResolvedValue({ items: [client("alpha")] });
    createClient.mockRejectedValue(new Error("client id 'alpha' is already taken"));

    await expect(store.createClient(client("alpha") as never)).rejects.toThrow(/already taken/);
    // ...and the existing record is untouched by the attempt.
    await store.refresh();
    expect(store.findClient("alpha")?.name_keywords).toEqual(["alpha ceo"]);
  });
});

describe("loading", () => {
  it("shares one request when several components ask at once", async () => {
    const store = await loadStore();
    listClients.mockResolvedValue({ items: [client("alpha")] });

    await Promise.all([store.refresh(), store.refresh(), store.refresh()]);

    expect(listClients).toHaveBeenCalledTimes(1);
    expect(store.listClients()).toHaveLength(1);
  });

  it("reports an unreachable database instead of looking empty", async () => {
    const store = await loadStore();
    listClients.mockRejectedValue(new Error("failed to fetch"));

    await store.refresh();

    expect(store.getSnapshot().error).toMatch(/failed to fetch/);
    // `loaded` still flips, so the UI can tell "asked and failed" apart
    // from "not asked yet" -- both of which otherwise render as no clients.
    expect(store.getSnapshot().loaded).toBe(true);
  });
});

describe("migrating clients out of localStorage", () => {
  const KEY = "bi_saved_clients";

  it("moves them, then stops reading that key", async () => {
    const store = await loadStore();
    localStorage.setItem(KEY, JSON.stringify({ legacy: client("legacy") }));
    createClient.mockResolvedValue(client("legacy"));

    const moved = await store.migrateLegacyLocalClients();

    expect(moved).toBe(1);
    expect(createClient).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it("KEEPS the local copy when the server could not take it", async () => {
    const store = await loadStore();
    localStorage.setItem(KEY, JSON.stringify({ legacy: client("legacy") }));
    createClient.mockRejectedValue(new Error("failed to fetch"));

    const moved = await store.migrateLegacyLocalClients();

    expect(moved).toBe(0);
    // Clearing here would destroy the only copy of that client there is.
    expect(localStorage.getItem(KEY)).not.toBeNull();
  });

  it("treats an id the database already has as migrated, not as a failure", async () => {
    const store = await loadStore();
    localStorage.setItem(KEY, JSON.stringify({ legacy: client("legacy") }));
    createClient.mockRejectedValue(new Error("client id 'legacy' is already taken"));

    await store.migrateLegacyLocalClients();

    // The client IS in the database, which is the whole goal -- so the
    // local copy is safe to drop.
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it("survives a corrupt stored value without throwing", async () => {
    const store = await loadStore();
    localStorage.setItem(KEY, "null");
    await expect(store.migrateLegacyLocalClients()).resolves.toBe(0);
    localStorage.setItem(KEY, "{not json");
    await expect(store.migrateLegacyLocalClients()).resolves.toBe(0);
  });
});
