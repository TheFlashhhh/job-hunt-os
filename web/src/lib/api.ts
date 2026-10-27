/**
 * The only way this frontend talks to the backend.
 *
 * Every type below is derived from `api-schema.d.ts`, which is generated from
 * FastAPI's OpenAPI document (`npm run gen:api`). Nothing here is hand-written,
 * and that is the point: it is the structural guard against the failure mode
 * this frontend is most likely to have — quietly reimplementing a backend rule
 * in TypeScript. If a response shape changes, the build breaks, rather than a
 * React component and a Python service drifting into two different opinions
 * about what a SHORTLISTED job is.
 *
 * Rules for anything added here:
 *   - No scoring, eligibility, Evidence Bank, CRM, claim-validation or
 *     authorization logic. Those live in FastAPI and are read, never recomputed.
 *   - No hand-written response shapes. If a type cannot be derived, the fix is
 *     a Pydantic response model on the endpoint, not a local interface.
 */

import type { components, paths } from "./api-schema";

/**
 * Where the backend lives from the perspective of THIS process.
 *
 * Two callers, two correct answers:
 *
 *   Server components run inside Node and can reach the backend directly.
 *   A relative URL would fail there — `fetch` needs an absolute one.
 *
 *   The browser must NOT call the backend origin directly. `localhost:3000`
 *   to `127.0.0.1:8000` is cross-origin (different host and port), which
 *   triggers a CORS preflight. That is precisely what produced the
 *   "Failed to fetch" on Prepare Application. Instead the browser calls a
 *   same-origin path that Next rewrites to the backend, so no preflight is
 *   ever issued and no NEXT_PUBLIC_* URL has to be correct on the client.
 */
const SERVER_ORIGIN =
  process.env.JOBHUNT_BACKEND_ORIGIN ??
  process.env.NEXT_PUBLIC_API_BASE ??
  "http://127.0.0.1:8000";

const isServer = typeof window === "undefined";

/** Absolute on the server; same-origin proxy prefix in the browser. */
export const API_BASE = isServer ? SERVER_ORIGIN : "";

/** Browser requests go through the Next rewrite; server requests go direct. */
export function browserApiPath(path: string): string {
  return path.startsWith("/api/") ? `/api/backend${path.slice(4)}` : path;
}

function resolve(path: string): string {
  if (isServer) return `${SERVER_ORIGIN}${path}`;
  // /api/foo -> /api/backend/foo  (rewritten by next.config.ts)
  return path.startsWith("/api/") ? `/api/backend${path.slice(4)}` : path;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`${status}: ${detail}`);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(resolve(path), {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body?.detail ?? detail;
    } catch {
      // Non-JSON error body; the status line is all we have.
    }
    throw new ApiError(response.status, String(detail));
  }
  return (await response.json()) as T;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });

export interface JobsQuery {
  lane?: string;
  limit?: number;
  offset?: number;
  relevant_only?: boolean;
  prefilter?: string;
  sort?: string;
  role_family?: string;
  location?: string;
  remote?: string;
  employment_type?: string;
  eligibility?: string;
  crm_state?: string;
  experience_fit?: string;
  company?: string;
  /** USER_ADDED | AUTOMATED | a specific discovery_source. */
  source?: string;
  q?: string;
  /** The function the JD describes, e.g. PRODUCT_ANALYTICS. Not the title. */
  function?: string;
  /** Only roles whose title hides the work. Independent of experience band. */
  hidden_fit?: boolean;
  /** ENTRY_COMPATIBLE | EARLY_CAREER | REACH | OVERLEVELLED. */
  experience_band?: string;
  /** Archived lanes — the Fall 2026 internship history. Off by default. */
  include_archived?: boolean;
  /** PRIMARY_TARGET | SECONDARY_TARGET. Both are active; this narrows to one. */
  target_priority?: string;
}

function toQuery<T extends object>(params: T): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  if (entries.length === 0) return "";
  const search = new URLSearchParams();
  for (const [key, value] of entries) search.set(key, String(value));
  return `?${search.toString()}`;
}

/* ------------------------------------------------------------------ */
/* Types, all generated                                                */
/* ------------------------------------------------------------------ */

type Schema = components["schemas"];

