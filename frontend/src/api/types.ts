// Typed mirror of the actual backend contract (backend/api/*_routes.py).
// This backend is a headless engine meant to be driven by another backend
// (see backend/main.py's own docstring), no /api prefix, no WebSocket
// (dropped per backend/docs/adr/0002), no bulk "list all clients" route, no
// export/preset endpoints.
//
// Shared by every api/*Api.ts module, request/response shapes live here
// once instead of being redeclared per module, since several (Job, Profile)
// are used by more than one backend resource's API file.

export type Status = "pending" | "approved" | "rejected";
export type Priority = "High" | "Medium" | "Low";
export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";
export type JobKind = "discovery" | "analysis";

// One protected name and the search terms an analyst generated for it.
//
// `parent` is the real name -- it is NEVER searched. It is what discovered
// profiles are scored against (name_score / the High-Low match badge) and
// the keyword bucket every result is filed under, so filtering the results
// grid by the parent shows everything all of its children turned up.
//
// `children` are the analyst's own permutations ("gautamadani",
// "gautam adani official", ...) -- these ARE what gets typed into each
// platform's search box, and are never scored against.
//
// A parent with no children searches itself, which is exactly how every
// client behaved before this existed. See backend/shared/keywords.py.
export interface KeywordGroup {
  parent: string;
  children: string[];
}

export interface Client {
  client_id: string;
  name: string;
  domain: string;
  // optional URL of the brand's own real logo, shown side-by-side with a
  // discovered profile's avatar during analysis triage.

  // The flat PARENT lists. Derived server-side from `keyword_groups` when
  // that is sent, so the two can never disagree. Every existing consumer
  // (filters, incident category, per-type caps) reads these unchanged.
  name_keywords: string[];
  domain_keywords: string[];
  // {"individual": KeywordGroup[], "domain": KeywordGroup[]}. Always
  // returned by the API -- synthesised from the flat lists above (one
  // childless parent each) for a client saved before this existed.
  keyword_groups?: Record<string, KeywordGroup[]>;
  // platform id -> max results to scrape for this client, scoped
  // separately to an individual-keyword (person/executive) sweep vs a
  // domain-keyword (brand) sweep, independent caps, so a noisy brand
  // search can be capped without also capping the exec-name search that
  // matters more. Missing (or 0) means uncapped for that keyword type.
  platform_limits_individual: Record<string, number>;
  platform_limits_domain: Record<string, number>;
  // platform id -> {tab -> {"individual"/"domain": max results}}, for
  // platforms with more than one discovery tab, currently only
  // "facebook": {people, pages, groups}, each independently capped per
  // keyword type (Facebook People x Individual can differ from People x
  // Domain, Pages x Individual, etc). The more restrictive of a (tab, type)
  // cell's own cap and the sweep's flat keyword-type cap above applies when
  // both are set; uncapped ("scrape everything found") when neither is.
  platform_tab_limits: Record<string, Record<string, Record<string, number>>>;
  // The round-robin engine's rotation position and the Scheduler tab's own
  // list order, ascending. See clientsApi.reorderClients.
  order?: number;
  cron?: string | null;
  created_at?: string;
}

// Four distinct not-successful outcomes, because each calls for a different
// next step:
//   skipped, never attempted; its session wasn't ready when the sweep
//              started. Fix the session under Sessions.
//   partial, started and covered only some of what was asked (a cap fired,
//              the session pool ran dry mid-run, a sweep stalled). The job's
//              `message` says which. Re-run to pick up the remainder.
//   failed  -- started and broke outright.
//   done    -- everything asked for was actually attempted.
export type PlatformJobStatus = "pending" | "running" | "done" | "partial" | "failed" | "skipped";

