"use client";

import { ErrorNote, ExampleBadge, Loading } from "@/components/Status";
import { PRODUCT_LABEL, inr, longDate } from "@/lib/format";
import { useSession } from "@/lib/session";
import type { Policy } from "@/lib/types";
import { useApi } from "@/lib/useApi";

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b border-line py-2 last:border-0">
      <dt className="text-muted">{label}</dt>
      <dd className="text-right font-medium">{value}</dd>
    </div>
  );
}

export default function PoliciesPage() {
  const { openChat } = useSession();
  const { data, error, loading, reload } = useApi<{ policies: Policy[] }>("/api/policies");

  return (
    <div className="space-y-6">
      <h1 className="text-3xl font-bold tracking-tight">My policies</h1>
      {loading && <Loading label="Loading your policies" />}
      {error && <ErrorNote message={error} onRetry={reload} />}
      {data && data.policies.length === 0 && <p className="text-muted">No policies yet. Upload one on the claim readiness page.</p>}
      {data?.policies.map((p) => {
        const waits = p.waiting_periods;
        return (
          <article key={p.id} className="max-w-2xl rounded-lg border border-line bg-white p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-xl font-semibold">{p.insurer}</h2>
                <p className="text-muted">{PRODUCT_LABEL[p.product] ?? p.product}</p>
              </div>
              {p.example && <ExampleBadge note={p.example} />}
            </div>
            <dl className="mt-4">
              <Row label="Sum insured" value={inr(p.sum_insured)} />
              <Row
                label="Room rent limit"
                value={
                  p.room_cap_per_day === null
                    ? "Not stated"
                    : `${inr(p.room_cap_per_day)} a day${p.room_cap_percent !== null ? ` (${p.room_cap_percent}% of sum insured)` : ""}`
                }
              />
              <Row label="Pre-existing diseases covered after" value={waits.pre_existing_months === null ? "Not stated" : `${waits.pre_existing_months} months`} />
              <Row label="Listed diseases and procedures covered after" value={waits.specified_disease_months === null ? "Not stated" : `${waits.specified_disease_months} months`} />
              <Row label="Policy start" value={longDate(p.policy_start_on)} />
            </dl>
            {p.exclusions.length > 0 && (
              <div className="mt-4">
                <h3 className="font-semibold">Never covered</h3>
                <ul className="mt-1 list-disc pl-5 text-muted">
                  {p.exclusions.map((x) => (
                    <li key={x}>{x}</li>
                  ))}
                </ul>
              </div>
            )}
            <button
              type="button"
              onClick={() => openChat({ insurer: p.insurer, product: p.product, label: `${p.insurer}, ${(PRODUCT_LABEL[p.product] ?? p.product).toLowerCase()}` })}
              className="mt-5 rounded-md bg-pine px-4 py-2.5 font-medium text-white hover:bg-pine-dark"
            >
              Ask about this policy
            </button>
          </article>
        );
      })}
    </div>
  );
}
