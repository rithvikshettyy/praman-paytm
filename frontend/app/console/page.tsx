"use client";

import Link from "next/link";
import { useState } from "react";

import { HandoffBrief } from "@/components/HandoffBrief";
import { api, errorText } from "@/lib/api";
import { ErrorNote, ExampleBadge, Loading } from "@/components/Status";
import { OUTCOME_LABEL, PRODUCT_LABEL, humanise, longDate } from "@/lib/format";
import type { ConsoleCase, ConsoleComplaint, Metrics } from "@/lib/types";
import { useApi } from "@/lib/useApi";

const REFRESH_MS = 5000;

export default function ConsolePage() {
  const [filter, setFilter] = useState<"" | "needs_paytm" | "routed_away" | "resolved">("");
  const [statusError, setStatusError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null); // the case whose brief is showing
  const [view, setView] = useState<"cases" | "complaints">("cases");
  const [complaintStatus, setComplaintStatus] = useState<"pending" | "resolved" | "all">("pending");
  const [openComplaint, setOpenComplaint] = useState<number | null>(null); // the complaint whose brief is showing
  const complaints = useApi<{ complaints: ConsoleComplaint[] }>(
    `/api/console/complaints?status=${complaintStatus}`,
    REFRESH_MS,
  );
  const metrics = useApi<Metrics>("/api/metrics", REFRESH_MS);
  const list = useApi<{ cases: ConsoleCase[] }>(`/api/console/cases${filter ? `?filter=${filter}` : ""}`, REFRESH_MS);
  const distributor = metrics.data?.headline.distributor ?? "the distributor";

  // An agent marks a case Resolved (it leaves this list, nothing is deleted) or Pending (it comes back).
  async function mark(caseId: string, status: "pending" | "resolved") {
    setStatusError(null);
    try {
      await api(`/api/console/cases/${caseId}/status`, { method: "POST", json: { status } });
      if (status === "resolved" && open === caseId) setOpen(null);
      list.reload();
      metrics.reload();
    } catch (err) {
      setStatusError(errorText(err));
    }
  }
  // An agent marks a complaint Resolved (it leaves the pending list) or Pending (it comes back).
  async function markComplaint(id: number, status: "pending" | "resolved") {
    setStatusError(null);
    try {
      await api(`/api/console/complaints/${id}/status`, { method: "POST", json: { status } });
      complaints.reload();
      metrics.reload();
    } catch (err) {
      setStatusError(errorText(err));
    }
  }
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
        { label: "Insurer queries caught before filing", value: c.insurer_queries_caught },
        { label: "Answers given in the chat", value: c.answers_given },
        { label: "Answers she confirmed solved her question", value: c.answers_confirmed_solved },
        { label: "Cases where she asked for a person", value: c.asked_for_a_person },
        { label: "Complaints registered", value: c.complaints_registered },
        { label: "Complaints waiting for a person", value: c.complaints_pending },
        { label: "Cases an agent marked resolved", value: c.cases_marked_resolved },
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
          <>
            <p className="text-4xl font-bold leading-tight tracking-tight sm:text-5xl">{metrics.data.headline.text}</p>
            {c && c.answers_given > 0 && (
              <p className="mt-3 text-lg text-muted">
                Of {c.answers_given} {c.answers_given === 1 ? "answer" : "answers"} Praman gave, she confirmed{" "}
                {c.answers_confirmed_solved} solved her question, with no agent involved.
              </p>
            )}
          </>
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

      <div role="tablist" aria-label="Console" className="flex gap-1 border-b border-line">
        {[
          { value: "cases", label: "Cases" },
          { value: "complaints", label: `Complaints${c ? ` (${c.complaints_pending} pending)` : ""}` },
        ].map((tab) => (
          <button
            key={tab.value}
            role="tab"
            aria-selected={view === tab.value}
            type="button"
            onClick={() => setView(tab.value as typeof view)}
            className={`-mb-px border-b-2 px-4 py-2 font-medium ${
              view === tab.value ? "border-pine text-pine-dark" : "border-transparent text-muted hover:text-ink"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {view === "complaints" && (
        <section aria-label="Complaints" className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-xl font-semibold">Complaints</h2>
              <p className="text-sm text-muted">
                Registered when the chat could not settle it, or she asked to talk to someone. Nothing has been sent to the
                insurer. Her contact shows here only.
              </p>
            </div>
            <div role="tablist" aria-label="Filter complaints" className="flex gap-1 rounded-md border border-line bg-white p-1 text-sm">
              {[
                { value: "pending", label: "Pending" },
                { value: "resolved", label: "Resolved" },
                { value: "all", label: "All" },
              ].map((tab) => (
                <button
                  key={tab.value}
                  role="tab"
                  aria-selected={complaintStatus === tab.value}
                  type="button"
                  onClick={() => setComplaintStatus(tab.value as typeof complaintStatus)}
                  className={`rounded px-3 py-1.5 ${complaintStatus === tab.value ? "bg-pine text-white" : "text-muted hover:text-ink"}`}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          </div>

          {statusError && <ErrorNote message={statusError} />}
          {complaints.error && <ErrorNote message={complaints.error} />}
          {complaints.loading && !complaints.data && <Loading label="Loading complaints" />}
          {complaints.data && complaints.data.complaints.length === 0 && (
            <p className="text-muted">{complaintStatus === "resolved" ? "No resolved complaints." : "No complaints here yet."}</p>
          )}
          {complaints.data && complaints.data.complaints.length > 0 && (
            <ul className="divide-y divide-line rounded-lg border border-line bg-white">
              {complaints.data.complaints.map((item) => (
                <li key={item.id}>
                  <div className="grid gap-2 px-4 py-3 sm:grid-cols-[1.6fr_1fr_auto] sm:items-start sm:gap-4">
                    <div>
                      <p className="font-medium">
                        C-{item.id}
                        {item.respondent_name ? `, ${item.respondent_name}` : ""}
                        {item.example && <span className="ml-2 align-middle"><ExampleBadge /></span>}
                      </p>
                      <p className="mt-1 whitespace-pre-wrap text-sm">{item.text || "No description given."}</p>
                      <p className="mt-1 text-sm text-muted">
                        {item.product ? PRODUCT_LABEL[item.product] ?? item.product : "Product unknown"}
                        {item.grievance_class ? `, ${humanise(item.grievance_class).toLowerCase()}` : ""}
                        {item.policy_last4 ? `, policy ending ${item.policy_last4}` : ""}
                      </p>
                    </div>
                    <div className="text-sm">
                      <p>
                        <span className="text-muted">Reach her on: </span>
                        <span className="font-medium">{item.contact ?? "No contact given"}</span>
                      </p>
                      <p className="text-muted">Registered {longDate(item.registered_at)}</p>
                    </div>
                    <div className="flex items-center gap-2 justify-self-start sm:justify-self-end">
                      <label className="sr-only" htmlFor={`complaint-${item.id}`}>
                        Status of this complaint
                      </label>
                      <select
                        id={`complaint-${item.id}`}
                        value={item.status}
                        onChange={(event) => void markComplaint(item.id, event.target.value as "pending" | "resolved")}
                        className={`rounded-md border px-2 py-1.5 text-sm font-medium ${
                          item.status === "resolved" ? "border-pine/40 bg-pine-wash text-pine-dark" : "border-line bg-white text-ink"
                        }`}
                      >
                        <option value="pending">Pending</option>
                        <option value="resolved">Resolved</option>
                      </select>
                      <button
                        type="button"
                        onClick={() => setOpenComplaint(openComplaint === item.id ? null : item.id)}
                        aria-expanded={openComplaint === item.id}
                        className="rounded-md border border-line bg-white px-3 py-1.5 text-sm font-medium hover:border-pine hover:text-pine"
                      >
                        {openComplaint === item.id ? "Hide brief" : "View brief"}
                      </button>
                    </div>
                  </div>
                  {openComplaint === item.id && <HandoffBrief caseId={item.case_id} />}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {view === "cases" && (
      <section aria-label="Cases" className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-xl font-semibold">Cases</h2>
          <div role="tablist" aria-label="Filter cases" className="flex gap-1 rounded-md border border-line bg-white p-1 text-sm">
            {[
              { value: "", label: "All" },
              { value: "needs_paytm", label: `Needs ${distributor}` },
              { value: "routed_away", label: "Routed to insurer or lender" },
              { value: "resolved", label: "Resolved" },
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

        {statusError && <ErrorNote message={statusError} />}
        {list.loading && !list.data && <Loading label="Loading cases" />}
        {list.data && list.data.cases.length === 0 && (
          <p className="text-muted">{filter === "resolved" ? "No resolved cases." : "No cases here yet."}</p>
        )}
        {list.data && list.data.cases.length > 0 && (
          <ul className="divide-y divide-line rounded-lg border border-line bg-white">
            {list.data.cases.map((row) => (
              <li key={row.case_id}>
                <div className="grid gap-1 px-4 py-3 hover:bg-paper sm:grid-cols-[1.4fr_1fr_1fr_auto] sm:items-center sm:gap-4">
                <Link href={`/case/${row.case_id}`} className="contents">
                  <div>
                    <p className="font-medium">
                      {row.respondent_name ?? (row.respondent ? `The ${row.respondent}` : row.asked_for_person ? "Question from the chat" : "Not routed yet")}
                    </p>
                    <p className="text-sm text-muted">
                      {row.product ? PRODUCT_LABEL[row.product] ?? row.product : "Product unknown"}
                      {row.grievance_class ? `, ${humanise(row.grievance_class).toLowerCase()}` : ""}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    {row.needs_distributor && (
                      <span className="rounded border border-stamp/40 bg-stamp-wash px-1.5 py-0.5 text-stamp">Needs {distributor}</span>
                    )}
                    {row.asked_for_person && (
                      <span className="rounded border border-amber/50 bg-amber-wash px-1.5 py-0.5 text-amber">Asked for a person</span>
                    )}
                    {!row.needs_distributor && row.distributor_owned === false && (
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
                <div className="flex items-center gap-2 justify-self-start sm:justify-self-end">
                  <label className="sr-only" htmlFor={`status-${row.case_id}`}>
                    Status of this case
                  </label>
                  <select
                    id={`status-${row.case_id}`}
                    value={row.status}
                    onChange={(event) => void mark(row.case_id, event.target.value as "pending" | "resolved")}
                    className={`rounded-md border px-2 py-1.5 text-sm font-medium ${
                      row.status === "resolved" ? "border-pine/40 bg-pine-wash text-pine-dark" : "border-line bg-white text-ink"
                    }`}
                  >
                    <option value="pending">Pending</option>
                    <option value="resolved">Resolved</option>
                  </select>
                  <button
                    type="button"
                    onClick={() => setOpen(open === row.case_id ? null : row.case_id)}
                    aria-expanded={open === row.case_id}
                    className="rounded-md border border-line bg-white px-3 py-1.5 text-sm font-medium hover:border-pine hover:text-pine"
                  >
                    {open === row.case_id ? "Hide brief" : "View brief"}
                  </button>
                </div>
                </div>
                {open === row.case_id && <HandoffBrief caseId={row.case_id} />}
              </li>
            ))}
          </ul>
        )}
      </section>
      )}
    </div>
  );
}
