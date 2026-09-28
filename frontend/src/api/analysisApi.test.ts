/**
 * The Analysis table scrolls endlessly through every saved result, so the
 * list behind it has to actually BE every saved result. One request stops at
 * the server's 1000-row ceiling; the rows past it used to vanish while the
 * counter still read "Showing N of N".
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { analysisApi } from "./analysisApi";

const row = (i: number) => ({ id: `r${i}`, result_id: `r${i}`, url: `https://x.com/${i}` });

afterEach(() => vi.unstubAllGlobals());

describe("listResults", () => {
  it("walks every page until the total is reached", async () => {
    const total = 2300;
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (u: string) => {
      calls.push(u);
      const q = new URL(u, "http://t").searchParams;
      const offset = Number(q.get("offset")), limit = Number(q.get("limit"));
      const items = Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, k) => row(offset + k));
      return new Response(JSON.stringify({ items, total, retention_hours: 24 }), { status: 200 });
    });

    const res = await analysisApi.listResults("acme");
    expect(res.items).toHaveLength(total);
    expect(new Set(res.items.map((i) => i.result_id)).size).toBe(total);
    expect(calls).toHaveLength(3);
    expect(calls[2]).toContain("offset=2000");
    expect(calls[0]).toContain("org_id=acme");
  });

  it("stops on an empty page even if the total disagrees", async () => {
    let n = 0;
    vi.stubGlobal("fetch", async () => {
      n += 1;
      const items = n === 1 ? [row(0), row(1)] : [];
      return new Response(JSON.stringify({ items, total: 99, retention_hours: 24 }), { status: 200 });
    });
    const res = await analysisApi.listResults("");
    expect(res.items).toHaveLength(2);
    expect(n).toBe(2);
  });
});
