import Link from "next/link";
import { notFound } from "next/navigation";

import { RegenerateBaseButton } from "@/components/actions";
import { BackendDown, Badge, Card } from "@/components/ui";
import { api, ApiError } from "@/lib/api";

export const dynamic = "force-dynamic";

const LINK_BUTTON =
  "rounded border border-neutral-300 px-3 py-1.5 text-sm font-medium transition hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800";

/**
 * The complete base resume for a role family, before any job tailoring.
 *
 * Exists so the review screen's "7 proposed changes" is legible as edits to a
 * real document rather than as the document itself - and, since the Resume
 * Library, so a base can be read and downloaded on its own. This is the
 * library document exactly: fitted to one page, the same content the PDF and
 * DOCX downloads hold.
 */
export default async function ResumeBasePage({
  params,
}: {
  params: Promise<{ baseName: string }>;
}) {
  const { baseName } = await params;

  let base;
  try {
    base = await api.resumeBasePreview(baseName);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    if (error instanceof ApiError && error.status === 422) {
      return (
        <Card title="Could not generate this base resume" subtitle={baseName}>
          <p className="text-sm text-rose-700 dark:text-rose-400">{error.detail}</p>
        </Card>
      );
    }
    return <BackendDown error={error} />;
  }

  const experiences = base.entries.filter((e) => e.kind === "EXPERIENCE");
  const projects = base.entries.filter((e) => e.kind !== "EXPERIENCE");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs text-neutral-500">
            <Link href="/resumes" className="underline hover:text-neutral-700 dark:hover:text-neutral-300">
              Resume Library
            </Link>{" "}
            / base resume
          </p>
          <h1 className="mt-1 text-lg font-semibold tracking-tight">
            {base.display_name || base.base_name}
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            {base.best_for.map((label) => (
              <Badge key={label} tone="info">
                {label}
              </Badge>
            ))}
            <span className="text-xs text-neutral-500">
              {base.page_count} page · {base.entries.length} entries · {base.total_bullets}{" "}
              bullets
            </span>
          </div>
        </div>
        <div className="flex flex-wrap items-start gap-2">
          <a
            href={api.resumeBaseDownloadPath(base.base_name, "pdf")}
            download={base.pdf_filename}
            className={LINK_BUTTON}
          >
            Download PDF
          </a>
          <a
            href={api.resumeBaseDownloadPath(base.base_name, "docx")}
            download={base.docx_filename}
            className={LINK_BUTTON}
          >
            Download DOCX
          </a>
          <RegenerateBaseButton baseName={base.base_name} />
        </div>
      </div>

      <p className="text-xs text-neutral-500">
        This is what job tailoring starts from. Every line is Evidence-Bank text; a job
        application re-ranks and patches it against the posting.
      </p>

      <Card title={base.name} subtitle={`${base.location} | ${base.contact_line}`}>
        <div className="space-y-4 text-sm">
          {base.summary ? (
            <section>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-500">
                Professional Summary
              </h3>
              <p className="mt-1">{base.summary}</p>
            </section>
          ) : null}

          <section>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-500">
              Education
            </h3>
            {base.education.length > 0
              ? base.education.map((school) => (
                  <div key={school.institution} className="mt-1">
                    <p>
                      <span className="font-medium">{school.institution}</span>
                      {school.dates ? (
                        <span className="text-neutral-500"> · {school.dates}</span>
                      ) : null}
                    </p>
                    <p className="text-xs text-neutral-600 dark:text-neutral-400">
                      {[school.degree, school.location].filter(Boolean).join(", ")}
                    </p>
                    {school.coursework ? (
                      <p className="text-xs italic text-neutral-500">
                        Relevant Coursework: {school.coursework}
                      </p>
                    ) : null}
                  </div>
                ))
              : base.education_lines.map((line) => (
                  <p key={line} className="mt-1">
                    {line}
                  </p>
                ))}
          </section>

          {(
            [
              ["Experience", experiences],
              ["Projects", projects],
            ] as const
          ).map(([label, group]) =>
            group.length > 0 ? (
              <section key={label}>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-500">
                  {label}
                </h3>
                {group.map((entry) => (
                  <div key={entry.heading} className="mt-2">
                    <p className="font-medium">{entry.heading}</p>
                    {entry.subheading ? (
                      <p className="text-xs text-neutral-500">{entry.subheading}</p>
                    ) : null}
                    <ul className="mt-1 list-disc space-y-1 pl-5">
                      {entry.bullets.map((bullet) => (
                        <li key={bullet}>{bullet}</li>
                      ))}
                    </ul>
                  </div>
                ))}
              </section>
            ) : null,
          )}

          <section>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-500">
              Skills
            </h3>
            {base.skill_lines.map((line) => (
              <p key={line} className="mt-1 text-xs">
                {line}
              </p>
            ))}
          </section>
        </div>
      </Card>
    </div>
  );
}
