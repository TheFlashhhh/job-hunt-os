"use client";

/**
 * The cover-letter workspace inside a job's Application tab.
 *
 * The boundary is the same one the rest of the system keeps: this produces a
 * document and hands it to the user. Nothing is uploaded to an employer and
 * nothing is submitted — the "Download" buttons are the end of the road here,
 * and the employer's own form is still theirs to fill in.
 *
 * Two things this UI is careful about:
 *
 * **A saved edit is the user's work.** Regenerate adds a version; it never
 * overwrites wording somebody typed. The backend refuses the destructive path
 * outright, and this component surfaces that refusal rather than hiding it.
 *
 * **Evidence IDs stay out of the letter.** They live in the collapsed "Why
 * these examples?" panel, which is internal. The textarea, the copied text and
 * both documents carry none.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { browserApiPath } from "@/lib/api";

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
    throw new Error(detail?.detail ?? `${response.status} ${response.statusText}`.trim());
  }
  return (await response.json()) as T;
}

const PRIMARY =
  "rounded bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-neutral-700 disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900";
const SECONDARY =
  "rounded border border-neutral-300 px-2.5 py-1 text-xs transition hover:bg-neutral-50 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800";

type Version = {
  cover_letter_version_id: string;
  version: number;
  status: string;
  user_edited: boolean;
  salutation?: string | null;
  content: string;
  word_count: number;
  pdf_available: boolean;
  docx_available: boolean;
};

type Story = { example: string; why: string; evidence_ids: string[] };

type GenerateResponse = {
  version: Version;
  why_these_examples: Story[];
  omissions: string[];
};

export function CoverLetterPanel({
  jobId,
  requirementLabel,
  requirementReason,
  requirementEvidence,
  initialVersion,
  versionCount,
  externalRecorded,
  downloadBase,
}: {
  jobId: string;
  requirementLabel: string;
  requirementReason: string;
  requirementEvidence: string;
  initialVersion: Version | null;
  versionCount: number;
  externalRecorded: boolean;
  downloadBase: string;
}) {
  const router = useRouter();
  const [version, setVersion] = useState<Version | null>(initialVersion);
  const [content, setContent] = useState(initialVersion?.content ?? "");
  const [stories, setStories] = useState<Story[]>([]);
  const [omissions, setOmissions] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const dirty = Boolean(version) && content !== version?.content;

  async function generate() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await postJson<GenerateResponse>(
        `/api/jobs/${jobId}/cover-letter/generate`,
        {},
      );
      setVersion(result.version);
      setContent(result.version.content);
      setStories(result.why_these_examples);
      setOmissions(result.omissions);
      setNotice(`Draft ${result.version.version} generated.`);
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!version) return;
    setBusy(true);
    setError(null);
    try {
      const saved = await postJson<Version>(
        `/api/cover-letters/${version.cover_letter_version_id}/save`,
        { content },
      );
      setVersion(saved);
      setNotice("Saved. This wording is what the PDF and DOCX now contain.");
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function recordExternal() {
    setBusy(true);
    setError(null);
    try {
      await postJson(`/api/jobs/${jobId}/cover-letter/external`, {});
      setNotice("Recorded that you used a letter written elsewhere.");
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-sm font-medium">Cover letter</span>
        <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-[11px] font-medium dark:bg-neutral-800">
          {requirementLabel}
        </span>
        {version ? (
          <span className="text-xs text-neutral-500">
            Version {version.version} · {version.word_count} words ·{" "}
            {version.user_edited ? "your wording" : "generated draft"}
          </span>
        ) : externalRecorded ? (
          <span className="text-xs text-neutral-500">Written outside Job Hunt OS</span>
        ) : (
          <span className="text-xs text-neutral-500">Not generated</span>
        )}
      </div>

      <p className="text-xs text-neutral-500">{requirementReason}</p>
      {requirementEvidence ? (
        <blockquote className="border-l-2 border-neutral-300 pl-2 text-xs text-neutral-600 dark:border-neutral-700 dark:text-neutral-400">
          &ldquo;{requirementEvidence}&rdquo;
        </blockquote>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className={PRIMARY} disabled={busy} onClick={generate}>
          {busy ? "Working…" : version ? "Regenerate" : "Generate Cover Letter"}
        </button>
        {!version && !externalRecorded ? (
          <button type="button" className={SECONDARY} disabled={busy} onClick={recordExternal}>
            I wrote one elsewhere
          </button>
        ) : null}
        {versionCount > 1 ? (
          <span className="text-xs text-neutral-500">
            {versionCount} versions kept — regenerating never overwrites a saved edit.
          </span>
        ) : null}
      </div>

      {error ? <p className="text-xs text-rose-600 dark:text-rose-400">{error}</p> : null}
      {notice ? (
        <p className="text-xs text-emerald-600 dark:text-emerald-400">{notice}</p>
      ) : null}

      {version ? (
        <div className="space-y-2 rounded border border-neutral-200 p-3 dark:border-neutral-800">
          {version.salutation ? (
            <p className="text-xs text-neutral-500">{version.salutation}</p>
          ) : null}
          <textarea
            className="h-72 w-full rounded border border-neutral-300 bg-white p-2 text-xs leading-relaxed dark:border-neutral-700 dark:bg-neutral-900"
            value={content}
            onChange={(e) => setContent(e.target.value)}
          />
          <p className="text-[11px] text-neutral-500">
            {content.split(/\s+/).filter(Boolean).length} words
            {dirty ? " · unsaved changes" : ""}
          </p>

          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className={PRIMARY} disabled={busy || !dirty} onClick={save}>
              Save Changes
            </button>
            <button
              type="button"
              className={SECONDARY}
              onClick={async () => {
                await navigator.clipboard.writeText(content);
                setCopied(true);
                setTimeout(() => setCopied(false), 2000);
              }}
            >
              {copied ? "Copied" : "Copy Text"}
            </button>
            {version.pdf_available ? (
              <a
                className={SECONDARY}
                href={`${downloadBase}/api/cover-letters/${version.cover_letter_version_id}/download.pdf`}
              >
                Download PDF
              </a>
            ) : null}
            {version.docx_available ? (
              <a
                className={SECONDARY}
                href={`${downloadBase}/api/cover-letters/${version.cover_letter_version_id}/download.docx`}
              >
                Download DOCX
              </a>
            ) : null}
          </div>

          {dirty ? (
            <p className="text-[11px] text-amber-700 dark:text-amber-400">
              The downloads still contain the last saved version. Save to update them.
            </p>
          ) : null}

          {omissions.length > 0 ? (
            <details>
              <summary className="cursor-pointer text-xs text-neutral-500">
                What this letter deliberately does not say
              </summary>
              <ul className="mt-1 space-y-1 text-[11px] text-neutral-500">
                {omissions.map((note) => (
                  <li key={note}>· {note}</li>
                ))}
              </ul>
            </details>
          ) : null}

          {stories.length > 0 ? (
            <details>
              <summary className="cursor-pointer text-xs text-neutral-500">
                Why these examples?
              </summary>
              <ul className="mt-1 space-y-1 text-[11px] text-neutral-500">
                {stories.map((story) => (
                  <li key={story.example}>
                    <span className="font-medium">{story.example}</span> — {story.why}
                    <span className="ml-1 font-mono opacity-60">
                      {story.evidence_ids.join(", ")}
                    </span>
                  </li>
                ))}
              </ul>
              <p className="mt-1 text-[11px] text-neutral-500">
                Evidence IDs are shown here only. They never appear in the letter, the
                PDF, the DOCX or the copied text.
              </p>
            </details>
          ) : null}
        </div>
      ) : null}

      <p className="text-[11px] text-neutral-500">
        Generated here, submitted by you. Job Hunt OS never uploads anything to an
        employer.
      </p>
    </div>
  );
}
