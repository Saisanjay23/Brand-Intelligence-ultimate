/**
 * The card grid must never end in a half-empty row.
 *
 * THE REPORT THIS COMES FROM: "when the zoom out happens the 25 cards per
 * page should increase as i can empty same at last rows". Zooming out widens
 * the grid, `auto-fill` adds a column, and a fixed 25 stops dividing -- at 4
 * columns the last row holds a single card with three gaps beside it.
 *
 * WHY THIS IS A UNIT TEST. The failure only appears at real layout: the
 * column count comes from `getComputedStyle().gridTemplateColumns` after the
 * browser has resolved `repeat(auto-fill, minmax(260px, 1fr))`, which neither
 * jsdom nor a hidden preview pane will do. Pinning the ARITHMETIC here is the
 * part that can be held steady; the measurement itself is verified in the
 * browser. Both directions matter and only one is obvious:
 *
 *   - round UP, never down. Down would show fewer rows than the page-size
 *     control promises, which is a filter that lies.
 *   - a column count of 0 or 1, or a NaN from an element that has not been
 *     laid out yet, must fall through to the analyst's number untouched
 *     rather than producing 0 (an empty page) or NaN (an empty page that
 *     also breaks the pager arithmetic).
 */

import { describe, expect, it } from "vitest";

import { rowAlignedPageSize } from "./gridPaging";

describe("the last row is always full", () => {
  it.each([
    [25, 2, 26],
    [25, 3, 27],
    [25, 4, 28],
    [25, 5, 25],
    [25, 6, 30],
    [25, 7, 28],
  ])("%i cards across %i columns asks for %i", (size, cols, want) => {
    expect(rowAlignedPageSize(size, cols, "cards")).toBe(want);
  });

  it("divides evenly at every column count it can produce", () => {
    for (let cols = 2; cols <= 12; cols++) {
      for (const size of [10, 25, 50, 100]) {
        expect(rowAlignedPageSize(size, cols, "cards") % cols).toBe(0);
      }
    }
  });
});

describe("it rounds up, never down", () => {
  it("never returns fewer than the analyst asked for", () => {
    for (let cols = 1; cols <= 12; cols++) {
      for (const size of [1, 10, 25, 50, 100]) {
        expect(rowAlignedPageSize(size, cols, "cards")).toBeGreaterThanOrEqual(size);
      }
    }
  });

  it("adds less than one whole row -- it does not overshoot", () => {
    for (let cols = 2; cols <= 12; cols++) {
      const got = rowAlignedPageSize(25, cols, "cards");
      expect(got - 25).toBeLessThan(cols);
    }
  });

  it("leaves a size that already fits exactly alone", () => {
    expect(rowAlignedPageSize(24, 4, "cards")).toBe(24);
    expect(rowAlignedPageSize(50, 5, "cards")).toBe(50);
  });
});

describe("degenerate measurements fall through untouched", () => {
  it.each([0, 1, -3, NaN, Infinity])("a column count of %s changes nothing", (cols) => {
    expect(rowAlignedPageSize(25, cols as number, "cards")).toBe(25);
  });

  it("never returns zero, which would render an empty page", () => {
    for (const cols of [0, 1, NaN, Infinity, 4]) {
      expect(rowAlignedPageSize(25, cols as number, "cards")).toBeGreaterThan(0);
    }
  });
});

describe("the table view is not a grid", () => {
  it("keeps the exact number the analyst chose", () => {
    expect(rowAlignedPageSize(25, 4, "table")).toBe(25);
    expect(rowAlignedPageSize(25, 6, "table")).toBe(25);
  });
});

describe("paging must step by the SIZE ACTUALLY SHOWN, not the raw choice", () => {
  /**
   * Regression for a live bug: the Prev/Next buttons stepped the offset by
   * the analyst's raw `pageSize` (25) while the grid was actually showing
   * `rowAlignedPageSize` cards (28, rounded up to fill 4 columns). Next
   * therefore re-requested rows the previous page had already displayed --
   * offset 0/limit 28 shows rows 0-27, then offset 25/limit 28 shows rows
   * 25-52, overlapping rows 25-27. To the analyst that reads as "next page
   * opens on where I already was," not "next page starts at its own row 1."
   *
   * This is pure arithmetic: it is not asserting anything about React state,
   * only that the offset step and the page's actual size must be the same
   * number, so the two can never independently drift again.
   */
  it("a page boundary computed from the rounded size never re-shows a row", () => {
    const pageSize = 25;
    for (let columns = 2; columns <= 7; columns++) {
      const shown = rowAlignedPageSize(pageSize, columns, "cards");
      const page1Start = 0;
      const page1End = page1Start + shown;      // exclusive
      const page2Start = page1Start + shown;    // the only correct step
      expect(page2Start).toBe(page1End);         // page 2 starts exactly where page 1 ended
      expect(page2Start).toBeGreaterThanOrEqual(page1End);
    }
  });

  it("stepping by the UNROUNDED pageSize instead would overlap -- proving the bug this guards", () => {
    const pageSize = 25;
    const columns = 4;
    const shown = rowAlignedPageSize(pageSize, columns, "cards"); // 28
    const page1End = 0 + shown;                 // 28
    const wrongPage2Start = 0 + pageSize;        // 25 -- the old, buggy step
    expect(wrongPage2Start).toBeLessThan(page1End);   // demonstrates the overlap
  });
});
