"use client";

/**
 * Client-side islands for the People CRM.
 *
 * The boundary these enforce is the one the whole networking design rests on:
 * **the system drafts, tracks and reminds; the human sends.** So "Draft
 * Outreach" produces editable text and a Copy button, "Mark Sent" is something
 * the user tells the system *after* they sent it themselves, and there is no
 * control anywhere that transmits a message.
 *
 * Everything posts through the same-origin Next rewrite and then calls
 * `router.refresh()`, so the server component re-reads state from the backend
 * rather than this component keeping its own copy.
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

const INPUT =
  "w-full rounded border border-neutral-300 bg-white px-2 py-1 text-sm dark:border-neutral-700 dark:bg-neutral-900";
const PRIMARY =
  "rounded bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-neutral-700 disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900";
const SECONDARY =
  "rounded border border-neutral-300 px-2.5 py-1 text-xs transition hover:bg-neutral-50 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800";

/** The user's own vocabulary for these, mapped onto the stored enum values. */
const RELATIONSHIPS: [string, string][] = [
  ["BU_ALUM", "BU alum"],
  ["PROFESSOR", "Professor"],
  ["FORMER_COLLEAGUE", "Former coworker"],
  ["RECRUITER", "Recruiter"],
  ["TEAM_MEMBER", "Employee"],
  ["HIRING_MANAGER", "Hiring manager"],
  ["FRIEND_CLASSMATE", "Friend / classmate"],
  ["OTHER", "Other"],
];

const LINK_PURPOSES: [string, string][] = [
  ["REFERRAL", "Referral"],
  ["INFORMATIONAL", "Informational chat"],
  ["RECRUITER", "Recruiter"],
  ["HIRING_TEAM", "Hiring team"],
  ["OTHER", "Other"],
];

function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-neutral-600 dark:text-neutral-400">{label}</span>
      <div className="mt-0.5">{children}</div>
      {hint ? <span className="mt-0.5 block text-[11px] text-neutral-500">{hint}</span> : null}
    </label>
  );
}

// ---------------------------------------------------------------------------
// Add a contact
// ---------------------------------------------------------------------------

export function AddContactForm({
  jobId,
  defaultCompany,
  compact,
}: {
  jobId?: string;
  defaultCompany?: string | null;
  compact?: boolean;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    name: "",
    role: "",
    company_name: defaultCompany ?? "",
    profile_url: "",
    email: "",
    contact_type: "OTHER",
    shared_context: "",
    notes: "",
  });
  const [purpose, setPurpose] = useState("INFORMATIONAL");

  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)} className={compact ? SECONDARY : PRIMARY}>
        + Add Contact
      </button>
    );
  }

  return (
    <form
      className="space-y-2 rounded border border-neutral-200 p-3 dark:border-neutral-800"
      onSubmit={async (event) => {
        event.preventDefault();
        setBusy(true);
        setError(null);
        try {
          const created = await postJson<{ contact_id: string }>("/api/contacts", {
            ...form,
            role: form.role || null,
            company_name: form.company_name || null,
            profile_url: form.profile_url || null,
            email: form.email || null,
            shared_context: form.shared_context || null,
            notes: form.notes || null,
          });
          if (jobId) {
            await postJson(`/api/jobs/${jobId}/contacts/link`, {
              contact_id: created.contact_id,
              purpose,
            });
          }
          setOpen(false);
          setForm({ ...form, name: "", role: "", profile_url: "", email: "", shared_context: "" });
          router.refresh();
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e));
        } finally {
          setBusy(false);
        }
      }}
    >
      <div className="grid gap-2 sm:grid-cols-2">
        <Field label="Full name *">
          <input
            required
            className={INPUT}
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </Field>
        <Field label="Title">
          <input
            className={INPUT}
            value={form.role}
            onChange={(e) => setForm({ ...form, role: e.target.value })}
          />
        </Field>
        <Field label="Company">
          <input
            className={INPUT}
            value={form.company_name}
            onChange={(e) => setForm({ ...form, company_name: e.target.value })}
          />
        </Field>
        <Field label="Relationship">
          <select
            className={INPUT}
            value={form.contact_type}
            onChange={(e) => setForm({ ...form, contact_type: e.target.value })}
          >
            {RELATIONSHIPS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Public profile URL">
          <input
            className={INPUT}
            placeholder="linkedin.com/in/…"
            value={form.profile_url}
            onChange={(e) => setForm({ ...form, profile_url: e.target.value })}
          />
        </Field>
        <Field label="Email" hint="Only if you already have it. Nothing looks one up.">
          <input
            className={INPUT}
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
          />
        </Field>
      </div>
      <Field
        label="How you know them"
        hint="Used in drafts only if you write it here. Nothing is inferred."
      >
        <input
          className={INPUT}
          value={form.shared_context}
          onChange={(e) => setForm({ ...form, shared_context: e.target.value })}
        />
      </Field>
      {jobId ? (
        <Field label="Link to this job as">
          <select className={INPUT} value={purpose} onChange={(e) => setPurpose(e.target.value)}>
            {LINK_PURPOSES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </Field>
      ) : null}
      <div className="flex items-center gap-2">
        <button type="submit" disabled={busy} className={PRIMARY}>
          {busy ? "Saving…" : "Save contact"}
        </button>
        <button type="button" onClick={() => setOpen(false)} className={SECONDARY}>
          Cancel
        </button>
      </div>
      {error ? <p className="text-xs text-rose-600 dark:text-rose-400">{error}</p> : null}
    </form>
  );
}

