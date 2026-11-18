/**
 * Selection and the Copy menus on the discovery grid.
 *
 * Ticks survive paging, so "Selected" must mean every tick, not the ticks
 * that happen to be on the current page. "All Filtered" must mean the whole
 * filtered tab, not the page on screen. A filter that changes the server's
 * result set must drop the old ticks, or a bulk action acts on rows the
 * analyst can no longer see. Copy Table Data must be a real TSV.
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { DiscoveredProfile, ListProfilesQuery } from "../api/discoveryApi";

const listProfiles = vi.fn();

vi.mock("../api/discoveryApi", () => ({
  discoveryApi: { listProfiles: (q: ListProfilesQuery) => listProfiles(q) },
}));
vi.mock("../api/analysisApi", () => ({ analysisApi: {} }));
vi.mock("../hooks/useRefreshOnFocus", () => ({ useRefreshOnFocus: () => {} }));
vi.mock("../services/clientDirectory", () => ({ findClient: () => undefined }));
vi.mock("react-hot-toast", () => {
  const t = Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() });
  return { default: t };
});

import { DiscoveryProfileGrid } from "./DiscoveryProfileGrid";

function profile(i: number): DiscoveredProfile {
  return {
    id: `id${i}`,
    platform: "twitter",
    username: `user${i}`,
    display_name: i === 0 ? "Acme, Inc." : `Person ${i}`,
    url: `https://x.com/user${i}`,
    status: "pending",
    has_logo: null,
    verified: null,
    followers: null,
    friends: null,
    location: "",
    bio: "",
    created_at: "",
    keywords: ["acme"],
    name_score: null,
    name_exact_run: null,
    source: "",
    first_seen: "2020-01-01T00:00:00Z",
    last_seen: null,
  } as unknown as DiscoveredProfile;
}

const ALL = Array.from({ length: 26 }, (_, i) => profile(i));

let clipboard = "";

beforeEach(() => {
  clipboard = "";
  listProfiles.mockReset();
  listProfiles.mockImplementation(async (q: ListProfilesQuery) => {
    const offset = q.offset ?? 0;
    const limit = q.limit ?? 25;
    return {
      items: ALL.slice(offset, offset + limit),
      total: ALL.length,
      limit,
      offset,
      counts: { ages: { new: ALL.length, old: 0 }, validated_ages: { new: 0, old: 0 }, keywords: {} },
    };
  });
  // jsdom has no layout, so no scrollIntoView (the grid scrolls on paging).
  Element.prototype.scrollIntoView = () => {};
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: async (s: string) => { clipboard = s; } },
  });
});

const card = (name: string) => screen.getByText(name).closest(".profile-card") as HTMLElement;

async function renderGrid() {
  render(<DiscoveryProfileGrid groupId="acme" refreshKey={0} onAnalyseStarted={() => {}} />);
  await screen.findByText("Person 1");
}

describe("DiscoveryProfileGrid selection", () => {
  it("copies ticks from every page, not just the current one", async () => {
    await renderGrid();
    fireEvent.click(card("Person 1"));
    fireEvent.click(screen.getByText("Next →"));
    await screen.findByText("Person 25");
    fireEvent.click(card("Person 25"));

    fireEvent.click(screen.getByText(/Copy \(2\)/));
    await act(async () => { fireEvent.click(screen.getByText("Copy Selected URLs (2)")); });
    await waitFor(() => expect(clipboard).not.toBe(""));
    expect(clipboard.split("\n").sort()).toEqual(["https://x.com/user1", "https://x.com/user25"]);
  });

  it("copies the whole filtered tab as a real TSV when nothing is ticked", async () => {
    await renderGrid();
    fireEvent.click(screen.getByText("Copy ▾"));
    expect(screen.getByText("All Filtered (26)")).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByText("Copy Table Data")); });
    await waitFor(() => expect(clipboard).not.toBe(""));

    const lines = clipboard.split("\n");
    expect(lines).toHaveLength(27);           // header + every filtered row
    expect(clipboard.charCodeAt(0)).not.toBe(0xfeff);
    const header = lines[0].split("\t");
    const row0 = lines[1].split("\t");
    expect(row0).toHaveLength(header.length);
    expect(row0[header.indexOf("Display Name")]).toBe("Acme, Inc.");
    // Paged through the server with the current tab's filters.
    expect(listProfiles).toHaveBeenCalledWith(expect.objectContaining({ age: "new", status: "pending", limit: 1000, offset: 0 }));
  });

  it("drops ticks when a server-side filter changes the result set", async () => {
    await renderGrid();
    fireEvent.click(card("Person 1"));
    expect(screen.getByText("✅ Validate Selected (1)")).toBeInTheDocument();
    fireEvent.change(screen.getByTitle("How closely the scraped name matches the keyword that found it"), { target: { value: "high" } });
    await waitFor(() => expect(screen.queryByText(/Validate Selected/)).not.toBeInTheDocument());
  });
});
