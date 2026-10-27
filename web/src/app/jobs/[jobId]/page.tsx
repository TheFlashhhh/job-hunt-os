import Link from "next/link";
import { notFound } from "next/navigation";

import {
  api,
  ApiError,
  type ApplicationChecklistView,
  type ApplicationResponse,
  type ContactDiscoveryResponse,
  type CoverLetterVersionsResponse,
  type WhyMissedResponse,
  type WhyThisMatchesView,
  type EligibilityView,
  type InterviewPrepView,
  type JobContactsResponse,
  type JobRow,
  type PriorityView,
  type RecommendationView,
  type ReferralsResponse,
  type RequirementView,
  type ResumeTargetingResponse,
  type ResumeVersionView,
  type SponsorshipView,
} from "@/lib/api";
import {
  Badge,
  BackendDown,
  Card,
  Empty,
  toneForBand,
  toneForEligibility,
  toneForExperienceFit,
  toneForGuardWarning,
  toneForRecommendation,
  toneForSupport,
} from "@/components/ui";
import {
  CrmTransitionControl,
  GenerateResumeButton,
  MarkAppliedForm,
  PrepareApplicationButton,
  RecommendationDecision,
} from "@/components/actions";
import { MonitorCompanyButton } from "@/components/collection-actions";
import { CoverLetterPanel } from "@/components/cover-letter";
import {
  AddContactForm,
  ContactLifecycleActions,
  LinkContactButton,
  OutreachComposer,
  ReferralControl,
  UnlinkContactButton,
} from "@/components/people-actions";

export const dynamic = "force-dynamic";


/** Provenance values that mean "the user put this here". */
const USER_SOURCES = new Set(["USER_URL", "MANUAL", "MANUAL_IMPORT"]);

const URGENCY_TONE = { URGENT: "bad", SOON: "warn", NORMAL: "info" } as const;

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "fit", label: "Fit" },
  { key: "resume", label: "Resume" },
  { key: "networking", label: "Networking" },
  { key: "application", label: "Application" },
  { key: "interview", label: "Interview" },
];

const CRM_STATUSES = [
  "DISCOVERED",
  "REVIEW",
  "SHORTLISTED",
  "NETWORKING",
  "READY_TO_APPLY",
  "APPLIED",
  "RECRUITER_SCREEN",
  "OA",
  "INTERVIEW_1",
  "INTERVIEW_2",
  "FINAL",
  "OFFER",
  "REJECTED",
  "WITHDRAWN",
  "ARCHIVED",
];

