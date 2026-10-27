/**
 * Shared presentational primitives.
 *
 * Presentation only. These components choose a colour for a status; they never
 * decide what the status is, whether it is eligible, or whether a claim is
 * safe. Those verdicts arrive from the API already made.
 */

import type { ReactNode } from "react";

export function Card({
  title,
  subtitle,
  children,
  action,
  id,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  action?: ReactNode;
  /** Anchor target, so an action can scroll its result into view. */
  id?: string;
}) {
  return (
    <section
      id={id}
      className="rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900"
    >
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
          {subtitle ? (
            <p className="mt-0.5 text-xs text-neutral-500">{subtitle}</p>
          ) : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-2 text-sm text-neutral-500">{children}</p>;
}

const TONES = {
  neutral: "bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300",
  good: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  warn: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-300",
  bad: "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300",
  info: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
} as const;

export type Tone = keyof typeof TONES;

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: ReactNode;
  tone?: Tone;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

/** Colour lookups. Presentation only — the backend owns the verdicts. */
export function toneForBand(band?: string | null): Tone {
  if (band === "HIGH") return "good";
  if (band === "MEDIUM") return "info";
  return "neutral";
}

export function toneForEligibility(status?: string | null): Tone {
  switch (status) {
    case "ELIGIBLE":
    case "LIKELY_ELIGIBLE":
      return "good";
    case "UNCERTAIN":
      return "warn";
    case "INELIGIBLE":
      return "bad";
    default:
      return "neutral";
  }
}

export function toneForSupport(status?: string | null): Tone {
  switch (status) {
    case "SUPPORTED":
      return "good";
    case "PARTIALLY_SUPPORTED":
      return "info";
    case "UNSUPPORTED":
      return "bad";
    default:
      return "neutral";
  }
}

export function toneForExperienceFit(fit?: string | null): Tone {
  switch (fit) {
    case "ENTRY_COMPATIBLE":
      return "good";
    case "EARLY_CAREER":
      return "info";
    case "REACH_2_PLUS":
      return "warn";
    case "STRETCH_3_PLUS":
    case "SENIOR":
      return "bad";
    default:
      return "neutral";
  }
}

export function toneForGuardWarning(code: string): Tone {
  switch (code) {
    case "EXPERIENCE_STRETCH":
    case "NEEDS_ISSO_REVIEW":
    case "POSSIBLY_CLOSED":
    case "STALE":
      return "warn";
    case "CITIZENSHIP_CLEARANCE_ISSUE":
    case "LOCATION_CONFLICT":
      return "bad";
    default:
      return "neutral";
  }
}

export function toneForRecommendation(kind: string): Tone {
  switch (kind) {
    case "EMPHASIZE":
      return "good";
    case "SAFE_REWORD":
      return "info";
    case "OWNERSHIP_GATED":
      return "warn";
    case "DO_NOT_ADD":
      return "bad";
    case "GAP":
      return "warn";
    default:
      return "neutral";
  }
}


export function BackendDown({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm dark:border-amber-900 dark:bg-amber-950">
      <p className="font-medium">The backend is not reachable.</p>
      <p className="mt-1 text-neutral-700 dark:text-neutral-300">
        Start it with{" "}
        <code className="rounded bg-white px-1 dark:bg-neutral-900">
          uvicorn app.main:app --reload --port 8000
        </code>
        , then reload.
      </p>
      <p className="mt-2 text-xs text-neutral-500">{message}</p>
    </div>
  );
}
