/**
 * Analysis: paste profile URLs, scrape them, read the results back.
 * (backend/api/analysis.py)
 *
 * This is the whole analysis feature. It takes URLs, not a client -- there
 * is no client-scoped "analyse every approved profile" call any more, and
 * no separate "quick" analysis: analysis IS the paste-URLs tool, and it is
 * independent of discovery in both directions.
 *
 * Results are held in the backend's memory only and are never persisted,
 * so a job that has aged out (or a backend restart) returns 404 and the
 * scrape has to be re-run. Export what you need while the job is live.
 */
import { blob, json, post, url } from "./httpClient";

export interface AnalysisItemData {
  id: string;
  url: string;
  platform: string;
  platform_name: string;
  entity_id: string;
  status: "pending" | "running" | "done" | "error";
  error?: string;
  analysed_at?: string;
  duration_seconds?: number | null;
  started_at_ts?: number | null;
  profile_name?: string;
  followers?: number | null;
  location?: string;
  bio?: string;
  last_post_date?: string;
  is_active?: boolean | null;
  has_logo?: boolean | null;
  has_name_match?: boolean | null;
  name_score?: number;
  risk_score?: number;
  priority?: string;
  profile_image_url?: string;
  verified?: boolean | null;
  comments?: string;
  has_screenshot?: boolean;
  incident_row: Record<string, any>;
  legacy_row: Record<string, any>;
}

export interface PlatformProgressData {
  status: "pending" | "running" | "done" | "failed";
  total: number;
  completed: number;
  display_name: string;
  current_url?: string;
  current_step?: string;
  item_started_at_ts?: number | null;
}

export interface AnalysisJobResponse {
  job_id: string;
  status: "queued" | "running" | "done" | "cancelled" | "failed";
  target_name?: string;
  official_feed?: string;
  total: number;
  completed: number;
  message?: string;
  started_at_ts?: number | null;
  finished_at_ts?: number | null;
  elapsed_seconds?: number | null;
  estimated_remaining_seconds?: number | null;
  platform_progress: Record<string, PlatformProgressData>;
  items: AnalysisItemData[];
}

export interface AnalysisStartResponse {
  job_id: string;
  status: string;
  poll_url: string;
  accepted: number;
  // URLs that never made it into the job (unsupported host, unparseable,
  // or a duplicate of another URL in the same paste), each with a reason --
  // reported rather than silently dropped.
  skipped: Array<{ value: string; reason: string }>;
}

export const analysisApi = {
  start: async (urls: string[], targetName?: string, officialFeed?: string): Promise<AnalysisStartResponse> => {
    const res = await post("/analysis/jobs", {
      urls,
      target_name: targetName || "",
      official_feed: officialFeed || "",
    });
    return json<AnalysisStartResponse>(res);
  },

  getJob: async (jobId: string): Promise<AnalysisJobResponse> => {
    const res = await fetch(url(`/analysis/jobs/${jobId}`));
    return json<AnalysisJobResponse>(res);
  },

  cancelJob: async (jobId: string): Promise<{ cancelled: boolean }> => {
    const res = await post(`/analysis/jobs/${jobId}/cancel`, {});
    return json<{ cancelled: boolean }>(res);
  },

  getScreenshotUrl: (jobId: string, itemId: string): string => {
    return url(`/analysis/jobs/${jobId}/items/${itemId}/screenshot`);
  },

  exportXlsx: async (filename: string, rows: Record<string, any>[]): Promise<Blob> => {
    const res = await post("/analysis/export/xlsx", { filename, rows });
    return blob(res);
  },
};
