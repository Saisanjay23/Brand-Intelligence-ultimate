// Sweep reports (backend/api/reports.py).
//
// THE THREE COUNTS: New = validated in the last 24h, Delta = validated
// before that, Total = both. They are read through the same server-side
// split the Validated tab uses, so a downloaded report and the screen an
// analyst is looking at can never disagree.
//
// READING NEVER SENDS. The preview and the download are GETs; mailing is a
// POST. That separation is deliberate -- a stray call while looking at
// numbers must not put mail in somebody's inbox.
import { json, post, url } from "./httpClient";

export interface ValidatedCounts {
  new: number;
  delta: number;
  total: number;
}

export interface ReportSample {
  name: string;
  platform: string;
  url: string;
  name_score?: number | null;
  logo_similarity?: number | null;
  logo_tier?: string;
}

export interface ClientReport {
  client_id: string;
  name: string;
  generated_at: string;
  validated: ValidatedCounts;
  pending: { new: number; total: number };
  logo_matches: number;
  samples: ReportSample[];
}

export interface CombinedReport {
  generated_at: string;
  clients: ClientReport[];
  totals: ValidatedCounts & { logo_matches: number; pending: number };
}

export interface SendResult {
  sent: boolean;
  detail: string;
}

/** The rendered HTML, byte-identical to what the email carries. */
export function clientReportHtmlUrl(clientId: string): string {
  return url(`/reports/client/${encodeURIComponent(clientId)}/html`);
}

export function combinedReportHtmlUrl(): string {
  return url("/reports/combined/html");
}

export const reportsApi = {
  client: (clientId: string) =>
    fetch(url(`/reports/client/${encodeURIComponent(clientId)}`)).then(json<ClientReport>),

  combined: () => fetch(url("/reports/combined")).then(json<CombinedReport>),

  sendClient: (clientId: string, toEmails?: string[]) =>
    post(`/reports/client/${encodeURIComponent(clientId)}/send`,
         { to_emails: toEmails ?? null }).then(json<SendResult>),

  sendCombined: (toEmails?: string[]) =>
    post("/reports/combined/send", { to_emails: toEmails ?? null }).then(json<SendResult>),
};
