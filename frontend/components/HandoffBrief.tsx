"use client";

// The agent's brief for one case: everything an agent needs to pick it up without asking her again.
// Display only: the backend builds every line from recorded events (app/handoff.py), always in
// English, with her own words shown beside their English.

import { useState } from "react";

import { ErrorNote, Loading } from "@/components/Status";
import type { HandoffBrief as Brief } from "@/lib/types";
import { useApi } from "@/lib/useApi";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">{title}</h4>
      {children}
    </section>
  );
}

const SOLVED: Record<string, string> = {
  true: "She said it solved her question",
  false: "She said it did not solve her question",
};

export function HandoffBrief({ caseId }: { caseId: string }) {
  const { data, error, loading } = useApi<Brief>(`/api/case/${caseId}/brief`, 5000);
  const [copied, setCopied] = useState(false);

  async function copy() {
    if (!data) return;
    try {
      await navigator.clipboard.writeText(data.text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // The clipboard can be blocked; the text is on screen to select.
    }
  }

  if (loading && !data) return <Loading label="Building the brief" />;
  if (error && !data) return <ErrorNote message={error} />;
  if (!data) return null;
  const b = data;

  return (
    <div className="space-y-4 border-t border-line bg-paper px-4 py-4 text-[15px]" aria-label="Case brief">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="max-w-2xl font-medium text-ink">{b.why_here.text}</p>
        <button
          type="button"
          onClick={copy}
          className="rounded-md border border-line bg-white px-3 py-1.5 text-sm font-medium hover:border-pine hover:text-pine"
        >
          {copied ? "Copied" : "Copy as text"}
        </button>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Section title="Problem">
          <p>
            {[b.product, b.problem ?? b.situation].filter(Boolean).join(", ") || "Not stated yet"}
          </p>
          {b.respondent && <p className="text-muted">Owed by: {b.respondent}</p>}
          <p className="text-muted">Her language: {b.language.name}</p>
        </Section>

        <Section title="Next step">
          <p>{b.next_step}</p>
        </Section>
      </div>

      {b.she_wrote.length > 0 && (
        <Section title="What she wrote">
          <ul className="space-y-1.5">
            {b.she_wrote.map((w, i) => (
              <li key={i} className="rounded-md border border-line bg-white px-3 py-2">
                <span translate="no">&ldquo;{w.text}&rdquo;</span>
                {w.text_en && <span className="block text-muted">In English: &ldquo;{w.text_en}&rdquo;</span>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {b.conversation.length > 0 && (
        <Section title="What she asked, and what Praman answered">
          <ul className="space-y-2">
            {b.conversation.map((t, i) => (
              <li key={i} className="rounded-md border border-line bg-white px-3 py-2">
                <p>
                  <span className="font-medium">Asked: </span>
                  <span translate="no">{t.question}</span>
                  {t.question_en && <span className="text-muted"> (in English: {t.question_en})</span>}
                </p>
                <p>
                  <span className="font-medium">Answered: </span>
                  {t.answered ? (
                    <>
                      <span translate="no">{t.answer_en}</span>
                      <span className="text-muted">
                        {" "}
                        (from {t.source}
                        {t.pages.length > 0 ? `, page${t.pages.length > 1 ? "s" : ""} ${t.pages.join(", ")}` : ""})
                      </span>
                    </>
                  ) : (
                    <span className="text-muted">Praman could not find this in {t.source}.</span>
                  )}
                </p>
                <p className={t.solved === false ? "font-medium text-stamp" : "text-muted"}>
                  {t.solved === null ? "No reply from her yet" : SOLVED[String(t.solved)]}
                </p>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {(b.readiness.outcome || b.facts.length > 0) && (
        <Section title="Facts on record">
          {b.readiness.outcome && (
            <p>
              Claim readiness: <span className="font-medium">{b.readiness.outcome}</span>
              {b.readiness.deduction ? `, deduction ${b.readiness.deduction}` : ""}
            </p>
          )}
          {b.readiness.paper_fixes.length > 0 && (
            <p className="text-stamp">
              Paper problems an insurer would query: {b.readiness.paper_fixes.map((x) => x.replace(/_/g, " ")).join(", ")}
            </p>
          )}
          {b.facts.length > 0 && (
            <dl className="grid gap-x-6 gap-y-0.5 sm:grid-cols-2">
              {b.facts.map((f) => (
                <div key={f.label} className="flex justify-between gap-3 border-b border-line/60 py-0.5">
                  <dt className="text-muted">{f.label}</dt>
                  <dd className="font-medium">{f.value}</dd>
                </div>
              ))}
            </dl>
          )}
        </Section>
      )}

      <Section title="Documents received">
        {b.documents.claim_documents.length === 0 && b.documents.read.length === 0 && !b.documents.shared_in_chat ? (
          <p className="text-muted">None yet.</p>
        ) : (
          <>
            {b.documents.shared_in_chat && (
              <p>She shared a policy in the chat. It was read for her answers; the file is not kept.</p>
            )}
            {b.documents.claim_documents.length > 0 && <p>Claim documents: {b.documents.claim_documents.join(", ")}</p>}
            {b.documents.claim_documents_missing.length > 0 && (
              <p className="text-muted">Still missing: {b.documents.claim_documents_missing.join(", ")}</p>
            )}
            {b.documents.read.length > 0 && <p className="text-muted">Documents read: {b.documents.read.join(", ")}</p>}
          </>
        )}
      </Section>

      {b.drafts.length > 0 && (
        <Section title="Letters">
          <ul>
            {b.drafts.map((d, i) => (
              <li key={i}>
                {d.kind} to {d.addressee}: <span className="text-muted">{d.status}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}
