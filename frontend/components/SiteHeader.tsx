"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";

import { WhatsAppButton } from "@/components/WhatsAppButton";
import { LANGUAGES, useSession } from "@/lib/session";

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
  const { caseId, language, setLanguage } = useSession();
  const header = useRef<HTMLElement | null>(null);

  // The chat floats just below this bar (globals.css reads --header-height), however tall it wraps.
  useEffect(() => {
    const element = header.current;
    if (!element) return;
    const publish = () =>
      document.documentElement.style.setProperty("--header-height", `${element.getBoundingClientRect().height}px`);
    publish();
    const observer = new ResizeObserver(publish);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const links = [
    { href: "/policies", label: "My policies" },
    { href: "/readiness", label: "Claim readiness" },
    { href: caseId ? `/checklist/${caseId}` : "", label: "Checklist" },
    { href: caseId ? `/case/${caseId}` : "", label: "My case" },
    { href: "/console", label: "Console" },
  ];
  return (
    <header ref={header} className="border-b border-line bg-paper">
      <div className="flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-8 lg:px-12">
        <Link href="/" className="flex items-center gap-2 text-pine">
          <Seal />
          <span translate="no" className="text-2xl font-bold tracking-tight text-ink">
            Praman
          </span>
        </Link>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
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
          <WhatsAppButton compact />
          {/* The whole site follows this choice (translated by Sarvam); the chat replies in it too. */}
          <label className="sr-only" htmlFor="site-language">
            Language
          </label>
          <select
            id="site-language"
            translate="no"
            value={language}
            onChange={(event) => setLanguage(event.target.value)}
            className="w-full rounded-md border border-line bg-white px-2 py-1.5 text-sm sm:w-auto"
          >
            {LANGUAGES.map((l) => (
              <option key={l.code} value={l.code}>
                {l.label}
              </option>
            ))}
          </select>
        </div>
      </div>
    </header>
  );
}
