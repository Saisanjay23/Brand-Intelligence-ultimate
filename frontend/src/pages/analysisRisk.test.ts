import { describe, expect, it } from "vitest";
import { computeDynamicRisk } from "./AnalysisView";

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