// ---------------------------------------------------------------------------
// Link an existing contact
// ---------------------------------------------------------------------------

export function LinkContactButton({
  jobId,
  contactId,
  label,
}: {
  jobId: string;
  contactId: string;
  label?: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [purpose, setPurpose] = useState("INFORMATIONAL");
  const [error, setError] = useState<string | null>(null);

  return (
    <span className="inline-flex items-center gap-1.5">
      <select
        className="rounded border border-neutral-300 bg-white px-1 py-0.5 text-xs dark:border-neutral-700 dark:bg-neutral-900"
        value={purpose}
        onChange={(e) => setPurpose(e.target.value)}
      >
        {LINK_PURPOSES.map(([value, text]) => (
          <option key={value} value={value}>
            {text}
          </option>
        ))}
      </select>
      <button
        type="button"
        disabled={busy}
        className={SECONDARY}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            await postJson(`/api/jobs/${jobId}/contacts/link`, {
              contact_id: contactId,
              purpose,
            });
            router.refresh();
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? "Linking…" : (label ?? "Link to this job")}
      </button>
      {error ? <span className="text-xs text-rose-600">{error}</span> : null}
    </span>
  );
}

export function UnlinkContactButton({
  jobId,
  contactId,
}: {
  jobId: string;
  contactId: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      disabled={busy}
      className={SECONDARY}
      onClick={async () => {
        setBusy(true);
        try {
          await postJson(`/api/jobs/${jobId}/contacts/${contactId}/unlink`);
          router.refresh();
        } finally {
          setBusy(false);
        }
      }}
    >
      Unlink
    </button>
  );
}

// ---------------------------------------------------------------------------
// Draft outreach → mark sent
// ---------------------------------------------------------------------------

type Draft = {
  channel: string;
  subject: string | null;
  body: string;
  omissions: string[];
  over_limit: boolean;
  character_count: number;
  interaction_id: string | null;
};

