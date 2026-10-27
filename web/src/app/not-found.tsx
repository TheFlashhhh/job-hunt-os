import Link from "next/link";

export default function NotFound() {
  return (
    <div className="py-8">
      <h1 className="text-lg font-semibold">Not found</h1>
      <p className="mt-1 text-sm text-neutral-500">
        That record does not exist in the Job Hunt OS database.
      </p>
      <Link href="/" className="mt-3 inline-block text-sm hover:underline">
        ← Dashboard
      </Link>
    </div>
  );
}
