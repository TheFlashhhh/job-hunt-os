"use client";

/**
 * Client-side action islands for Slice 5: everything on the Job Detail page
 * that POSTs to the backend and then needs the page to reflect the new
 * state. These run in the BROWSER, so they must not call the backend origin
 * directly — that is cross-origin and triggers a CORS preflight. They go
 * through the same-origin Next rewrite (see `lib/api.ts` and
 * `next.config.ts`) and then call `router.refresh()` so the surrounding
 * Server Component re-fetches
 * everything fresh — no client-side cache to get out of sync with the
 * backend, which is the same source-of-truth rule `lib/api.ts` states for
 * the server side.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { browserApiPath } from "@/lib/api";

/**
 * A `fetch` rejection here means the request never reached the backend at all
 * — the server is down, or the proxy is misconfigured. The browser reports
 * that as the bare string "Failed to fetch", which tells a user nothing and
 * was exactly what appeared under Prepare Application. Translate it.
 */
async function postJson<T>(path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(browserApiPath(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
  } catch {
    throw new Error(
      "Could not reach the Job Hunt OS backend. Is it running on port 8000? " +
        "Start it with: uvicorn app.main:app --reload --port 8000",
    );
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new Error(
      detail?.detail ?? `${response.status} ${response.statusText}`.trim(),
    );
  }
  return (await response.json()) as T;
}

function ActionButton({
  label,
  busyLabel,
  successLabel,
  onRun,
  className,
  disabled,
}: {
  label: string;
  busyLabel?: string;
  successLabel?: string;
  onRun: () => Promise<void>;
  className?: string;
  disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  return (
    <div>
      <button
        type="button"
        disabled={busy || disabled}
        onClick={async () => {
          setBusy(true);
          setError(null);
          setDone(false);
          try {
            await onRun();
            setDone(true);
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
        className={
          className ??
          "rounded bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-neutral-700 disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900"
        }
      >
        {busy ? (busyLabel ?? "Working…") : label}
      </button>
      {error ? (
        <p className="mt-1 max-w-xs text-xs text-rose-600 dark:text-rose-400">{error}</p>
      ) : null}
      {done && !error && successLabel ? (
        <p className="mt-1 text-xs text-emerald-600 dark:text-emerald-400">{successLabel}</p>
      ) : null}
    </div>
  );
}

export function PrepareApplicationButton({ jobId, blocked }: { jobId: string; blocked: boolean }) {
  const router = useRouter();
  return (
    <ActionButton
      label="Prepare Application"
      busyLabel="Preparing…"
      successLabel="Prepared successfully"
      disabled={blocked}
      onRun={async () => {
        await postJson(`/api/jobs/${jobId}/prepare-application`);
        router.refresh();
        // Bring the freshly-built review list into view.
        document
          .getElementById("proposed-changes")
          ?.scrollIntoView({ behavior: "smooth", block: "start" });
      }}
    />
  );
}

export function RecommendationDecision({
  jobId,
  recommendationId,
  accepted,
}: {
  jobId: string;
  recommendationId: string;
  accepted: boolean | null;
}) {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  async function decide(value: boolean | null) {
    setPending(true);
    try {
      await postJson(`/api/jobs/${jobId}/recommendations/${recommendationId}/decision`, {
        accepted: value,
      });
      router.refresh();
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex gap-1.5">
      <button
        type="button"
        disabled={pending}
        onClick={() => decide(true)}
        className={`rounded px-2 py-0.5 text-xs font-medium transition disabled:opacity-50 ${
          accepted === true
            ? "bg-emerald-600 text-white"
            : "bg-emerald-100 text-emerald-800 hover:bg-emerald-200 dark:bg-emerald-950 dark:text-emerald-300"
        }`}
      >
        Accept
      </button>
      <button
        type="button"
        disabled={pending}
        onClick={() => decide(false)}
        className={`rounded px-2 py-0.5 text-xs font-medium transition disabled:opacity-50 ${
          accepted === false
            ? "bg-rose-600 text-white"
            : "bg-rose-100 text-rose-800 hover:bg-rose-200 dark:bg-rose-950 dark:text-rose-300"
        }`}
      >
        Reject
      </button>
      {accepted !== null ? (
        <button
          type="button"
          disabled={pending}
          onClick={() => decide(null)}
          className="rounded bg-neutral-100 px-2 py-0.5 text-xs text-neutral-600 transition hover:bg-neutral-200 disabled:opacity-50 dark:bg-neutral-800 dark:text-neutral-400"
        >
          Undo
        </button>
      ) : null}
    </div>
  );
}

export function GenerateResumeButton({ jobId, canGenerate }: { jobId: string; canGenerate: boolean }) {
  const router = useRouter();
  return (
    <ActionButton
      label="Generate Resume"
      busyLabel="Generating…"
      disabled={!canGenerate}
      onRun={async () => {
        await postJson(`/api/jobs/${jobId}/generate-resume`);
        router.refresh();
      }}
    />
  );
}

export function CrmTransitionControl({
  jobId,
  currentStatus,
  statuses,
}: {
  jobId: string;
  currentStatus: string;
  statuses: string[];
}) {
  const router = useRouter();
  const [target, setTarget] = useState(currentStatus);
  const [busy, setBusy] = useState(false);
  const [warnings, setWarnings] = useState<string[]>([]);

  return (
    <div className="flex flex-wrap items-center gap-2">
      <select
        value={target}
        onChange={(e) => setTarget(e.target.value)}
        className="rounded border border-neutral-300 bg-white px-2 py-1 text-sm dark:border-neutral-700 dark:bg-neutral-950"
      >
        {statuses.map((s) => (
          <option key={s} value={s}>
            {s.replaceAll("_", " ")}
          </option>
        ))}
      </select>
      <button
        type="button"
        disabled={busy || target === currentStatus}
        onClick={async () => {
          setBusy(true);
          setWarnings([]);
          try {
            const result = await postJson<{ warnings: string[] }>(`/api/jobs/${jobId}/status`, {
              status: target,
            });
            setWarnings(result.warnings ?? []);
            router.refresh();
          } finally {
            setBusy(false);
          }
        }}
        className="rounded bg-neutral-900 px-3 py-1 text-sm text-white transition hover:bg-neutral-700 disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900"
      >
        {busy ? "Moving…" : "Move"}
      </button>
      {warnings.map((w) => (
        <span key={w} className="text-xs text-amber-600 dark:text-amber-400">
          {w}
        </span>
      ))}
    </div>
  );
}

/**
 * Records an application the USER submitted — whether it was prepared here or
 * somewhere else entirely.
 *
 * The external-resume option matters more than it looks. A job imported from a
 * URL and applied to before Job Hunt OS ever saw it has no ResumeVersion, and
 * leaving the field blank would later read as "we do not know what was sent"
 * rather than "a resume from outside this system was sent". Those are different
 * facts, and the interview-prep and follow-up flows depend on which one is true.
 */
export function MarkAppliedForm({
  jobId,
  resumeVersions,
}: {
  jobId: string;
  resumeVersions: { resume_version_id: string; label: string }[];
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  if (done) {
    return <p className="text-sm text-emerald-700 dark:text-emerald-400">Marked as applied.</p>;
  }

  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        const form = new FormData(e.currentTarget);
        const resume = String(form.get("resume_version_id") || "");
        try {
          await postJson(`/api/jobs/${jobId}/already-applied`, {
            applied_at: String(form.get("applied_at") || ""),
            resume_version_id: resume === "EXTERNAL" ? null : resume || null,
            external_resume: resume === "EXTERNAL",
            notes: String(form.get("notes") || "") || null,
            referral_used: form.get("referral_used") === "on",
            confirmation_id: String(form.get("confirmation_id") || "") || null,
          });
          setDone(true);
          router.refresh();
        } catch (e2) {
          setError(e2 instanceof Error ? e2.message : String(e2));
        } finally {
          setBusy(false);
        }
      }}
      className="space-y-2 text-sm"
    >
      <label className="block">
        <span className="text-xs text-neutral-500">Application date</span>
        <input
          type="date"
          name="applied_at"
          required
          defaultValue={new Date().toISOString().slice(0, 10)}
          className="mt-0.5 block w-full rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        />
      </label>
      <label className="block">
        <span className="text-xs text-neutral-500">Resume version used</span>
        <select
          name="resume_version_id"
          className="mt-0.5 block w-full rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        >
          <option value="">— not recorded —</option>
          <option value="EXTERNAL">External resume / not generated in Job Hunt OS</option>
          {resumeVersions.map((v) => (
            <option key={v.resume_version_id} value={v.resume_version_id}>
              {v.label}
            </option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-1.5 text-xs text-neutral-500">
        <input type="checkbox" name="referral_used" />
        Applied with a referral
      </label>
      <label className="block">
        <span className="text-xs text-neutral-500">Confirmation ID (optional)</span>
        <input
          name="confirmation_id"
          className="mt-0.5 block w-full rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        />
      </label>
      <label className="block">
        <span className="text-xs text-neutral-500">Notes (optional)</span>
        <textarea
          name="notes"
          rows={2}
          className="mt-0.5 block w-full rounded border border-neutral-300 bg-white px-2 py-1 dark:border-neutral-700 dark:bg-neutral-950"
        />
      </label>
      <button
        type="submit"
        disabled={busy}
        className="rounded bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-neutral-700 disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900"
      >
        {busy ? "Recording…" : "Mark Applied"}
      </button>
      {error ? <p className="text-xs text-rose-600 dark:text-rose-400">{error}</p> : null}
    </form>
  );
}

/**
 * Re-render a base resume from the current Evidence Bank. Creates no job,
 * application or resume version - the library page just shows the new
 * "last generated" time. A base that cannot be fitted to one page comes back
 * as an error and nothing is written.
 */
export function RegenerateBaseButton({ baseName }: { baseName: string }) {
  const router = useRouter();
  return (
    <ActionButton
      label="Regenerate"
      busyLabel="Regenerating…"
      successLabel="Regenerated from the current Evidence Bank."
      className="rounded border border-neutral-300 px-3 py-1.5 text-sm font-medium transition hover:bg-neutral-100 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800"
      onRun={async () => {
        await postJson(`/api/resume-bases/${encodeURIComponent(baseName)}/generate`);
        router.refresh();
      }}
    />
  );
}
