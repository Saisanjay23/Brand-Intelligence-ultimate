/**
 * Scrape limits are read strictly, and unsaved edits are noticed.
 *
 * THE DEFECTS THIS GUARDS
 *   - A cap typed as "-5", "0.5" or "1e99" was dropped without a word and
 *     saved as "no cap" -- an UNLIMITED sweep behind a green "Saved" tick.
 *   - Discover is built from the SAVED client, so a keyword added on screen
 *     and not yet saved was never searched.
 *   - "https://acme.com" pasted as a keyword was split at its colon into the
 *     parent "https" and a child "//acme.com".
 */

import { describe, expect, it } from "vitest";

import { parseBulkKeywordGroups } from "./keywordGroups";
import {
  parseCap,
  parseCapMap,
  parseSweepSeconds,
  parseTabCaps,
  sameCaps,
  sameKeywordGroups,
} from "./scrapeLimits";

describe("parseCap", () => {
  it("reads a whole number", () => {
    expect(parseCap("50", "x")).toBe(50);
    expect(parseCap(" 7 ", "x")).toBe(7);
  });

  it("treats blank and 0 as unlimited, the two things the screen says mean that", () => {
    expect(parseCap("", "x")).toBeNull();
    expect(parseCap("   ", "x")).toBeNull();
    expect(parseCap("0", "x")).toBeNull();
  });

  it.each(["-5", "0.5", "1.5", "abc", "1e99", "Infinity", "NaN", "2000000"])(
    "refuses %s instead of quietly saving it as unlimited",
    (bad) => {
      expect(() => parseCap(bad, "Facebook individual cap")).toThrow(/Facebook individual cap/);
    },
  );
});

describe("parseCapMap / parseTabCaps", () => {
  it("leaves unlimited platforms out and names the bad box", () => {
    expect(parseCapMap({ facebook: "20", twitter: "", instagram: "0" }, String)).toEqual({
      facebook: 20,
    });
    expect(() => parseCapMap({ facebook: "20", twitter: "-1" }, (p) => `${p} cap`)).toThrow(
      /twitter cap/,
    );
  });

  it("builds the stored tab shape and drops empty tabs", () => {
    expect(
      parseTabCaps({
        people: { individual: "5", domain: "" },
        pages: { individual: "", domain: "" },
        groups: { individual: "0", domain: "9" },
      }),
    ).toEqual({ people: { individual: 5 }, groups: { domain: 9 } });
  });

  it("refuses a bad tab cap", () => {
    expect(() => parseTabCaps({ people: { individual: "x", domain: "" } })).toThrow(/people/);
  });
});

describe("parseSweepSeconds", () => {
  it("is blank for the server default and minutes*60 otherwise", () => {
    expect(parseSweepSeconds("")).toBeUndefined();
    expect(parseSweepSeconds("15")).toBe(900);
    expect(parseSweepSeconds("0.5")).toBe(30);
  });

  it.each(["0", "-3", "abc", "100000"])("refuses %s", (bad) => {
    expect(() => parseSweepSeconds(bad)).toThrow(/Sweep time budget/);
  });
});

describe("sameKeywordGroups", () => {
  const saved = [{ parent: "Gautam Adani", children: ["gautamadani", "adani gautam"] }];

  it("is true for the same keywords, however spaced or cased", () => {
    expect(
      sameKeywordGroups(saved, [
        { parent: "gautam  adani", children: ["GautamAdani", "Adani Gautam"] },
      ]),
    ).toBe(true);
  });

  it("notices a new parent, a new permutation and a removed one", () => {
    expect(sameKeywordGroups(saved, [...saved, { parent: "Karan Adani", children: [] }])).toBe(false);
    expect(
      sameKeywordGroups(saved, [{ parent: "Gautam Adani", children: ["gautamadani"] }]),
    ).toBe(false);
    expect(
      sameKeywordGroups(saved, [
        { parent: "Gautam Adani", children: ["gautamadani", "adani gautam", "g.adani"] },
      ]),
    ).toBe(false);
  });

  it("ignores a blank parent row", () => {
    expect(sameKeywordGroups(saved, [...saved, { parent: "  ", children: [] }])).toBe(true);
  });
});

describe("sameCaps", () => {
  it("treats 0 and absent as the same, and ignores key order", () => {
    expect(sameCaps({ a: 1, b: 0 }, { a: 1 })).toBe(true);
    expect(sameCaps({ a: 1, b: 2 }, { b: 2, a: 1 })).toBe(true);
    expect(sameCaps(undefined, {})).toBe(true);
  });

  it("notices a changed or added cap, including inside a tab", () => {
    expect(sameCaps({ a: 1 }, { a: 2 })).toBe(false);
    expect(sameCaps({ a: 1 }, { a: 1, b: 5 })).toBe(false);
    expect(sameCaps({ people: { individual: 5 } }, { people: { individual: 6 } })).toBe(false);
    expect(sameCaps({ people: {} }, {})).toBe(true);
  });
});

describe("a URL is a keyword, not a separator", () => {
  it("keeps https://acme.com whole", () => {
    expect(parseBulkKeywordGroups("https://acme.com")).toEqual([
      { parent: "https://acme.com", children: [] },
    ]);
  });

  it("still splits a real 'parent: children' line, even with a URL child", () => {
    expect(parseBulkKeywordGroups("Acme: https://acme.com, acmecorp")).toEqual([
      { parent: "Acme", children: ["https://acme.com", "acmecorp"] },
    ]);
  });
});
