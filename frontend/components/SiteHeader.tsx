"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useSession } from "@/lib/session";

function Seal() {
  // Our own mark: a seal with a tick, for "proof".
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true" className="h-7 w-7">
      <circle cx="16" cy="16" r="14" fill="none" stroke="currentColor" strokeWidth="2.5" />
      <circle cx="16" cy="16" r="9.5" fill="none" stroke="currentColor" strokeWidth="1.2" strokeDasharray="2 2.2" />
      <path d="M11.2 16.4l3.3 3.3 6.4-7" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function SiteHeader() {
  const pathname = usePathname();
  const { caseId } = useSession();
  const links = [
    { href: "/policies", label: "My policies" },
    { href: "/readiness", label: "Claim readiness" },
    { href: caseId ? `/checklist/${caseId}` : "", label: "Checklist" },
    { href: caseId ? `/case/${caseId}` : "", label: "My case" },
    { href: "/console", label: "Console" },
  ];
  return (
    <header className="border-b border-line bg-paper">
      <div className="mx-auto flex max-w-5xl flex-col gap-3 px-4 py-4 sm:flex-row sm:items-center sm:justify-between">
        <Link href="/" className="flex items-center gap-2 text-pine">
          <Seal />
          <span className="text-2xl font-bold tracking-tight text-ink">Praman</span>
        </Link>
        <nav aria-label="Main" className="-mx-4 overflow-x-auto px-4">
          <ul className="flex gap-1 whitespace-nowrap text-[15px]">
            {links.map((link) => {
              const active = link.href !== "" && (pathname === link.href || pathname.startsWith(`${link.href}/`));
              return (
                <li key={link.label}>
                  {link.href ? (
                    <Link
                      href={link.href}
                      aria-current={active ? "page" : undefined}
                      className={`block rounded-md px-3 py-1.5 ${active ? "bg-pine-wash font-medium text-pine-dark" : "text-muted hover:text-ink"}`}
                    >
                      {link.label}
                    </Link>
                  ) : (
                    <span className="block px-3 py-1.5 text-line" aria-disabled="true">
                      {link.label}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </nav>
      </div>
    </header>
  );
}
