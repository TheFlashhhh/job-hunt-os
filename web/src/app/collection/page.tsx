import Link from "next/link";

import { RunCollectionButton, UnmonitorCompanyButton } from "@/components/collection-actions";
import { BackendDown, Badge, Card, Empty, type Tone } from "@/components/ui";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

/**
 * The one canonical Collection page.
 *
 * The nav said "Collection" and pointed at `/runs`, and `/collection` 404'd —
 * so the question this page exists to answer, *how fresh is what I am looking
 * at*, had no home. `/runs` now redirects here and keeps the per-run table.
 *
 * Every figure below arrives from `/api/collection/status` already computed.
 * Nothing on this page derives freshness, counts a sweep, or decides what is
 * stale: the frontend re-implements no backend rule.
 */

function toneForFreshness(freshness: string): Tone {
  if (freshness === "FRESH") return "good";
  if (freshness === "AGING") return "warn";
  return "bad";
}

function freshnessLabel(freshness: string): string {
  switch (freshness) {
    case "FRESH":
      return "Fresh";
    case "AGING":
      return "Aging";
    case "STALE":
      return "Stale";
    default:
      return "Never collected";
  }
}

function ago(hours: number | null | undefined): string {
  if (hours === null || hours === undefined) return "never";
  if (hours < 1) return `${Math.round(hours * 60)} minutes ago`;
  if (hours < 48) return `${Math.round(hours)} hours ago`;
  return `${Math.round(hours / 24)} days ago`;
}

function stamp(value: string | null | undefined): string {
  return value ? value.slice(0, 16).replace("T", " ") + " UTC" : "—";
}

/** Collection timestamps are UTC and labelled UTC; schedule times are the
 *  operating system's and are local. Every timestamp on this page carries its
 *  zone rather than leaving the reader to guess which one they are looking at. */
function localStamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ` +
    `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
  );
}

function scheduleTone(state: string): Tone {
  if (state === "REGISTERED_NEXT_RUN_KNOWN") return "good";
  if (state === "REGISTERED_NEXT_RUN_UNKNOWN") return "info";
  if (state === "DISABLED" || state === "ERROR") return "bad";
  return "warn";
}

function scheduleLabel(state: string): string {
  switch (state) {
    case "REGISTERED_NEXT_RUN_KNOWN":
      return "Scheduled";
    case "REGISTERED_NEXT_RUN_UNKNOWN":
      return "Registered — next run unknown";
    case "DISABLED":
      return "Registered but disabled";
    case "ERROR":
      return "Could not read the schedule";
    case "UNSUPPORTED_PLATFORM":
      return "No scheduler on this platform";
    default:
      return "Nothing is scheduled";
  }
}

