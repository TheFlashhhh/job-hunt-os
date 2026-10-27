import Link from "next/link";

import { api } from "@/lib/api";
import {
  Badge,
  BackendDown,
  Empty,
  toneForBand,
  toneForEligibility,
} from "@/components/ui";

export const dynamic = "force-dynamic";

type SearchParams = Record<string, string | undefined>;

const SORTS = [
  { value: "RECOMMENDED", label: "Recommended" },
  { value: "NEWEST", label: "Newest" },
  { value: "HIGHEST_FIT", label: "Highest fit" },
  { value: "HIGHEST_PRIORITY", label: "Highest priority" },
  { value: "RECENTLY_DISCOVERED", label: "Recently discovered" },
];

const ROLE_FAMILIES = [
  "DATA_SCIENTIST",
  "ML_ENGINEER",
  "AI_ENGINEER",
  "APPLIED_AI",
  "DATA_ANALYST",
  "PRODUCT_ANALYTICS",
  "DATA_ENGINEERING",
  "ANALYTICS_ENGINEERING",
];

const CRM_STATES = [
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

/** Target functions, in the order they matter for this search. Reads as
 *  English because it is a filter label, not a code identifier. */
const FUNCTIONS = [
  { value: "DATA_SCIENCE", label: "Data Science" },
  { value: "DATA_ANALYTICS", label: "Data & Business Analytics" },
  { value: "PRODUCT_ANALYTICS", label: "Product Analytics" },
  { value: "DECISION_ANALYTICS", label: "Decision Science" },
  { value: "ANALYTICS_ENGINEERING", label: "Analytics Engineering / BI" },
  { value: "DATA_ENGINEERING", label: "Data Engineering" },
  { value: "MACHINE_LEARNING_ENGINEERING", label: "ML Engineering" },
  { value: "APPLIED_AI_SOFTWARE", label: "Applied AI" },
  // A secondary target since 2026-09-07 (D-49) — real, and usually ranked
  // behind a data or AI role at similar fit.
  { value: "SOFTWARE_ENGINEERING", label: "Software Engineering" },
];

const TARGET_PRIORITIES = [
  { value: "PRIMARY_TARGET", label: "Primary targets" },
  { value: "SECONDARY_TARGET", label: "Secondary (software)" },
];

const FUNCTION_LABELS: Record<string, string> = {
  DATA_SCIENCE: "Data Science",
  DATA_ANALYTICS: "Data & Business Analytics",
  PRODUCT_ANALYTICS: "Product Analytics",
  DECISION_ANALYTICS: "Decision Science",
  ANALYTICS_ENGINEERING: "Analytics Engineering / BI",
  DATA_ENGINEERING: "Data Engineering",
  MACHINE_LEARNING_ENGINEERING: "ML Engineering",
  APPLIED_AI_SOFTWARE: "Applied AI",
  SOFTWARE_ENGINEERING: "Software Engineering",
};

const EXPERIENCE_BANDS = [
  { value: "ENTRY_COMPATIBLE", label: "Entry compatible" },
  { value: "EARLY_CAREER", label: "Early career" },
  { value: "REACH", label: "Reach" },
  { value: "EXPERIENCE_UNSTATED", label: "Not stated" },
];

export default async function JobsPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await searchParams;
  const sort = sp.sort ?? "RECOMMENDED";
  const showRejected = sp.show_rejected === "1";
  // Function-first filters (D-46). `hidden_fit` is about the TITLE hiding the
  // work; `experience_band` is about seniority. They are independent.
  const hiddenFitOnly = sp.hidden_fit === "1";

  let data;
  try {
    data = await api.jobs({
      sort,
      role_family: sp.role_family,
      location: sp.location,
      remote: sp.remote,
      employment_type: sp.employment_type,
      eligibility: sp.eligibility,
      crm_state: sp.crm_state,
      experience_fit: sp.experience_fit,
      company: sp.company,
      source: sp.source,
      q: sp.q,
      function: sp.function,
      target_priority: sp.target_priority,
      hidden_fit: hiddenFitOnly,
      experience_band: sp.experience_band,
      relevant_only: !showRejected,
      limit: 100,
    });
  } catch (error) {
    return <BackendDown error={error} />;
  }

  const jobs = data.jobs ?? [];

  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Jobs</h1>
          <p className="text-xs text-neutral-500">
            Full-time roles starting Dec 2026 / Jan 2027 onward.
          </p>
        </div>
        <Link
          href="/jobs/import"
          className="shrink-0 rounded bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-neutral-700 dark:bg-neutral-100 dark:text-neutral-900"
        >
          + Add Job
        </Link>
      </div>

      <form
        method="get"
        className="mb-4 grid grid-cols-2 gap-2 rounded-lg border border-neutral-200 bg-white p-3 text-sm dark:border-neutral-800 dark:bg-neutral-900 sm:grid-cols-4 lg:grid-cols-8"
      >

        <select
          name="function"
          defaultValue={sp.function ?? ""}
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        >
          <option value="">Any function</option>
          {FUNCTIONS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>

        <select
          name="target_priority"
          defaultValue={sp.target_priority ?? ""}
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        >
          <option value="">Any priority</option>
          {TARGET_PRIORITIES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>

        <select
          name="experience_band"
          defaultValue={sp.experience_band ?? ""}
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        >
          <option value="">Any experience</option>
          {EXPERIENCE_BANDS.map((b) => (
            <option key={b.value} value={b.value}>
              {b.label}
            </option>
          ))}
        </select>

        <input
          name="q"
          defaultValue={sp.q ?? ""}
          placeholder="Search title/company/location"
          className="col-span-2 rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950 lg:col-span-2"
        />

        <select
          name="sort"
          defaultValue={sort}
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        >
          {SORTS.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>

        <select
          name="role_family"
          defaultValue={sp.role_family ?? ""}
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        >
          <option value="">Any role family</option>
          {ROLE_FAMILIES.map((rf) => (
            <option key={rf} value={rf}>
              {rf.replaceAll("_", " ")}
            </option>
          ))}
        </select>

        <select
          name="remote"
          defaultValue={sp.remote ?? ""}
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        >
          <option value="">Any location type</option>
          <option value="REMOTE">Remote</option>
          <option value="HYBRID">Hybrid</option>
          <option value="ONSITE">Onsite</option>
        </select>

        <select
          name="employment_type"
          defaultValue={sp.employment_type ?? ""}
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        >
          {/* Internship and Co-op were options here until 2026-09-08 (D-51).
              The search is full-time only and those records were purged, so
              both filters returned an empty list and implied a target that
              does not exist. */}
          <option value="">Any employment type</option>
          <option value="FULL_TIME">Full-time</option>
          <option value="CONTRACT">Contract</option>
        </select>

        <select
          name="eligibility"
          defaultValue={sp.eligibility ?? ""}
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        >
          <option value="">Any eligibility</option>
          <option value="ELIGIBLE">Eligible</option>
          <option value="LIKELY_ELIGIBLE">Likely eligible</option>
          <option value="UNCERTAIN">Uncertain</option>
          <option value="INELIGIBLE">Ineligible</option>
        </select>

        <select
          name="experience_fit"
          defaultValue={sp.experience_fit ?? ""}
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        >
          <option value="">Any experience fit</option>
          <option value="ENTRY_COMPATIBLE">Entry compatible</option>
          <option value="EARLY_CAREER">Early career</option>
          <option value="REACH_2_PLUS">Reach (2+ yrs)</option>
          <option value="STRETCH_3_PLUS">Stretch (3+ yrs)</option>
          <option value="SENIOR">Senior</option>
        </select>

        <select
          name="crm_state"
          defaultValue={sp.crm_state ?? ""}
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        >
          <option value="">Any CRM state</option>
          {CRM_STATES.map((s) => (
            <option key={s} value={s}>
              {s.replaceAll("_", " ")}
            </option>
          ))}
        </select>

        {/* A filter over the same list — user-added jobs still appear in every
            other view, which is the point of routing them through one pipeline. */}
        <select
          name="source"
          defaultValue={sp.source ?? ""}
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        >
          <option value="">Any source</option>
          <option value="USER_ADDED">User added</option>
          <option value="AUTOMATED">Automated discovery</option>
        </select>

        <input
          name="company"
          defaultValue={sp.company ?? ""}
          placeholder="Company"
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        />
        <input
          name="location"
          defaultValue={sp.location ?? ""}
          placeholder="Location contains…"
          className="rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        />

        <label className="flex items-center gap-1.5 text-xs">

          <input

            type="checkbox"

            name="hidden_fit"

            value="1"

            defaultChecked={hiddenFitOnly}

          />

          Hidden fits only

        </label>

        <label className="flex items-center gap-1.5 text-xs text-neutral-600 dark:text-neutral-400">
          <input type="checkbox" name="show_rejected" value="1" defaultChecked={showRejected} />
          Show rejected
        </label>

        <button
          type="submit"
          className="col-span-2 rounded bg-neutral-900 px-3 py-1 text-white transition hover:bg-neutral-700 dark:bg-neutral-100 dark:text-neutral-900 lg:col-span-1"
        >
          Apply filters
        </button>
        <Link
          href="/jobs"
          className="col-span-2 rounded border border-neutral-300 px-3 py-1 text-center text-neutral-600 hover:bg-neutral-50 dark:border-neutral-700 dark:text-neutral-400 dark:hover:bg-neutral-800 lg:col-span-1"
        >
          Reset
        </Link>
      </form>

      <p className="mb-3 text-xs text-neutral-500">
        {data.count} job{data.count === 1 ? "" : "s"} match
        {showRejected ? " (including rejected/unrelated)" : " — recommended actionable jobs only"}
        . The score is not a probability of being hired.
      </p>

      {jobs.length === 0 ? (
        <Empty>No jobs match these filters. Try loosening one, or Reset.</Empty>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900">
          <table className="w-full text-sm">
            <thead className="border-b border-neutral-200 text-left text-xs uppercase tracking-wide text-neutral-500 dark:border-neutral-800">
              <tr>
                <th className="px-3 py-2 font-medium">Role</th>
                <th className="px-3 py-2 font-medium">Badges</th>
                <th className="px-3 py-2 font-medium">Priority</th>
                <th className="px-3 py-2 font-medium">Eligibility</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Posted</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr
                  key={job.job_id}
                  className="border-b border-neutral-100 last:border-0 dark:border-neutral-800"
                >
                  <td className="px-3 py-2">
                    <Link
                      href={`/jobs/${job.job_id}`}
                      className="font-medium hover:underline"
                    >
                      {job.title}
                    </Link>
                    <div className="text-xs text-neutral-500">
                      {job.company}
                      {job.location ? ` · ${job.location}` : ""}
                      {job.employment_type ? ` · ${job.employment_type}` : ""}
                    </div>
                    {/* The function the JD describes, which for a hidden fit is
                        the thing the title does not say. */}
                    {job.functional_primary ? (
                      <div className="text-xs text-neutral-400">
                        {FUNCTION_LABELS[job.functional_primary] ?? job.functional_primary}
                      </div>
                    ) : null}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1">
                      {job.band === "HIGH" ? <Badge tone="good">HIGH FIT</Badge> : null}
                      {job.next_action ? <Badge tone="warn">REVIEW TODAY</Badge> : null}
                      {/* Unusual title, relevant function. Deliberately NOT the
                          same badge as REACH — that one is about seniority, and
                          a job can be either, both or neither. */}
                      {job.hidden_fit ? <Badge tone="good">HIDDEN FIT</Badge> : null}
                      {/* Only SECONDARY is badged. Marking the majority
                          PRIMARY would be badge noise, and the absence of this
                          badge already means primary. */}
                      {job.target_priority === "SECONDARY_TARGET" ? (
                        <Badge tone="neutral">SECONDARY</Badge>
                      ) : null}
                      {job.experience_band === "ENTRY_COMPATIBLE" ? (
                        <Badge tone="info">ENTRY</Badge>
                      ) : null}
                      {job.experience_band === "EARLY_CAREER" ? (
                        <Badge tone="info">EARLY CAREER</Badge>
                      ) : null}
                      {job.experience_band === "REACH" ? <Badge tone="warn">REACH</Badge> : null}
                      {job.experience_band === "OVERLEVELLED" ? (
                        <Badge tone="bad">OVERLEVELLED</Badge>
                      ) : null}
                      {job.start_fit === "START_TIMING_MISMATCH" ? (
                        <Badge tone="warn">TIMING</Badge>
                      ) : null}
                      {/* The Fall lane is archived, so its records are labelled
                          as history rather than presented as targets. */}
                      {job.lane_archived ? <Badge tone="neutral">ARCHIVED</Badge> : null}
                      {job.remote === "REMOTE" ? <Badge tone="neutral">REMOTE</Badge> : null}
                      {job.eligibility === "UNCERTAIN" ? (
                        <Badge tone="warn">ELIGIBILITY REVIEW</Badge>
                      ) : null}
                      {job.is_stale ? <Badge tone="warn">STALE</Badge> : null}

                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={toneForBand(job.band)}>
                      {job.priority} {job.band}
                    </Badge>
                  </td>
                  <td className="px-3 py-2">
                    <Badge
                      tone={toneForEligibility(job.eligibility)}
                      title={job.authorization ?? undefined}
                    >
                      {job.eligibility}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 text-xs">{job.application_status}</td>
                  <td className="px-3 py-2 text-xs text-neutral-500">
                    {job.posted_date ?? job.first_seen?.slice(0, 10) ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
