"use client";

// One browser = one case, like one WhatsApp number. Holds the session id, the case id the
// backend gives it, her chosen language, the chat panel, and consent before any upload.

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { api, errorText } from "@/lib/api";

export const LANGUAGES = [
  { code: "mr-IN", label: "मराठी (Marathi)" },
  { code: "hi-IN", label: "हिन्दी (Hindi)" },
  { code: "en-IN", label: "English" },
  { code: "ta-IN", label: "தமிழ் (Tamil)" },
  { code: "bn-IN", label: "বাংলা (Bengali)" },
  { code: "gu-IN", label: "ગુજરાતી (Gujarati)" },
  { code: "te-IN", label: "తెలుగు (Telugu)" },
  { code: "kn-IN", label: "ಕನ್ನಡ (Kannada)" },
] as const;

export interface ChatContext {
  insurer: string;
  product: string;
  label: string;
}

interface SessionValue {
  sessionId: string | null;
  caseId: string | null;
  caseError: string | null;
  language: string;
  setLanguage: (code: string) => void;
  renewCase: () => void;
  chatOpen: boolean;
  chatContext: ChatContext | null;
  openChat: (context?: ChatContext | null) => void;
  closeChat: () => void;
  clearChatContext: () => void;
  ensureConsent: () => Promise<boolean>;
}

const SessionContext = createContext<SessionValue | null>(null);

const SESSION_KEY = "praman.session";
const LANGUAGE_KEY = "praman.language";
const consentKey = (caseId: string) => `praman.consent.${caseId}`;

function readStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Private windows may refuse storage; the session then lasts for this tab only.
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [caseId, setCaseId] = useState<string | null>(null);
  const [caseError, setCaseError] = useState<string | null>(null);
  const [language, setLanguageState] = useState("en-IN");
  const [chatOpen, setChatOpen] = useState(false);
  const [chatContext, setChatContext] = useState<ChatContext | null>(null);
  const [consentAsked, setConsentAsked] = useState(false);
  const [consentBusy, setConsentBusy] = useState(false);
  const [consentError, setConsentError] = useState<string | null>(null);
  const consentResolver = useRef<((granted: boolean) => void) | null>(null);
  const [caseVersion, setCaseVersion] = useState(0);

  // Read the stored session once, in the browser.
  useEffect(() => {
    let id = readStorage(SESSION_KEY);
    if (!id) {
      id = crypto.randomUUID();
      writeStorage(SESSION_KEY, id);
    }
    const storedLanguage = readStorage(LANGUAGE_KEY);
    queueMicrotask(() => {
      setSessionId(id);
      if (storedLanguage) setLanguageState(storedLanguage);
    });
  }, []);

  // Ask the backend which case belongs to this session (it opens one on first contact).
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    api<{ case_id: string }>("/api/session", { method: "POST", json: { session_id: sessionId } })
      .then((body) => {
        if (!cancelled) {
          setCaseId(body.case_id);
          setCaseError(null);
        }
      })
      .catch((error) => {
        if (!cancelled) setCaseError(errorText(error));
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, caseVersion]);

  const setLanguage = useCallback((code: string) => {
    setLanguageState(code);
    writeStorage(LANGUAGE_KEY, code);
  }, []);

  const renewCase = useCallback(() => setCaseVersion((v) => v + 1), []);

  const openChat = useCallback((context?: ChatContext | null) => {
    if (context !== undefined) setChatContext(context);
    setChatOpen(true);
  }, []);

  const ensureConsent = useCallback((): Promise<boolean> => {
    if (caseId && readStorage(consentKey(caseId)) === "granted") return Promise.resolve(true);
    setConsentError(null);
    setConsentAsked(true);
    return new Promise((resolve) => {
      consentResolver.current = resolve;
    });
  }, [caseId]);

  async function answerConsent(granted: boolean) {
    if (!caseId) {
      setConsentError("Your case is not ready yet. Check that the backend is running, then try again.");
      return;
    }
    setConsentBusy(true);
    try {
      await api("/api/consent", { method: "POST", json: { case_id: caseId, scope: "read_documents", granted } });
      if (granted) {
        await api("/api/consent", { method: "POST", json: { case_id: caseId, scope: "store_fields", granted } });
      }
      writeStorage(consentKey(caseId), granted ? "granted" : "declined");
      setConsentAsked(false);
      consentResolver.current?.(granted);
      consentResolver.current = null;
    } catch (error) {
      setConsentError(errorText(error));
    } finally {
      setConsentBusy(false);
    }
  }

  function cancelConsent() {
    setConsentAsked(false);
    consentResolver.current?.(false);
    consentResolver.current = null;
  }

  const value: SessionValue = {
    sessionId,
    caseId,
    caseError,
    language,
    setLanguage,
    renewCase,
    chatOpen,
    chatContext,
    openChat,
    closeChat: () => setChatOpen(false),
    clearChatContext: () => setChatContext(null),
    ensureConsent,
  };

  return (
    <SessionContext.Provider value={value}>
      {children}
      {consentAsked && (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-ink/40 p-4 sm:items-center" role="presentation">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="consent-title"
            className="w-full max-w-md rounded-lg border border-line bg-paper p-6 shadow-xl"
          >
            <h2 id="consent-title" className="text-xl font-semibold text-ink">
              Before I read your documents
            </h2>
            <ul className="mt-4 space-y-2 text-[15px] leading-relaxed text-ink">
              <li>To read a document, I send it to our document-reading service, Sarvam.</li>
              <li>I keep only the details your claim needs, not the file itself.</li>
              <li>
                While we chat, I hold a document&rsquo;s text in memory so I can answer questions about it. It is never
                saved, and goes when you delete everything.
              </li>
              <li>Account, Aadhaar, PAN and policy numbers are hidden in any text I send out.</li>
              <li>You can delete everything at any time from your case page.</li>
            </ul>
            {consentError && <p className="mt-4 text-sm text-stamp">{consentError}</p>}
            <div className="mt-6 flex flex-wrap gap-3">
              <button
                type="button"
                disabled={consentBusy}
                onClick={() => answerConsent(true)}
                className="rounded-md bg-pine px-4 py-2.5 font-medium text-white hover:bg-pine-dark disabled:opacity-60"
              >
                I agree
              </button>
              <button
                type="button"
                disabled={consentBusy}
                onClick={() => answerConsent(false)}
                className="rounded-md border border-line px-4 py-2.5 font-medium text-ink hover:bg-white disabled:opacity-60"
              >
                Don&apos;t read my documents
              </button>
              <button type="button" onClick={cancelConsent} className="px-2 py-2.5 text-sm text-muted underline">
                Not now
              </button>
            </div>
          </div>
        </div>
      )}
    </SessionContext.Provider>
  );
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside SessionProvider");
  return value;
}