export default async function JobDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ jobId: string }>;
  searchParams: Promise<{ tab?: string }>;
}) {
  const { jobId } = await params;
  const { tab = "overview" } = await searchParams;

  let detail;
  let application;
  let recommendations;
  let contacts;
  let referrals;
  let resumeVersions;
  let applicationTab;
  let interview;
  let targeting;
  let nextAction;
  let discovery;
  let whyMissed;
  let coverLetter;
  try {
    [
      detail,
      application,
      recommendations,
      contacts,
      referrals,
      resumeVersions,
      applicationTab,
      interview,
      targeting,
      nextAction,
      discovery,
      whyMissed,
      coverLetter,
    ] = await Promise.all([
      api.job(jobId),
      api.application(jobId),
      api.resumeRecommendations(jobId),
      api.jobContacts(jobId),
      api.referrals(jobId),
      api.resumeVersions(jobId),
      api.applicationTab(jobId),
      api.interviewPrep(jobId),
      api.resumeTargeting(jobId),
      api.jobNextAction(jobId),
      api.contactDiscovery(jobId),
      api.whyMissed(jobId),
      api.coverLetter(jobId),
    ]);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    return <BackendDown error={error} />;
  }

  const { job, eligibility, sponsorship, guard } = detail;
  const priority = detail.priority ?? null;
  const requirements = detail.requirements ?? [];
  const app = application;
  const recs = recommendations.recommendations ?? [];
  const versions = resumeVersions.versions ?? [];
  const userAdded = USER_SOURCES.has(job.discovery_source ?? "");

  const tabHref = (key: string) => `/jobs/${jobId}?tab=${key}`;

  return (
    <div className="space-y-5">
      <div>
        <Link href="/jobs" className="text-xs text-neutral-500 hover:underline">
          ← All jobs
        </Link>
        <h1 className="mt-1 text-xl font-semibold tracking-tight">{job.title}</h1>
        <p className="text-sm text-neutral-500">
          {job.company}
          {job.location ? ` · ${job.location}` : ""}
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          {priority ? (
            <Badge tone={toneForBand(priority.band)}>
              Priority {priority.total} {priority.band}
            </Badge>
          ) : (
            <Badge tone="neutral">Not analyzed</Badge>
          )}
          <Badge tone={toneForEligibility(eligibility.status)}>{eligibility.status}</Badge>
          <Badge tone={toneForExperienceFit(job.experience_fit)}>
            {job.experience_fit.replaceAll("_", " ")}
          </Badge>
          <Badge tone="neutral">{app.current_status}</Badge>
          {app.is_stale ? <Badge tone="warn">Stale application</Badge> : null}
          {/* Provenance, stated plainly. A job the user pasted must never be
              presented as something an automated channel discovered. */}
          {userAdded ? <Badge tone="info">Added by you</Badge> : null}
        </div>
      </div>

      {/* One concrete next step, at the top of every tab. A tracked job with no
          obvious next move is the thing most likely to quietly stall. */}
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg border border-neutral-200 bg-white p-3 dark:border-neutral-800 dark:bg-neutral-900">
        <Badge tone={URGENCY_TONE[nextAction.urgency as keyof typeof URGENCY_TONE] ?? "info"}>
          Next
        </Badge>
        <Link
          href={`/jobs/${jobId}${nextAction.href ?? ""}`}
          className="text-sm font-medium hover:underline"
        >
          {nextAction.action}
        </Link>
        <span className="text-xs text-neutral-500">{nextAction.detail}</span>
        {nextAction.due ? (
          <span className="ml-auto text-xs text-neutral-500">due {nextAction.due}</span>
        ) : null}
      </div>

      {/* Warning banners — Slice 5 §4/14: a fit score must never conceal these. */}
      {guard.blocked || guard.warnings.length > 0 ? (
        <div className="space-y-1.5">
          {guard.blocked ? (
            <div className="rounded-lg border border-rose-300 bg-rose-50 p-3 text-sm text-rose-900 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-200">
              <span className="font-semibold">BLOCKED — </span>
              {guard.block_reason}
            </div>
          ) : null}
          {guard.warnings.map((w) => (
            <div
              key={w.code}
              className="flex items-baseline gap-2 rounded-lg border border-amber-300 bg-amber-50 p-2.5 text-sm dark:border-amber-900 dark:bg-amber-950"
            >
              <Badge tone={toneForGuardWarning(w.code)}>{w.code.replaceAll("_", " ")}</Badge>
              <span className="text-neutral-700 dark:text-neutral-300">{w.message}</span>
            </div>
          ))}
        </div>
      ) : null}

      <nav className="flex gap-1 border-b border-neutral-200 text-sm dark:border-neutral-800">
        {TABS.map((t) => (
          <Link
            key={t.key}
            href={tabHref(t.key)}
            className={`-mb-px border-b-2 px-3 py-2 transition ${
              tab === t.key
                ? "border-neutral-900 font-medium text-neutral-900 dark:border-neutral-100 dark:text-neutral-100"
                : "border-transparent text-neutral-500 hover:text-neutral-800 dark:hover:text-neutral-300"
            }`}
          >
            {t.label}
          </Link>
        ))}
      </nav>

      {tab === "overview" ? (
        <OverviewTab
          whyThisMatches={detail.why_this_matches ?? null}
          job={job}
          description={detail.description}
          eligibility={eligibility}
          sponsorship={sponsorship}
          contacts={contacts}
          jobId={jobId}
          whyMissed={userAdded ? whyMissed : null}
        />
      ) : null}

      {tab === "fit" ? (
        <FitTab
          requirements={requirements}
          priority={priority}
          whyApply={detail.why_apply}
          risks={detail.risks}
          missing={detail.missing_requirements}
          job={job}
          eligibility={eligibility}
        />
      ) : null}

      {tab === "resume" ? (
        <ResumeTab
          jobId={jobId}
          blocked={guard.blocked}
          resumeBase={recommendations.resume_base}
          recs={recs}
          versions={versions}
          targeting={targeting}
        />
      ) : null}

      {tab === "networking" ? (
        <NetworkingTab
          jobId={jobId}
          job={job}
          referrals={referrals}
          app={app}
          discovery={discovery}
        />
      ) : null}

      {tab === "application" ? (
        <ApplicationTab
          jobId={jobId}
          job={job}
          checklist={applicationTab.checklist}
          coverLetter={coverLetter}
          canonicalUrl={applicationTab.canonical_url}
          app={app}
          versions={versions}
        />
      ) : null}

      {tab === "interview" ? <InterviewTab interview={interview} /> : null}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="w-40 shrink-0 text-neutral-500">{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// OVERVIEW
// ---------------------------------------------------------------------------

/**
 * Why the system thinks this posting is relevant.
 *
 * Plain language, and only what the classifier actually read. No scores and no
 * formulas: a number here would look like a prediction about recruiters, and it
 * is nothing of the kind. The internal detail sits behind a disclosure for when
 * the answer looks wrong and someone wants to know why.
 */
function WhyThisMatchesPanel({
  why,
  job,
}: {
  why: WhyThisMatchesView;
  job: JobRow;
}) {
  const label = (value: string) =>
    value.replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());

  return (
    <Card
      title="Why this matches"
      subtitle={why.hidden_fit ? "The title does not say so — the description does" : undefined}
    >
      <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
        <Row label="Primary function">{label(why.primary_function)}</Row>
        {why.secondary_functions.length > 0 ? (
          <Row label="Also">{why.secondary_functions.map(label).join(", ")}</Row>
        ) : null}
        {why.domains.length > 0 ? (
          <Row label="Domain">{why.domains.map(label).join(", ")}</Row>
        ) : null}
        <Row label="Experience">{why.experience_label || "unknown"}</Row>
        {why.target_priority ? (
          <Row label="Priority">
            {why.target_priority === "SECONDARY_TARGET"
              ? "Secondary target"
              : why.target_priority === "PRIMARY_TARGET"
                ? "Primary target"
                : "Deprioritised"}
          </Row>
        ) : null}
        {why.specialization_fit && why.specialization_fit !== "NEUTRAL" ? (
          <Row label="Specialisation">
            {why.specialization_fit === "ALIGNED"
              ? "Matches the engineering evidence"
              : "Outside the evidence (frontend, mobile, embedded)"}
          </Row>
        ) : null}
        <Row label="Employer title">{job.title}</Row>
        <Row label="Title alignment">
          {why.title_alignment === "TITLE_UNDERSELLS"
            ? "Non-standard title, strong functional match"
            : why.title_alignment === "TITLE_ALIGNED"
              ? "Title matches the work"
              : label(why.title_alignment || "unclear")}
        </Row>
      </dl>

      {why.strong_signals.length > 0 ? (
        <div className="mt-3">
          <p className="text-xs uppercase tracking-wide text-neutral-500">Strong signals</p>
          <div className="mt-1 flex flex-wrap gap-1">
            {why.strong_signals.map((signal) => (
              <Badge key={signal} tone="good">
                {label(signal)}
              </Badge>
            ))}
          </div>
        </div>
      ) : null}

      {why.concerns.length > 0 ? (
        <div className="mt-3">
          <p className="text-xs uppercase tracking-wide text-neutral-500">Worth checking</p>
          <div className="mt-1 flex flex-wrap gap-1">
            {why.concerns.map((concern) => (
              <Badge key={concern} tone="warn">
                {label(concern)}
              </Badge>
            ))}
          </div>
        </div>
      ) : null}

      {why.experience_detail ? (
        <p className="mt-3 text-xs text-neutral-500">{why.experience_detail}</p>
      ) : null}
      {why.start ? <p className="mt-1 text-xs text-neutral-500">{why.start}</p> : null}

      <details className="mt-3">
        <summary className="cursor-pointer text-xs text-neutral-500">
          How this was decided
        </summary>
        <ul className="mt-1 space-y-1 text-xs text-neutral-500">
          {why.reasons.map((reason) => (
            <li key={reason}>· {reason}</li>
          ))}
          {why.exclusion_reasons.map((reason) => (
            <li key={reason}>· {reason}</li>
          ))}
        </ul>
        {why.supporting_signals.length > 0 ? (
          <p className="mt-1 text-xs text-neutral-500">
            Supporting: {why.supporting_signals.map(label).join(", ")}
          </p>
        ) : null}
        <p className="mt-1 text-[11px] text-neutral-400">
          Classifier {why.classifier_version}. This describes what the posting asks
          for — it is not a prediction about whether you will hear back.
        </p>
      </details>
    </Card>
  );
}

function OverviewTab({
  job,
  description,
  eligibility,
  sponsorship,
  contacts,
  jobId,
  whyMissed,
  whyThisMatches,
}: {
  job: JobRow;
  description: string;
  eligibility: EligibilityView;
  sponsorship: SponsorshipView;
  contacts: JobContactsResponse;
  jobId: string;
  whyMissed: WhyMissedResponse | null;
  whyThisMatches: WhyThisMatchesView | null;
}) {
  return (
    <div className="space-y-4">
      {whyThisMatches?.available ? (
        <WhyThisMatchesPanel why={whyThisMatches} job={job} />
      ) : null}
      {whyMissed && whyMissed.reason !== "DISCOVERED_AUTOMATICALLY" ? (
        <WhyMissedPanel jobId={jobId} whyMissed={whyMissed} />
      ) : null}
      <Card title="At a glance">
        <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          <Row label="Company">{job.company}</Row>
          <Row label="Location">
            {job.location ?? "unknown"} · {job.remote ?? "unknown"}
          </Row>
          <Row label="Employment type">{job.employment_type ?? "unknown"}</Row>
          <Row label="Salary">
            {job.comp_min || job.comp_max
              ? `${job.comp_min ?? "?"} – ${job.comp_max ?? "?"} ${job.comp_currency ?? ""}`
              : "not listed"}
          </Row>
          <Row label="Posted / first seen">
            {job.posted_date ?? "unknown"} / {job.first_seen?.slice(0, 10) ?? "unknown"}
          </Row>
          <Row label="Source">
            {job.discovery_source ?? "unknown"} · lifecycle {job.lifecycle_status ?? "unknown"}
          </Row>
          <Row label="Role family">
            {job.role_family} ({job.role_priority})
          </Row>
          <Row label="Lane">{job.lane}</Row>
          <Row label="Eligibility">{eligibility.status}</Row>
          <Row label="Work authorization">{eligibility.authorization_state}</Row>
          <Row label="Sponsorship signal">{sponsorship.role_signal}</Row>
          <Row label="Contacts on file">{contacts.count ?? 0}</Row>
        </dl>
        {sponsorship.quote ? (
          <blockquote className="mt-3 border-l-2 border-neutral-300 pl-3 text-xs text-neutral-600 dark:border-neutral-700 dark:text-neutral-400">
            “{sponsorship.quote}”
            <span className="mt-1 block text-neutral-500">
              Verbatim from the posting. Never means “this role sponsors.”
            </span>
          </blockquote>
        ) : null}
        {job.url ? (
          <p className="mt-3 text-sm">
            <a
              href={job.url}
              target="_blank"
              rel="noreferrer"
              className="text-sky-700 hover:underline dark:text-sky-400"
            >
              Canonical employer posting ↗
            </a>
          </p>
        ) : (
          <p className="mt-3 text-xs text-neutral-500">No canonical URL on file.</p>
        )}
      </Card>


      <Card title="Job description" subtitle="Stored verbatim from the source posting">
        {description ? (
          <pre className="max-h-[32rem] overflow-y-auto whitespace-pre-wrap font-sans text-sm leading-relaxed text-neutral-800 dark:text-neutral-200">
            {description}
          </pre>
        ) : (
          <Empty>No description text was captured for this posting.</Empty>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// FIT
// ---------------------------------------------------------------------------

function FitTab({
  requirements,
  priority,
  whyApply,
  risks,
  missing,
  job,
  eligibility,
}: {
  requirements: RequirementView[];
  priority: PriorityView | null;
  whyApply?: string | null;
  risks?: string[];
  missing?: string[];
  job: JobRow;
  eligibility: EligibilityView;
}) {
  const strengths = requirements.filter((r) => r.support_status === "SUPPORTED");
  const gaps = requirements.filter((r) => r.support_status === "UNSUPPORTED");

  return (
    <div className="space-y-4">
      <Card title="Requirement → evidence match">
        {requirements.length === 0 ? (
          <Empty>No requirements were parsed from this posting.</Empty>
        ) : (
          <ul className="space-y-2 text-sm">
            {requirements.map((req, index) => (
              <li
                key={index}
                className="rounded border border-neutral-100 p-2 dark:border-neutral-800"
              >
                <div className="flex items-baseline gap-2">
                  <Badge tone={toneForSupport(req.support_status)}>{req.support_status}</Badge>
                  <span className="flex-1 font-medium">{req.text}</span>
                  <span className="text-xs text-neutral-500">
                    {Math.round((req.confidence ?? 0) * 100)}%
                  </span>
                </div>
                {req.evidence_ids?.length ? (
                  <p className="mt-1 text-xs text-neutral-500">
                    Evidence: {req.evidence_ids.join(", ")}
                  </p>
                ) : (
                  <p className="mt-1 text-xs text-neutral-500">No resume-ready evidence.</p>
                )}
                {req.explanation ? (
                  <p className="mt-1 text-xs text-neutral-400">{req.explanation}</p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Top strengths">
          {strengths.length === 0 ? (
            <Empty>No directly supported requirements yet.</Empty>
          ) : (
            <ul className="list-disc space-y-1 pl-4 text-sm">
              {strengths.slice(0, 8).map((r, i) => (
                <li key={i}>{r.text}</li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Top gaps">
          {gaps.length === 0 && (missing ?? []).length === 0 ? (
            <Empty>No unsupported requirements flagged.</Empty>
          ) : (
            <ul className="list-disc space-y-1 pl-4 text-sm">
              {gaps.slice(0, 8).map((r, i) => (
                <li key={i}>{r.text}</li>
              ))}
              {(missing ?? []).slice(0, 4).map((m: string, i: number) => (
                <li key={`missing-${i}`} className="text-neutral-500">
                  {m}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Experience fit">
          <Badge tone={toneForExperienceFit(job.experience_fit)}>
            {job.experience_fit.replaceAll("_", " ")}
          </Badge>
          <p className="mt-2 text-xs text-neutral-500">{job.experience_fit_reason}</p>
        </Card>
        <Card title="Location fit">
          <p className="text-sm">
            {job.location ?? "unknown"} · {job.remote ?? "unknown"}
          </p>
        </Card>
        <Card title="Eligibility / authorization risk">
          <Badge tone={toneForEligibility(eligibility.status)}>{eligibility.status}</Badge>
          <p className="mt-2 text-xs text-neutral-500">{eligibility.authorization_state}</p>
        </Card>
      </div>

      {whyApply || (risks ?? []).length ? (
        <Card title="Why this job is ranked here">
          {whyApply ? <p className="text-sm">{whyApply}</p> : null}
          {(risks ?? []).length ? (
            <ul className="mt-2 list-disc space-y-1 pl-4 text-xs text-amber-700 dark:text-amber-400">
              {(risks ?? []).map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          ) : null}
          {priority ? (
            <p className="mt-2 text-xs text-neutral-500">{priority.explanation}</p>
          ) : null}
        </Card>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// RESUME
// ---------------------------------------------------------------------------

function TargetingCard({ targeting }: { targeting: ResumeTargetingResponse }) {
  const view = targeting.targeting;
  if (!view) return null;

  return (
    <Card
      title="Why this resume looks different"
      subtitle={`Positioned as ${targeting.archetype_label} · ${targeting.intensity.toLowerCase()} targeting`}
    >
      <div className="space-y-2.5 text-sm">
        {view.prioritized.length ? (
          <p>
            <span className="text-neutral-500">Prioritized: </span>
            <span className="font-medium">{view.prioritized.join(" · ")}</span>
          </p>
        ) : null}
        {view.deemphasized.length ? (
          <p>
            <span className="text-neutral-500">De-emphasized: </span>
            <span className="text-neutral-600 dark:text-neutral-400">
              {view.deemphasized.join(" · ")}
            </span>
          </p>
        ) : null}
        {view.lead_projects.length ? (
          <p>
            <span className="text-neutral-500">Projects, in order: </span>
            {view.lead_projects.join(" → ")}
          </p>
        ) : null}
        {view.unsupported_jd_terms.length ? (
          <p className="text-xs text-rose-700 dark:text-rose-400">
            Left off deliberately: {view.unsupported_jd_terms.join(", ")} — the posting
            asks for these and the Evidence Bank does not support them.
          </p>
        ) : null}
        {view.notes.map((note, i) => (
          <p key={i} className="text-xs text-neutral-500">
            {note}
          </p>
        ))}

        {/* The scoring detail exists and is auditable, but a review screen that
            leads with a formula invites tuning the formula instead of reading
            the resume. */}
        <details className="pt-1">
          <summary className="cursor-pointer text-xs text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300">
            Targeting diagnostics
          </summary>
          <dl className="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
            <Row label="Archetype">{targeting.role_archetype}</Row>
            <Row label="Base resume">{targeting.resume_base}</Row>
            <Row label="Experience bar">{targeting.experience_bar}</Row>
            {targeting.audit ? (
              <>
                <Row label="Requirement coverage">
                  {Math.round(targeting.audit.jd_requirement_coverage * 100)}%
                </Row>
                <Row label="Supported-keyword coverage">
                  {Math.round(targeting.audit.supported_keyword_coverage * 100)}%
                </Row>
                <Row label="Unsupported leakage">
                  {targeting.audit.unsupported_keyword_leakage}
                </Row>
                <Row label="Ownership risks">{targeting.audit.ownership_risk_count}</Row>
                <Row label="Generic content">
                  {Math.round(targeting.audit.generic_content_ratio * 100)}%
                </Row>
                <Row label="Bullets">{targeting.audit.total_bullets}</Row>
              </>
            ) : null}
          </dl>
          {targeting.audit ? (
            <>
              <p className="mt-2 text-[11px] text-neutral-500">{targeting.audit.note}</p>
              {targeting.audit.findings.length ? (
                <ul className="mt-2 space-y-1 text-[11px]">
                  {targeting.audit.findings.map((f, i) => (
                    <li key={i} className="flex items-baseline gap-1.5">
                      <Badge tone={f.severity === "BLOCK" ? "bad" : f.severity === "WARN" ? "warn" : "neutral"}>
                        {f.code}
                      </Badge>
                      <span className="text-neutral-600 dark:text-neutral-400">{f.message}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </>
          ) : null}
        </details>
      </div>
    </Card>
  );
}

function ResumeTab({
  jobId,
  blocked,
  resumeBase,
  recs,
  versions,
  targeting,
}: {
  jobId: string;
  blocked: boolean;
  resumeBase?: string | null;
  recs: RecommendationView[];
  versions: ResumeVersionView[];
  targeting: ResumeTargetingResponse;
}) {
  const acceptedCount = recs.filter((r) => r.accepted === true).length;
  const doNotAdd = recs.filter((r) => r.kind === "DO_NOT_ADD");
  const actionable = recs.filter((r) => r.kind === "EMPHASIZE" || r.kind === "SAFE_REWORD");
  const other = recs.filter(
    (r) => r.kind !== "DO_NOT_ADD" && r.kind !== "EMPHASIZE" && r.kind !== "SAFE_REWORD",
  );

  return (
    <div className="space-y-4">
      <TargetingCard targeting={targeting} />

      <Card
        title="Resume for this application"
        subtitle="Tailoring edits a complete base resume — it does not replace it."
        action={<PrepareApplicationButton jobId={jobId} blocked={blocked} />}
      >
        {recs.length === 0 ? (
          <Empty>
            No proposed changes yet. Click Prepare Application to build a review screen.
          </Empty>
        ) : (
          <div className="space-y-2">
            {/* The base and the changes are separate things. Showing only a
                change count made seven edits read as the whole document. */}
            <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
              <span>
                <span className="text-neutral-500">Base resume:</span>{" "}
                <span className="font-medium">{resumeBase ?? "—"}</span>
              </span>
              {/* Stored proposals can predate a change in how this posting is
                  positioned. Showing DS_ML_AI here next to DATA_ENGINEERING in
                  the targeting card, with no explanation, reads as a bug. */}
              {resumeBase && resumeBase !== targeting.resume_base ? (
                <span className="text-amber-700 dark:text-amber-400">
                  Targeting now selects {targeting.resume_base} — re-run Prepare
                  Application to rebuild these against it.
                </span>
              ) : null}
              <span>
                <span className="text-neutral-500">Proposed changes:</span>{" "}
                <span className="font-medium">{actionable.length}</span>
              </span>
              <span>
                <span className="text-neutral-500">Accepted:</span>{" "}
                <span className="font-medium">
                  {acceptedCount} of {actionable.length}
                </span>
              </span>
            </div>
            {resumeBase ? (
              <p className="text-xs text-neutral-500">
                The generated resume always contains the full base — education,
                experience, projects and skills — whether you accept these
                changes or none of them.{" "}
                <a
                  href={`/resume-base/${resumeBase}`}
                  className="underline hover:text-neutral-700 dark:hover:text-neutral-300"
                >
                  View base resume
                </a>
              </p>
            ) : null}
          </div>
        )}
      </Card>

      {actionable.length > 0 ? (
        <Card
          id="proposed-changes"
          title="Proposed changes"
          subtitle="Accept or reject each line before generating"
        >
          <ul className="space-y-3 text-sm">
            {actionable.map((rec) => (
              <li
                key={rec.id ?? rec.body}
                className="rounded border border-neutral-100 p-3 dark:border-neutral-800"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <Badge tone={toneForRecommendation(rec.kind)}>{rec.kind}</Badge>

                    {/* The proposed line is the thing being decided on, so it
                        leads. Everything else is supporting context. */}
                    <p className="mt-2 font-medium leading-snug">{rec.body}</p>

                    {rec.rationale ? (
                      <p className="mt-1.5 text-xs text-neutral-600 dark:text-neutral-400">
                        {rec.rationale}
                      </p>
                    ) : null}

                    {/* Human-readable sources first. Raw Evidence IDs are kept
                        for auditability but must not dominate normal review. */}
                    {rec.evidence_labels?.length ? (
                      <p className="mt-1.5 text-xs text-neutral-500">
                        Supporting evidence:{" "}
                        <span className="text-neutral-700 dark:text-neutral-300">
                          {rec.evidence_labels.join(" · ")}
                        </span>
                      </p>
                    ) : null}

                    {rec.evidence_ids?.length ? (
                      <details className="mt-1">
                        <summary className="cursor-pointer text-[11px] text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300">
                          Show Evidence IDs
                        </summary>
                        <p className="mt-1 font-mono text-[11px] text-neutral-500">
                          {rec.evidence_ids.join(", ")}
                        </p>
                      </details>
                    ) : null}

                    {/* The JD requirement is why this was proposed, but it was
                        occupying several lines at the top of every card. */}
                    {rec.requirement_text ? (
                      <details className="mt-1">
                        <summary className="cursor-pointer text-[11px] text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300">
                          Matched requirement
                        </summary>
                        <p className="mt-1 text-[11px] leading-relaxed text-neutral-500">
                          {rec.requirement_text}
                        </p>
                      </details>
                    ) : null}

                    {rec.kind === "OWNERSHIP_GATED" ? (
                      <p className="mt-2 rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-700 dark:bg-amber-950 dark:text-amber-400">
                        Personal ownership unresolved — project-level wording only.
                      </p>
                    ) : null}
                  </div>

                  <RecommendationDecision
                    jobId={jobId}
                    recommendationId={rec.id ?? ""}
                    accepted={rec.accepted ?? null}
                  />
                </div>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}


      {doNotAdd.length > 0 ? (
        <Card
          title="Do not add / unsupported keywords"
          subtitle="The JD wants these; the Evidence Bank does not support them"
        >
          <ul className="list-disc space-y-1 pl-4 text-sm text-rose-700 dark:text-rose-400">
            {doNotAdd.map((rec, i) => (
              <li key={i}>{rec.body}</li>
            ))}
          </ul>
        </Card>
      ) : null}

      {other.length > 0 ? (
        <Card title="Other guidance" subtitle="Gaps and de-emphasis — not resume text">
          <ul className="space-y-1.5 text-sm">
            {other.map((rec, i) => (
              <li key={i} className="flex items-baseline gap-2">
                <Badge tone={toneForRecommendation(rec.kind)}>{rec.kind}</Badge>
                <span>{rec.body}</span>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      <Card
        title="Generate + Download"
        subtitle="Only accepted lines above are rendered onto the document"
        action={<GenerateResumeButton jobId={jobId} canGenerate={acceptedCount > 0} />}
      >
        {versions.length === 0 ? (
          <Empty>No resume has been generated for this job yet.</Empty>
        ) : (
          <ul className="space-y-2 text-sm">
            {versions.map((v) => (
              <li key={v.resume_version_id} className="flex items-center gap-3">
                <span className="flex-1">{v.label}</span>
                <span className="text-xs text-neutral-500">{v.created_at.slice(0, 16)}</span>
                {v.pdf_available ? (
                  <a
                    href={`${process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000"}/api/resumes/${v.resume_version_id}/download.pdf`}
                    className="rounded bg-neutral-900 px-2 py-1 text-xs text-white hover:bg-neutral-700 dark:bg-neutral-100 dark:text-neutral-900"
                  >
                    Download PDF
                  </a>
                ) : null}
                {v.docx_available ? (
                  <a
                    href={`${process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000"}/api/resumes/${v.resume_version_id}/download.docx`}
                    className="rounded border border-neutral-300 px-2 py-1 text-xs hover:bg-neutral-50 dark:border-neutral-700 dark:hover:bg-neutral-800"
                  >
                    Download DOCX
                  </a>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// NETWORKING
// ---------------------------------------------------------------------------

/**
 * Networking for one job.
 *
 * What was here before was developer notes shipped as product: "add one from
 * the FastAPI admin", "a dedicated compose UI here is a follow-up slice", plus
 * two endpoint paths that did not exist. This is the product instead — find
 * someone, record them, draft, send it yourself, track the referral.
 *
 * The boundary is unchanged and stated once, at the top: the system drafts,
 * tracks and reminds. Every send is the user's.
 */
function NetworkingTab({
  jobId,
  job,
  referrals,
  app,
  discovery,
}: {
  jobId: string;
  job: JobRow;
  referrals: ReferralsResponse;
  app: ApplicationResponse;
  discovery: ContactDiscoveryResponse;
}) {
  const linked = discovery.existing_contacts.filter((c) => c.already_linked);
  const sameCompany = discovery.existing_contacts.filter((c) => !c.already_linked);
  const referralByContact = new Map(
    referrals.referrals.map((r) => [r.contact_id, r.state ?? "NONE"]),
  );

  return (
    <div className="space-y-4">
      <p className="text-xs text-neutral-500">
        Job Hunt OS drafts messages, tracks who you spoke to, and reminds you to follow up.
        It never sends a message, a connection request or an email — every send is yours.
      </p>

      {/* People worth contacting ---------------------------------------- */}
      <Card
        title="People worth contacting"
        subtitle={`${discovery.function_label} and adjacent roles at ${discovery.company_name}`}
      >
        {sameCompany.length > 0 ? (
          <div className="mb-4">
            <p className="text-sm font-medium">
              {sameCompany.length} {sameCompany.length === 1 ? "person" : "people"} at{" "}
              {discovery.company_name} already in your network
            </p>
            <ul className="mt-1.5 divide-y divide-neutral-100 text-sm dark:divide-neutral-800">
              {sameCompany.map((person) => (
                <li key={person.contact_id} className="flex flex-wrap items-baseline gap-2 py-2">
                  <span className="font-medium">{person.name}</span>
                  <span className="text-xs text-neutral-500">
                    {person.reasons.join(" · ")}
                  </span>
                  <span className="ml-auto">
                    <LinkContactButton jobId={jobId} contactId={person.contact_id} />
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <p className="text-sm font-medium">Find People to Contact</p>
        <p className="mt-0.5 text-xs text-neutral-500">{discovery.note}</p>
        <ul className="mt-2 space-y-2">
          {discovery.strategies.map((strategy) => (
            <li
              key={strategy.persona}
              className="rounded border border-neutral-200 p-2.5 dark:border-neutral-800"
            >
              <p className="text-sm font-medium">{strategy.label}</p>
              <p className="mt-0.5 text-xs text-neutral-500">{strategy.why}</p>
              <p className="mt-1 font-mono text-[11px] text-neutral-500">{strategy.query}</p>
              <div className="mt-1.5 flex flex-wrap gap-3 text-xs">
                <a
                  href={strategy.linkedin_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline"
                >
                  Search LinkedIn
                </a>
                <a
                  href={strategy.web_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline"
                >
                  Search the web
                </a>
              </div>
            </li>
          ))}
        </ul>
      </Card>

      {/* Your contacts for this job -------------------------------------- */}
      <Card
        title="Your contacts"
        subtitle="Linked to this job"
        action={<AddContactForm jobId={jobId} defaultCompany={job.company} compact />}
      >
        {linked.length === 0 ? (
          <Empty>
            No contacts linked to this job yet. Add someone above, or link a person you
            already know at {discovery.company_name}.
          </Empty>
        ) : (
          <ul className="divide-y divide-neutral-100 dark:divide-neutral-800">
            {linked.map((person) => (
              <li key={person.contact_id} className="space-y-2 py-3">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="font-medium">{person.name}</span>
                  {person.role ? (
                    <span className="text-xs text-neutral-500">{person.role}</span>
                  ) : null}
                  <Badge tone="neutral">{person.status}</Badge>
                  <span className="text-xs text-neutral-500">
                    {person.reasons.join(" · ")}
                  </span>
                  <span className="ml-auto flex items-center gap-2">
                    <ReferralControl
                      jobId={jobId}
                      contactId={person.contact_id}
                      contactName={person.name}
                      state={referralByContact.get(person.contact_id) ?? "NONE"}
                    />
                    <UnlinkContactButton jobId={jobId} contactId={person.contact_id} />
                  </span>
                </div>
                <OutreachComposer
                  contactId={person.contact_id}
                  contactName={person.name}
                  jobId={jobId}
                />
                <ContactLifecycleActions contactId={person.contact_id} />
              </li>
            ))}
          </ul>
        )}
      </Card>

      {/* Referrals -------------------------------------------------------- */}
      <Card
        title="Referrals"
        subtitle="A duplicate ask warns; it never blocks, and it never blocks applying"
      >
        {referrals.referrals.length === 0 ? (
          <Empty>No referral activity for this job yet.</Empty>
        ) : (
          <ul className="space-y-1.5 text-sm">
            {referrals.referrals.map((referral) => (
              <li key={referral.id} className="flex items-baseline gap-2">
                <span>{referral.contact_name ?? referral.contact_id}</span>
                <Badge tone="info">{referral.state}</Badge>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-3 text-xs text-neutral-500">
          Application referral state: <strong>{app.referral_state}</strong> (
          {app.referral_used ? "used" : "not used"}). You can submit with or without one —
          networking never gates an application.
        </p>
      </Card>

      <p className="text-xs text-neutral-500">
        Everyone recorded here also appears on{" "}
        <Link href="/people" className="underline">
          People
        </Link>
        , with their follow-ups.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// APPLICATION
// ---------------------------------------------------------------------------

function ApplicationTab({
  jobId,
  job,
  checklist,
  coverLetter,
  canonicalUrl,
  app,
  versions,
}: {
  jobId: string;
  job: JobRow;
  checklist: ApplicationChecklistView;
  coverLetter: CoverLetterVersionsResponse;
  canonicalUrl?: string | null;
  app: ApplicationResponse;
  versions: ResumeVersionView[];
}) {
  const items: [string, boolean][] = [
    ["Canonical posting verified", checklist.canonical_posting_verified],
    ["Eligibility reviewed", checklist.eligibility_reviewed],
    ["Resume prepared", checklist.resume_prepared],
    ["Networking checked", checklist.networking_checked],
    ["Authorization questions reviewed", checklist.authorization_reviewed],
  ];
  // Only a REQUIRED letter is a checklist item. Listing an optional one as
  // unticked would read as a blocker for something the employer did not ask for.
  if (checklist.cover_letter_required) {
    items.push(["Cover letter prepared", checklist.cover_letter_recorded]);
  }

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Checklist">
        <ul className="space-y-1.5 text-sm">
          {items.map(([label, ok]) => (
            <li key={label} className="flex items-center gap-2">
              <span
                className={`inline-block h-4 w-4 rounded border text-center text-[10px] leading-4 ${
                  ok
                    ? "border-emerald-600 bg-emerald-600 text-white"
                    : "border-neutral-300 dark:border-neutral-700"
                }`}
              >
                {ok ? "✓" : ""}
              </span>
              {label}
            </li>
          ))}
        </ul>
        <p className="mt-3 text-xs text-neutral-500">
          Ready to apply: <strong>{checklist.ready_to_apply ? "yes" : "not yet"}</strong>
        </p>
        {canonicalUrl ? (
          <a
            href={canonicalUrl}
            target="_blank"
            rel="noreferrer"
            className="mt-3 inline-block rounded bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-neutral-700 dark:bg-neutral-100 dark:text-neutral-900"
          >
            Open Employer Application ↗
          </a>
        ) : (
          <p className="mt-3 text-xs text-rose-600 dark:text-rose-400">
            No canonical application URL on file for {job.company}.
          </p>
        )}
      </Card>

      <Card
        title="Application documents"
        subtitle="Prepared here, submitted by you"
      >
        <div className="mb-4 flex flex-wrap items-baseline gap-2 border-b border-neutral-100 pb-3 text-sm dark:border-neutral-800">
          <span className="font-medium">Resume</span>
          {versions.length > 0 ? (
            <span className="text-xs text-emerald-600 dark:text-emerald-400">
              ✓ Generated ({versions.length} version{versions.length === 1 ? "" : "s"})
            </span>
          ) : (
            <span className="text-xs text-neutral-500">
              Not generated — see the Resume tab
            </span>
          )}
        </div>

        <CoverLetterPanel
          jobId={jobId}
          requirementLabel={coverLetter.requirement.label}
          requirementReason={coverLetter.requirement.reason ?? ""}
          requirementEvidence={coverLetter.requirement.evidence ?? ""}
          initialVersion={coverLetter.current ?? null}
          versionCount={coverLetter.count}
          externalRecorded={coverLetter.external_recorded ?? false}
          downloadBase={process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000"}
        />
      </Card>

      <Card title="Mark Applied" subtitle="Submission always happens outside this app, manually">
        {app.current_status === "APPLIED" ? (
          <p className="text-sm text-emerald-700 dark:text-emerald-400">
            Already marked applied on {app.applied_at?.slice(0, 10) ?? "an earlier date"}.
          </p>
        ) : (
          <MarkAppliedForm jobId={jobId} resumeVersions={versions} />
        )}
      </Card>

      <Card title="CRM state" subtitle="Uses the existing append-only transition system">
        <CrmTransitionControl
          jobId={jobId}
          currentStatus={app.current_status}
          statuses={CRM_STATUSES}
        />
      </Card>

      <Card title="Transition history" subtitle="Append-only">
        <ol className="space-y-1.5 text-sm">
          {app.timeline.map((entry, index) => (
            <li key={index}>
              <span className="text-neutral-500">{entry.at.slice(0, 10)}</span>{" "}
              {entry.from ? `${entry.from} → ` : ""}
              <span className="font-medium">{entry.to}</span>
              {entry.warnings?.length ? (
                <span className="ml-1 text-xs text-amber-600 dark:text-amber-400">
                  {entry.warnings.join(" ")}
                </span>
              ) : null}
            </li>
          ))}
        </ol>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// INTERVIEW
// ---------------------------------------------------------------------------

function InterviewTab({ interview }: { interview: InterviewPrepView }) {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Likely technical topics">
        {interview.likely_technical_topics.length === 0 ? (
          <Empty>Prepare the application first to populate this from matched requirements.</Empty>
        ) : (
          <ul className="list-disc space-y-1 pl-4 text-sm">
            {interview.likely_technical_topics.map((t: string, i: number) => (
              <li key={i}>{t}</li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Likely project stories">
        {interview.likely_project_stories.length === 0 ? (
          <Empty>No accepted evidence yet.</Empty>
        ) : (
          <ul className="list-disc space-y-1 pl-4 text-sm">
            {interview.likely_project_stories.map((s: string, i: number) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Evidence to review">
        {interview.evidence_to_review.length === 0 ? (
          <Empty>Nothing yet.</Empty>
        ) : (
          <p className="text-sm">{interview.evidence_to_review.join(", ")}</p>
        )}
      </Card>
      <Card title="Known gaps">
        {interview.known_gaps.length === 0 ? (
          <Empty>No gaps recorded.</Empty>
        ) : (
          <ul className="list-disc space-y-1 pl-4 text-sm text-amber-700 dark:text-amber-400">
            {interview.known_gaps.map((g: string, i: number) => (
              <li key={i}>{g}</li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Questions to practice" subtitle="Not a full interview curriculum — a start">
        <ul className="list-disc space-y-1 pl-4 text-sm">
          {interview.questions_to_practice.map((q: string, i: number) => (
            <li key={i}>{q}</li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

/**
 * Why automated discovery did not find a job the user pasted in.
 *
 * "The engine collects 8,000 jobs — why not this one?" had no answer in the
 * product, and the honest-sounding conclusion (discovery is unreliable) was
 * usually wrong. Every reason shown here is derived from stored provenance and
 * collection history; when the recorded facts support none, the backend returns
 * UNKNOWN and says so rather than inventing something plausible.
 */
function WhyMissedPanel({
  jobId,
  whyMissed,
}: {
  jobId: string;
  whyMissed: WhyMissedResponse;
}) {
  const offer = whyMissed.monitor_offer;
  return (
    <Card
      title="Why wasn't this discovered automatically?"
      subtitle={whyMissed.reason.replace(/_/g, " ").toLowerCase()}
    >
      <p className="text-sm font-medium">{whyMissed.headline}</p>
      <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
        {whyMissed.detail}
      </p>

      {offer ? (
        <div className="mt-3">
          <MonitorCompanyButton
            companyName={offer.company_name}
            atsType={offer.ats_type}
            atsIdentifier={offer.ats_identifier}
            careersUrl={offer.careers_url}
            jobId={jobId}
          />
          <p className="mt-1 text-xs text-neutral-500">
            Adds this employer&rsquo;s board to every future collection. Reversible from{" "}
            <Link href="/collection" className="underline">
              Collection
            </Link>
            .
          </p>
        </div>
      ) : null}

      {whyMissed.secondary_reasons.includes("BROAD_DISCOVERY_NOT_ACTIVE") ? (
        <p className="mt-3 text-xs text-neutral-500">
          Broad discovery beyond your monitored boards is paused — no source has passed
          feasibility testing. That is the other reason a job like this arrives by hand.
        </p>
      ) : null}
    </Card>
  );
}
