import Link from "next/link";

import { UnverifiedBadge } from "@/components/Status";
import { OUTCOME_LABEL, inr, longDate } from "@/lib/format";
import type { ReadinessView } from "@/lib/types";

const STAMP: Record<string, string> = {
  file: "border-pine text-pine",
  do_not_file_yet: "border-stamp text-stamp",
  file_with_known_deduction: "border-amber text-amber",
  facts_pending: "border-muted text-muted",
  no_verdict: "border-muted text-muted",
};

const EFFECT: Record<string, string> = {
  block: "Stops the claim",
  deduction: "Cuts the claim",
  ground: "In your favour",
};

export function VerdictCard({ verdict, caseId }: { verdict: ReadinessView; caseId: string | null }) {
  const b = verdict.breakdown;
  return (
    <article className="rounded-lg border border-line bg-white p-5 sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div
          className={`-rotate-2 rounded-md border-[3px] border-double px-4 py-2 text-2xl font-bold tracking-tight sm:text-3xl ${
            STAMP[verdict.outcome] ?? STAMP.no_verdict
          }`}
        >
          {OUTCOME_LABEL[verdict.outcome] ?? verdict.outcome}
        </div>
        {verdict.unverified && <UnverifiedBadge />}
      </div>

      {verdict.outcome === "do_not_file_yet" && (
        <p className="mt-4 text-ink">
          {verdict.possible_on ? `You can file from ${longDate(verdict.possible_on)}.` : "There is no date yet from which filing becomes possible."}
        </p>
      )}
      {verdict.outcome === "facts_pending" && (
        <p className="mt-4 text-ink">Answer the questions below. Nothing is assumed where your documents are silent.</p>
      )}

      {verdict.messages.length > 0 && (
        <ul className="mt-5 space-y-3">
          {verdict.messages.map((m) => (
            <li key={m.rule_id} className="border-l-4 border-line pl-3">
              <p className="text-sm font-medium text-muted">{EFFECT[m.effect]}</p>
              <p className="leading-relaxed">{m.text}</p>
              {m.unverified && (
                <div className="mt-1">
                  <UnverifiedBadge />
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {b && (
        <div className="mt-6">
          <h3 className="font-semibold">How the room cap cuts this bill</h3>
          <p className="mt-1 text-sm text-muted">
            Room limit {inr(b.room_cap_per_day)} a day, room charged {inr(b.room_quoted_per_day)} a day.
          </p>
          <dl className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div className="rounded-md bg-amber-wash p-3">
              <dt className="text-sm text-muted">Room, doctor and surgery charges</dt>
              <dd className="text-lg font-semibold">{inr(b.deductible_heads)}</dd>
              <dd className="text-sm text-amber">Cut by {b.pending ? "an amount worked out once the bill is shared" : inr(b.deduction)}</dd>
            </div>
            <div className="rounded-md bg-pine-wash p-3">
              <dt className="text-sm text-muted">Medicines, tests and implants</dt>
              <dd className="text-lg font-semibold">{inr(b.exempt_heads)}</dd>
              <dd className="text-sm text-pine-dark">Never cut</dd>
            </div>
            <div className="rounded-md border border-line p-3">
              <dt className="text-sm text-muted">Likely payable</dt>
              <dd className="text-lg font-semibold">{b.pending ? "Worked out once the bill is shared" : inr(b.payable_estimate)}</dd>
              <dd className="text-sm text-muted">An estimate. The insurer decides.</dd>
            </div>
          </dl>
        </div>
      )}

      <div className="mt-6 rounded-md bg-paper p-4">
        <h3 className="font-semibold">What to do next</h3>
        {verdict.next_action === "COVERAGE_QUERY" ? (
          <p className="mt-1">
            Ask the insurer in writing. A written coverage question starts their clock.{" "}
            {caseId && (
              <Link href={`/case/${caseId}`} className="font-medium text-pine underline">
                Draft it on your case page
              </Link>
            )}
          </p>
        ) : verdict.outcome === "facts_pending" ? (
          <p className="mt-1">Answer the open questions so the check can finish.</p>
        ) : verdict.outcome === "file_with_known_deduction" ? (
          <p className="mt-1">You can file. Choosing a room within the limit would avoid the cut.</p>
        ) : verdict.outcome === "file" ? (
          <p className="mt-1">You can file. Keep the documents on your checklist ready.</p>
        ) : (
          <p className="mt-1">Upload your policy and bill to run the check.</p>
        )}
      </div>
    </article>
  );
}
