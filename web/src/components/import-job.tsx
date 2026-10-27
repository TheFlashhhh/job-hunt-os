"use client";

/**
 * Add Job — paste a listing URL you found somewhere else.
 *
 * The flow is deliberately three-stage rather than one submit button:
 *
 *   1. URL in.
 *   2. If the fetch or extraction fails, offer the two things that actually
 *      recover it — paste the description, or save the link on its own. "Import
 *      failed" with no way forward is the single worst outcome here, because
 *      the user already has the posting open in another tab.
 *   3. Show what was actually read, THEN ask what they intend to do with it.
 *      Asking first would make the user commit before seeing whether the
 *      company and title came back right.
 *
 * The import lands the job at DISCOVERED. Moving to REVIEW is a separate call
 * through the normal transition endpoint, and "already applied" is a separate
 * form on the job itself — nothing here marks anything as submitted.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { browserApiPath, type ImportJobResponse } from "@/lib/api";

type Stage = "url" | "paste" | "save-only" | "done";

async function postJson<T>(path: string, body: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(browserApiPath(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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

const inputClass =
  "mt-1 block w-full rounded border border-neutral-300 bg-white px-2.5 py-1.5 text-sm " +
  "dark:border-neutral-700 dark:bg-neutral-950";

const primaryClass =
  "rounded bg-neutral-900 px-3.5 py-2 text-sm font-medium text-white transition " +
  "hover:bg-neutral-700 disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900";

const secondaryClass =
  "rounded border border-neutral-300 px-3 py-1.5 text-sm transition hover:bg-neutral-50 " +
  "disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800";

function Field({
  label,
  name,
  value,
  onChange,
  placeholder,
  required,
}: {
  label: string;
  name: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  required?: boolean;
}) {
  return (
    <label className="block">
      <span className="text-xs text-neutral-500">{label}</span>
      <input
        name={name}
        value={value}
        required={required}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={inputClass}
      />
    </label>
  );
}

export function ImportJobForm() {
  const router = useRouter();
  const [stage, setStage] = useState<Stage>("url");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [canPaste, setCanPaste] = useState(false);
  const [result, setResult] = useState<ImportJobResponse | null>(null);

  const [url, setUrl] = useState("");
  const [company, setCompany] = useState("");
  const [title, setTitle] = useState("");
  const [location, setLocation] = useState("");
  const [description, setDescription] = useState("");

  async function run(body: Record<string, unknown>) {
    setBusy(true);
    setError(null);
    try {
      const response = await postJson<ImportJobResponse>("/api/jobs/import-url", {
        // The import itself never advances state. Intent is applied after the
        // user has seen what was actually read.
        intent: "SAVE",
        ...body,
      });
      if (!response.ok) {
        setError(response.error ?? "That listing could not be imported.");
        setCanPaste(Boolean(response.can_paste_description));
        return;
      }
      setResult(response);
      setStage("done");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setCanPaste(true);
    } finally {
      setBusy(false);
    }
  }

  if (stage === "done" && result?.job_id) {
    return <Imported result={result} router={router} />;
  }

  return (
    <div className="space-y-4">
      {stage === "url" ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run({ url });
          }}
          className="space-y-3"
        >
          <Field
            label="Job listing URL"
            name="url"
            value={url}
            onChange={setUrl}
            required
            placeholder="https://boards.greenhouse.io/company/jobs/1234567"
          />
          <p className="text-xs text-neutral-500">
            Greenhouse, Lever and SmartRecruiters links are read from those systems’
            own APIs. Other sites are read from their <code>schema.org</code> JobPosting
            data, then from page metadata.
          </p>
          <button type="submit" disabled={busy || !url.trim()} className={primaryClass}>
            {busy ? "Importing…" : "Import Job"}
          </button>
        </form>
      ) : null}

      {stage === "paste" ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run({ url, description, company_name: company, title, location });
          }}
          className="space-y-3"
        >
          <Field label="Job listing URL (optional)" name="url" value={url} onChange={setUrl} />
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Company" name="company" value={company} onChange={setCompany} />
            <Field label="Job title" name="title" value={title} onChange={setTitle} />
          </div>
          <Field label="Location (optional)" name="location" value={location} onChange={setLocation} />
          <label className="block">
            <span className="text-xs text-neutral-500">
              Job description — paste the whole posting, headings and bullets included
            </span>
            <textarea
              rows={12}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className={`${inputClass} font-mono text-xs`}
              placeholder={"About the Role\n…\n\nQualifications\n- …"}
            />
          </label>
          <p className="text-xs text-neutral-500">
            Keep the section headings. The requirement parser recognises a
            qualifications block by its heading, so a flattened paste yields far
            fewer matched requirements.
          </p>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={busy || description.trim().length < 40}
              className={primaryClass}
            >
              {busy ? "Importing…" : "Import from pasted description"}
            </button>
            <button type="button" onClick={() => setStage("url")} className={secondaryClass}>
              Back
            </button>
          </div>
        </form>
      ) : null}

      {stage === "save-only" ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run({ url, save_url_only: true, company_name: company, title, location });
          }}
          className="space-y-3"
        >
          <Field label="Job listing URL" name="url" value={url} onChange={setUrl} required />
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Company" name="company" value={company} onChange={setCompany} required />
            <Field label="Job title" name="title" value={title} onChange={setTitle} required />
          </div>
          <p className="text-xs text-neutral-500">
            Only the link and what you type here is stored. No description, no
            requirements and no fit score — the record is marked to verify, and
            nothing about the role is guessed.
          </p>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={busy || !url.trim() || !company.trim() || !title.trim()}
              className={primaryClass}
            >
              {busy ? "Saving…" : "Save URL only"}
            </button>
            <button type="button" onClick={() => setStage("url")} className={secondaryClass}>
              Back
            </button>
          </div>
        </form>
      ) : null}

      {error ? (
        <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-900 dark:bg-amber-950">
          <p className="font-medium text-amber-900 dark:text-amber-200">
            We couldn’t read this listing automatically.
          </p>
          <p className="mt-1 text-xs text-amber-800 dark:text-amber-300">{error}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {canPaste ? (
              <button
                type="button"
                onClick={() => {
                  setError(null);
                  setStage("paste");
                }}
                className={primaryClass}
              >
                Paste Job Description
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => {
                setError(null);
                setStage("save-only");
              }}
              className={secondaryClass}
            >
              Save URL Only
            </button>
            <button
              type="button"
              onClick={() => {
                setError(null);
                setStage("url");
              }}
              className={secondaryClass}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function Imported({
  result,
  router,
}: {
  result: ImportJobResponse;
  router: ReturnType<typeof useRouter>;
}) {
  const [busy, setBusy] = useState(false);
  const jobId = result.job_id as string;

  async function choose(intent: "SAVE" | "REVIEW" | "APPLIED") {
    setBusy(true);
    try {
      if (intent === "REVIEW") {
        await postJson(`/api/jobs/${jobId}/status`, {
          status: "REVIEW",
          note: "Imported from a URL you pasted; moved to review.",
        });
      }
      // "Already applied" is NOT recorded here. It needs a date and a resume,
      // which the Application tab collects — a one-click "yes I applied" would
      // write an application record with no evidence of when or with what.
      router.push(
        intent === "APPLIED" ? `/jobs/${jobId}?tab=application` : `/jobs/${jobId}`,
      );
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      {result.duplicate ? (
        <div className="rounded-lg border border-sky-300 bg-sky-50 p-3 text-sm dark:border-sky-900 dark:bg-sky-950">
          <p className="font-medium">This job is already in Job Hunt OS.</p>
          <p className="mt-1 text-xs text-neutral-600 dark:text-neutral-400">
            Your link was added to it as another source rather than creating a
            second copy.
          </p>
          <a
            href={`/jobs/${jobId}`}
            className="mt-2 inline-block rounded bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white dark:bg-neutral-100 dark:text-neutral-900"
          >
            Open Existing Job
          </a>
        </div>
      ) : (
        <div className="rounded-lg border border-emerald-300 bg-emerald-50 p-3 dark:border-emerald-900 dark:bg-emerald-950">
          <p className="text-sm font-medium text-emerald-900 dark:text-emerald-200">
            Imported.
          </p>
          <dl className="mt-2 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
            <Row label="Company">{result.company}</Row>
            <Row label="Title">{result.title}</Row>
            <Row label="Location">{result.location ?? "not stated"}</Row>
            <Row label="Read from">{result.provider ?? "unknown"}</Row>
            <Row label="Description">
              {result.description_chars.toLocaleString()} characters
            </Row>
            <Row label="State">{result.application_status ?? "DISCOVERED"}</Row>
          </dl>
        </div>
      )}

      {result.messages?.length ? (
        <ul className="list-disc space-y-1 pl-4 text-xs text-neutral-500">
          {result.messages.map((m, i) => (
            <li key={i}>{m}</li>
          ))}
        </ul>
      ) : null}

      <div className="rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
        <p className="text-sm font-medium">What are you doing with this job?</p>
        <div className="mt-3 grid gap-2 sm:grid-cols-3">
          <Choice
            disabled={busy}
            onClick={() => choose("SAVE")}
            title="Just saving it"
            detail="Stays at DISCOVERED. Next step: review fit."
          />
          <Choice
            disabled={busy}
            onClick={() => choose("REVIEW")}
            title="I want to review / apply"
            detail="Moves to REVIEW and opens fit, evidence and the resume."
          />
          <Choice
            disabled={busy}
            onClick={() => choose("APPLIED")}
            title="I already applied"
            detail="Opens the Application tab to record the date and resume."
          />
        </div>
      </div>
    </div>
  );
}

function Choice({
  title,
  detail,
  onClick,
  disabled,
}: {
  title: string;
  detail: string;
  onClick: () => void;
  disabled: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="rounded border border-neutral-300 p-3 text-left transition hover:border-neutral-900 disabled:opacity-50 dark:border-neutral-700 dark:hover:border-neutral-100"
    >
      <span className="block text-sm font-medium">{title}</span>
      <span className="mt-1 block text-xs text-neutral-500">{detail}</span>
    </button>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="w-28 shrink-0 text-xs text-neutral-500">{label}</dt>
      <dd className="text-sm">{children}</dd>
    </div>
  );
}