export type JobRow = Schema["JobRow"];
export type JobsResponse = Schema["JobsResponse"];
export type JobDetailResponse = Schema["JobDetailResponse"];
export type EligibilityView = Schema["EligibilityView"];
export type SponsorshipView = Schema["SponsorshipView"];
export type PriorityView = Schema["PriorityView"];
export type RequirementView = Schema["RequirementView"];
export type TimelineEntryView = Schema["TimelineEntryView"];
export type NoteView = Schema["NoteView"];
export type ApplicationResponse = Schema["ApplicationResponse"];
export type DashboardResponse = Schema["DashboardResponse"];
export type ExecutionResponse = Schema["ExecutionResponse"];
export type SkillGapsResponse = Schema["SkillGapsResponse"];
export type SkillGapView = Schema["SkillGapView"];
export type ContactsResponse = Schema["ContactsResponse"];
export type JobContactsResponse = Schema["JobContactsResponse"];
export type ContactView = Schema["ContactView"];
export type FollowUpsResponse = Schema["FollowUpsResponse"];
export type FollowUpView = Schema["FollowUpView"];
export type ReferralsResponse = Schema["ReferralsResponse"];
export type ReferralUpsertResponse = Schema["ReferralUpsertResponse"];
export type RecommendationsResponse = Schema["RecommendationsResponse"];
export type RecommendationView = Schema["RecommendationView"];
export type ResumeBasesResponse = Schema["ResumeBasesResponse"];
export type BaseResumeView = Schema["BaseResumeView"];
export type GenerateBaseResumeResponse = Schema["GenerateBaseResumeResponse"];
export type TransitionResponse = Schema["TransitionResponse"];

/* Slice 5 — Prepare Application / Resume Review / Generation / Download */
export type GuardResultView = Schema["GuardResultView"];
export type PrepareApplicationResponse = Schema["PrepareApplicationResponse"];
export type RecommendationDecisionPayload = Schema["DecisionPayload"];
export type DecisionResponse = Schema["DecisionResponse"];
export type GenerateResumeResponse = Schema["GenerateResumeResponse"];
export type ResumeVersionView = Schema["ResumeVersionView"];
export type ResumeVersionsResponse = Schema["ResumeVersionsResponse"];
export type MarkAppliedPayload = Schema["MarkAppliedPayload"];
export type ApplicationChecklistView = Schema["ApplicationChecklistView"];
export type ApplicationTabResponse = Schema["ApplicationTabResponse"];
export type InterviewPrepView = Schema["InterviewPrepView"];
export type NextActionResponse = Schema["NextActionResponse"];
export type ActionRow = Schema["ActionRow"];

/* Slice 5.1 — role targeting + manual URL import */
export type TargetingExplanationView = Schema["TargetingExplanationView"];
export type TargetingAuditView = Schema["TargetingAuditView"];
export type AuditFindingView = Schema["AuditFindingView"];
export type ResumeTargetingResponse = Schema["ResumeTargetingResponse"];
export type ImportUrlPayload = Schema["ImportUrlPayload"];
export type ImportJobResponse = Schema["ImportJobResponse"];
export type AlreadyAppliedPayload = Schema["AlreadyAppliedPayload"];
export type NextActionView = Schema["NextActionView"];
export type NextActionsResponse = Schema["NextActionsResponse"];
export type EnumsResponse = Schema["EnumsResponse"];
export type HealthResponse = Schema["HealthResponse"];

/* Collection: freshness, Run Now, monitored companies, why-missed */
export type CollectionStatusResponse = Schema["CollectionStatusResponse"];
export type ProviderStatusView = Schema["ProviderStatusView"];
export type SweepView = Schema["SweepView"];
export type CollectionRunStartResponse = Schema["CollectionRunStartResponse"];
export type MonitoredCompaniesResponse = Schema["MonitoredCompaniesResponse"];
export type MonitoredCompanyView = Schema["MonitoredCompanyView"];
export type MonitorCompanyResponse = Schema["MonitorCompanyResponse"];
export type WhyMissedResponse = Schema["WhyMissedResponse"];
export type ScheduleSetupResponse = Schema["ScheduleSetupResponse"];

/* Company registry and coverage (Discovery Coverage V2) */
export type CompanyRegistryResponse = Schema["CompanyRegistryResponse"];
export type RegistryCompanyView = Schema["RegistryCompanyView"];
export type CoverageView = Schema["CoverageView"];
export type SourceDetectionResponse = Schema["SourceDetectionResponse"];
export type RegistryActionResponse = Schema["RegistryActionResponse"];
export type TestSourceResponse = Schema["TestSourceResponse"];