export default async function CollectionPage() {
  let status;
  let monitored;
  let scheduleSetup;
  let runs;
  try {
    [status, monitored, scheduleSetup, runs] = await Promise.all([
      api.collectionStatus(),
      api.monitoredCompanies(),
      api.collectionSchedule(),
      api.collectionRuns(25, false),
    ]);
  } catch (error) {
    return <BackendDown error={error} />;
  }

  const sweep = status.latest_sweep;
  // What the operating system says, as opposed to what registering one would
  // look like — `scheduleSetup` is the command, `schedule` is the reality.
  const schedule = status.schedule ?? {};
  const discovered = monitored.companies.filter((c) => c.source === "DISCOVERED");
  const implemented = status.providers.filter((p) => p.implemented);
  const deferred = status.providers.filter((p) => !p.implemented);
  const broadState = String(status.broad_discovery?.state ?? "PAUSED");

  return (
    <div className="space-y-5">
      {/* ---------------------------------------------------------------- */}
      <Card
        title="Collection status"
        subtitle="Structured ATS collection. Public job boards cost $0."
        action={<RunCollectionButton running={status.run_in_progress} />}
      >
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <p className="text-xs uppercase tracking-wide text-neutral-500">Freshness</p>
            <p className="mt-1">
              <Badge tone={toneForFreshness(status.freshness)}>
                {freshnessLabel(status.freshness)}
              </Badge>
            </p>
            <p className="mt-1 text-xs text-neutral-500">
              Measured against a {status.interval_hours}-hour cadence.
            </p>
          </div>
          <div>
            <p className="text-xs uppercase tracking-wide text-neutral-500">
              Last successful collection
            </p>
            <p className="mt-1 text-sm font-medium">{stamp(status.last_successful_at)}</p>
            <p className="mt-1 text-xs text-neutral-500">
              {ago(status.hours_since_success)}
            </p>
          </div>
          <div>
            <p className="text-xs uppercase tracking-wide text-neutral-500">Next scheduled</p>
            {/* The headline comes from the scheduler's own state. "Registered
                but the next run time could not be read" is a different fact
                from "not scheduled", and the backend keeps them apart. */}
            <p className="mt-1 text-sm font-medium">{String(schedule.headline ?? "Not scheduled")}</p>
            <p className="mt-1 text-xs text-neutral-500">
              {schedule.registered
                ? `${schedule.scheduler}${schedule.cadence ? ` · ${schedule.cadence}` : ""}`
                : "Nothing runs on its own — see Scheduling below."}
            </p>
          </div>
          <div>
            <p className="text-xs uppercase tracking-wide text-neutral-500">
              Companies monitored
            </p>
            <p className="mt-1 text-sm font-medium tabular-nums">
              {String(status.monitored?.total_polled ?? 0)}
            </p>
            <p className="mt-1 text-xs text-neutral-500">
              {String(status.monitored?.core_automated ?? 0)} curated ·{" "}
              {String(status.monitored?.discovered_monitored ?? 0)} added from a job.{" "}
              {String(status.monitored?.core_manual_only ?? 0)} more are on the watchlist
              with no connector for their ATS.
            </p>
          </div>
        </div>

        {status.run_in_progress ? (
          <p className="mt-4 rounded bg-sky-50 px-3 py-2 text-xs text-sky-800 dark:bg-sky-950 dark:text-sky-300">
            A collection is running now. Only one runs at a time, so the same boards are
            never polled twice.
          </p>
        ) : null}
      </Card>

      {/* ---------------------------------------------------------------- */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Latest run" subtitle="The last group of board polls, not one company">
          {sweep.companies === 0 ? (
            <Empty>No collection has run yet.</Empty>
          ) : (
            <>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
                <div>
                  <dt className="text-xs text-neutral-500">New jobs</dt>
                  <dd className="tabular-nums">{sweep.jobs_new}</dd>
                </div>
                <div>
                  <dt className="text-xs text-neutral-500">Updated</dt>
                  <dd className="tabular-nums">{sweep.jobs_changed}</dd>
                </div>
                <div>
                  <dt className="text-xs text-neutral-500">Unchanged</dt>
                  <dd className="tabular-nums">{sweep.jobs_unchanged}</dd>
                </div>
                <div>
                  <dt className="text-xs text-neutral-500">Boards polled</dt>
                  <dd className="tabular-nums">{sweep.companies}</dd>
                </div>
                <div>
                  <dt className="text-xs text-neutral-500">Failures</dt>
                  <dd className="tabular-nums">{sweep.jobs_failed + sweep.failures.length}</dd>
                </div>
                <div>
                  <dt className="text-xs text-neutral-500">Duration</dt>
                  <dd className="tabular-nums">
                    {sweep.duration_seconds
                      ? `${Math.round(sweep.duration_seconds / 60)} min`
                      : "—"}
                  </dd>
                </div>
              </dl>
              <p className="mt-3 text-xs text-neutral-500">
                Started {stamp(sweep.started_at)} · triggered by {sweep.triggered_by || "—"} ·{" "}
                {sweep.closure_candidates} posting(s) currently suspected closed. Disappearance
                checks run after every successful board poll; a failed poll is never treated as
                a closed job.
              </p>
              {sweep.failures.length > 0 ? (
                <ul className="mt-2 space-y-1 text-xs text-rose-600 dark:text-rose-400">
                  {sweep.failures.slice(0, 5).map((failure) => (
                    <li key={failure}>{failure}</li>
                  ))}
                </ul>
              ) : null}
            </>
          )}
        </Card>

        <Card title="Job corpus" subtitle="What is currently held, and how much is confirmed">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <div>
              <dt className="text-xs text-neutral-500">Jobs held</dt>
              <dd className="tabular-nums">{String(status.corpus?.jobs ?? 0)}</dd>
            </div>
            <div>
              <dt className="text-xs text-neutral-500">Currently open</dt>
              <dd className="tabular-nums">{String(status.corpus?.open ?? 0)}</dd>
            </div>
            <div>
              <dt className="text-xs text-neutral-500">Confirmed in the last run</dt>
              <dd className="tabular-nums">
                {String(status.corpus?.confirmed_in_last_sweep ?? 0)}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-neutral-500">Oldest first seen</dt>
              <dd>{stamp(status.corpus?.oldest_first_seen as string | null)}</dd>
            </div>
          </dl>
          <p className="mt-3 text-xs text-neutral-500">
            &ldquo;Confirmed&rdquo; means the source returned the posting during the last
            sweep. Anything below that number has not been re-checked since.
          </p>
        </Card>
      </div>

      {/* ---------------------------------------------------------------- */}
      <Card title="Providers" subtitle="What can be collected from, and what cannot">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-neutral-500">
              <tr>
                <th className="py-1.5 pr-4 font-medium">Provider</th>
                <th className="py-1.5 pr-4 font-medium">Boards</th>
                <th className="py-1.5 pr-4 font-medium" title="Collected successfully in the last 24 hours">
                  Healthy
                </th>
                <th className="py-1.5 pr-4 font-medium" title="Last poll failed, or no success in 24 hours">
                  Failing / stale
                </th>
                <th className="py-1.5 pr-4 font-medium" title="Jobs from this provider still open on their board">
                  Open jobs
                </th>
                <th className="py-1.5 pr-4 font-medium">New 24h</th>
                <th className="py-1.5 pr-4 font-medium">Last success</th>
                <th className="py-1.5 font-medium">Note</th>
              </tr>
            </thead>
            <tbody>
              {implemented.map((provider) => (
                <tr
                  key={provider.provider}
                  className="border-t border-neutral-100 dark:border-neutral-800"
                >
                  <td className="py-1.5 pr-4 font-medium">
                    {provider.provider}
                    {provider.enabled === false ? (
                      <span className="ml-1">
                        <Badge tone="warn">OFF</Badge>
                      </span>
                    ) : null}
                  </td>
                  <td className="py-1.5 pr-4 tabular-nums">{provider.monitored_companies}</td>
                  <td className="py-1.5 pr-4 tabular-nums">{provider.healthy_boards ?? 0}</td>
                  <td className="py-1.5 pr-4 tabular-nums">
                    {(provider.failing_boards ?? 0) + (provider.stale_boards ?? 0) > 0 ? (
                      <Badge tone="bad">
                        {(provider.failing_boards ?? 0) + (provider.stale_boards ?? 0)}
                      </Badge>
                    ) : (
                      0
                    )}
                    {provider.never_collected_boards ? (
                      <span
                        className="ml-1 text-xs text-neutral-500"
                        title="Monitored, but no poll has run yet"
                      >
                        +{provider.never_collected_boards} not yet run
                      </span>
                    ) : null}
                  </td>
                  <td className="py-1.5 pr-4 tabular-nums">{provider.open_jobs ?? 0}</td>
                  <td className="py-1.5 pr-4 tabular-nums">{provider.new_jobs_24h ?? 0}</td>
                  <td className="py-1.5 pr-4 text-xs text-neutral-500">
                    {stamp(provider.last_success_at)}
                  </td>
                  <td className="py-1.5 text-xs text-neutral-500">{provider.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <details className="mt-3">
          <summary className="cursor-pointer text-xs text-neutral-500">
            {deferred.length} source(s) deliberately not implemented
          </summary>
          <ul className="mt-2 space-y-1 text-xs text-neutral-500">
            {deferred.map((provider) => (
              <li key={provider.provider}>
                <span className="font-medium">{provider.provider}</span> — {provider.note}
              </li>
            ))}
          </ul>
        </details>
      </Card>

      {/* ---------------------------------------------------------------- */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="Broad discovery"
          subtitle="Coverage beyond the boards you monitor"
        >
          <p>
            <Badge tone={broadState === "ACTIVE" ? "good" : "warn"}>
              {broadState.replace(/_/g, " ")}
            </Badge>
          </p>
          <p className="mt-2 text-xs text-neutral-500">
            {String(status.broad_discovery?.detail ?? "")}
          </p>
          <p className="mt-2 text-xs text-neutral-500">
            {String(status.broad_discovery?.approved_actors ?? 0)} approved source(s). Until
            that changes, anything outside your monitored boards reaches the system by{" "}
            <Link href="/jobs/import" className="underline">
              pasting a link
            </Link>
            .
          </p>
        </Card>

        <Card title="Scheduling" subtitle="Local machine, honest answer">
          <p className="text-sm">
            <Badge tone={scheduleTone(String(schedule.state ?? ""))}>
              {scheduleLabel(String(schedule.state ?? ""))}
            </Badge>
          </p>

          {schedule.registered ? (
            <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
              <dt className="text-neutral-500">Scheduler</dt>
              <dd>{String(schedule.scheduler ?? "")}</dd>
              <dt className="text-neutral-500">Task</dt>
              <dd className="font-mono text-[11px]">{String(schedule.task_name ?? "")}</dd>
              <dt className="text-neutral-500">Cadence</dt>
              <dd>{String(schedule.cadence || `Every ${scheduleSetup.interval_hours} hours`)}</dd>
              <dt className="text-neutral-500">Next run</dt>
              <dd>
                {schedule.next_run_local
                  ? `${String(schedule.next_run_local)} local`
                  : "unavailable"}
              </dd>
              <dt className="text-neutral-500">Last run</dt>
              <dd>
                {schedule.last_run_at
                  ? `${localStamp(schedule.last_run_at as string)} local`
                  : "never"}
              </dd>
            </dl>
          ) : null}

          {schedule.detail ? (
            <p className="mt-2 text-xs text-neutral-500">{String(schedule.detail)}</p>
          ) : null}

          {!schedule.registered ? (
            <>
              <p className="mt-2 text-xs text-neutral-500">
                Nothing collects while this machine is off, so the schedule belongs to
                Windows Task Scheduler rather than to a background thread that would only
                run when the server happens to be up.
              </p>
              <pre className="mt-2 overflow-x-auto rounded bg-neutral-100 p-2 text-[11px] dark:bg-neutral-800">
                python -m app.cli schedule --register
              </pre>
              <ul className="mt-2 space-y-1 text-xs text-neutral-500">
                {scheduleSetup.notes.slice(0, 3).map((note) => (
                  <li key={note}>· {note}</li>
                ))}
              </ul>
            </>
          ) : null}
        </Card>
      </div>

      {/* ---------------------------------------------------------------- */}
      <Card
        title="Monitored companies"
        subtitle={
          `${monitored.core_count} on the curated watchlist, ` +
          `${status.monitored?.core_automated ?? 0} of them pollable · ` +
          `${monitored.discovered_count} added from a job`
        }
      >
        {discovered.length === 0 ? (
          <Empty>
            Nothing has been added beyond the curated watchlist yet. When you import a job
            from an employer on a supported board, its Networking and Discovery panels offer
            to monitor them.
          </Empty>
        ) : (
          <ul className="divide-y divide-neutral-100 text-sm dark:divide-neutral-800">
            {discovered.map((company) => (
              <li key={company.company_id} className="flex items-baseline gap-3 py-2">
                <span className="font-medium">{company.company_name}</span>
                <Badge tone="info">{company.ats_type}</Badge>
                <span className="text-xs text-neutral-500">
                  {company.jobs_collected} job(s) collected
                </span>
                <span className="ml-auto">
                  {company.company_id ? (
                    <UnmonitorCompanyButton
                      companyId={company.company_id}
                      companyName={company.company_name}
                    />
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-3 text-xs text-neutral-500">
          The curated watchlist lives in <code>data/config/companies.yaml</code> and is edited
          by hand — the app reads it and never writes it. Companies added from a job are
          application state and can be removed here.{" "}
          <Link href="/sources" className="underline">
            Source health
          </Link>{" "}
          checks whether each board is reachable right now.
        </p>
      </Card>

      {/* ---------------------------------------------------------------- */}
      <Card title="Recent runs" subtitle="One row per board poll">
        {runs.count === 0 ? (
          <Empty>No collection runs recorded.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-neutral-500">
                <tr>
                  <th className="py-1.5 pr-4 font-medium">Connector</th>
                  <th className="py-1.5 pr-4 font-medium">Company</th>
                  <th className="py-1.5 pr-4 font-medium">Started</th>
                  <th className="py-1.5 pr-3 font-medium">New</th>
                  <th className="py-1.5 pr-3 font-medium">Changed</th>
                  <th className="py-1.5 pr-3 font-medium">Unchanged</th>
                  <th className="py-1.5 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {runs.runs.map((run) => (
                  <tr
                    key={run.collection_run_id}
                    className="border-t border-neutral-100 dark:border-neutral-800"
                  >
                    <td className="py-1.5 pr-4">{run.connector}</td>
                    <td className="py-1.5 pr-4 text-neutral-600 dark:text-neutral-400">
                      {run.company_name ?? run.source ?? "—"}
                    </td>
                    <td className="py-1.5 pr-4 text-xs text-neutral-500">
                      {stamp(run.started_at)}
                    </td>
                    <td className="py-1.5 pr-3 tabular-nums">{run.jobs_new}</td>
                    <td className="py-1.5 pr-3 tabular-nums">{run.jobs_changed}</td>
                    <td className="py-1.5 pr-3 tabular-nums">{run.jobs_unchanged}</td>
                    <td className="py-1.5">
                      <Badge
                        tone={
                          run.status === "COMPLETED"
                            ? "good"
                            : run.status === "PARTIAL"
                              ? "warn"
                              : run.status === "FAILED"
                                ? "bad"
                                : "neutral"
                        }
                      >
                        {run.status}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
