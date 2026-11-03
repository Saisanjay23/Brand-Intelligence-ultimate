/**
 * Reading the scrape-limit boxes, and noticing when a client has edits that
 * were never saved.
 *
 * Kept here rather than inline in HomeView.tsx so the rules are testable
 * without rendering the page (see scrapeLimits.test.ts).
 *
 * THE DEFECT THIS CLOSES. The limits form parsed every box with
 * `Number(raw)` and quietly DROPPED anything that was not a positive finite
 * number: "-5", "0.5", "abc" (a type=number box still lets "e" and "-"
 * through) all saved as "no cap", i.e. UNLIMITED. An analyst who typed a cap
 * to protect a session got an uncapped sweep and a green "Saved" tick.
 * Invalid input is now refused out loud, and only a blank box or 0 -- what
 * the screen says means "Unlimited" -- is allowed to mean that.
 */

import type { KeywordGroup } from "../api/types";
import { cleanKeyword } from "./keywordGroups";

/** A sanity ceiling, not a policy: nobody means 10 billion results. */
export const MAX_CAP = 1_000_000;

/**
 * One cap box -> a positive whole number, or `null` for "unlimited" (blank
 * or 0). Throws, naming the box, for anything else.
 */
export function parseCap(raw: string, label: string): number | null {
  const text = (raw ?? "").trim();
  if (!text) return null;
  const n = Number(text);
  if (!Number.isInteger(n) || n < 0 || n > MAX_CAP) {
    throw new Error(
      `${label}: "${text}" is not a valid scrape limit. Use a whole number from 1 to ` +
        `${MAX_CAP.toLocaleString()}, or leave it empty (or 0) for unlimited.`,
    );
  }
  return n > 0 ? n : null;
}

/** `{platform: "50"}` -> `{platform: 50}`, unlimited entries left out. */
export function parseCapMap(
  raws: Record<string, string>,
  label: (key: string) => string,
): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [key, raw] of Object.entries(raws)) {
    const n = parseCap(raw, label(key));
    if (n !== null) out[key] = n;
  }
  return out;
}

/** What the Facebook sub-tab boxes hold: tab -> keyword type -> text. */
export type TabCapForm = Record<string, { individual: string; domain: string }>;

/** The form -> the stored `{tab: {kwType: cap}}` shape (empty tabs left out). */
export function parseTabCaps(form: TabCapForm): Record<string, Record<string, number>> {
  const out: Record<string, Record<string, number>> = {};
  for (const [tab, byType] of Object.entries(form)) {
    const perType: Record<string, number> = {};
    for (const [kwType, raw] of Object.entries(byType)) {
      const n = parseCap(raw, `Facebook ${tab} tab (${kwType})`);
      if (n !== null) perType[kwType] = n;
    }
    if (Object.keys(perType).length) out[tab] = perType;
  }
  return out;
}

/**
 * The "Sweep time budget (minutes)" box -> seconds for the API, or
 * `undefined` for the server default (blank). Throws for anything the API
 * would refuse anyway (0, negative, not a number) so the analyst reads why
 * instead of a bare 422.
 */
export function parseSweepSeconds(raw: string): number | undefined {
  const text = (raw ?? "").trim();
  if (!text) return undefined;
  const minutes = Number(text);
  if (!Number.isFinite(minutes) || minutes <= 0 || minutes > 24 * 60) {
    throw new Error(
      `Sweep time budget: "${text}" is not valid. Enter minutes between 1 and 1440, ` +
        "or leave it empty for the default.",
    );
  }
  return minutes * 60;
}

// ---------------------------------------------------------- unsaved edits

/** Ignores key order and case, so two equal maps compare equal. */
function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") {
    const o = value as Record<string, unknown>;
    return `{${Object.keys(o)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${stable(o[k])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

/** Do two lists of keyword groups search the same things, in the same order? */
export function sameKeywordGroups(a: KeywordGroup[], b: KeywordGroup[]): boolean {
  const norm = (gs: KeywordGroup[]) =>
    gs
      .map((g) => ({
        p: cleanKeyword(g.parent).toLowerCase(),
        c: (g.children || []).map((c) => cleanKeyword(c).toLowerCase()).filter(Boolean),
      }))
      .filter((g) => g.p);
  return stable(norm(a)) === stable(norm(b));
}

/** Do two stored cap maps mean the same thing (0 and absent are the same)? */
export function sameCaps(a: unknown, b: unknown): boolean {
  const strip = (v: unknown): unknown => {
    if (Array.isArray(v) || v === null || typeof v !== "object") return v;
    const out: Record<string, unknown> = {};
    for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
      const s = strip(x);
      const empty =
        s === 0 || s === null || s === undefined ||
        (typeof s === "object" && s !== null && Object.keys(s).length === 0);
      if (!empty) out[k] = s;
    }
    return out;
  };
  return stable(strip(a ?? {})) === stable(strip(b ?? {}));
}
