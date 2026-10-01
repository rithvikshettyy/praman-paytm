"use client";

import { useState, type FormEvent } from "react";

import { ErrorNote, ExampleBadge, Loading } from "@/components/Status";
import { PaperChecks } from "@/components/PaperChecks";
import { VerdictCard } from "@/components/VerdictCard";
import { ApiError, api, errorText } from "@/lib/api";
import { humanise, inr } from "@/lib/format";
import { useSession } from "@/lib/session";
import type {
  BillLine,
  DocumentSummary,
  Facts,
  PaperChecksView,
  Question,
  ReadinessFromDocuments,
  ReadinessView,
} from "@/lib/types";

function field(summary: DocumentSummary | undefined, name: string): unknown {
  return summary?.fields[name]?.value ?? null;
}

function Lines({ title, lines, total, note }: { title: string; lines: BillLine[]; total?: number; note: string }) {
  if (lines.length === 0) return null;
  return (
    <div>
      <h4 className="font-semibold">{title}</h4>
      <p className="text-sm text-muted">{note}</p>
      <ul className="mt-2">
        {lines.map((line) => (
          <li key={line.description} className="flex justify-between gap-4 border-b border-line py-1.5 text-[15px] last:border-0">
            <span>{line.description}</span>
            <span className="font-medium">{inr(line.amount)}</span>
          </li>
        ))}
      </ul>
      {total !== undefined && <p className="mt-2 text-right font-semibold">Total {inr(total)}</p>}
    </div>
  );
}

function answerValue(question: Question, raw: string): string | number | boolean | null {
  if (raw === "") return null;
  if (question.input === "yes_no") return raw === "yes";
  if (question.input === "number") {
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  }
  return raw;
}

