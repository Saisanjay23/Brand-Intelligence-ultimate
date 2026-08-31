// API calls for the backend's discovery module (backend/api/discovery.py).
//
// The workflow this is built around: keywords in, candidate profiles come
// back as cards (listProfiles, status=pending by default). An analyst marks
// each one validated/rejected (setProfileStatus). Filtering the same list
// by status=validated is the "Validated" tab. From there, either read `url`
// off each card directly, or call analyseValidated, which pulls every
// validated URL for the group and hands it straight to the analysis engine
// in one call. Analysis's own results are memory-only and never touch this
// collection -- see analysisApi.ts.
import { json, post, url } from "./httpClient";
import type { JobStatus, PlatformState } from "./types";

export type ProfileStatus = "pending" | "validated" | "rejected";

export interface SkippedInput {
  value: string;
  reason: string;
}

export interface StartDiscoveryBody {
  group_id: string;
  // Split by type, not a flat list: each is swept under its own cap
  // (platform_limits_individual/_domain below), matching how the backend
  // actually resolves what to enforce -- see discovery/runner.py::
  // _resolve_cap. At least one of the two must be non-empty.
  individual_keywords: string[];
  domain_keywords: string[];
  platforms?: string[];
  max_results?: number;
  max_seconds?: number;
  // Same shapes as Client's own fields (api/types.ts) -- callers that have
  // an active client's config on hand just forward it, no transformation.
  platform_limits_individual?: Record<string, number>;
  platform_limits_domain?: Record<string, number>;
  platform_tab_limits?: Record<string, Record<string, Record<string, number>>>;
}

export interface StartDiscoveryAccepted {
  job_id: string;
  status: JobStatus;
  poll_url: string;
  platforms_queued: string[];
  skipped: SkippedInput[];
}

export interface PlatformSweepState {
  platform: string;
  display_name: string;
  status: "pending" | "running" | "done" | "partial" | "failed" | "skipped";
  keywords_total: number;
  keywords_done: number;
  found: number;
  new: number;
  note: string;
}

export interface DiscoveryJobState {
  job_id: string;
  group_id: string;
  status: JobStatus;
  keywords: string[];
  message: string;
  total: number;
  completed: number;
  found: number;
  new: number;
  started_at: string | null;
  finished_at: string | null;
  platforms: PlatformSweepState[];
}

export interface DiscoveredProfile {
  id: string;
  group_id: string;
  platform: string;
  url: string;
  status: ProfileStatus;
  entity_id: string;
  entity_type: string;
  display_name: string;
  username: string;
  profile_image_url: string;
  has_logo: boolean | null;
  verified: boolean | null;
  followers: number | null;
  friends: number | null;
  location: string;
  bio: string;
  created_at: string;
  keywords: string[];
  name_score: number | null;
  // True High Match: the keyword's letters appear in this name as one
  // contiguous run (spacing/punctuation/case ignored). Word-order-sensitive
  // in a way name_score alone is not -- see matchLevelOf in
  // DiscoveryProfileGrid.tsx, which gates "high" on this rather than a
  // name_score threshold.
  name_exact_run: boolean | null;
  source: string;
  first_seen: string | null;
  last_seen: string | null;
}

export interface DiscoveredProfilePage {
  items: DiscoveredProfile[];
  total: number;
  limit: number;
  offset: number;
}

export interface ListProfilesQuery {
  group_id: string;
  platform?: string;
  status?: ProfileStatus;
  keyword?: string;
  search?: string;
  limit?: number;
  offset?: number;
}

export interface SetProfileStatusResult {
  updated: string[];
  failed: SkippedInput[];
}

export interface AnalyseValidatedBody {
  group_id: string;
  platform?: string;
  ids?: string[];
  target_name?: string;
  official_feed?: string;
  // The client's own domain, as typed when it was created -- written to
  // the export's incident-row Domain column. OrgId comes from group_id
  // (the client_id by convention), no separate field needed for it.
  domain?: string;
}

// Reuses the same accepted-job envelope analysisApi's own start() returns,
// since this literally starts an ordinary analysis job -- just pre-sourced
// from validated discovery URLs instead of a pasted list.
export interface AnalyseValidatedAccepted {
  job_id: string;
  status: JobStatus;
  poll_url: string;
  accepted: number;
  skipped: SkippedInput[];
}

export const discoveryApi = {
  platforms: () => fetch(url("/discovery/platforms")).then(json<{ items: PlatformState[] }>),

  startDiscovery: (body: StartDiscoveryBody) =>
    post("/discovery/jobs", body).then(json<StartDiscoveryAccepted>),

  getJob: (jobId: string) => fetch(url(`/discovery/jobs/${jobId}`)).then(json<DiscoveryJobState>),

  cancelJob: (jobId: string) =>
    post(`/discovery/jobs/${jobId}/cancel`, {}).then(json<{ cancelled: boolean }>),

  listProfiles: (q: ListProfilesQuery) => {
    const p = new URLSearchParams({ group_id: q.group_id });
    if (q.platform) p.set("platform", q.platform);
    if (q.status) p.set("status", q.status);
    if (q.keyword) p.set("keyword", q.keyword);
    if (q.search) p.set("search", q.search);
    if (q.limit) p.set("limit", String(q.limit));
    if (q.offset) p.set("offset", String(q.offset));
    return fetch(url(`/discovery/profiles?${p}`)).then(json<DiscoveredProfilePage>);
  },

  setProfileStatus: (ids: string[], status: ProfileStatus) =>
    post("/discovery/profiles/status", { ids, status }).then(json<SetProfileStatusResult>),

  analyseValidated: (body: AnalyseValidatedBody) =>
    post("/discovery/profiles/analyse", body).then(json<AnalyseValidatedAccepted>),

  // Irreversible. Omitting both platform and status deletes every
  // discovery-phase row for this group -- the confirmation dialog before
  // calling this is what stands between an analyst and that, not the API.
  deletePlatformData: (q: { group_id: string; platform?: string; status?: ProfileStatus }) => {
    const p = new URLSearchParams({ group_id: q.group_id });
    if (q.platform) p.set("platform", q.platform);
    if (q.status) p.set("status", q.status);
    return post(`/discovery/profiles/delete?${p}`, {}).then(json<{ deleted: number }>);
  },
};