export interface PlatformProgress {
  status: PlatformJobStatus;
  processed: number;
  total: number;
  started: number | null; // epoch seconds
  updated: number | null; // epoch seconds
  eta_seconds: number | null; // server-computed, only set while running
  // wall-clock seconds this platform has taken so far, ticks live while
  // running, frozen once it stops. null until `started` is set.
  elapsed_seconds: number | null;
  // a bounded LIVE PREVIEW of which keywords (discovery) / URLs (analysis)
  // are next up or just finished for this platform, see
  // job_service.py's ITEM_PREVIEW_CAP. `processed`/`total` above remain
  // the exact counts; these are a representative sample once a run's item
  // count exceeds the cap, not a complete inventory.
  done_items: string[];
  pending_items: string[];
  // how many pending items exist beyond what `pending_items` shows
  pending_overflow: number;
}

export interface Job {
  id: string;
  kind: JobKind;
  client_id: string;
  platform: string | null; // null = every ready platform (discovery always is)
  params: { keywords?: string[]; tabs?: string[]; max_seconds?: number; concurrency?: number; delay?: number; [k: string]: unknown };
  status: JobStatus;
  message: string;
  found: number;
  total: number;
  new_profiles: number;
  error: string;
  started: string;
  finished: string;
  last_seq: number;
  // oldest event still retained server-side (the per-job event buffer is a
  // ring, see MAX_EVENTS_PER_JOB). A client resuming from an after_seq
  // below this missed a range rather than receiving a silently short stream.
  first_seq?: number;
  platforms: Record<string, PlatformProgress>;
  // set only while status="queued" and something else holds this job's
  // platform lock, turns an unexplained "queued" into "waiting on
  // client X's Facebook sweep" (see job_service.py's per-platform lock).
  blocked_by?: { job_id: string; client_id: string; kind: JobKind; platform: string | null } | null;
}

export interface JobEvent {
  seq: number;
  job_id: string;
  type: string; // queued|running|progress|item|done|failed|cancelled
  message: string;
  found: number;
  total: number;
  ts: string;
  client_id: string;
}

