import Link from "next/link";

import {
  AddContactForm,
  ContactLifecycleActions,
  OutreachComposer,
} from "@/components/people-actions";
import { BackendDown, Badge, Card, Empty, type Tone } from "@/components/ui";
import { api, type ContactDetailView } from "@/lib/api";

export const dynamic = "force-dynamic";

/**
 * The People CRM.
 *
 * This page was a read-only shell over five empty tables: there was no way to
 * create a contact, so nothing was ever recorded, so no referral ever happened.
 * Everything here now writes.
 *
 * What it deliberately does not do is send. Outreach is drafted, marked sent by
 * the user after they send it themselves, and followed up by reminder. Every
 * count comes from `/api/people/summary` already computed — this page decides
 * nothing about status, urgency or what to do next.
 */

const STATUS_LABELS: Record<string, string> = {
  IDENTIFIED: "Identified",
  OUTREACH_READY: "Draft ready",
  OUTREACH_SENT: "Contacted",
  REPLIED: "Replied",
  CONVERSATION: "In conversation",
  REFERRAL_REQUESTED: "Referral requested",
  REFERRED: "Referred",
  DECLINED: "Declined",
  NO_RESPONSE: "No response",
  CLOSED: "Closed",
};

const RELATIONSHIP_LABELS: Record<string, string> = {
  BU_ALUM: "BU alum",
  PROFESSOR: "Professor",
  FORMER_COLLEAGUE: "Former coworker",
  RECRUITER: "Recruiter",
  TEAM_MEMBER: "Employee",
  HIRING_MANAGER: "Hiring manager",
  FRIEND_CLASSMATE: "Friend / classmate",
  OTHER: "Other",
};

const REFERRAL_LABELS: Record<string, string> = {
  NONE: "None",
  POTENTIAL: "Considering",
  REQUESTED: "Requested",
  RECEIVED: "Referred",
  SUBMITTED_WITH_REFERRAL: "Applied with referral",
  DECLINED: "Declined",
  NO_RESPONSE: "No response",
};

function toneForStatus(status: string): Tone {
  switch (status) {
    case "REFERRED":
    case "REPLIED":
    case "CONVERSATION":
      return "good";
    case "OUTREACH_SENT":
    case "REFERRAL_REQUESTED":
      return "info";
    case "IDENTIFIED":
    case "OUTREACH_READY":
      return "warn";
    case "DECLINED":
    case "NO_RESPONSE":
      return "bad";
    default:
      return "neutral";
  }
}

function Kpi({ label, value, hint }: { label: string; value: number; hint?: string }) {
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-3 dark:border-neutral-800 dark:bg-neutral-900">
      <p className="text-xs uppercase tracking-wide text-neutral-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
      {hint ? <p className="mt-0.5 text-[11px] text-neutral-500">{hint}</p> : null}
    </div>
  );
}

