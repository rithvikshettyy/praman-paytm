"use client";

import Link from "next/link";
import { useState } from "react";

import { ErrorNote, ExampleBadge, Loading } from "@/components/Status";
import { OUTCOME_LABEL, PRODUCT_LABEL, humanise, longDate } from "@/lib/format";
import type { ConsoleCase, Metrics } from "@/lib/types";
import { useApi } from "@/lib/useApi";

const REFRESH_MS = 5000;

export default function ConsolePage() {
  const [filter, setFilter] = useState<"" | "needs_paytm" | "routed_away">("");
  const metrics = useApi<Metrics>("/api/metrics", REFRESH_MS);
  const list = useApi<{ cases: ConsoleCase[] }>(`/api/console/cases${filter ? `?filter=${filter}` : ""}`, REFRESH_MS);
  const distributor = metrics.data?.headline.distributor ?? "the distributor";
  const c = metrics.data?.counters;
  const why = c ? Object.entries(c.claims_stopped_why) : [];

  const counters = c
    ? [
        { label: "Readiness checks run", value: c.readiness_checks_run },
        { label: "Claims stopped before filing", value: c.claims_stopped },
        { label: "Known deductions explained", value: c.known_deductions_explained },
        { label: "Coverage questions drafted", value: c.coverage_queries_drafted },
        { label: "Escalations drafted", value: c.escalations_drafted },
        { label: `Cases routed away from ${distributor}`, value: c.cases_routed_away },
      ]
    : [];

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-2">
        <h1 className="text-3xl font-bold tracking-tight">Distributor console</h1>
        <p className="text-sm text-muted">
          Refreshes every 5 seconds{metrics.updatedAt ? `, last at ${metrics.updatedAt.toLocaleTimeString("en-IN")}` : ""}. Every number is counted from recorded events.
        </p>
      </header>

      {(metrics.error || list.error) && (
        <ErrorNote message={`${metrics.error ?? list.error}${metrics.data ? " The numbers below are from the last successful refresh." : ""}`} />
      )}

      <section aria-label="Headline" className="rounded-lg border border-line bg-white p-6">
        {metrics.loading && !metrics.data ? (
          <Loading label="Counting" />
        ) : metrics.data ? (
          <p className="text-4xl font-bold leading-tight tracking-tight sm:text-5xl">{metrics.data.headline.text}</p>
        ) : null}
      </section>

      {c && (
        <section aria-label="Counters">
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {counters.map((item) => (
              <div key={item.label} className="rounded-lg border border-line bg-white p-4">
                <dt className="text-sm text-muted">{item.label}</dt>
                <dd className="mt-1 text-3xl font-semibold">{item.value}</dd>
              </div>
            ))}
          </dl>
          {why.length > 0 && (
            <p className="mt-3 text-sm text-muted">
              Why claims were stopped: {why.map(([rule, count]) => `${humanise(rule)} (${count})`).join(", ")}.
            </p>
          )}
        </section>
      )}

      <section aria-label="Cases" className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-xl font-semibold">Cases</h2>
          <div role="tablist" aria-label="Filter cases" className="flex gap-1 rounded-md border border-line bg-white p-1 text-sm">
            {[
              { value: "", label: "All" },
              { value: "needs_paytm", label: `Needs ${distributor}` },
              { value: "routed_away", label: "Routed to insurer or lender" },
            ].map((tab) => (
              <button
                key={tab.value}
                role="tab"
                aria-selected={filter === tab.value}
                type="button"
                onClick={() => setFilter(tab.value as typeof filter)}
                className={`rounded px-3 py-1.5 ${filter === tab.value ? "bg-pine text-white" : "text-muted hover:text-ink"}`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {list.loading && !list.data && <Loading label="Loading cases" />}
        {list.data && list.data.cases.length === 0 && <p className="text-muted">No cases here yet.</p>}
        {list.data && list.data.cases.length > 0 && (
          <ul className="divide-y divide-line rounded-lg border border-line bg-white">
            {list.data.cases.map((row) => (
              <li key={row.case_id}>
                <Link href={`/case/${row.case_id}`} className="grid gap-1 px-4 py-3 hover:bg-paper sm:grid-cols-[1.4fr_1fr_1fr] sm:items-center sm:gap-4">
                  <div>
                    <p className="font-medium">
                      {row.respondent_name ?? (row.respondent ? `The ${row.respondent}` : "Not routed yet")}
                    </p>
                    <p className="text-sm text-muted">
                      {row.product ? PRODUCT_LABEL[row.product] ?? row.product : "Product unknown"}
                      {row.grievance_class ? `, ${humanise(row.grievance_class).toLowerCase()}` : ""}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    {row.distributor_owned === true && (
                      <span className="rounded border border-stamp/40 bg-stamp-wash px-1.5 py-0.5 text-stamp">Needs {distributor}</span>
                    )}
                    {row.distributor_owned === false && (
                      <span className="rounded border border-pine/40 bg-pine-wash px-1.5 py-0.5 text-pine-dark">Routed away</span>
                    )}
                    {row.example && <ExampleBadge />}
                  </div>
                  <div className="text-sm">
                    <p>{row.verdict ? OUTCOME_LABEL[row.verdict] : "No readiness check"}</p>
                    <p className="text-muted">
                      {row.clock ? (row.clock.respond_by ? `Reply due ${longDate(row.clock.respond_by)}` : "Clock running") : "No clock yet"}
                    </p>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
