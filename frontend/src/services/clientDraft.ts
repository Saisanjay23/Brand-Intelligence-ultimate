/**
 * Unsaved client forms, kept safe across tab switches and reloads.
 *
 * THE DEFECT THIS CLOSES. The Clients page held its form only in React
 * state. Moving to Live Results or Admin unmounts the page, so a half-built
 * new client (org id, keywords, scrape caps) was simply gone when the
 * analyst came back -- and so were unsaved edits to an existing client's
 * keywords or limits. Nothing said so; the form just came back blank or
 * showing the old saved values.
 *
 * Every keystroke is written here, and the page reads it back when it opens:
 *   - "new"         the Create New Client form
 *   - "edit:<id>"   unsaved edits to one saved client
 *   - ui            which mode / workspace tab the page was on
 * A draft is removed when it is saved, cancelled, or discarded.
 *
 * localStorage, so a reload or a closed tab does not lose it either. Every
 * access is wrapped: storage can be blocked or full, and a draft that
 * cannot be stored must never break the form itself.
 */

import { useEffect, useState } from "react";
import type { KeywordGroup } from "../api/types";

const PREFIX = "bi.clientDraft.";

export interface ClientFormDraft {
  id: string;
  name: string;
  domain: string;
  nameGroups: KeywordGroup[];
  domainGroups: KeywordGroup[];
  limitsIndividual: Record<string, string>;
  limitsDomain: Record<string, string>;
  fbTabLimits: Record<string, { individual: string; domain: string }>;
  cron: string;
}

export type DraftKey = "new" | `edit:${string}`;

export const editKey = (clientId: string): DraftKey => `edit:${clientId}`;

function read(key: string): unknown {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function write(key: string, value: unknown): void {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    // Storage blocked or full: the form keeps working, it just will not
    // survive a tab switch. Better than throwing out of a keystroke.
  }
}

function remove(key: string): void {
  try {
    localStorage.removeItem(PREFIX + key);
  } catch {
    /* see write() */
  }
}

const isStrMap = (v: unknown): v is Record<string, string> =>
  !!v && typeof v === "object" && !Array.isArray(v) &&
  Object.values(v as object).every((x) => typeof x === "string");

const isGroups = (v: unknown): v is KeywordGroup[] =>
  Array.isArray(v) &&
  v.every(
    (g) => g && typeof g.parent === "string" &&
      Array.isArray(g.children) && g.children.every((c: unknown) => typeof c === "string"),
  );

/** A stored draft, or null when there is none or it is not a valid shape. */
export function loadDraft(key: DraftKey): ClientFormDraft | null {
  const d = read(key) as Partial<ClientFormDraft> | null;
  if (!d || typeof d !== "object") return null;
  const fb = d.fbTabLimits;
  const fbOk = !!fb && typeof fb === "object" &&
    Object.values(fb).every((t) => t && typeof t.individual === "string" && typeof t.domain === "string");
  if (
    typeof d.id !== "string" || typeof d.name !== "string" || typeof d.domain !== "string" ||
    typeof d.cron !== "string" || !isGroups(d.nameGroups) || !isGroups(d.domainGroups) ||
    !isStrMap(d.limitsIndividual) || !isStrMap(d.limitsDomain) || !fbOk
  ) {
    // An old or corrupted entry. Drop it rather than half-apply it.
    remove(key);
    return null;
  }
  return d as ClientFormDraft;
}

export function saveDraft(key: DraftKey, draft: ClientFormDraft): void {
  write(key, draft);
}

export function clearDraft(key: DraftKey): void {
  remove(key);
}

/** True when a new-client form holds nothing the analyst typed. */
export function isBlankDraft(d: ClientFormDraft): boolean {
  const anyText = (m: Record<string, string>) => Object.values(m).some((v) => v.trim() !== "");
  return (
    !d.id.trim() && !d.name.trim() && !d.domain.trim() && !d.cron.trim() &&
    !d.nameGroups.length && !d.domainGroups.length &&
    !anyText(d.limitsIndividual) && !anyText(d.limitsDomain) &&
    !Object.values(d.fbTabLimits).some((t) => t.individual.trim() || t.domain.trim())
  );
}

// ---------------------------------------------------------------- page ui

export interface HomeUiState {
  mode: "create" | "select";
  workspaceTab: string;
}

export function loadUi(): HomeUiState | null {
  const u = read("ui") as Partial<HomeUiState> | null;
  if (!u || (u.mode !== "create" && u.mode !== "select") || typeof u.workspaceTab !== "string") {
    return null;
  }
  return u as HomeUiState;
}

export function saveUi(ui: HomeUiState): void {
  write("ui", ui);
}

// ------------------------------------------------- half-typed input boxes

/** Forget every half-typed box stored under one owner (e.g. "new."). */
export function clearDraftTexts(ownerPrefix: string): void {
  try {
    const full = `${PREFIX}text.${ownerPrefix}`;
    const doomed: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith(full)) doomed.push(k);
    }
    doomed.forEach((k) => localStorage.removeItem(k));
  } catch {
    /* see write() */
  }
}

/**
 * useState for a text box whose contents should outlive the component --
 * a keyword typed but not yet added with Enter. Same storage, own key.
 */
export function useDraftText(
  key: string,
): [string, (v: string | ((prev: string) => string)) => void] {
  const storageKey = `text.${key}`;
  const [value, setValue] = useState<string>(() => {
    const v = read(storageKey);
    return typeof v === "string" ? v : "";
  });
  // A different key (another client opened in the same component) starts
  // from that client's own saved text, not the previous one's.
  useEffect(() => {
    const v = read(storageKey);
    setValue(typeof v === "string" ? v : "");
  }, [storageKey]);
  const set = (v: string | ((prev: string) => string)) => {
    setValue((prev) => {
      const next = typeof v === "function" ? v(prev) : v;
      // Idempotent, so React running this updater twice is harmless.
      if (next) write(storageKey, next);
      else remove(storageKey);
      return next;
    });
  };
  return [value, set];
}