function ContactRow({
  contact,
  followUpId,
}: {
  contact: ContactDetailView;
  followUpId?: string;
}) {
  return (
    <li className="space-y-2 py-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-medium">{contact.name}</span>
        {contact.role ? (
          <span className="text-xs text-neutral-500">{contact.role}</span>
        ) : null}
        {contact.company_name ? (
          <span className="text-xs text-neutral-500">· {contact.company_name}</span>
        ) : null}
        <Badge tone="neutral">
          {RELATIONSHIP_LABELS[contact.contact_type] ?? contact.contact_type}
        </Badge>
        <Badge tone={toneForStatus(contact.status)}>
          {STATUS_LABELS[contact.status] ?? contact.status}
        </Badge>
        {contact.referral_state !== "NONE" ? (
          <Badge tone="info">
            Referral: {REFERRAL_LABELS[contact.referral_state] ?? contact.referral_state}
          </Badge>
        ) : null}
        {contact.follow_up_overdue ? <Badge tone="bad">Follow-up overdue</Badge> : null}
      </div>

      <p className="text-xs text-neutral-600 dark:text-neutral-400">
        <span className="font-medium">Next:</span> {contact.next_action} —{" "}
        {contact.next_action_detail}
      </p>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-neutral-500">
        <span>Last touch: {contact.last_contact_at?.slice(0, 10) ?? "never"}</span>
        <span>Next follow-up: {contact.next_follow_up ?? "none set"}</span>
        {contact.linked_jobs.length > 0 ? (
          <span className="flex flex-wrap items-center gap-1">
            Linked:
            {contact.linked_jobs.map((job) => {
              const row = job as { job_id: string; title: string; purpose: string };
              return (
                <Link
                  key={row.job_id}
                  href={`/jobs/${row.job_id}?tab=networking`}
                  className="underline"
                >
                  {row.title}
                </Link>
              );
            })}
          </span>
        ) : (
          <span>Not linked to a job</span>
        )}
        {contact.profile_url ? (
          <a
            href={contact.profile_url}
            target="_blank"
            rel="noopener noreferrer"
            className="underline"
          >
            Profile
          </a>
        ) : null}
      </div>

      <details>
        <summary className="cursor-pointer text-xs text-neutral-500">Outreach</summary>
        <div className="mt-2">
          <OutreachComposer
            contactId={contact.contact_id}
            contactName={contact.name}
            jobId={
              contact.linked_jobs.length > 0
                ? (contact.linked_jobs[0] as { job_id: string }).job_id
                : undefined
            }
          />
          <div className="mt-2">
            <ContactLifecycleActions
              contactId={contact.contact_id}
              followUpId={followUpId}
            />
          </div>
        </div>
      </details>
    </li>
  );
}

