// What an insurer would query about her papers, caught before she files. Display only: every
// finding, label and message comes from the backend (engine checks, words from data/claim_checks.yaml).

import { UnverifiedBadge } from "@/components/Status";
import type { PaperChecksView } from "@/lib/types";

export function PaperChecks({ papers }: { papers: PaperChecksView }) {
  const fixes = papers.findings.filter((f) => f.severity === "fix");
  const headsUp = papers.findings.filter((f) => f.severity !== "fix");
  if (!papers.findings.length && !papers.passed.length) return null; // nothing could be checked yet

  return (
    <section className="space-y-4 rounded-lg border border-line bg-white p-5" aria-labelledby="paper-checks">
      <div>
        <h2 id="paper-checks" className="text-lg font-semibold">
          Before you file: what the insurer would ask about
        </h2>
        <p className="text-sm text-muted">
          {fixes.length
            ? `${fixes.length} ${fixes.length === 1 ? "thing" : "things"} to fix first, or the insurer is likely to send a query and the claim waits.`
            : "Nothing here that an insurer would query."}
        </p>
      </div>

      {fixes.map((f) => (
        <div key={f.problem} className="rounded-md border border-stamp/40 bg-stamp-wash p-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-stamp">Fix before you file</p>
          <p className="mt-1 text-[15px] leading-relaxed text-ink">{f.message}</p>
          {f.unverified && <UnverifiedBadge />}
        </div>
      ))}

      {headsUp.map((f) => (
        <div key={f.problem} className="rounded-md border border-amber/40 bg-amber-wash p-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-amber">Good to know</p>
          <p className="mt-1 text-[15px] leading-relaxed text-ink">{f.message}</p>
          {f.unverified && (
            <div className="mt-2">
              <UnverifiedBadge />
            </div>
          )}
        </div>
      ))}

      {papers.passed.length > 0 && (
        <ul className="space-y-1 text-sm text-ink" aria-label="Checks that passed">
          {papers.passed.map((c) => (
            <li key={c.check} className="flex items-center gap-2">
              <svg viewBox="0 0 24 24" aria-hidden="true" className="h-4 w-4 shrink-0 fill-none stroke-current text-pine" strokeWidth="2.5">
                <path d="M5 12.5l4.5 4.5L19 7.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              {c.label}
            </li>
          ))}
        </ul>
      )}

      {papers.skipped.length > 0 && (
        <div className="text-sm text-muted">
          <p className="font-medium">Not checked yet</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {papers.skipped.map((c) => (
              <li key={c.check}>
                {c.label}: needs {c.needs}.
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
