import { describe, expect, it } from "vitest";
import { computeDynamicRisk, isSettled } from "./AnalysisView";

// A blank Logo cell means the picture was never settled -- it must score
// like "No", never like "Yes" (a logo alone would force the top tiers).
describe("computeDynamicRisk logo handling", () => {
  const base = { "Name (Yes/No)": "Yes", "Active (Yes/No)": "No", Location: "" };

  it("scores a blank logo exactly like No", () => {
    const blank = computeDynamicRisk({ ...base, "Logo (Yes/No)": "" }, "incident");
    const missing = computeDynamicRisk({ ...base }, "incident");
    const no = computeDynamicRisk({ ...base, "Logo (Yes/No)": "No" }, "incident");
    expect(blank).toBe(no);
    expect(missing).toBe(no);
  });

  it("still scores a real Yes higher", () => {
    const yes = computeDynamicRisk({ ...base, "Logo (Yes/No)": "Yes" }, "incident");
    const no = computeDynamicRisk({ ...base, "Logo (Yes/No)": "No" }, "incident");
    expect(yes).toBeGreaterThan(no);
  });
});

// A row still pending/running has an empty row, and scoring `{}` gives a
// confident "3 / Low". Such rows must be treated as unknown, not low: they
// show "—", match no risk filter and stay out of exports.
describe("isSettled", () => {
  it("is false while a row is pending or running", () => {
    expect(isSettled({ status: "pending" })).toBe(false);
    expect(isSettled({ status: "running" })).toBe(false);
  });

  it("is true once the row is done or failed", () => {
    expect(isSettled({ status: "done" })).toBe(true);
    expect(isSettled({ status: "error" })).toBe(true);
  });

  it("an empty unsettled row would otherwise score a misleading Low", () => {
    expect(computeDynamicRisk({}, "incident")).toBe(3);
  });
});