// One shape for both response variants of GET /profiles (card when
// phase=discovery/omitted, full when phase=analysis), the fields each
// variant doesn't carry are simply undefined.
export interface Profile {
  id: string;
  platform: string;
  url: string;
  status: Status;
  has_logo: boolean;
  phase?: "discovery" | "analysis";
  // card fields (discovery)
  profile_name?: string;
  profile_image_url?: string;
  verified?: boolean;
  risk_score?: number | null;
  priority?: Priority | null;
  comments?: string | null;
  followers?: number | null;
  // "profile" | "page" | "group", only ever set on Facebook discovery
  // rows (the one platform whose discovery distinguishes people, pages,
  // and groups); blank everywhere else.
  entity_type?: string;
  // every keyword sweep that has (re)found this profile, discovery cards
  // only, see backend/services/profile_service.py::_to_card
  keywords?: string[];
  // 0-100 name-vs-keyword closeness (discovery-seeded, analysis-refined) --
  // powers the card's High/Low match badge. The only automated match
  // signal in the system, confidence is scored off the client's own
  // search keywords, not a separate handle/username lookup.
  name_score?: number | null;
  name_exact_run?: boolean | null;
  // an analyst's own visual confirmation, set via the discovery card's
  // Validate action, carried through to the analysis-phase record too
  logo_match?: boolean | null;
  username_match?: boolean | null;
  changes?: Record<string, { old: unknown; new: unknown }> | null;
  changed_at?: string | null;
  // full fields (phase=analysis)
  client_id?: string;
  client_name?: string;
  keyword?: string;
  username?: string;
  location?: string;
  last_post_date?: string | null;
  // true/false/null, computed from last_post_date against a 6-month
  // dormant-account window. null means no last-post date was ever found
  // (unknown), never "confirmed inactive", do not render null as Inactive.
  is_active?: boolean | null;
  // true/false/null, same tri-state as is_active, null means the
  // name-match check never ran, not "confirmed no match".
  has_name_match?: boolean | null;
  // the real/official account this profile is being compared against, when
  // an analyst has one on hand, blank on most profiles, editable via
  // ProfilePatch.
  target?: string;
  official_feed?: string;
  // publish hold (phase=analysis only), see backend/docs/adr/0007-publish-hold.md.
  // A row missing these (discovery-phase, or analysed before this feature
  // existed) should be treated as already published. The hold now genuinely
  // EXPIRES on its own once publish_hold_until passes; publishing early is
  // what the Publish action is for.
  published?: boolean;
  publish_hold_until?: string | null;
  // Evidence capture taken while analysis was reading the profile, often
  // the only surviving proof the account existed by the time a takedown
  // request is actually read. Null when no capture was taken (evidence
  // disabled, an API-only platform like YouTube/Telegram, or a run that
  // never reached the profile). Append ?download=true for an attachment.
  screenshot_url?: string | null;
  screenshot_at?: string | null;
  // "OK" | "PARTIAL" | "GONE" | "ERROR" | "CHECKPOINT" | "LOGIN_REQUIRED".
  // The last three mean the profile was never actually read, the run is
  // retried automatically until analysis_attempts hits the server's cap.
  analysis_status?: string;
  analysis_attempts?: number;
  analysed_at?: string | null;
  first_seen?: string | null;
  last_seen?: string | null;
  // live preview of the exact record Publish writes to published_incidents
  // (see backend/services/incident_publisher.py), analysis phase only,
  // null when the client record is gone. Editable pre-publish via
  // ProfilePatch.incident_overrides.
  incident?: PublishedIncidentPreview | null;
  // Retry-queue view only (GET /profiles/retry-queue), absent everywhere
  // else. "eligible" -- will be revisited automatically (catch-up sweep or
  // round-robin); "exhausted" -- hit the server's attempt cap
  // (MAX_ANALYSIS_ATTEMPTS) and needs Resume; "stopped" -- an analyst
  // turned retry off for this profile via the Stop action. See
  // backend/services/profile_service.py::_retry_state.
  retry_state?: "eligible" | "exhausted" | "stopped";
  // One line explaining why this row is in the queue at all: either the
  // analysis run never actually reached the profile ("never reached
  // (CHECKPOINT)") or it did and came back short of a field the platform
  // publishes ("missing: last_post_date, screenshot"). See
  // backend/services/profile_service.py::_retry_reason.
  retry_reason?: string;
  retry_disabled?: boolean;
}

export interface PublishedIncidentSocialProfileInfo {
  isActive: boolean;
  isSimilarName: boolean;
  isSimilarLogo: boolean;
  numberOfFollowers: number | null;
  profileName: string | null;
  location: string | null;
  profileImage: string | null;
  lastPostDate: string | null;
  posts: number | null;
}

// This is the client-facing finding this tool hands off on Publish, not
// to be confused with the backend's own internal job-failure diagnostic
// log (formerly typed here as `Incident`, removed with the Dashboard page
// that was its only reader; see backend/services/incident_service.py for
// where that diagnostic log still lives).
export interface PublishedIncidentPreview {
  title: string;
  category: string;
  subCategory: string;
  assetType: string;
  source: string;
  date: string | null;
  description: string;
  riskRating: string;
  domain: string;
  orgId: string;
  assetCategory: string;
  assetName: string;
  thirdParty: boolean;
  socialProfileInfo: PublishedIncidentSocialProfileInfo;
  // how much of this record rests on something actually read
  evidence?: {
    screenshot: boolean;
    fieldsRead: number;
    analysisStatus: string;
  };
}

// GET /profiles/coverage, "did we actually check everything?", which
// volume counts alone can't answer. `complete` is false while anything is
// still owed or was permanently given up on.
export interface Coverage {
  approved: number;
  analysed: number;
  awaiting_analysis: number;
  analysis_failed: number;
  held: number;
  with_evidence: number;
  complete: boolean;
  // profiles that will NOT be retried without intervention
  blocked: {
    id: string;
    url: string;
    platform: string;
    profile_name: string;
    reason: string;
    attempts: number;
    detail: string;
  }[];
}

