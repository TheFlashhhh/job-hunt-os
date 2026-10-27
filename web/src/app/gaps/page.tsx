import { api } from "@/lib/api";
import { BackendDown, Badge, Card, Empty } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function GapsPage() {
  let data;
  try {
    data = await api.skillGaps("ALL");
  } catch (error) {
    return <BackendDown error={error} />;
  }

  const payload = data;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Skill Gaps</h1>
        <p className="text-xs text-neutral-500">
          Across full-time roles starting Dec 2026 / Jan 2027 onward.
        </p>
      </div>

      <div className="rounded-lg border border-neutral-200 bg-white p-3 text-xs dark:border-neutral-800 dark:bg-neutral-900">
        <p>
          Population: <strong>{payload.population}</strong> ·{" "}
          {payload.jobs_considered} job
          {payload.jobs_considered === 1 ? "" : "s"} counted
          {payload.small_sample
            ? " · small sample, treat as directional only"
            : ""}
        </p>
        <p className="mt-1 text-neutral-500">{payload.note}</p>
      </div>

      <Card
        title="Recurring gaps"
        subtitle="Ranked by priority-weighted job count"
      >
        {payload.gaps.length === 0 ? (
          <Empty>
            No gaps yet. Shortlist or apply to jobs and the baseline builds
            itself.
          </Empty>
        ) : (
          <ul className="space-y-3 text-sm">
            {payload.gaps.map((gap) => (
              <li
                key={gap.skill}
                className="border-b border-neutral-100 pb-3 last:border-0 dark:border-neutral-800"
              >
                <div className="flex items-baseline gap-2">
                  <span className="font-medium">{gap.skill}</span>
                  <Badge tone={gap.mostly === "REQUIRED" ? "bad" : "warn"}>
                    {gap.mostly}
                  </Badge>
                  <Badge tone="neutral">{gap.evidence_level}</Badge>
                  <span className="ml-auto text-xs text-neutral-500">
                    {gap.job_count} job{gap.job_count === 1 ? "" : "s"} · weight{" "}
                    {gap.weighted_job_count}
                  </span>
                </div>
                <p className="mt-1 text-xs text-neutral-500">
                  {gap.representative_jobs
                    .map((job) => `${job.title} @ ${job.company}`)
                    .join(" · ")}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
