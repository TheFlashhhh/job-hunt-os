import Link from "next/link";

import { api, type JobRow } from "@/lib/api";
import { BackendDown, Badge, Card, Empty, toneForBand } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  // No lane parameter. Job Hunt OS is full-time-only as of 2026-09-08 (D-51):
  // there is one thing being searched for, so there is nothing to switch
  // between and no lane for the user to hold in their head.
  let dashboard;
  let execution;
  let reviewCount;
  let hiddenFitCount;
  let strongMatches;
  let recommended;
  try {
    [dashboard, execution, reviewCount, hiddenFitCount, strongMatches, recommended] =
      await Promise.all([
        api.dashboard("ALL"),
        api.execution("ALL"),
        api.jobs({ crm_state: "REVIEW", limit: 1 }),
        api.jobs({ hidden_fit: true, limit: 1 }),
        api.jobs({ sort: "HIGHEST_FIT", limit: 5 }),
        api.jobs({ sort: "RECOMMENDED", limit: 5 }),
      ]);
  } catch (error) {
    return <BackendDown error={error} />;
  }

  const kpis = dashboard.kpis;
  const funnel = dashboard.funnel;
  const panel = execution;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-3">
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

      <div className="grid gap-3 sm:grid-cols-4 lg:grid-cols-8">
        {/* A KPI that cannot be opened is trivia. Each one that has a real
            destination links to the filtered view that explains it. */}
        {(
          [
            ["Jobs to Review", reviewCount.count, "/jobs?relevant_only=true"],
            ["Apply Soon", panel.applications_needing_action.length, "/jobs?crm_state=READY_TO_APPLY"],
            ["Strong Matches", strongMatches.count, "/jobs?sort=HIGHEST_FIT"],
            ["Hidden Fits", hiddenFitCount.count, "/jobs?hidden_fit=1"],
            ["Applications This Week", panel.applications_this_week, "/jobs?crm_state=APPLIED"],
            ["Networking Follow-ups", panel.networking_follow_ups.length, "/people?due=1"],
            ["Interviews", kpis.interviews, null],
            ["Offers", kpis.offers, null],
          ] as [string, number, string | null][]
        ).map(([label, value, href]) => {
          const tile = (
            <>
              <div className="text-2xl font-semibold tabular-nums">{Number(value ?? 0)}</div>
              <div className="mt-0.5 text-xs text-neutral-500">{label}</div>
            </>
          );
          const className =
            "rounded-lg border border-neutral-200 bg-white p-3 dark:border-neutral-800 dark:bg-neutral-900";
          return href ? (
            <Link
              key={label}
              href={href}
              className={`${className} transition hover:border-neutral-400 dark:hover:border-neutral-600`}
            >
              {tile}
            </Link>
          ) : (
            <div key={label} className={className}>
              {tile}
            </div>
          );
        })}
      </div>

      <Card
        title="ACTION REQUIRED"
        subtitle="Active applications with nothing planned, or with an action now due"
      >
        {panel.applications_needing_action.length === 0 ? (
          <Empty>Nothing waiting on you.</Empty>
        ) : (
          <ul className="divide-y divide-neutral-100 text-sm dark:divide-neutral-800">
            {panel.applications_needing_action.map((row) => (
              <li key={row.job_id} className="flex items-baseline gap-2 py-2">
                <Link href={`/jobs/${row.job_id}`} className="font-medium hover:underline">
                  {row.title}
                </Link>
                <span className="text-neutral-500">{row.company}</span>
                <Badge tone="info">{row.status}</Badge>
                <span className="ml-auto text-xs text-neutral-500">
                  {row.next_action ?? "no next action set"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <JobMiniList
          title="RECENT STRONG MATCHES"
          subtitle="Highest-fit actionable jobs right now"
          jobs={strongMatches.jobs}
        />
        {/* One list, because there is one search. The two lane columns this
            replaced (FALL 2026 / 2027 FULL-TIME) described a split that no
            longer exists. */}
        <JobMiniList
          title="RECOMMENDED JOBS"
          subtitle="Primary data and AI roles first, then software"
          jobs={recommended.jobs}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="FOLLOW-UPS DUE" subtitle="Within 7 days. Drafting only — sending is always manual.">
          {panel.networking_follow_ups.length === 0 ? (
            <Empty>No follow-ups due.</Empty>
          ) : (
            <ul className="divide-y divide-neutral-100 text-sm dark:divide-neutral-800">
              {panel.networking_follow_ups.map((row) => (
                <li key={row.id} className="flex items-baseline gap-2 py-2">
                  <span className="flex-1">{row.subject}</span>
                  <Badge tone={row.overdue ? "bad" : "neutral"}>{row.due_date}</Badge>
                  {row.job_id ? (
                    <Link
                      href={`/jobs/${row.job_id}?tab=networking`}
                      className="text-xs underline"
                    >
                      Job
                    </Link>
                  ) : null}
                  <Link href="/people?due=1" className="text-xs underline">
                    People
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Stale applications" subtitle="Submitted, no movement">
          {panel.stale_applications.length === 0 ? (
            <Empty>Nothing has gone quiet.</Empty>
          ) : (
            <ul className="divide-y divide-neutral-100 text-sm dark:divide-neutral-800">
              {panel.stale_applications.map((row) => (
                <li key={row.job_id} className="flex items-baseline gap-2 py-2">
                  <Link href={`/jobs/${row.job_id}`} className="font-medium hover:underline">
                    {row.title}
                  </Link>
                  <span className="text-neutral-500">{row.company}</span>
                  <span className="ml-auto text-xs text-neutral-500">
                    {row.days_since_change ?? "?"} days
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title="Resume recommendations pending"
          subtitle="Shortlisted jobs with no tailoring advice generated yet"
        >
          {panel.resume_recommendations_pending.length === 0 ? (
            <Empty>All shortlisted jobs have recommendations.</Empty>
          ) : (
            <ul className="divide-y divide-neutral-100 text-sm dark:divide-neutral-800">
              {panel.resume_recommendations_pending.map((row) => (
                <li key={row.job_id} className="py-2">
                  <Link href={`/jobs/${row.job_id}`} className="font-medium hover:underline">
                    {row.title}
                  </Link>{" "}
                  <span className="text-neutral-500">{row.company}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Recurring skill gaps" subtitle="From shortlisted and applied jobs only">
          {panel.recurring_skill_gaps.length === 0 ? (
            <Empty>No gaps yet — shortlist or apply to jobs to build a baseline.</Empty>
          ) : (
            <ul className="space-y-1 text-sm">
              {panel.recurring_skill_gaps.map((gap) => (
                <li key={gap.skill} className="flex items-baseline gap-2">
                  <span className="font-medium">{gap.skill}</span>
                  <Badge tone={gap.mostly === "REQUIRED" ? "bad" : "warn"}>{gap.mostly}</Badge>
                  <span className="ml-auto text-xs text-neutral-500">
                    {gap.job_count} job{gap.job_count === 1 ? "" : "s"}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card title="Funnel" subtitle="Counts of applications that ever reached each stage">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <tbody>
              {funnel.map((row) => (
                <tr
                  key={row.stage}
                  className="border-b border-neutral-100 last:border-0 dark:border-neutral-800"
                >
                  <td className="py-1.5 pr-4">{row.stage}</td>
                  <td className="py-1.5 pr-4 tabular-nums">{row.count}</td>
                  <td className="py-1.5 text-xs text-neutral-500">
                    {row.small_sample ? "small sample — not meaningful yet" : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function JobMiniList({
  title,
  subtitle,
  jobs,
}: {
  title: string;
  subtitle: string;
  jobs: JobRow[];
}) {
  return (
    <Card title={title} subtitle={subtitle}>
      {jobs.length === 0 ? (
        <Empty>Nothing here right now.</Empty>
      ) : (
        <ul className="divide-y divide-neutral-100 text-sm dark:divide-neutral-800">
          {jobs.map((job) => (
            <li key={job.job_id} className="py-2">
              <Link href={`/jobs/${job.job_id}`} className="font-medium hover:underline">
                {job.title}
              </Link>
              <div className="mt-0.5 flex items-center gap-2 text-xs text-neutral-500">
                <span>{job.company}</span>
                <Badge tone={toneForBand(job.band)}>
                  {job.priority} {job.band}
                </Badge>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
