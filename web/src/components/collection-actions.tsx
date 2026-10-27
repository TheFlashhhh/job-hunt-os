"use client";

/**
 * Client-side islands for the Collection page.
 *
 * Two of them start real work on someone else's servers, so both make the
 * boundary visible rather than implicit: Run Collection Now polls its own
 * status until the sweep finishes, and Monitor This Company names the board it
 * is about to add. Neither can be triggered by anything but a click.
 *
 * Like the rest of the client islands, these go through the same-origin Next
 * rewrite (never the backend origin directly) and then call `router.refresh()`
 * so the surrounding Server Component re-reads the truth from the backend.
 */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { browserApiPath } from "@/lib/api";

async function callApi<T>(path: string, options?: { method?: string; body?: unknown }): Promise<T> {
  let response: Response;
  try {
    response = await fetch(browserApiPath(path), {
      method: options?.method ?? "POST",
      headers: { "Content-Type": "application/json" },
      body: options?.method === "GET" ? undefined : JSON.stringify(options?.body ?? {}),
    });
  } catch {
    throw new Error(
      "Could not reach the Job Hunt OS backend. Is it running on port 8000? " +
        "Start it with: uvicorn app.main:app --reload --port 8000",
    );
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new Error(detail?.detail ?? `${response.status} ${response.statusText}`.trim());
  }
  return (await response.json()) as T;
}

type RunResponse = {
  started: boolean;
  reason?: string;
  detail?: string;
};

type StatusResponse = {
  run_in_progress: boolean;
  latest_sweep: {
    companies: number;
    jobs_new: number;
    jobs_changed: number;
    jobs_failed: number;
    duration_seconds: number | null;
  };
};

/**
 * A full sweep of the watchlist took just over fifty minutes the last time it
 * ran, so the button starts the work and then watches for it to finish rather
 * than holding a request open.
 */
export function RunCollectionButton({ running }: { running: boolean }) {
  const router = useRouter();
  const [busy, setBusy] = useState(running);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (timer.current) {
      clearInterval(timer.current);
      timer.current = null;
    }
  }, []);

  const poll = useCallback(() => {
    stopPolling();
    timer.current = setInterval(async () => {
      try {
        const status = await callApi<StatusResponse>("/api/collection/status", { method: "GET" });
        if (!status.run_in_progress) {
          stopPolling();
          setBusy(false);
          const sweep = status.latest_sweep;
          setMessage(
            `Finished: ${sweep.companies} board(s), ${sweep.jobs_new} new, ` +
              `${sweep.jobs_changed} updated` +
              (sweep.jobs_failed ? `, ${sweep.jobs_failed} failed` : "") +
              (sweep.duration_seconds ? ` in ${Math.round(sweep.duration_seconds)}s` : ""),
          );
          router.refresh();
        }
      } catch {
        // A transient failure while polling is not a failed collection. The
        // sweep runs in the backend regardless of whether this page can see it.
      }
    }, 5000);
  }, [router, stopPolling]);

  useEffect(() => {
    if (running) poll();
    return stopPolling;
  }, [running, poll, stopPolling]);

  return (
    <div>
      <button
        type="button"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError(null);
          setMessage(null);
          try {
            const result = await callApi<RunResponse>("/api/collection/run");
            if (!result.started) {
              setError(result.detail ?? "A collection is already running.");
              setBusy(false);
              return;
            }
            setMessage("Collecting. This takes a few minutes for the full watchlist.");
            poll();
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
            setBusy(false);
          }
        }}
        className="rounded bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-neutral-700 disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900"
      >
        {busy ? "Collecting…" : "Run Collection Now"}
      </button>
      {message ? (
        <p className="mt-1 text-xs text-neutral-600 dark:text-neutral-400">{message}</p>
      ) : null}
      {error ? (
        <p className="mt-1 max-w-sm text-xs text-rose-600 dark:text-rose-400">{error}</p>
      ) : null}
    </div>
  );
}

type MonitorResponse = {
  ok: boolean;
  detail?: string;
  reason?: string;
};

/** Adds one employer's board to what gets polled. One click, one company. */
export function MonitorCompanyButton({
  companyName,
  atsType,
  atsIdentifier,
  careersUrl,
  jobId,
  compact,
}: {
  companyName: string;
  atsType: string;
  atsIdentifier: string;
  careersUrl?: string | null;
  jobId?: string;
  compact?: boolean;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (done) {
    return <p className="text-xs text-emerald-600 dark:text-emerald-400">{done}</p>;
  }

  return (
    <div>
      <button
        type="button"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            const result = await callApi<MonitorResponse>("/api/monitored-companies", {
              body: {
                company_name: companyName,
                ats_type: atsType,
                ats_identifier: atsIdentifier,
                careers_url: careersUrl ?? null,
                job_id: jobId ?? null,
              },
            });
            if (!result.ok) {
              setError(result.detail ?? "Could not monitor this company.");
              return;
            }
            setDone(result.detail ?? `${companyName} is now monitored.`);
            router.refresh();
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
        className={
          compact
            ? "rounded border border-neutral-300 px-2 py-0.5 text-xs transition hover:bg-neutral-50 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800"
            : "rounded bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-neutral-700 disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900"
        }
      >
        {busy ? "Adding…" : `Monitor ${companyName}`}
      </button>
      {error ? (
        <p className="mt-1 max-w-sm text-xs text-rose-600 dark:text-rose-400">{error}</p>
      ) : null}
    </div>
  );
}

/** Stops polling a company that was added from a job. Never touches the curated file. */
export function UnmonitorCompanyButton({
  companyId,
  companyName,
}: {
  companyId: string;
  companyName: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <div>
      <button
        type="button"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            const result = await callApi<MonitorResponse>(
              `/api/monitored-companies/${companyId}/unmonitor`,
            );
            if (!result.ok) {
              setError(result.detail ?? "Could not stop monitoring.");
              return;
            }
            router.refresh();
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
        className="rounded border border-neutral-300 px-2 py-0.5 text-xs text-neutral-600 transition hover:bg-neutral-50 disabled:opacity-50 dark:border-neutral-700 dark:text-neutral-400 dark:hover:bg-neutral-800"
        title={`Stop polling ${companyName}. Jobs already collected stay.`}
      >
        Stop monitoring
      </button>
      {error ? (
        <p className="mt-1 text-xs text-rose-600 dark:text-rose-400">{error}</p>
      ) : null}
    </div>
  );
}
