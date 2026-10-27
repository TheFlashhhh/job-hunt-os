"use client";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="rounded-lg border border-rose-300 bg-rose-50 p-4 text-sm dark:border-rose-900 dark:bg-rose-950">
      <p className="font-medium">Something went wrong loading this page.</p>
      <p className="mt-1 text-neutral-700 dark:text-neutral-300">{error.message}</p>
      <button
        onClick={reset}
        className="mt-3 rounded bg-neutral-900 px-3 py-1 text-xs text-white dark:bg-neutral-100 dark:text-neutral-900"
      >
        Try again
      </button>
    </div>
  );
}
