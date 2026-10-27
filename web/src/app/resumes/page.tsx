import Link from "next/link";

import { RegenerateBaseButton } from "@/components/actions";
import { BackendDown, Badge, Card } from "@/components/ui";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

const LINK_BUTTON =
  "rounded border border-neutral-300 px-3 py-1.5 text-sm font-medium transition hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800";

function formatGenerated(iso: string | null | undefined): string {
  if (!iso) return "Not generated yet";
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/**
 * The Resume Library: every canonical base resume, on its own.
 *
 * A download here is the same document a job application starts from - same
 * builder, same Evidence-Bank gate, same one-page fitting - just with no
 * posting to tailor it to. Downloads regenerate from the current Evidence
 * Bank, so a file is never staler than the Bank behind it.
 */
export default async function ResumeLibraryPage() {
  let data;
  try {
    data = await api.resumeBases();
  } catch (error) {
    return <BackendDown error={error} />;
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Resume Library</h1>
        <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
          Your reusable base resumes. Job applications start from one of these and
          tailor it to the posting.
        </p>
        <p className="mt-1 text-xs text-neutral-500">
          Every base is one page, built only from Evidence-Bank claims, and regenerated
          from the current Evidence Bank each time you download it.
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {data.bases.map((base) => (
          <Card
            key={base.name}
            title={base.display_name || base.name}
            subtitle={base.description ?? undefined}
          >
            <div className="space-y-3 text-sm">
              {base.best_for.length > 0 ? (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-xs text-neutral-500">Best for:</span>
                  {base.best_for.map((label) => (
                    <Badge key={label} tone="info">
                      {label}
                    </Badge>
                  ))}
                </div>
              ) : null}

              <p className="text-xs text-neutral-500">
                Last generated:{" "}
                <span className="font-medium text-neutral-700 dark:text-neutral-300">
                  {formatGenerated(base.last_generated_at)}
                </span>
              </p>

              <div className="flex flex-wrap items-start gap-2">
                <Link href={`/resume-base/${base.name}`} className={LINK_BUTTON}>
                  Preview
                </Link>
                <a
                  href={api.resumeBaseDownloadPath(base.name, "pdf")}
                  download={base.pdf_filename}
                  className={LINK_BUTTON}
                >
                  Download PDF
                </a>
                <a
                  href={api.resumeBaseDownloadPath(base.name, "docx")}
                  download={base.docx_filename}
                  className={LINK_BUTTON}
                >
                  Download DOCX
                </a>
                <RegenerateBaseButton baseName={base.name} />
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
