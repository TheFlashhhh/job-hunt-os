"use client";

/**
 * Client islands for the Sources page's company coverage section.
 *
 * Every action here is a click on one named company. Adding a company runs
 * detection twice — once to show the person what was found, and again on the
 * server when they confirm — because the browser never decides what a board
 * is. Only a confirmed board can be monitored; anything else can be kept as a
 * visible gap, never as coverage.
 */

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { browserApiPath } from "@/lib/api";
import type {
  RegistryActionResponse,
  RegistryCompanyView,
  SourceDetectionResponse,
  TestSourceResponse,
} from "@/lib/api";
import { Badge, type Tone } from "@/components/ui";
import { providerLabel } from "@/lib/providers";

async function callApi<T>(path: string, body: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(browserApiPath(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error(
      "Could not reach the Job Hunt OS backend. Is it running on port 8000?",
    );
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new Error(detail?.detail ?? `${response.status} ${response.statusText}`.trim());
  }
  return (await response.json()) as T;
}

const SOURCE_LABELS: Record<string, string> = {
  CORE_SEED: "Curated list",
  DISCOVERED: "From a job",
  USER_ADDED: "Added by you",
  IMPORTED_FROM_JOB: "From an import",
  CURATED_EXPANSION: "Expansion batch",
};

const STATUS_LABELS: Record<string, string> = {
  ACTIVE: "Monitored",
  PAUSED: "Paused",
  EXPERIMENTAL: "Trial",
  MANUAL_ONLY: "Manual only",
  VERIFY_LATER: "Verify later",
};

const HEALTH: Record<string, { label: string; tone: Tone }> = {
  HEALTHY: { label: "Healthy", tone: "good" },
  STALE: { label: "Stale", tone: "warn" },
  FAILING: { label: "Failing", tone: "bad" },
  UNSUPPORTED: { label: "Not pollable", tone: "neutral" },
  PAUSED: { label: "Paused", tone: "neutral" },
  EXPERIMENTAL: { label: "Trial", tone: "info" },
  NOT_YET_COLLECTED: { label: "Not yet run", tone: "info" },
};

/** UTC and labelled, as on the Collection page. A locale-formatted date renders
 *  differently on the server and in the browser and breaks hydration. */
function when(value?: string | null): string {
  return value ? value.slice(0, 16).replace("T", " ") + " UTC" : "—";
}

const BUTTON =
  "rounded border border-neutral-300 px-2 py-0.5 text-xs transition hover:bg-neutral-50 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800";
const SELECT =
  "rounded border border-neutral-300 bg-white px-2 py-1 text-xs dark:border-neutral-700 dark:bg-neutral-900";

/* ------------------------------------------------------------------------ */
/* The registry table                                                        */
/* ------------------------------------------------------------------------ */

export function CompanyRegistryTable({ companies }: { companies: RegistryCompanyView[] }) {
  const [provider, setProvider] = useState("ALL");
  const [status, setStatus] = useState("ALL");
  const [source, setSource] = useState("ALL");
  const [problemsOnly, setProblemsOnly] = useState(false);
  const [query, setQuery] = useState("");

  const providers = useMemo(
    () => Array.from(new Set(companies.map((c) => c.ats_type))).sort(),
    [companies],
  );
  const sources = useMemo(
    () => Array.from(new Set(companies.map((c) => c.registry_source))).sort(),
    [companies],
  );

  const rows = companies.filter(
    (c) =>
      (provider === "ALL" || c.ats_type === provider) &&
      (status === "ALL" || c.monitoring_status === status) &&
      (source === "ALL" || c.registry_source === source) &&
      (!problemsOnly || c.health === "FAILING" || c.health === "STALE") &&
      (!query || c.company_name.toLowerCase().includes(query.toLowerCase())),
  );

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search companies"
          className={`${SELECT} w-40`}
          aria-label="Search companies"
        />
        <select value={provider} onChange={(e) => setProvider(e.target.value)} className={SELECT} aria-label="Provider">
          <option value="ALL">All providers</option>
          {providers.map((p) => (
            <option key={p} value={p}>
              {providerLabel(p)}
            </option>
          ))}
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)} className={SELECT} aria-label="Monitoring status">
          <option value="ALL">Any status</option>
          {Object.entries(STATUS_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <select value={source} onChange={(e) => setSource(e.target.value)} className={SELECT} aria-label="Source type">
          <option value="ALL">Any source</option>
          {sources.map((s) => (
            <option key={s} value={s}>
              {SOURCE_LABELS[s] ?? s}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-1 text-xs text-neutral-600 dark:text-neutral-400">
          <input type="checkbox" checked={problemsOnly} onChange={(e) => setProblemsOnly(e.target.checked)} />
          Failing or stale only
        </label>
        <span className="ml-auto text-xs text-neutral-500">
          {rows.length} of {companies.length}
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-neutral-500">
            <tr>
              <th className="py-1.5 pr-3 font-medium">Company</th>
              <th className="py-1.5 pr-3 font-medium">Provider</th>
              <th className="py-1.5 pr-3 font-medium">Status</th>
              <th className="py-1.5 pr-3 font-medium">Last success</th>
              <th className="py-1.5 pr-3 font-medium" title="Postings the board listed on its last successful poll">
                Listed
              </th>
              <th className="py-1.5 pr-3 font-medium" title="New jobs from the last successful poll">
                Last new
              </th>
              <th className="py-1.5 pr-3 font-medium">Source</th>
              <th className="py-1.5 font-medium" />
            </tr>
          </thead>
          <tbody>
            {rows.map((company) => (
              <RegistryRow key={`${company.company_name}-${company.ats_identifier ?? ""}`} company={company} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RegistryRow({ company }: { company: RegistryCompanyView }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; tone: "ok" | "bad" } | null>(null);
  const health = HEALTH[company.health] ?? { label: company.health, tone: "neutral" as Tone };

  async function act(path: string, body: unknown, describe: (r: unknown) => { text: string; ok: boolean }) {
    setBusy(true);
    setNote(null);
    try {
      const result = await callApi<unknown>(path, body);
      const outcome = describe(result);
      setNote({ text: outcome.text, tone: outcome.ok ? "ok" : "bad" });
      if (outcome.ok && path !== "/api/company-registry/test") router.refresh();
    } catch (e) {
      setNote({ text: e instanceof Error ? e.message : String(e), tone: "bad" });
    } finally {
      setBusy(false);
    }
  }

  const identity = { company_id: company.company_id ?? null, company_name: company.company_name };

  return (
    <tr className="border-t border-neutral-100 align-top dark:border-neutral-800">
      <td className="py-1.5 pr-3">
        <div className="font-medium">{company.company_name}</div>
        {company.last_failure_reason && company.health === "FAILING" ? (
          <div className="max-w-xs text-xs text-rose-600 dark:text-rose-400" title={company.last_failure_reason}>
            {company.last_failure_reason.slice(0, 90)}
          </div>
        ) : null}
        {note ? (
          <div
            className={`max-w-xs text-xs ${
              note.tone === "ok" ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"
            }`}
          >
            {note.text}
          </div>
        ) : null}
      </td>
      <td className="py-1.5 pr-3 text-xs">
        {company.careers_url ? (
          <a href={company.careers_url} className="hover:underline" target="_blank" rel="noreferrer">
            {providerLabel(company.ats_type)}
          </a>
        ) : (
          providerLabel(company.ats_type)
        )}
      </td>
      <td className="py-1.5 pr-3">
        <Badge tone={health.tone}>{health.label}</Badge>
      </td>
      <td className="py-1.5 pr-3 text-xs text-neutral-500">{when(company.last_successful_collection_at)}</td>
      <td className="py-1.5 pr-3 text-xs tabular-nums">{company.last_jobs_seen ?? "—"}</td>
      <td className="py-1.5 pr-3 text-xs tabular-nums">{company.last_new_jobs ?? "—"}</td>
      <td className="py-1.5 pr-3 text-xs text-neutral-500">
        {SOURCE_LABELS[company.registry_source] ?? company.registry_source}
      </td>
      <td className="py-1.5">
        <div className="flex flex-wrap justify-end gap-1">
          {company.can_test ? (
            <button
              type="button"
              disabled={busy}
              className={BUTTON}
              title="One request to the board. Collects nothing."
              onClick={() =>
                act("/api/company-registry/test", identity, (r) => {
                  const result = r as TestSourceResponse;
                  return { text: result.ok ? `Reachable — ${result.detail}` : result.detail, ok: result.ok };
                })
              }
            >
              Test source
            </button>
          ) : null}
          {company.can_pause ? (
            <button
              type="button"
              disabled={busy}
              className={BUTTON}
              title="Stop polling. Jobs already collected stay; resume any time."
              onClick={() =>
                act("/api/company-registry/monitoring", { ...identity, status: "PAUSED" }, (r) => {
                  const result = r as RegistryActionResponse;
                  return { text: result.detail ?? "", ok: result.ok };
                })
              }
            >
              Pause
            </button>
          ) : null}
          {company.can_resume ? (
            <button
              type="button"
              disabled={busy}
              className={BUTTON}
              onClick={() =>
                act("/api/company-registry/monitoring", { ...identity, status: "ACTIVE" }, (r) => {
                  const result = r as RegistryActionResponse;
                  return { text: result.detail ?? "", ok: result.ok };
                })
              }
            >
              Monitor
            </button>
          ) : null}
        </div>
      </td>
    </tr>
  );
}

/* ------------------------------------------------------------------------ */
/* + Add Company                                                             */
/* ------------------------------------------------------------------------ */

export function AddCompanyForm() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [detection, setDetection] = useState<SourceDetectionResponse | null>(null);
  const [message, setMessage] = useState<{ text: string; tone: "ok" | "bad" } | null>(null);

  function reset() {
    setDetection(null);
    setMessage(null);
  }

  async function detect() {
    setBusy(true);
    reset();
    try {
      setDetection(
        await callApi<SourceDetectionResponse>("/api/company-registry/detect", {
          company_name: name.trim(),
          careers_url: url.trim(),
        }),
      );
    } catch (e) {
      setMessage({ text: e instanceof Error ? e.message : String(e), tone: "bad" });
    } finally {
      setBusy(false);
    }
  }

  async function add(manualOnly: boolean) {
    setBusy(true);
    setMessage(null);
    try {
      const result = await callApi<RegistryActionResponse>("/api/company-registry", {
        company_name: name.trim(),
        careers_url: url.trim(),
        manual_only: manualOnly,
      });
      setMessage({ text: result.detail ?? "", tone: result.ok ? "ok" : "bad" });
      if (result.ok) {
        setDetection(null);
        setName("");
        setUrl("");
        router.refresh();
      }
    } catch (e) {
      setMessage({ text: e instanceof Error ? e.message : String(e), tone: "bad" });
    } finally {
      setBusy(false);
    }
  }

  const ready = name.trim().length > 0 && url.trim().length > 0;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-neutral-600 dark:text-neutral-400">
          Company
          <input
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              reset();
            }}
            placeholder="Capital One"
            className="mt-0.5 block w-44 rounded border border-neutral-300 bg-white px-2 py-1 text-sm dark:border-neutral-700 dark:bg-neutral-900"
          />
        </label>
        <label className="text-xs text-neutral-600 dark:text-neutral-400">
          Careers URL
          <input
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              reset();
            }}
            placeholder="https://… the job board or careers page"
            className="mt-0.5 block w-80 max-w-full rounded border border-neutral-300 bg-white px-2 py-1 text-sm dark:border-neutral-700 dark:bg-neutral-900"
          />
        </label>
        <button
          type="button"
          disabled={!ready || busy}
          onClick={detect}
          className="rounded bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-neutral-700 disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900"
        >
          {busy && !detection ? "Checking…" : "Test source"}
        </button>
      </div>

      {detection ? (
        <div className="rounded border border-neutral-200 p-3 text-sm dark:border-neutral-800">
          {detection.provider ? (
            <p>
              Detected provider: <strong>{providerLabel(detection.provider)}</strong>
              {detection.identifier ? (
                <>
                  {" "}
                  · Board:{" "}
                  {detection.board_url ? (
                    <a href={detection.board_url} target="_blank" rel="noreferrer" className="underline">
                      {detection.identifier}
                    </a>
                  ) : (
                    detection.identifier
                  )}
                </>
              ) : null}
            </p>
          ) : null}
          <p className="mt-1 text-xs text-neutral-600 dark:text-neutral-400">{detection.detail}</p>

          {detection.already_in_registry ? (
            <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
              Already in the registry as {detection.already_in_registry}.
            </p>
          ) : detection.can_monitor ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => add(false)}
              className="mt-2 rounded bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-neutral-700 disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900"
            >
              Monitor {name.trim()}
            </button>
          ) : detection.confidence === "LIKELY_REVIEW" ? (
            <p className="mt-2 text-xs text-neutral-600 dark:text-neutral-400">
              Not monitored from a page link alone. Open the board above; if it is this
              employer&apos;s own, paste the board&apos;s URL here and test again.
            </p>
          ) : (
            <div className="mt-2">
              <p className="text-xs text-neutral-600 dark:text-neutral-400">
                This career site is not currently supported for automatic monitoring.
              </p>
              <button type="button" disabled={busy} onClick={() => add(true)} className={`mt-1 ${BUTTON}`}>
                Keep as manual-only
              </button>
            </div>
          )}
        </div>
      ) : null}

      {message ? (
        <p
          className={`text-xs ${
            message.tone === "ok" ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"
          }`}
        >
          {message.text}
        </p>
      ) : null}
    </div>
  );
}
