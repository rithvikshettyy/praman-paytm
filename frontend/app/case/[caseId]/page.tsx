"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { ErrorNote, ExampleBadge, Loading, UnverifiedBadge } from "@/components/Status";
import { api, errorText } from "@/lib/api";
import { OUTCOME_LABEL, PRODUCT_LABEL, humanise, longDate } from "@/lib/format";
import { useSession } from "@/lib/session";
import type { CaseDetail, Draft } from "@/lib/types";
import { useApi } from "@/lib/useApi";

export default function CasePage() {
  const { caseId } = useParams<{ caseId: string }>();
  const session = useSession();
  const { data, error, loading, reload } = useApi<CaseDetail>(`/api/case/${caseId}`);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [approval, setApproval] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [showEnglish, setShowEnglish] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleted, setDeleted] = useState(false);

  const latest = draft ?? data?.drafts[0] ?? null;

  async function makeDraft() {
    setBusy("Drafting the letter");
    setActionError(null);
    setApproval(null);
    try {
      const body = await api<{ draft: Draft }>(`/api/case/${caseId}/draft`, { method: "POST", json: { language: session.language } });
      setDraft(body.draft);
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setBusy(null);
    }
  }

  async function approve(id: number) {
    setBusy("Approving");
    setActionError(null);
    try {
      const body = await api<{ draft: Draft; message: string }>(`/api/case/${caseId}/draft/${id}/approve`, { method: "POST" });
      setDraft((current) => ({ ...(current ?? body.draft), ...body.draft }));
      setApproval(body.message);
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setBusy(null);
    }
  }

  async function deleteEverything() {
    setBusy("Deleting");
    setActionError(null);
    try {
      await api(`/api/case/${caseId}`, { method: "DELETE" });
      setDeleted(true);
      if (caseId === session.caseId) session.renewCase();
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setBusy(null);
      setConfirmDelete(false);
    }
  }

  if (deleted) {
    return (
      <div className="max-w-2xl space-y-4">
        <h1 className="text-3xl font-bold tracking-tight">Everything is deleted</h1>
        <p className="text-muted">The case, its documents and every record of them held by this service are gone.</p>
        <Link href="/" className="font-medium text-pine underline">
          Back to the start
        </Link>
      </div>
    );
  }

  return (
    <div className="max-w-3xl space-y-6">
      {loading && <Loading label="Loading the case" />}
      {error && <ErrorNote message={error} onRetry={reload} />}

      {data && (
        <>
          <header className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-3xl font-bold tracking-tight">
                {data.respondent_name ? `Case with ${data.respondent_name}` : "Your case"}
              </h1>
              {data.example && <ExampleBadge />}
            </div>
            <p className="text-muted">
              {data.product ? PRODUCT_LABEL[data.product] ?? data.product : "Product not known yet"}
              {data.grievance_class ? `, ${humanise(data.grievance_class).toLowerCase()}` : ""}
            </p>
          </header>

          <section className="grid gap-4 sm:grid-cols-2">
            <div className="rounded-lg border border-line bg-white p-4">
              <h2 className="font-semibold">Who owes the answer</h2>
              {data.respondent ? (
                <>
                  <p className="mt-1 text-lg">{data.respondent_name ?? `The ${data.respondent} (legal name not known yet)`}</p>
                  <p className="text-sm text-muted">
                    {data.distributor_owned ? "The distributor owes this answer." : `Sent straight to the ${data.respondent}, not the distributor.`}
                  </p>
                </>
              ) : (
                <p className="mt-1 text-muted">Not worked out yet. Tell the chat what happened.</p>
              )}
            </div>
            <div className="rounded-lg border border-line bg-white p-4">
              <h2 className="font-semibold">Clock</h2>
              {data.clock ? (
                <p className="mt-1">
                  Started {longDate(data.clock.started_on)}.{" "}
                  {data.clock.respond_by ? `Reply due by ${longDate(data.clock.respond_by)}.` : "No deadline is known for this step."}
                </p>
              ) : data.route ? (
                <p className="mt-1">
                  Starts when the letter is sent.{" "}
                  {data.route.steps[0].respond_within_days
                    ? `${data.route.steps[0].label} then has ${data.route.steps[0].respond_within_days} days to reply.`
                    : "No reply window is known for the first step."}
                </p>
              ) : (
                <p className="mt-1 text-muted">No clock until there is someone to write to.</p>
              )}
              {data.route?.steps[0].verified_by === "UNVERIFIED" && (
                <div className="mt-2">
                  <UnverifiedBadge title="This reply window is not yet checked against the regulation." />
                </div>
              )}
            </div>
          </section>

          {data.route && (
            <section className="rounded-lg border border-line bg-white p-4">
              <h2 className="font-semibold">The ladder, in order</h2>
              <ol className="mt-2 list-decimal space-y-1 pl-5">
                {data.route.steps.map((s) => (
                  <li key={s.step}>
                    {s.label}
                    {s.respond_within_days ? <span className="text-muted">, {s.respond_within_days} days to reply</span> : null}
                  </li>
                ))}
              </ol>
            </section>
          )}

          <section className="flex flex-wrap gap-x-6 gap-y-1 text-[15px]">
            <p>
              Readiness: <strong>{data.verdict ? OUTCOME_LABEL[data.verdict] : "Not checked yet"}</strong>
            </p>
            <p>
              Documents: <strong>{data.checklist.documents_collected} of {data.checklist.documents_required}</strong>{" "}
              <Link href={`/checklist/${caseId}`} className="text-pine underline">
                Open checklist
              </Link>
            </p>
          </section>

          <section className="space-y-3 rounded-lg border border-line bg-white p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="text-xl font-semibold">Letter</h2>
              <button
                type="button"
                onClick={makeDraft}
                disabled={busy !== null}
                className="rounded-md border border-line px-3 py-2 text-sm font-medium hover:bg-paper disabled:opacity-50"
              >
                {latest ? "Draft again" : "Draft the letter"}
              </button>
            </div>
            {!latest && <p className="text-muted">No letter yet. Drafting reads it back to you before anything else happens.</p>}
            {latest && (
              <>
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="text-muted">To {latest.addressee}</span>
                  {latest.unverified && <UnverifiedBadge title="Part of this letter rests on a rule not yet checked against the regulation." />}
                </div>
                {latest.readback && latest.readback !== latest.text && (
                  <button type="button" onClick={() => setShowEnglish((v) => !v)} className="text-sm text-pine underline">
                    {showEnglish ? "Show it in your language" : "Show the English letter"}
                  </button>
                )}
                <pre className="whitespace-pre-wrap rounded-md bg-paper p-4 font-sans text-[15px] leading-relaxed">
                  {showEnglish || !latest.readback ? latest.text : latest.readback}
                </pre>
                {latest.status === "approved" ? (
                  <p role="status" className="rounded-md bg-pine-wash px-4 py-3 font-semibold text-pine-dark">
                    {approval ?? "Approved and ready to send"}
                  </p>
                ) : (
                  <button
                    type="button"
                    onClick={() => approve(latest.id)}
                    disabled={busy !== null}
                    className="rounded-md bg-pine px-4 py-2.5 font-medium text-white hover:bg-pine-dark disabled:opacity-50"
                  >
                    Approve
                  </button>
                )}
              </>
            )}
          </section>

          {busy && <Loading label={busy} />}
          {actionError && <ErrorNote message={actionError} />}

          <section className="border-t border-line pt-6">
            {confirmDelete ? (
              <div className="space-y-3 rounded-lg border border-stamp/40 bg-stamp-wash p-4">
                <p className="font-medium">Delete this case, its documents, letters and every record of them? This cannot be undone.</p>
                <div className="flex gap-3">
                  <button type="button" onClick={deleteEverything} disabled={busy !== null} className="rounded-md bg-stamp px-4 py-2 font-medium text-white">
                    Delete everything
                  </button>
                  <button type="button" onClick={() => setConfirmDelete(false)} className="rounded-md border border-line bg-white px-4 py-2">
                    Keep it
                  </button>
                </div>
              </div>
            ) : (
              <button type="button" onClick={() => setConfirmDelete(true)} className="font-medium text-stamp underline">
                Delete everything
              </button>
            )}
          </section>
        </>
      )}
    </div>
  );
}
