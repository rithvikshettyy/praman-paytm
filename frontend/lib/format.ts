// Display formatting only: every number arrives computed by the backend.

const rupees = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });

export function inr(amount: number | null | undefined): string {
  return amount === null || amount === undefined ? "Not known yet" : rupees.format(amount);
}

export function longDate(iso: string | null | undefined): string {
  if (!iso) return "Not known yet";
  const date = new Date(`${iso.slice(0, 10)}T00:00:00`);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleDateString("en-IN", { day: "numeric", month: "long", year: "numeric" });
}

export const OUTCOME_LABEL: Record<string, string> = {
  file: "Ready to file",
  do_not_file_yet: "Do not file yet",
  file_with_known_deduction: "File, with a known deduction",
  facts_pending: "Needs your answers",
  no_verdict: "No readiness check",
};

export const PRODUCT_LABEL: Record<string, string> = {
  health_policy: "Health policy",
  motor_policy: "Motor policy (bike, car)",
  life_policy: "Life policy",
  travel_policy: "Travel policy",
  home_policy: "Home policy",
  other_insurance: "Other insurance",
  merchant_loan: "Merchant loan",
};

export function humanise(value: string | null | undefined): string {
  if (!value) return "Not known yet";
  const last = value.split("/").pop() ?? value;
  const words = last.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
