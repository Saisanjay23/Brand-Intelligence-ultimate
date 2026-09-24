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
  // Which of Facebook's three result tabs to sweep: any of "people",
  // "pages", "groups". EMPTY MEANS ALL THREE, exactly as an omitted
  // `platforms` means every platform. Inert for every other platform.
  facebook_tabs?: string[];
  platforms?: string[];
  max_results?: number;
  max_seconds?: number;
  // Same shapes as Client's own fields (api/types.ts) -- callers that have
  // an active client's config on hand just forward it, no transformation.
  platform_limits_individual?: Record<string, number>;
  platform_limits_domain?: Record<string, number>;
  platform_tab_limits?: Record<string, Record<string, Record<string, number>>>;
  // GAP-CLOSING PASS. Sweeps only the (platform, tab, keyword) cells this
  // client's coverage ledger still lists as owed -- never searched, missed
  // by an earlier run, or broken -- instead of the whole plan. Costs what
  // the gaps cost rather than what the client costs, which is what makes
  // closing them something the scheduler can just do rather than something
  // an analyst has to notice and decide to do.
  only_owed?: boolean;
}

// One (platform, tab, keyword) still needing a search, straight off
// GET /discovery/coverage/{group_id}.
export interface OwedCell {
  platform: string;
  tab: string;
  kw_type: string;
  search: string;
  // The keyword an analyst recognises: `search` may be one of their
  // curated permutations, which on its own means little to a reader.
  parent: string;
  // "" = planned but never attempted; "missed" = a sweep gave up before
  // reaching it; "broken" = it was reached and could not complete.
  outcome: string;
  reason: string;
  attempts: number;
  attempted_at: string | null;
  // Null with a non-null attempted_at is a keyword that has been tried and
  // never once actually searched.
  covered_at: string | null;
}

export interface CoverageReport {
  group_id: string;
  cells: number;
  owed: number;
  by_outcome: Record<string, number>;
  by_platform: Record<string, Record<string, number>>;
  items: OwedCell[];
}

export interface StartDiscoveryAccepted {
  job_id: string;
  status: JobStatus;
  poll_url: string;
  platforms_queued: string[];
  skipped: SkippedInput[];
}