export default async function PeoplePage({
  searchParams,
}: {
  searchParams: Promise<{ company?: string; status?: string; relationship?: string; due?: string }>;
}) {
  const filters = await searchParams;

  let summary;
  try {
    summary = await api.peopleSummary();
  } catch (error) {
    return <BackendDown error={error} />;
  }

  const followUpByContact = new Map<string, string>();
  for (const followUp of summary.follow_ups_due) {
    if (followUp.contact_id) followUpByContact.set(followUp.contact_id, followUp.id);
  }

  let all = summary.contacts;
  if (filters.company) all = all.filter((c) => c.company_name === filters.company);
  if (filters.status) all = all.filter((c) => c.status === filters.status);
  if (filters.relationship)
    all = all.filter((c) => c.contact_type === filters.relationship);
  if (filters.due === "1") all = all.filter((c) => c.follow_up_overdue);

  const filtered = Boolean(
    filters.company || filters.status || filters.relationship || filters.due,
  );

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">People</h1>
          <p className="text-xs text-neutral-500">
            This system drafts, tracks and reminds. It never sends a message, a connection
            request or an email — you do that yourself.
          </p>
        </div>
        <AddContactForm />
      </div>

      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
        <Kpi label="Contacts" value={summary.kpis.contacts ?? 0} />
        <Kpi
          label="Need outreach"
          value={summary.kpis.need_outreach ?? 0}
          hint="Recorded, not yet contacted"
        />
        <Kpi
          label="Awaiting reply"
          value={summary.kpis.awaiting_reply ?? 0}
          hint="Sent, no answer recorded"
        />
        <Kpi
          label="Follow-ups due"
          value={summary.kpis.follow_ups_due ?? 0}
          hint={`${summary.kpis.follow_ups_next_7_days ?? 0} in the next 7 days`}
        />
        <Kpi label="Referrals" value={summary.kpis.referrals ?? 0} hint="Live asks" />
      </div>

      {/* Filters ------------------------------------------------------- */}
      {summary.contacts.length > 0 ? (
        <Card title="Filters" subtitle="Narrow the list below">
          <div className="flex flex-wrap gap-2 text-xs">
            {filtered ? (
              <Link href="/people" className="rounded bg-neutral-900 px-2 py-1 text-white dark:bg-neutral-100 dark:text-neutral-900">
                Clear filters
              </Link>
            ) : null}
            <Link
              href="/people?due=1"
              className="rounded border border-neutral-300 px-2 py-1 dark:border-neutral-700"
            >
              Follow-up due
            </Link>
            {summary.filters.companies.map((company) => (
              <Link
                key={company}
                href={`/people?company=${encodeURIComponent(company)}`}
                className="rounded border border-neutral-300 px-2 py-1 dark:border-neutral-700"
              >
                {company}
              </Link>
            ))}
            {summary.filters.statuses.map((status) => (
              <Link
                key={status}
                href={`/people?status=${encodeURIComponent(status)}`}
                className="rounded border border-neutral-300 px-2 py-1 dark:border-neutral-700"
              >
                {STATUS_LABELS[status] ?? status}
              </Link>
            ))}
            {summary.filters.relationships.map((relationship) => (
              <Link
                key={relationship}
                href={`/people?relationship=${encodeURIComponent(relationship)}`}
                className="rounded border border-neutral-300 px-2 py-1 dark:border-neutral-700"
              >
                {RELATIONSHIP_LABELS[relationship] ?? relationship}
              </Link>
            ))}
          </div>
        </Card>
      ) : null}

      {/* Action required ----------------------------------------------- */}
      <Card
        title="Action required"
        subtitle="Waiting on you, not on them"
      >
        {summary.action_required.length === 0 ? (
          <Empty>
            {summary.contacts.length === 0
              ? "No contacts yet. Open a job you care about and use Find People to Contact."
              : "Nothing is waiting on you."}
          </Empty>
        ) : (
          <ul className="divide-y divide-neutral-100 dark:divide-neutral-800">
            {summary.action_required.map((contact) => (
              <ContactRow
                key={contact.contact_id}
                contact={contact}
                followUpId={followUpByContact.get(contact.contact_id)}
              />
            ))}
          </ul>
        )}
      </Card>

      {/* Follow-ups ----------------------------------------------------- */}
      <Card title="Follow-ups due" subtitle="Overdue and the next 7 days">
        {summary.follow_ups_due.length === 0 ? (
          <Empty>Nothing due.</Empty>
        ) : (
          <ul className="divide-y divide-neutral-100 text-sm dark:divide-neutral-800">
            {summary.follow_ups_due.map((followUp) => (
              <li key={followUp.id} className="flex flex-wrap items-baseline gap-2 py-2">
                <span className="flex-1">{followUp.subject}</span>
                <Badge tone={followUp.overdue ? "bad" : "neutral"}>{followUp.due_date}</Badge>
                {followUp.job_id ? (
                  <Link
                    href={`/jobs/${followUp.job_id}?tab=networking`}
                    className="text-xs underline"
                  >
                    Open job
                  </Link>
                ) : null}
                {followUp.contact_id ? (
                  <ContactLifecycleActions
                    contactId={followUp.contact_id}
                    followUpId={followUp.id}
                  />
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {/* Recent --------------------------------------------------------- */}
      {summary.recent.length > 0 ? (
        <Card title="Recent contacts" subtitle="Most recently touched">
          <ul className="divide-y divide-neutral-100 text-sm dark:divide-neutral-800">
            {summary.recent.map((contact) => (
              <li key={contact.contact_id} className="flex flex-wrap items-baseline gap-2 py-2">
                <span className="font-medium">{contact.name}</span>
                <span className="text-xs text-neutral-500">{contact.company_name ?? "—"}</span>
                <Badge tone={toneForStatus(contact.status)}>
                  {STATUS_LABELS[contact.status] ?? contact.status}
                </Badge>
                <span className="ml-auto text-xs text-neutral-500">
                  {contact.last_contact_at?.slice(0, 10)}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      {/* All contacts --------------------------------------------------- */}
      <Card
        title="All contacts"
        subtitle={
          filtered ? `${all.length} of ${summary.contacts.length} shown` : `${all.length} recorded`
        }
      >
        {all.length === 0 ? (
          <Empty>
            {summary.contacts.length === 0
              ? "Nobody recorded yet."
              : "No contact matches these filters."}
          </Empty>
        ) : (
          <ul className="divide-y divide-neutral-100 dark:divide-neutral-800">
            {all.map((contact) => (
              <ContactRow
                key={contact.contact_id}
                contact={contact}
                followUpId={followUpByContact.get(contact.contact_id)}
              />
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
