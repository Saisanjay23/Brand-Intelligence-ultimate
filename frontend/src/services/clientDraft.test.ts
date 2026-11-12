/**
 * Unsaved client forms must survive leaving the page.
 *
 * THE DEFECT THESE GUARD. Switching tabs while creating a client (or while
 * editing one's keywords or scrape caps) threw the unsaved form away. The
 * draft store is what the Clients page reads back when it opens again.
 */
import { beforeEach, describe, expect, it } from "vitest";
import {
  clearDraft,
  clearDraftTexts,
  editKey,
  isBlankDraft,
  loadDraft,
  loadUi,
  saveDraft,
  saveUi,
  type ClientFormDraft,
} from "./clientDraft";

const blank = (): ClientFormDraft => ({
  id: "",
  name: "",
  domain: "",
  nameGroups: [],
  domainGroups: [],
  limitsIndividual: {},
  limitsDomain: {},
  fbTabLimits: {
    people: { individual: "", domain: "" },
    pages: { individual: "", domain: "" },
    groups: { individual: "", domain: "" },
  },
  cron: "",
});

beforeEach(() => localStorage.clear());

describe("client drafts", () => {
  it("round-trips a half-built new client", () => {
    const d = {
      ...blank(),
      id: "acme",
      name: "Acme",
      nameGroups: [{ parent: "Jane Doe", children: ["jane.doe"] }],
      limitsIndividual: { facebook: "50" },
    };
    saveDraft("new", d);
    expect(loadDraft("new")).toEqual(d);
  });

  it("keeps each client's edits apart from the new-client form", () => {
    saveDraft("new", { ...blank(), id: "new-one" });
    saveDraft(editKey("acme"), { ...blank(), id: "acme", domain: "acme.com" });
    expect(loadDraft("new")?.id).toBe("new-one");
    expect(loadDraft(editKey("acme"))?.domain).toBe("acme.com");
    expect(loadDraft(editKey("other"))).toBeNull();
  });

  it("forgets a draft once cleared", () => {
    saveDraft("new", { ...blank(), id: "acme" });
    clearDraft("new");
    expect(loadDraft("new")).toBeNull();
  });

  it("drops a corrupted entry instead of half-applying it", () => {
    localStorage.setItem("bi.clientDraft.new", JSON.stringify({ id: 5, nameGroups: "x" }));
    expect(loadDraft("new")).toBeNull();
    expect(localStorage.getItem("bi.clientDraft.new")).toBeNull();
    localStorage.setItem("bi.clientDraft.new", "{not json");
    expect(loadDraft("new")).toBeNull();
  });

  it("treats only a truly empty form as blank", () => {
    expect(isBlankDraft(blank())).toBe(true);
    expect(isBlankDraft({ ...blank(), limitsDomain: { twitter: "  " } })).toBe(true);
    expect(isBlankDraft({ ...blank(), limitsDomain: { twitter: "10" } })).toBe(false);
    expect(isBlankDraft({
      ...blank(),
      fbTabLimits: { ...blank().fbTabLimits, pages: { individual: "5", domain: "" } },
    })).toBe(false);
    expect(isBlankDraft({ ...blank(), domainGroups: [{ parent: "acme", children: [] }] })).toBe(false);
  });

  it("remembers which mode and tab the page was on", () => {
    expect(loadUi()).toBeNull();
    saveUi({ mode: "create", workspaceTab: "limits" });
    expect(loadUi()).toEqual({ mode: "create", workspaceTab: "limits" });
  });

  it("clears half-typed keyword boxes for one owner only", () => {
    localStorage.setItem("bi.clientDraft.text.new.individual.parent", '"Jane"');
    localStorage.setItem("bi.clientDraft.text.acme.domain.parent", '"acme"');
    clearDraftTexts("new.");
    expect(localStorage.getItem("bi.clientDraft.text.new.individual.parent")).toBeNull();
    expect(localStorage.getItem("bi.clientDraft.text.acme.domain.parent")).toBe('"acme"');
  });
});
