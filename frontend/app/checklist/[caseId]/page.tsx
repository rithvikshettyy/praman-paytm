"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useRef, useState } from "react";

import { ErrorNote, Loading, UnverifiedBadge } from "@/components/Status";
import { api, errorText } from "@/lib/api";
import { useSession } from "@/lib/session";
import type { Checklist } from "@/lib/types";
import { useApi } from "@/lib/useApi";

export default function ChecklistPage() {
  const { caseId } = useParams<{ caseId: string }>();
  const { ensureConsent } = useSession();
  const { data, error, loading, reload, setData } = useApi<Checklist>(`/api/checklist/${caseId}`);
  const [busySlot, setBusySlot] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const inputs = useRef<Record<string, HTMLInputElement | null>>({});

  async function choose(slot: string) {
    if (!(await ensureConsent())) return;
    inputs.current[slot]?.click();
  }

  async function upload(slot: string, file: File | undefined) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    form.append("slot", slot);
    setBusySlot(slot);
    setUploadError(null);
    try {
      setData(await api<Checklist>(`/api/checklist/${caseId}`, { method: "POST", body: form }));
    } catch (err) {
      setUploadError(errorText(err));
    } finally {
      setBusySlot(null);
    }
  }

  async function place(slot: string) {
    if (!data?.pending_document_id) return;
    const form = new FormData();
    form.append("document_id", String(data.pending_document_id));
    form.append("slot", slot);
    setBusySlot(slot);
    try {
      setData(await api<Checklist>(`/api/checklist/${caseId}`, { method: "POST", body: form }));
    } catch (err) {
      setUploadError(errorText(err));
    } finally {
      setBusySlot(null);
    }
  }

  return (
    <div className="max-w-2xl space-y-6">
      <header>
        <h1 className="text-3xl font-bold tracking-tight">Document checklist</h1>
        <p className="mt-2 text-muted">
          Add each document as a photo or PDF. Only the fact that it arrived is kept, not the file, unless you ask me
          to keep it.
        </p>
      </header>

      {loading && <Loading label="Loading the checklist" />}
      {error && <ErrorNote message={error} onRetry={reload} />}

      {data && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-xl font-semibold">
              {data.documents_collected} of {data.documents_required} in
            </p>
            {data.unverified && <UnverifiedBadge title="This list of documents is not yet checked against the insurer's claim form." />}
          </div>

          {data.pending_document_id && (
            <div className="rounded-lg border border-amber/40 bg-amber-wash p-4">
              <p className="font-medium">A photo is waiting. Which document is it?</p>
              <div className="mt-3 flex flex-wrap gap-2">
                {data.slots.map((s) => (
                  <button key={s.slot} type="button" onClick={() => place(s.slot)} disabled={busySlot !== null} className="rounded-md border border-line bg-white px-3 py-1.5 text-sm">
                    {s.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          <ul className="divide-y divide-line rounded-lg border border-line bg-white">
            {data.slots.map((s) => (
              <li key={s.slot} className="flex items-center justify-between gap-4 px-4 py-3">
                <div className="flex items-center gap-3">
                  <span
                    aria-hidden="true"
                    className={`flex h-6 w-6 items-center justify-center rounded-full border-2 text-sm ${
                      s.filled ? "border-pine bg-pine text-white" : "border-line text-transparent"
                    }`}
                  >
                    ✓
                  </span>
                  <span className={s.filled ? "" : "text-ink"}>
                    {s.label}
                    <span className="sr-only">{s.filled ? ", received" : ", missing"}</span>
                  </span>
                </div>
                <div>
                  <input
                    ref={(el) => {
                      inputs.current[s.slot] = el;
                    }}
                    type="file"
                    accept="application/pdf,image/*"
                    className="hidden"
                    onChange={(e) => {
                      void upload(s.slot, e.target.files?.[0]);
                      e.target.value = "";
                    }}
                  />
                  <button
                    type="button"
                    disabled={busySlot !== null}
                    onClick={() => choose(s.slot)}
                    className={`rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${
                      s.filled ? "border border-line text-muted" : "bg-pine text-white hover:bg-pine-dark"
                    }`}
                  >
                    {busySlot === s.slot ? "Adding…" : s.filled ? "Add another page" : "Add"}
                  </button>
                </div>
              </li>
            ))}
          </ul>
          {uploadError && <ErrorNote message={uploadError} />}
          <p className="text-sm text-muted">
            <Link href={`/case/${caseId}`} className="text-pine underline">
              Back to the case
            </Link>
          </p>
        </>
      )}
    </div>
  );
}
