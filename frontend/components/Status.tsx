// Loading, error and badge pieces used on every screen.

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <p role="status" className="flex items-center gap-2 text-muted">
      <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-line border-t-pine motion-reduce:animate-none" />
      {label}…
    </p>
  );
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="rounded-md border border-stamp/40 bg-stamp-wash p-4 text-ink">
      <p>{message}</p>
      {onRetry && (
        <button type="button" onClick={onRetry} className="mt-2 text-sm font-medium text-stamp underline">
          Try again
        </button>
      )}
    </div>
  );
}

export function UnverifiedBadge({ title = "Based on a rule or document not yet checked against the original source." }: { title?: string }) {
  return (
    <span
      title={title}
      className="inline-flex items-center gap-1 rounded border border-amber/50 bg-amber-wash px-1.5 py-0.5 text-xs font-medium text-amber"
    >
      <span aria-hidden="true">⚠</span> Not yet verified
    </span>
  );
}

export function ExampleBadge({ note }: { note?: string | null }) {
  return (
    <span
      title={note ?? "Example data for the demo. Not a real person, policy or company."}
      className="inline-flex items-center rounded border border-line bg-white px-1.5 py-0.5 text-xs font-medium text-muted"
    >
      Example case
    </span>
  );
}