export interface CompletedSweepTelemetry {
  platform: string;
  display_name: string;
  keyword: string;
  tab: string;
  duration_seconds: number;
  hits_found: number;
  hits_new: number;
  timestamp: string;
  // How the sweep ENDED, not just how long it took. Shortening a timing knob
  // makes a sweep faster and makes it give up sooner, and those are
  // indistinguishable in a duration alone -- these are what tell them apart.
  complete?: boolean;
  stopped?: string;
  // WHAT THAT ENDING MEANS, which `complete` on its own does not say.
  // `complete` is raw engine telemetry -- it is only true when the platform
  // ran out of results -- so a sweep that stopped because it had collected
  // exactly the configured `max_results` reports false while being a
  // perfectly complete answer. That conflation is why every capped run used
  // to warn "N sweep(s) did not run to completion". Read this instead:
  // "satisfied" needs no comment, "truncated" means a time/page budget left
  // results on the table, "broken" is the only one worth alarming about.
  outcome?: "satisfied" | "truncated" | "broken";
  // The profile-visit reconciliation phase: the slowest part of a sweep, and
  // the one that costs the most in detection surface.
  resolved_visits?: number;
  resolve_seconds?: number;
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
  current_keyword?: string;
  current_tab?: string;
  current_step?: string;
  item_started_at_ts?: number | null;
  started_at_ts?: number | null;
  finished_at_ts?: number | null;
  // How many pooled sessions are sweeping this platform in parallel right
  // now. 1 (or absent) is the ordinary single-session run; >1 means the
  // keyword list was split across accounts -- see
  // _MAX_SESSIONS_PER_PLATFORM in backend/discovery/runner.py.
  workers?: number;
  // What each pooled account is sweeping right now, one entry per worker.
  // Empty for the ordinary single-session sweep, where current_keyword /
  // current_tab above still say everything there is to say. Present and
  // populated only when the keyword list was sharded across accounts --
  // at which point those two fields are deliberately blank, because with
  // three workers there is no single "current" keyword and naming one was
  // the flicker that kept sharding switched off.
  worker_slots?: {
    account?: string;
    keyword?: string;
    tab?: string;
    step?: string;
    started_at_ts?: number;
  }[];
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
  // Picture batches landed; a change means saved cards may have been
  // corrected (e.g. a logo verdict), so the grid is re-read.
  avatar_updates?: number;
  // True while picture checks still run after the sweep finished.
  avatars_settling?: boolean;
  started_at: string | null;
  finished_at: string | null;
  started_at_ts?: number | null;
  finished_at_ts?: number | null;
  elapsed_seconds?: number | null;
  estimated_remaining_seconds?: number | null;
  platforms: PlatformSweepState[];
  history?: CompletedSweepTelemetry[];
  // Opaque; echoed back as `rev` to long-poll for the next change.
  rev?: string;
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
  // sha256 of the picture bytes held by the backend. Prefer it over the
  // URL above, which is signed by the CDN and expires within hours.
  // Empty until the sweep's background caching has fetched it (and stays
  // empty if that never succeeded).
  avatar_sha?: string;
  // 0-100 resemblance to one of the client's reference logos, absent when
  // it did not match (or the client uploaded none). A RANKING signal only:
  // it never sets has_logo and never changes the risk score.
  logo_similarity?: number | null;
  logo_ref_id?: string;
  // "exact" (the identical file re-uploaded) or "phash" (near-identical).
  logo_match_tier?: string;
  // An analyst marked this as the GENUINE account -- the real brand or
  // person, not an impersonation. Permanent: no re-discovery clears it.
  is_original?: boolean;
  has_logo: boolean | null;
  verified: boolean | null;
  followers: number | null;
  friends: number | null;
  location: string;
  bio: string;
  created_at: string;
  // The PARENT keyword(s) whose investigation this profile belongs to --
  // the bucket and the filter option. NOT necessarily what the name was
  // scored against: that is `match_term`, which may be one of the parent's
  // children. Never a permutation itself.
  keywords: string[];
  // The permutation(s) actually typed into the platform's search box to
  // surface it, when they differ from the parent. EMPTY MEANS "found by
  // its own keyword", not "unknown": a parent with no permutations
  // configured searches itself, so there is nothing to distinguish.
  matched_keywords?: string[];
  // WHICH keyword the High/Medium/Low badge is about: the parent, or
  // whichever of its configured children this name actually resembled.
  // name_score and name_exact_run are both computed against this one term,
  // so it is the reason for the grade. Distinct from `matched_keywords`,
  // which is provenance (what was searched) rather than the verdict.
  match_term?: string;
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
  // UTC ISO timestamp of when a repeat sweep detected a genuinely different
  // profile picture. Absent until a change is observed.
  avatar_changed_at?: string | null;
}

export interface DiscoveredProfilePage {
  items: DiscoveredProfile[];
  total: number;
  limit: number;
  offset: number;
  // Totals for the WHOLE filtered set, not this page -- so the New/Old tab
  // badges can state the true size of a tab that isn't open. Counted with
  // the `age` filter itself dropped, so both numbers are always the real
  // ones regardless of which tab is being viewed.
  counts?: {
    ages?: { new?: number; old?: number };
    // The same split for VALIDATED rows, on a different clock: how long
    // ago the analyst validated it, not how long ago it was discovered.
    // Counted with the `validated_age` filter dropped, so the badge for
    // the half that is not open is still the real total.
    validated_ages?: { new?: number; old?: number };
    // Per-keyword totals across the WHOLE filtered set. The dropdown used to
    // tally the rows it happened to have loaded, which with server-side
    // paging would have meant one page -- a keyword only present further in
    // would not have appeared in the list at all.
    keywords?: Record<string, number>;
  };
}

export interface ListProfilesQuery {
  group_id: string;
  platform?: string;
  status?: ProfileStatus;
  keyword?: string;
  search?: string;
  // "new" = first seen within the last 24h, "old" = everything else.
  // Server-side, so the New/Old split survives pagination: it used to be
  // computed in the browser over one capped fetch, which silently hid every
  // pending profile past the cap from both tabs.
  age?: "new" | "old";
  // "new" = validated within the last 24h, "old" = everything else, INCLUDING
  // anything validated before the timestamp existed. Splits the Validated tab
  // by the analyst's decision time rather than by discovery time.
  validated_age?: "new" | "old";
  // First-seen date range, as ISO-8601 instants. The UI resolves the
  // analyst's picked calendar dates against the BROWSER's timezone before
  // sending, so "6 Sep" means 6 Sep where they are sitting rather than in
  // UTC -- for IST that is a 5.5h shift, enough to move a whole evening's
  // discoveries into the wrong day. `to` is exclusive: the UI sends the
  // start of the day AFTER the one selected.
  first_seen_from?: string;
  first_seen_to?: string;
  // Only profiles whose picture matched a reference logo.
  logo_matched?: boolean;
  // true = only genuine accounts, false = everything except them.
  is_original?: boolean;
  match_level?: "high" | "medium" | "low";
  entity_type?: string;
  // Which of THIS CLIENT's two keyword buckets a profile was found under.
  // Resolved server-side against group_id's own saved keyword lists, so
  // (like match_level/entity_type) it survives pagination.
  keyword_match_type?: "individual" | "domain";
  limit?: number;
  offset?: number;
}