export function OutreachComposer({
  contactId,
  contactName,
  jobId,
}: {
  contactId: string;
  contactName: string;
  jobId?: string;
}) {
  const router = useRouter();
  const [channel, setChannel] = useState("LINKEDIN");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [sentNote, setSentNote] = useState<string | null>(null);
  const [followUp, setFollowUp] = useState("");
  const [sentNotes, setSentNotes] = useState("");

  async function generate() {
    setBusy(true);
    setError(null);
    setSentNote(null);
    try {
      const result = await postJson<Draft>(`/api/contacts/${contactId}/generate-draft`, {
        job_id: jobId ?? null,
        channel,
      });
      setDraft(result);
      setBody(result.body);
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <select
          className="rounded border border-neutral-300 bg-white px-2 py-1 text-xs dark:border-neutral-700 dark:bg-neutral-900"
          value={channel}
          onChange={(e) => setChannel(e.target.value)}
        >
          <option value="LINKEDIN">LinkedIn</option>
          <option value="EMAIL">Email</option>
        </select>
        <button type="button" disabled={busy} className={PRIMARY} onClick={generate}>
          {busy ? "Drafting…" : draft ? "Regenerate" : "Draft Outreach"}
        </button>
        <span className="text-xs text-neutral-500">
          Draft only — you send it yourself, from your own account.
        </span>
      </div>

      {error ? <p className="text-xs text-rose-600 dark:text-rose-400">{error}</p> : null}

      {draft ? (
        <div className="space-y-2 rounded border border-neutral-200 p-3 dark:border-neutral-800">
          {draft.subject ? (
            <p className="text-xs text-neutral-500">
              Subject: <span className="font-medium">{draft.subject}</span>
            </p>
          ) : null}
          <textarea
            className="h-44 w-full rounded border border-neutral-300 bg-white p-2 font-mono text-xs dark:border-neutral-700 dark:bg-neutral-900"
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
          <p className="text-[11px] text-neutral-500">
            {body.length} characters
            {draft.over_limit ? " — over LinkedIn's 300-character limit" : ""}
          </p>

          {draft.omissions.length > 0 ? (
            <ul className="space-y-1 text-[11px] text-amber-700 dark:text-amber-400">
              {draft.omissions.map((note) => (
                <li key={note}>· {note}</li>
              ))}
            </ul>
          ) : null}

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className={SECONDARY}
              onClick={async () => {
                await navigator.clipboard.writeText(body);
                setCopied(true);
                setTimeout(() => setCopied(false), 2000);
              }}
            >
              {copied ? "Copied" : "Copy"}
            </button>
            <button type="button" className={SECONDARY} onClick={generate} disabled={busy}>
              Regenerate
            </button>
          </div>

          <div className="border-t border-neutral-100 pt-2 dark:border-neutral-800">
            <p className="text-xs font-medium">
              Sent it? Record it so the follow-up gets scheduled.
            </p>
            <div className="mt-1.5 grid gap-2 sm:grid-cols-3">
              <Field label="Follow up on" hint="Four days out by default.">
                <input
                  type="date"
                  className={INPUT}
                  value={followUp}
                  onChange={(e) => setFollowUp(e.target.value)}
                />
              </Field>
              <div className="sm:col-span-2">
                <Field label="Notes">
                  <input
                    className={INPUT}
                    placeholder="What you actually sent, or anything worth remembering"
                    value={sentNotes}
                    onChange={(e) => setSentNotes(e.target.value)}
                  />
                </Field>
              </div>
            </div>
            <button
              type="button"
              disabled={busy || !draft.interaction_id}
              className={`${PRIMARY} mt-2`}
              onClick={async () => {
                if (!draft.interaction_id) return;
                setBusy(true);
                setError(null);
                try {
                  const result = await postJson<{ note: string }>(
                    `/api/outreach/${draft.interaction_id}/sent`,
                    {
                      channel,
                      notes: sentNotes || null,
                      follow_up_on: followUp || null,
                    },
                  );
                  setSentNote(result.note);
                  router.refresh();
                } catch (e) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              I sent this to {contactName.split(" ")[0]}
            </button>
            {sentNote ? (
              <p className="mt-1 text-xs text-emerald-600 dark:text-emerald-400">{sentNote}</p>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Lifecycle: reply, snooze, close
// ---------------------------------------------------------------------------

export function ContactLifecycleActions({
  contactId,
  followUpId,
}: {
  contactId: string;
  followUpId?: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(path: string, body?: unknown) {
    setBusy(true);
    setError(null);
    try {
      await postJson(path, body);
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <button
        type="button"
        disabled={busy}
        className={SECONDARY}
        onClick={() => run(`/api/contacts/${contactId}/reply`)}
      >
        Mark replied
      </button>
      {followUpId ? (
        <button
          type="button"
          disabled={busy}
          className={SECONDARY}
          onClick={() => run(`/api/follow-ups/${followUpId}/snooze`, { days: 7 })}
        >
          Snooze 7d
        </button>
      ) : null}
      <button
        type="button"
        disabled={busy}
        className={SECONDARY}
        onClick={() => run(`/api/contacts/${contactId}/close`)}
      >
        Close
      </button>
      {error ? <span className="text-xs text-rose-600">{error}</span> : null}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Referrals
// ---------------------------------------------------------------------------

export function ReferralControl({
  jobId,
  contactId,
  contactName,
  state,
}: {
  jobId: string;
  contactId: string;
  contactName: string;
  state: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const OPTIONS: [string, string][] = [
    ["NONE", "None"],
    ["POTENTIAL", "Considering"],
    ["REQUESTED", "Requested"],
    ["RECEIVED", "Referred"],
    ["DECLINED", "Declined"],
    ["NO_RESPONSE", "Not appropriate"],
  ];

  return (
    <span className="inline-flex flex-col gap-1">
      <span className="inline-flex items-center gap-1.5">
        <span className="text-xs text-neutral-500">Referral</span>
        <select
          disabled={busy}
          className="rounded border border-neutral-300 bg-white px-1 py-0.5 text-xs dark:border-neutral-700 dark:bg-neutral-900"
          value={state}
          onChange={async (event) => {
            setBusy(true);
            setError(null);
            try {
              const result = await postJson<{ warnings?: { message: string }[] }>(
                `/api/jobs/${jobId}/referrals`,
                { contact_id: contactId, state: event.target.value },
              );
              setWarnings((result.warnings ?? []).map((w) => w.message));
              router.refresh();
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          {OPTIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </span>
      {warnings.map((warning) => (
        <span key={warning} className="text-[11px] text-amber-700 dark:text-amber-400">
          {warning} Recorded anyway — this is your call, not the system&rsquo;s.
        </span>
      ))}
      {error ? <span className="text-xs text-rose-600">{error}</span> : null}
      <span className="sr-only">{contactName}</span>
    </span>
  );
}