export default function ReadinessPage() {
  const { caseId, caseError, ensureConsent } = useSession();
  const [policy, setPolicy] = useState<File | null>(null);
  const [bill, setBill] = useState<File | null>(null);
  const [documents, setDocuments] = useState<ReadinessFromDocuments["documents"] | null>(null);
  const [verdict, setVerdict] = useState<ReadinessView | null>(null);
  const [papers, setPapers] = useState<PaperChecksView | null>(null); // kept while she answers questions
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function check(demo: boolean) {
    if (!caseId) return;
    if (!demo) {
      if (!policy && !bill) {
        setError("Choose a policy or a bill first, or use the demo files.");
        return;
      }
      if (!(await ensureConsent())) {
        setError("Nothing was read. You can agree to reading your documents whenever you are ready.");
        return;
      }
    }
    const form = new FormData();
    form.append("case_id", caseId);
    if (demo) form.append("demo", "true");
    if (!demo && policy) form.append("policy", policy);
    if (!demo && bill) form.append("bill", bill);
    setBusy(demo ? "Reading the demo files" : "Reading your documents. This can take a minute");
    setError(null);
    try {
      const result = await api<ReadinessFromDocuments>("/api/readiness/documents", { method: "POST", body: form });
      setDocuments(result.documents);
      setVerdict(result);
      setPapers(result.papers);
      setAnswers({});
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setError("Reading your documents needs your consent first. Press the button again to answer.");
      } else {
        setError(errorText(err));
      }
    } finally {
      setBusy(null);
    }
  }

  async function submitAnswers(event: FormEvent) {
    event.preventDefault();
    if (!verdict || !caseId) return;
    const facts: Facts = { ...verdict.facts };
    for (const question of verdict.questions) {
      const value = answerValue(question, answers[question.fact] ?? "");
      if (value !== null) facts[question.fact] = value;
    }
    setBusy("Checking again");
    setError(null);
    try {
      setVerdict(await api<ReadinessView>("/api/readiness", { method: "POST", json: { case_id: caseId, facts } }));
      setAnswers({});
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  }

  const required = verdict?.questions.filter((q) => q.required) ?? [];
  const optional = verdict?.questions.filter((q) => !q.required) ?? [];
  const policySummary = documents?.policy;
  const billSummary = documents?.bill;

  function questionInput(q: Question) {
    const id = `q-${q.fact}`;
    const value = answers[q.fact] ?? "";
    const set = (v: string) => setAnswers((current) => ({ ...current, [q.fact]: v }));
    return (
      <div key={q.fact} className="space-y-1">
        <label htmlFor={id} className="block font-medium">
          {q.question}
        </label>
        {q.input === "yes_no" ? (
          <select id={id} value={value} onChange={(e) => set(e.target.value)} className="w-full rounded-md border border-line bg-white px-3 py-2 sm:w-48">
            <option value="">Choose</option>
            <option value="yes">Yes</option>
            <option value="no">No</option>
          </select>
        ) : q.input === "choice" ? (
          <select id={id} value={value} onChange={(e) => set(e.target.value)} className="w-full rounded-md border border-line bg-white px-3 py-2 sm:w-64">
            <option value="">Choose</option>
            {q.options?.map((o) => (
              <option key={o} value={o}>
                {humanise(o)}
              </option>
            ))}
          </select>
        ) : (
          <input
            id={id}
            type={q.input === "date" ? "date" : "number"}
            min={q.input === "number" ? 0 : undefined}
            value={value}
            onChange={(e) => set(e.target.value)}
            className="w-full rounded-md border border-line bg-white px-3 py-2 sm:w-48"
          />
        )}
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <header className="max-w-2xl">
        <h1 className="text-3xl font-bold tracking-tight">Claim readiness</h1>
        <p className="mt-2 leading-relaxed text-muted">
          Will this claim be stopped, and if not, how much will be cut? Upload the policy and the hospital bill, or
          try the demo files.
        </p>
      </header>

      {caseError && <ErrorNote message={caseError} />}

      <section className="max-w-2xl space-y-4 rounded-lg border border-line bg-white p-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block">
            <span className="font-medium">Policy</span>
            <input
              type="file"
              accept="application/pdf,image/*"
              onChange={(e) => setPolicy(e.target.files?.[0] ?? null)}
              className="mt-1 block w-full text-sm file:mr-3 file:rounded-md file:border file:border-line file:bg-paper file:px-3 file:py-1.5"
            />
          </label>
          <label className="block">
            <span className="font-medium">Hospital bill</span>
            <input
              type="file"
              accept="application/pdf,image/*"
              onChange={(e) => setBill(e.target.files?.[0] ?? null)}
              className="mt-1 block w-full text-sm file:mr-3 file:rounded-md file:border file:border-line file:bg-paper file:px-3 file:py-1.5"
            />
          </label>
        </div>
        <div className="flex flex-wrap gap-3">
          <button
            type="button"
            disabled={!caseId || busy !== null}
            onClick={() => check(false)}
            className="rounded-md bg-pine px-4 py-2.5 font-medium text-white hover:bg-pine-dark disabled:opacity-50"
          >
            Check with these files
          </button>
          <button
            type="button"
            disabled={!caseId || busy !== null}
            onClick={() => check(true)}
            className="rounded-md border border-line px-4 py-2.5 font-medium hover:bg-paper disabled:opacity-50"
          >
            Use demo files
          </button>
        </div>
        {busy && <Loading label={busy} />}
        {error && <ErrorNote message={error} />}
      </section>

      {verdict && (
        <div className="grid gap-6 lg:grid-cols-[1.4fr_1fr]">
          <div className="space-y-6">
            <VerdictCard verdict={verdict} caseId={caseId} />
            {papers && <PaperChecks papers={papers} />}

            {verdict.questions.length > 0 && (
              <form onSubmit={submitAnswers} className="space-y-5 rounded-lg border border-line bg-white p-5">
                {required.length > 0 && (
                  <fieldset className="space-y-4">
                    <legend className="text-lg font-semibold">Your documents don&apos;t say this. Tell me:</legend>
                    {required.map(questionInput)}
                  </fieldset>
                )}
                {optional.length > 0 && (
                  <details className="rounded-md bg-paper p-3">
                    <summary className="cursor-pointer font-medium">Optional: if a claim was already rejected</summary>
                    <div className="mt-3 space-y-4">{optional.map(questionInput)}</div>
                  </details>
                )}
                <button
                  type="submit"
                  disabled={busy !== null}
                  className="rounded-md bg-pine px-4 py-2.5 font-medium text-white hover:bg-pine-dark disabled:opacity-50"
                >
                  Check again with my answers
                </button>
              </form>
            )}
          </div>

          {documents && (
            <aside className="space-y-5 rounded-lg border border-line bg-white p-5">
              <h2 className="text-lg font-semibold">What I read</h2>
              {policySummary && (
                <div className="space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="font-semibold">Policy</h3>
                    {policySummary.example && <ExampleBadge note={policySummary.example} />}
                  </div>
                  <p>{String(field(policySummary, "insurer") ?? "Insurer not found")}</p>
                  <p className="text-muted">Sum insured {inr(field(policySummary, "sum_insured") as number | null)}</p>
                  {policySummary.to_confirm.length > 0 && (
                    <p className="text-sm text-amber">Read with low confidence, so asked instead: {policySummary.to_confirm.map(humanise).join(", ")}</p>
                  )}
                </div>
              )}
              {billSummary?.heads && (
                <div className="space-y-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="font-semibold">Hospital bill</h3>
                    {billSummary.example && <ExampleBadge note={billSummary.example} />}
                  </div>
                  <Lines
                    title="Room, doctor and surgery"
                    note="A room-cap cut applies to these."
                    lines={billSummary.heads.deductible}
                    total={billSummary.heads.deductible_total}
                  />
                  <Lines
                    title="Medicines, tests and implants"
                    note="Never cut."
                    lines={billSummary.heads.exempt}
                    total={billSummary.heads.exempt_total}
                  />
                  <Lines title="Not placed yet" note="I will ask which group these belong to." lines={billSummary.heads.unmapped} />
                </div>
              )}
            </aside>
          )}
        </div>
      )}
    </div>
  );
}