export interface ProfilePatch {
  status?: Status;
  priority?: Priority;
  logo_match?: boolean;
  username_match?: boolean;
  comments?: string;
  followers?: number;
  location?: string;
  last_post_date?: string;
  display_name?: string;
  // flat dotted-path edits merged into the profile's own incident_overrides
  // (see ProfilePatch in backend/dto/profile_dto.py), e.g.
  // {"title": "..."} or {"socialProfileInfo.location": "..."}
  incident_overrides?: Record<string, unknown>;
}

export interface SessionItem {
  id: string;
  identifier: string;
  status: string;
  rate_limited_until: number;
  last_used: number;
  // how many times this session has actually been handed to a job,
  // durable, never reset, distinct from `last_used` (a timestamp, not a
  // count)
  use_count: number;
  // is a job holding this EXACT session right now, this instant, derived
  // server-side from the real running job that acquired it (see
  // sessions/manager.py::_session_in_use), so it clears itself the moment
  // that job stops running rather than needing anything to "release" it
  in_use: boolean;
  cookie_count: number;
  proxy_host: string;
  is_api_key?: boolean;
  // consecutive failures since this session last demonstrably worked --
  // drives the graduated quarantine ladder (15m -> 1h -> 6h -> 24h), so a
  // count of 1 is a blip and 4 is a probably-burned account
  consecutive_failures?: number;
  quarantine_minutes?: number;
  username?: string;
  password?: string;
  two_factor_secret?: string;
  // server-computed "could a job use this right now": not dead, and past
  // any cooldown. Distinct from `status` alone.
  available?: boolean;
  // epoch seconds this item first went dead (expired/checkpointed/
  // unreadable); 0 while healthy or merely cooling off
  dead_since?: number;
  // days left before the server auto-purges this dead row; null while not
  // dead
  purge_in_days?: number | null;
  // why this account last stopped working, in words, the live check's own
  // detail when there is a current one, otherwise whatever the last
  // auto-login attempt recorded. Empty when nothing has gone wrong.
  last_error?: string;
  // when this specific account was last live-checked (tz-aware ISO), and
  // what the check concluded. Empty/null when it has never been checked, or
  // when the only result on file predates the credentials now in the row.
  last_checked?: string;
  last_check_ok?: boolean | null;
  // epoch seconds at which the SOONEST of this account's login cookies
  // lapses, 0 when unknown or not a cookie-based platform
  expires_at?: number;
}

export interface SessionInfo {
  platform: string;
  name: string;
  // "exhausted" = a complete, valid session exists but every one is
  // quarantined or cooling off right now. Distinct from "incomplete" (a
  // botched cookie export) because the fix differs: wait, or add another
  // account. This state used to be reported as "ready", so a fully dead
  // pool showed green while every job launched into it failed.
  state: "ready" | "missing" | "incomplete" | "exhausted" | "unreadable" | "expired" | "checkpointed";
  kind: "cookies" | "api-key" | "mtproto";
  can_login: boolean;
  cookie_count: number;
  sessions: SessionItem[];
  pool_total: number;
  pool_ready: number;
  expires: string;
  message: string;
  last_verified: string;
  login?: { status: string; message: string; started: string; finished: string };
}

// PlatformHealth (GET /health/platforms, a rolling health score) has no
// backend behind it any more -- that service was deleted along with the
// old api/controllers layer. GET /discovery/platforms is what actually
// answers "can this platform run right now" on the rebuilt backend, and
// PlatformState below is its real shape. Every consumer of platform
// readiness (usePlatformState, HomeView's platform selector, Live Results'
// platform rail) reads PlatformState now.
export interface PlatformState {
  platform: string;
  name: string;
  enabled: boolean;
  session_state: "ready" | "missing" | "incomplete" | "exhausted";
  can_discover: boolean;
  stability_note: string;
}