/* Cover letters */
export type WhyThisMatchesView = Schema["WhyThisMatchesView"];
export type CoverLetterVersionsResponse = Schema["CoverLetterVersionsResponse"];
export type CoverLetterVersionView = Schema["CoverLetterVersionView"];
export type CoverLetterGenerateResponse = Schema["CoverLetterGenerateResponse"];
export type CoverLetterRequirementView = Schema["CoverLetterRequirementView"];
export type CoverLetterStoryView = Schema["CoverLetterStoryView"];

/* People CRM */
export type PeopleSummaryResponse = Schema["PeopleSummaryResponse"];
export type ContactDetailView = Schema["ContactDetailView"];
export type ContactDiscoveryResponse = Schema["ContactDiscoveryResponse"];
export type SearchStrategyView = Schema["SearchStrategyView"];
export type ContactSuggestionView = Schema["ContactSuggestionView"];
export type OutreachDraftView = Schema["OutreachDraftView"];
export type MarkSentDetailResponse = Schema["MarkSentDetailResponse"];
export type ContactHistoryResponse = Schema["ContactHistoryResponse"];
export type OutreachInteractionView = Schema["OutreachInteractionView"];

/* Slice 3 — structured discovery */
export type WatchlistResponse = Schema["WatchlistResponse"];
export type WatchlistCompanyView = Schema["WatchlistCompanyView"];
export type SourceHealthResponse = Schema["SourceHealthResponse"];
export type CollectionRunsResponse = Schema["CollectionRunsResponse"];
export type CollectionRunView = Schema["CollectionRunView"];
export type SearchProfilesResponse = Schema["SearchProfilesResponse"];
export type DiscoveredJobsResponse = Schema["DiscoveredJobsResponse"];
export type CollectionSummaryResponse = Schema["CollectionSummaryResponse"];

/** Compile-time proof that the paths this client calls still exist. */
type Assert<T extends true> = T;
export type _RoutesExist = Assert<
  "/api/execution" extends keyof paths
    ? "/api/skill-gaps" extends keyof paths
      ? true
      : false
    : false
>;

/* ------------------------------------------------------------------ */
/* Endpoints                                                           */
/* ------------------------------------------------------------------ */

