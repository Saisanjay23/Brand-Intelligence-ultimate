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

// Every column any row has, in first-seen order -- the first row's own key
// order first, then anything later rows add. Callers still control layout by
// controlling the row shape.
//
// NOT `Object.keys(rows[0])`, which is what this used to be. An analysis
// export taken while a job was still running could lead with a row whose
// data had not arrived yet (an `{}` plus the one computed column), and the
// whole file was then cut down to that one column: every other row's real
// values silently dropped, with nothing in the file to say so.
export function csvColumns(rows: Record<string, unknown>[]): string[] {
  const seen = new Set<string>();
  const cols: string[] = [];
  for (const r of rows) {
    for (const k of Object.keys(r)) {
      if (!seen.has(k)) { seen.add(k); cols.push(k); }
    }
  }
  return cols;
}

// Flat records -> CSV text. Columns: see csvColumns.
//
// Cell values here come from scraped, attacker-influenced social-media
// profile content (an impersonator's own display name, bio, etc), and these
// exports are analyst-facing takedown reports meant to be opened in Excel,
// so a leading =, +, -, or @ is formula-injection, not just a CSV quoting
// edge case (CWE-1236). Prefixing with a bare `'` neutralizes it in Excel
// without changing the visible text, same mitigation OWASP recommends.
export function rowsToCsv(rows: Record<string, unknown>[]): string {
  if (!rows.length) return "";
  const cols = csvColumns(rows);
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
  const cols = csvColumns(rows);
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
