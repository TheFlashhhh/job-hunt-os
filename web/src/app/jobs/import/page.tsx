import Link from "next/link";

import { ImportJobForm } from "@/components/import-job";
import { Card } from "@/components/ui";

export const metadata = { title: "Add a job" };

export default function ImportJobPage() {
  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <div>
        <Link href="/jobs" className="text-xs text-neutral-500 hover:underline">
          ← All jobs
        </Link>
        <h1 className="mt-1 text-xl font-semibold tracking-tight">Add a job</h1>
        <p className="text-sm text-neutral-500">
          Paste a listing you found anywhere — LinkedIn, a careers page, a friend, a
          professor. Once imported it goes through the same screening, eligibility,
          evidence matching and CRM as anything the system discovered itself.
        </p>
      </div>

      <Card title="Import from a URL" subtitle="Fetched server-side; nothing is submitted anywhere">
        <ImportJobForm />
      </Card>

      <p className="text-xs text-neutral-500">
        Imported jobs are recorded as <strong>added by you</strong>, never as
        automated discovery. Applications are always submitted by you on the
        employer’s own site — this app prepares and tracks, it never applies.
      </p>
    </div>
  );
}
