import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Job Hunt OS",
  description:
    "Evidence-grounded career decision-and-execution system for full-time roles starting Dec 2026 / Jan 2027 onward.",
};

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/jobs", label: "Jobs" },
  { href: "/people", label: "People" },
  { href: "/resumes", label: "Resumes" },
  { href: "/gaps", label: "Skill Gaps" },
  { href: "/sources", label: "Sources" },
  { href: "/collection", label: "Collection" },
];

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-neutral-50 text-neutral-900 antialiased dark:bg-neutral-950 dark:text-neutral-100">
        <div className="mx-auto flex min-h-screen max-w-6xl flex-col px-4">
          <header className="flex flex-wrap items-baseline gap-x-6 gap-y-2 border-b border-neutral-200 py-4 dark:border-neutral-800">
            <Link href="/" className="text-lg font-semibold tracking-tight">
              Job Hunt OS
            </Link>
            <nav className="flex gap-4 text-sm">
              {NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="text-neutral-600 transition hover:text-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100"
                >
                  {item.label}
                </Link>
              ))}
            </nav>
            <span className="ml-auto text-xs text-neutral-500">
              Decision support. Never auto-applies, never auto-sends.
            </span>
          </header>

          <main className="flex-1 py-6">{children}</main>

          <footer className="border-t border-neutral-200 py-4 text-xs text-neutral-500 dark:border-neutral-800">
            Evidence Bank is authoritative. Every claim traces to an Evidence ID.
          </footer>
        </div>
      </body>
    </html>
  );
}
