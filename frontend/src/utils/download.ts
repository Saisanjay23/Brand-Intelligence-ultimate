// Generic browser download trigger, no domain knowledge, reusable
// anywhere a Blob needs to become a saved file.
export function download(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  downloadBlob(filename, blob);
}

// Same trigger, for a Blob the caller already has (e.g. a binary file
// fetched from the server) rather than string content to wrap.
export function downloadBlob(filename: string, blob: Blob) {
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  a.click();
  // Released on the next turn, not straight after click(): Firefox and
  // Safari start reading the blob asynchronously, and revoking it in the
  // same tick can cancel the download before it begins.
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

// Flat records -> CSV text. Column order is the first row's own key order,
// so callers control layout by controlling the row shape, not this function.
//
// Cell values here come from scraped, attacker-influenced social-media
// profile content (an impersonator's own display name, bio, etc), and these
// exports are analyst-facing takedown reports meant to be opened in Excel,
// so a leading =, +, -, or @ is formula-injection, not just a CSV quoting
// edge case (CWE-1236). Prefixing with a bare `'` neutralizes it in Excel
// without changing the visible text, same mitigation OWASP recommends.
export function rowsToCsv(rows: Record<string, unknown>[]): string {
  if (!rows.length) return "";
  const cols = Object.keys(rows[0]);
  const esc = (v: unknown) => {
    let s = v === null || v === undefined ? "" : String(v);
    if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [cols.map(esc).join(",")];
  for (const r of rows) lines.push(cols.map((c) => esc(r[c])).join(","));
  // Leading UTF-8 BOM: without it, Excel opens a UTF-8 CSV using the
  // system's ANSI code page instead of UTF-8, so any scraped non-ASCII
  // text (Arabic/Chinese/Cyrillic, or even just an accented Latin name)
  // renders as mojibake the moment the file is double-clicked open --
  // the BOM is what makes Excel's own sniffer pick UTF-8. Every other
  // UTF-8 CSV reader (Python's `utf-8-sig`, pandas, Node) already strips
  // a leading BOM transparently, so this costs nothing there.
  return "﻿" + lines.join("\n");
}

// Flat records -> TSV text, designed for clipboard copying and direct
// column/row pasting into Microsoft Excel and Google Sheets.
export function rowsToTsv(rows: Record<string, unknown>[]): string {
  if (!rows.length) return "";
  const cols = Object.keys(rows[0]);
  const esc = (v: unknown) => {
    let s = v === null || v === undefined ? "" : String(v);
    if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
    s = s.replace(/\t/g, " ").replace(/\r?\n/g, " ");
    return s;
  };
  const lines = [cols.map(esc).join("\t")];
  for (const r of rows) lines.push(cols.map((c) => esc(r[c])).join("\t"));
  return lines.join("\n");
}