export interface SetProfileStatusResult {
  updated: string[];
  failed: SkippedInput[];
}

export interface AnalyseValidatedBody {
  group_id: string;
  // ONE platform. The Discovery grid's rail is single-select, so it sends
  // this.
  platform?: string;
  // SEVERAL platforms, for the multi-select picker on Run & Overview.
  // Empty/omitted means every platform -- the same thing omitting
  // `platform` means. Wins over `platform` when both are sent.
  platforms?: string[];
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

  // WHAT THIS CLIENT STILL OWES, read from the durable per-cell ledger
  // rather than from a job. A job's progress is aggregate, in-memory and
  // gone by morning; this answers "was every keyword actually searched"
  // for a sweep that finished last week.
  coverage: (groupId: string, platform?: string) =>
    fetch(url(
      `/discovery/coverage/${encodeURIComponent(groupId)}`
      + (platform ? `?platform=${encodeURIComponent(platform)}` : ""),
    )).then(json<CoverageReport>),

// LONG POLL, NOT A TICK. Passing the `rev` from the previous response plus
// a `wait` makes the backend hold the request open until the job actually
// moves, then answer at once -- so a saved profile reaches the screen about
// a tenth of a second after it is written, instead of on the next interval,
// and an idle job costs one request per wait window rather than one every
// two seconds. Omit both and this is the plain immediate snapshot it has
// always been. See backend/shared/live_poll.py.
  getJob: (jobId: string, watch?: { rev?: string; wait?: number; signal?: AbortSignal }) => {
    const p = new URLSearchParams();
    if (watch?.rev) p.set("rev", watch.rev);
    if (watch?.wait) p.set("wait", String(watch.wait));
    const q = p.toString();
    return fetch(url(`/discovery/jobs/${jobId}${q ? `?${q}` : ""}`), { signal: watch?.signal })
      .then(json<DiscoveryJobState>);
  },

  cancelJob: (jobId: string) =>
    post(`/discovery/jobs/${jobId}/cancel`, {}).then(json<{ cancelled: boolean }>),

  listProfiles: (q: ListProfilesQuery) => {
    const p = new URLSearchParams({ group_id: q.group_id });
    if (q.platform) p.set("platform", q.platform);
    if (q.status) p.set("status", q.status);
    if (q.keyword) p.set("keyword", q.keyword);
    if (q.search) p.set("search", q.search);
    if (q.age) p.set("age", q.age);
    if (q.validated_age) p.set("validated_age", q.validated_age);
    if (q.first_seen_from) p.set("first_seen_from", q.first_seen_from);
    if (q.first_seen_to) p.set("first_seen_to", q.first_seen_to);
    if (q.logo_matched) p.set("logo_matched", "true");
    if (q.is_original !== undefined) p.set("is_original", String(q.is_original));
    if (q.match_level) p.set("match_level", q.match_level);
    if (q.entity_type) p.set("entity_type", q.entity_type);
    if (q.keyword_match_type) p.set("keyword_match_type", q.keyword_match_type);
    if (q.limit) p.set("limit", String(q.limit));
    if (q.offset) p.set("offset", String(q.offset));
    return fetch(url(`/discovery/profiles?${p}`)).then(json<DiscoveredProfilePage>);
  },

  setProfileStatus: (ids: string[], status: ProfileStatus) =>
    post("/discovery/profiles/status", { ids, status }).then(json<SetProfileStatusResult>),

  // Mark profiles as the genuine account (or un-mark them). Independent of
  // triage status: it answers "is this us?", not "have we looked at it?"
  setProfileOriginal: (ids: string[], isOriginal: boolean) =>
    post("/discovery/profiles/original", { ids, is_original: isOriginal })
      .then(json<SetProfileStatusResult>),

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
