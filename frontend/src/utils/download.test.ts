/**
 * THE REPORT THIS COMES FROM: "excel and csv exports should [have] whatever
 * the text is there or scraped, [in the] same language, without any
 * gibberish errors." A scraped profile's non-ASCII text (Arabic, Chinese,
 * Cyrillic, or even just an accented Latin name) came through correctly
 * everywhere EXCEPT the raw .csv file opened in Excel: without a leading
 * UTF-8 BOM, Excel's own file sniffer falls back to the system's ANSI code
 * page instead of UTF-8, and decodes every multi-byte UTF-8 sequence as if
 * it were single-byte ANSI -- the classic "mojibake" symptom. The XLSX
 * export (openpyxl, a self-describing UTF-8 XML container) and the TSV
 * export (real Unicode text on the OS clipboard, not sniffed file bytes)
 * never had this problem; only the bare CSV file did.
 */

import { describe, expect, it } from "vitest";

import { rowsToCsv } from "./download";

describe("rowsToCsv", () => {
  it("leads with a UTF-8 BOM so Excel reads it as UTF-8, not ANSI", () => {
    const csv = rowsToCsv([{ Name: "test" }]);
    expect(csv.charCodeAt(0)).toBe(0xfeff);
  });

  it("keeps non-ASCII scraped text intact after the BOM", () => {
    const csv = rowsToCsv([
      { "Profile name": "Валерий Иванов" },
      { "Profile name": "عبدالله" },
      { "Profile name": "李雷" },
    ]);
    expect(csv).toContain("Валерий Иванов");
    expect(csv).toContain("عبدالله");
    expect(csv).toContain("李雷");
  });

  it("still quotes/escapes values the same way it always did", () => {
    const csv = rowsToCsv([{ a: 'has "quotes", and a comma' }]);
    const [, dataLine] = csv.split("\n");
    expect(dataLine).toBe(`"has ""quotes"", and a comma"`);
  });
});
