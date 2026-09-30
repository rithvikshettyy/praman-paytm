// Shapes the backend returns. The site only displays them.

export interface Citation {
  label: string;
  insurer: string;
  doc_type: string;
  page: number;
  source_url: string;
  verified_by: string;
}

export interface ChatMessage {
  text: string;
  unverified: boolean;
  citations: Citation[];
  audio_url: string | null;
}

export interface ChatReply {
  case_id: string | null;
  language: string;
  messages: ChatMessage[];
  transcript?: string;
}

export interface Policy {
  id: string;
  example: string | null;
  insurer: string;
  product: string;
  policy_number: string | null;
  policy_start_on: string | null;
  sum_insured: number | null;
  room_cap_percent: number | null;
  room_cap_per_day: number | null;
  co_pay_percent: number | null;
  waiting_periods: { pre_existing_months: number | null; specified_disease_months: number | null };
  exclusions: string[];
  network_status: string | null;
}

export interface VerdictMessage {
  rule_id: string;
  effect: "block" | "deduction" | "ground";
  text: string;
  verified_by: string;
  unverified: boolean;
}

export interface Question {
  fact: string;
  question: string;
  input: "yes_no" | "number" | "date" | "choice";
  required: boolean;
  options?: string[];
}

export interface Breakdown {
  room_cap_per_day: number;
  room_quoted_per_day: number;
  ratio: number | null;
  deductible_heads: number | null;
  exempt_heads: number | null;
  deduction: number | null;
  payable_estimate: number | null;
  pending: boolean;
}

export type Outcome = "file" | "do_not_file_yet" | "file_with_known_deduction" | "facts_pending" | "no_verdict";

export type Facts = Record<string, string | number | boolean>;

export interface ReadinessView {
  outcome: Outcome;
  possible_on: string | null;
  next_action: string | null;
  facts_pending: string[];
  messages: VerdictMessage[];
  unverified: boolean;
  questions: Question[];
  breakdown: Breakdown | null;
  facts: Facts;
}

export interface BillLine {
  description: string;
  amount: number | null;
}

export interface DocumentSummary {
  doc_type: string;
  source: string;
  example: string | null;
  fields: Record<string, { value: unknown; confidence: number }>;
  to_confirm: string[];
  missing: string[];
  heads?: {
    deductible: BillLine[];
    exempt: BillLine[];
    unmapped: BillLine[];
    deductible_total: number;
    exempt_total: number;
  };
}

export interface ReadinessFromDocuments extends ReadinessView {
  case_id: string;
  documents: { policy?: DocumentSummary; bill?: DocumentSummary };
}

export interface ChecklistSlot {
  number: number;
  slot: string;
  label: string;
  filled: boolean;
}

export interface Checklist {
  case_id: string;
  slots: ChecklistSlot[];
  missing: string[];
  pending_document_id: number | null;
  documents_collected: number;
  documents_required: number;
  verified_by: string;
  unverified: boolean;
  attached?: { document_id: number; slot: string | null; needs_choice: boolean };
  options?: { number: number; slot: string; label: string }[];
}

export interface Draft {
  id: number;
  case_id: string;
  kind: "escalation" | "coverage_query";
  addressee: string;
  text: string;
  unverified: boolean;
  status: "drafted" | "approved";
  created_at: string;
  approved_at: string | null;
  readback?: string;
  readback_language?: string;
}

export interface Step {
  step: string;
  label: string;
  respond_within_days: number | null;
  verified_by: string;
}

export interface CaseDetail {
  case_id: string;
  example: boolean;
  language: string;
  product: string | null;
  grievance_class: string | null;
  respondent: string | null;
  respondent_name: string | null;
  distributor_owned: boolean | null;
  route: { situation: string; ladder: string; steps: Step[] } | null;
  clock: { step: string; started_on: string; respond_by: string | null; verified_by: string } | null;
  verdict: Outcome | null;
  checklist: { documents_collected: number; documents_required: number };
  drafts: Draft[];
}

export interface ConsoleCase {
  case_id: string;
  example: boolean;
  product: string | null;
  grievance_class: string | null;
  respondent: string | null;
  respondent_name: string | null;
  distributor_owned: boolean | null;
  verdict: Outcome | null;
  clock: { step: string; respond_by: string | null; verified_by: string | null } | null;
  last_event_at: string | null;
}

export interface Metrics {
  headline: { cases: number; needed_distributor: number; distributor: string; text: string };
  counters: {
    readiness_checks_run: number;
    claims_stopped: number;
    claims_stopped_why: Record<string, number>;
    known_deductions_explained: number;
    coverage_queries_drafted: number;
    escalations_drafted: number;
    cases_routed_away: number;
  };
}
