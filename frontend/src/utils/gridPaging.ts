/**
 * How many profiles to request so the card grid never ends in a ragged row.
 *
 * The grid is `repeat(auto-fill, minmax(260px, 1fr))`, so the column count is
 * decided by the browser at layout time -- it changes with the window AND with
 * zoom, and the app cannot know it without measuring. A fixed page size of 25
 * therefore leaves a gap at most widths: 4 columns is 6 full rows plus one
 * lonely card, 6 columns is 4 rows plus four.
 *
 * The rule is to round the analyst's chosen size UP to a whole number of rows.
 * Up, never down, because rounding down would silently show FEWER results than
 * the page-size control claims -- a filter that lies is worse than a gap.
 */
export function rowAlignedPageSize(
  pageSize: number,
  columns: number,
  viewMode: "cards" | "table",
): number {
  // The table has no columns to fill, so it keeps the exact number.
  if (viewMode !== "cards") return pageSize;
  // A single column is already row-aligned; so is a nonsense measurement.
  if (!Number.isFinite(columns) || columns <= 1) return pageSize;
  return Math.ceil(pageSize / columns) * columns;
}
