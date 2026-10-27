import { api } from "@/lib/api";
import { BackendDown, Badge, Card, Empty } from "@/components/ui";
import { AddCompanyForm, CompanyRegistryTable } from "@/components/company-registry";
import { providerLabel } from "@/lib/providers";

export const dynamic = "force-dynamic";

export default async function SourcesPage() {
  let watchlist;
  let summary;
  let registry;
  try {
    [watchlist, summary, registry] = await Promise.all([
      api.watchlist(),
      api.collectionSummary(),
      api.companyRegistry(),
    ]);
  } catch (error) {
    return <BackendDown error={error} />;
  }
  const coverage = registry.coverage;
  const gaps = coverage.manual_only + coverage.verify_later;

  return (
    <div className="space-y-5">
      <Card
        title="Company coverage"
        subtitle="Which employers are actually watched — the number that matters more than job count"
      >
        <div className="grid gap-3 sm:grid-cols-4">
          {(
            [
              ["Monitored companies", coverage.monitored, "Polled on every structured refresh"],
              ["Healthy in last 24h", coverage.healthy, "Last poll succeeded within a day"],
              [
                "Failing / stale",
                coverage.failing + coverage.stale,
                "Last poll failed, or nothing succeeded for a day",
              ],
              [
                "Unsupported / manual-only",
                gaps,
                "Relevant employers whose careers site cannot be polled",
              ],
            ] as const
          ).map(([label, value, hint]) => (
            <div
              key={label}
              title={hint}
              className="rounded-lg border border-neutral-200 bg-white p-3 dark:border-neutral-800 dark:bg-neutral-900"
            >
              <div className="text-2xl font-semibold tabular-nums">{value}</div>
              <div className="mt-0.5 text-xs text-neutral-500">{label}</div>
            </div>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-neutral-600 dark:text-neutral-400">
          {Object.entries(coverage.by_provider)
            .filter(([provider]) => provider !== "UNKNOWN")
            .map(([provider, row]) => (
              <span key={provider}>
                <span className="font-medium text-neutral-800 dark:text-neutral-200">
                  {providerLabel(provider)}
                </span>{" "}
                <span className="tabular-nums">{row.monitored}</span>
              </span>
            ))}
          {coverage.not_yet_collected ? (
            <span>{coverage.not_yet_collected} monitored but not yet run</span>
          ) : null}
          {coverage.experimental ? <span>{coverage.experimental} on trial</span> : null}
          {coverage.paused ? <span>{coverage.paused} paused</span> : null}
          {coverage.unknown_ats ? <span>{coverage.unknown_ats} with an unknown careers system</span> : null}
        </div>
        {coverage.failed_recently.length ? (
          <p className="mt-2 text-xs text-rose-600 dark:text-rose-400">
            Failed in the last 24 hours: {coverage.failed_recently.join(", ")}
          </p>
        ) : null}
      </Card>

      <Card
        title="Add a company"
        subtitle="Paste its job board or careers page. Nothing is monitored until you confirm."
      >
        <AddCompanyForm />
      </Card>

      <Card
        title="Monitored companies"
        subtitle="The curated list plus every company added since. Pausing keeps collected jobs."
      >
        {registry.companies.length === 0 ? (
          <Empty>No companies in the registry yet.</Empty>
        ) : (
          <CompanyRegistryTable companies={registry.companies} />
        )}
      </Card>

      {watchlist.awaiting_approval ? (
        <div className="rounded-lg border border-sky-300 bg-sky-50 p-3 text-sm dark:border-sky-900 dark:bg-sky-950">
          <p className="font-medium">No companies are active yet.</p>
          <p className="mt-1 text-neutral-700 dark:text-neutral-300">
            That is deliberate. Candidates are proposed in{" "}
            <code className="rounded bg-white px-1 dark:bg-neutral-900">
              context/WATCHLIST_PROPOSAL.md
            </code>{" "}
            and stay inactive until you approve them.
          </p>
        </div>
      ) : (
        <div className="rounded-lg border border-emerald-300 bg-emerald-50 p-3 text-sm dark:border-emerald-900 dark:bg-emerald-950">
          <p className="font-medium">
            Watchlist v{watchlist.watchlist_version ?? "?"} active
            {watchlist.activated_date ? ` since ${watchlist.activated_date}` : ""}.
          </p>
          <p className="mt-1 text-neutral-700 dark:text-neutral-300">
            <strong>{watchlist.automation_enabled}</strong> companies can be
            polled by a Channel A connector today.{" "}
            <strong>{watchlist.manual_only}</strong> stay on the watchlist with
            automation off — their ATS is not supported yet, which is a
            capability fact, not a reason to stop watching them.
          </p>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-4">
        {[
          ["Watchlist active", watchlist.watchlist_active],
          ["Automation enabled", watchlist.automation_enabled],
          ["Manual only", watchlist.manual_only],
          ["Fixtures", watchlist.fixtures],
        ].map(([label, value]) => (
          <div
            key={String(label)}
            className="rounded-lg border border-neutral-200 bg-white p-3 dark:border-neutral-800 dark:bg-neutral-900"
          >
            <div className="text-2xl font-semibold tabular-nums">
              {Number(value)}
            </div>
            <div className="mt-0.5 text-xs text-neutral-500">{label}</div>
          </div>
        ))}
      </div>

      <Card
        title="Configured sources"
        subtitle="Health is checked on demand from the CLI, never automatically"
      >
        {watchlist.entries.length === 0 ? (
          <Empty>No sources configured.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-neutral-500">
                <tr>
                  <th className="py-1.5 pr-4 font-medium">Company</th>
                  <th className="py-1.5 pr-4 font-medium">ATS</th>
                  <th className="py-1.5 pr-4 font-medium">Capability</th>
                  <th className="py-1.5 pr-4 font-medium">Scope</th>
                  <th className="py-1.5 pr-4 font-medium">Tier</th>
                  <th className="py-1.5 pr-4 font-medium">Fall signal</th>
                  <th className="py-1.5 font-medium">State</th>
                </tr>
              </thead>
              <tbody>
                {watchlist.entries.map((entry) => (
                  <tr
                    key={`${entry.company_name}-${entry.ats_identifier}`}
                    className="border-t border-neutral-100 dark:border-neutral-800"
                  >
                    <td className="py-1.5 pr-4 font-medium">
                      {entry.company_name}
                    </td>
                    <td className="py-1.5 pr-4 text-xs">
                      {entry.ats_type}:{entry.ats_identifier}
                    </td>
                    <td className="py-1.5 pr-4 text-xs text-neutral-500">
                      {entry.capability ?? "—"}
                    </td>
                    <td className="py-1.5 pr-4 text-xs">
                      {entry.watchlist_scope}
                    </td>
                    <td className="py-1.5 pr-4 text-xs">{entry.tier}</td>
                    <td className="py-1.5 pr-4 text-xs">
                      {entry.fall_signal?.startsWith("CURRENT_FALL") ? (
                        <Badge
                          tone={
                            entry.fall_signal === "CURRENT_FALL_ROLE_CANONICAL_OPEN"
                              ? "good"
                              : "warn"
                          }
                        >
                          {entry.fall_signal.replace("CURRENT_FALL_ROLE_", "")}
                        </Badge>
                      ) : (
                        <span className="text-neutral-500">
                          {entry.fall_signal ?? "UNKNOWN"}
                        </span>
                      )}
                    </td>
                    <td className="py-1.5">
                      <div className="flex flex-wrap gap-1">
                        {entry.is_fixture ? (
                          <Badge tone="neutral">FIXTURE</Badge>
                        ) : entry.active ? (
                          <Badge tone="good">WATCHED</Badge>
                        ) : (
                          <Badge tone="warn">AWAITING APPROVAL</Badge>
                        )}
                        {entry.active && !entry.is_fixture ? (
                          entry.automated_collection_enabled ? (
                            <Badge tone="info">AUTO</Badge>
                          ) : (
                            <Badge tone="warn">MANUAL</Badge>
                          )
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Connectors" subtitle="Structured public sources only">
        <ul className="space-y-1.5 text-sm">
          {summary.connectors.map((connector) => (
            <li
              key={connector.connector_id}
              className="flex items-baseline gap-2"
            >
              <span className="font-medium">{connector.source_type}</span>
              <Badge tone="info">{connector.capability}</Badge>
              <span className="text-xs text-neutral-500">
                v{connector.version}
              </span>
              <a
                href={connector.docs_url}
                className="ml-auto text-xs text-neutral-500 hover:underline"
              >
                docs
              </a>
            </li>
          ))}
        </ul>
      </Card>

      <Card
        title="Deliberately not implemented"
        subtitle="Recorded decisions, not gaps"
      >
        <ul className="space-y-1 text-xs text-neutral-600 dark:text-neutral-400">
          {Object.entries(summary.deferred_sources).map(([name, reason]) => (
            <li key={name}>
              <span className="font-medium text-neutral-800 dark:text-neutral-200">
                {name}
              </span>{" "}
              — {String(reason)}
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
