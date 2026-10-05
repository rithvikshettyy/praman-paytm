// Opens a WhatsApp chat with Praman's number. wa.me needs the country code and digits only.
const NUMBER = (process.env.NEXT_PUBLIC_WHATSAPP_NUMBER || "919833875293").replace(/\D/g, "");

export function whatsappLink(text = "Hi"): string {
  return `https://wa.me/${NUMBER}?text=${encodeURIComponent(text)}`;
}

export function WhatsAppButton({ compact = false }: { compact?: boolean }) {
  return (
    <a
      href={whatsappLink()}
      target="_blank"
      rel="noopener noreferrer"
      className={`inline-flex items-center gap-2 rounded-md bg-pine font-medium text-white hover:bg-pine-dark ${
        compact ? "px-3 py-1.5 text-sm" : "px-5 py-3 text-base"
      }`}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true" className={compact ? "h-4 w-4" : "h-5 w-5"} fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round">
        <path d="M4 5h16v11H9l-5 4z" />
      </svg>
      <span>
        Chat on <span translate="no">WhatsApp</span>
      </span>
    </a>
  );
}