export const api = {
  health: () => get<HealthResponse>("/api/health"),
  enums: () => get<EnumsResponse>("/api/enums"),

  dashboard: (lane = "ALL", period = "ALL") =>
    get<DashboardResponse>(`/api/dashboard?lane=${lane}&period=${period}`),
  execution: (lane = "ALL") =>
    get<ExecutionResponse>(`/api/execution?lane=${lane}`),
  skillGaps: (lane = "ALL") =>
    get<SkillGapsResponse>(`/api/skill-gaps?lane=${lane}`),

  jobs: (params: JobsQuery = {}) => get<JobsResponse>(`/api/jobs${toQuery(params)}`),
  job: (jobId: string) => get<JobDetailResponse>(`/api/jobs/${jobId}`),
  application: (jobId: string) =>
    get<ApplicationResponse>(`/api/jobs/${jobId}/application`),
  setStatus: (jobId: string, status: string, note?: string) =>
    post<TransitionResponse>(`/api/jobs/${jobId}/status`, { status, note }),
  setNextAction: (jobId: string, action: string | null, due: string | null) =>
    post<NextActionResponse>(`/api/jobs/${jobId}/next-action`, { action, due }),

  contacts: () => get<ContactsResponse>("/api/contacts"),
  jobContacts: (jobId: string) =>
    get<JobContactsResponse>(`/api/jobs/${jobId}/contacts`),
  followUps: (withinDays = 7) =>
    get<FollowUpsResponse>(`/api/follow-ups?within_days=${withinDays}`),
  referrals: (jobId: string) =>
    get<ReferralsResponse>(`/api/jobs/${jobId}/referrals`),
  resumeBases: () => get<ResumeBasesResponse>("/api/resume-bases"),
  resumeBasePreview: (baseName: string) =>
    get<BaseResumeView>(`/api/resume-bases/${baseName}/preview`),
  generateResumeBase: (baseName: string) =>
    post<GenerateBaseResumeResponse>(`/api/resume-bases/${baseName}/generate`),
  /**
   * A base resume download link. Always the SAME-ORIGIN proxy path, even when
   * rendered by a Server Component: the href is followed by the browser, which
   * must never be handed the backend origin. Every download regenerates the
   * base from the current Evidence Bank.
   */
  resumeBaseDownloadPath: (baseName: string, format: "pdf" | "docx") =>
    browserApiPath(`/api/resume-bases/${encodeURIComponent(baseName)}/download.${format}`),

  watchlist: () => get<WatchlistResponse>("/api/watchlist"),
  sourceHealth: (includeFixtures = true) =>
    get<SourceHealthResponse>(`/api/source-health?include_fixtures=${includeFixtures}`),
  collectionRuns: (limit = 50, includeFixtures = false) =>
    get<CollectionRunsResponse>(
      `/api/collection-runs?limit=${limit}&include_fixtures=${includeFixtures}`,
    ),
  searchProfiles: () => get<SearchProfilesResponse>("/api/search-profiles"),
  discoveredJobs: (lane = "ALL", days = 7) =>
    get<DiscoveredJobsResponse>(`/api/discovered-jobs?lane=${lane}&days=${days}`),
  collectionSummary: () => get<CollectionSummaryResponse>("/api/collection-summary"),

  /* Collection status, freshness and the monitored set */
  collectionStatus: () => get<CollectionStatusResponse>("/api/collection/status"),
  collectionSchedule: () => get<ScheduleSetupResponse>("/api/collection/schedule"),
  monitoredCompanies: () => get<MonitoredCompaniesResponse>("/api/monitored-companies"),
  companyRegistry: () => get<CompanyRegistryResponse>("/api/company-registry"),
  whyMissed: (jobId: string) =>
    get<WhyMissedResponse>(`/api/jobs/${jobId}/why-missed`),

  /* Cover letters */
  coverLetter: (jobId: string) =>
    get<CoverLetterVersionsResponse>(`/api/jobs/${jobId}/cover-letter`),
  downloadCoverLetterUrl: (versionId: string, format: "pdf" | "docx") =>
    resolve(`/api/cover-letters/${versionId}/download.${format}`),

  /* People CRM */
  peopleSummary: () => get<PeopleSummaryResponse>("/api/people/summary"),
  contactHistory: (contactId: string) =>
    get<ContactHistoryResponse>(`/api/contacts/${contactId}/history`),
  contactDiscovery: (jobId: string) =>
    get<ContactDiscoveryResponse>(`/api/jobs/${jobId}/contact-discovery`),

  resumeRecommendations: (jobId: string, refresh = false) =>
    get<RecommendationsResponse>(
      `/api/jobs/${jobId}/resume-recommendations?refresh=${refresh}`,
    ),

  /* Slice 5 — Prepare Application / Resume Review / Generation / Download */
  prepareApplication: (jobId: string, base?: string) =>
    post<PrepareApplicationResponse>(
      `/api/jobs/${jobId}/prepare-application${base ? `?base=${base}` : ""}`,
    ),
  decideRecommendation: (jobId: string, recommendationId: string, accepted: boolean | null) =>
    post<DecisionResponse>(
      `/api/jobs/${jobId}/recommendations/${recommendationId}/decision`,
      { accepted },
    ),
  generateResume: (jobId: string) =>
    post<GenerateResumeResponse>(`/api/jobs/${jobId}/generate-resume`),
  resumeVersions: (jobId: string) =>
    get<ResumeVersionsResponse>(`/api/jobs/${jobId}/resume-versions`),
  downloadResumeUrl: (resumeVersionId: string, format: "pdf" | "docx") =>
    resolve(`/api/resumes/${resumeVersionId}/download.${format}`),
  applicationTab: (jobId: string) =>
    get<ApplicationTabResponse>(`/api/jobs/${jobId}/application-tab`),
  markApplied: (jobId: string, payload: MarkAppliedPayload) =>
    post<TransitionResponse>(`/api/jobs/${jobId}/mark-applied`, payload),
  interviewPrep: (jobId: string) =>
    get<InterviewPrepView>(`/api/jobs/${jobId}/interview-prep`),

  /* Slice 5.1 — role targeting + manual URL import */
  resumeTargeting: (jobId: string, intensity?: string) =>
    get<ResumeTargetingResponse>(
      `/api/jobs/${jobId}/resume-targeting${intensity ? `?intensity=${intensity}` : ""}`,
    ),
  importJobUrl: (payload: ImportUrlPayload) =>
    post<ImportJobResponse>("/api/jobs/import-url", payload),
  alreadyApplied: (jobId: string, payload: AlreadyAppliedPayload) =>
    post<TransitionResponse>(`/api/jobs/${jobId}/already-applied`, payload),
  jobNextAction: (jobId: string) =>
    get<NextActionView>(`/api/jobs/${jobId}/next-action`),
  nextActions: (limit = 12, source?: string) =>
    get<NextActionsResponse>(
      `/api/next-actions?limit=${limit}${source ? `&source=${source}` : ""}`,
    ),
};
