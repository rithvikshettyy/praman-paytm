"use client";

// The chat on every page. Text, voice and documents all go to the backend, which runs the
// same conversation as WhatsApp; this widget only shows what comes back.

import { useEffect, useRef, useState, type FormEvent } from "react";

import { UnverifiedBadge } from "@/components/Status";
import { api, errorText, mediaUrl } from "@/lib/api";
import { LANGUAGES, useSession } from "@/lib/session";
import type { ChatMessage, ChatReply, Journey } from "@/lib/types";

interface Item {
  id: number;
  from: "you" | "praman";
  text: string;
  unverified?: boolean;
  citations?: ChatMessage["citations"];
  audioUrl?: string | null;
  files?: string[]; // documents she sent with this message
  feedback?: ChatMessage["feedback"]; // ask "Did this solve it?" under this message
  rated?: boolean; // she has answered it
  buttons?: { id: string; title: string }[]; // quick replies; shown only under the newest message
}

// The start options of the chat. Each one tells the backend which conversation to have.
const START_OPTIONS: { journey: Journey; title: string; hint: string }[] = [
  { journey: "find", title: "Find a policy", hint: "Answer a few questions and see options, for you or for your parents." },
  { journey: "check", title: "Check my policy", hint: "Send your policy and ask me anything about it." },
  {
    journey: "complain",
    title: "Complaint",
    hint: "Something went wrong? Tell me. If I cannot settle it, I will register it for our team.",
  },
];

let nextId = 1;

// Line icons drawn in currentColor, so they stay sharp at any size and follow the button's colour.
function PaperclipIcon({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={`${className} shrink-0 fill-none stroke-current`} strokeWidth="2">
      <path
        d="M21 11.5l-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l8.6-8.6a3.7 3.7 0 0 1 5.2 5.2l-8.6 8.6a1.8 1.8 0 0 1-2.6-2.6l7.9-7.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-5 w-5 fill-none stroke-current" strokeWidth="2">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-4 w-4 fill-current">
      <rect x="5" y="5" width="14" height="14" rx="2" />
    </svg>
  );
}

const ICON_BUTTON = "flex h-[42px] w-[42px] shrink-0 items-center justify-center rounded-md border disabled:opacity-50";

export function ChatWidget() {
  const {
    sessionId,
    language,
    setLanguage,
    chatOpen,
    openChat,
    closeChat,
    chatContext,
    clearChatContext,
    renewCase,
    ensureConsent,
  } = useSession();
  const [items, setItems] = useState<Item[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [speak, setSpeak] = useState(true);
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [heard, setHeard] = useState(false); // the box holds words from a recording, not yet sent
  const [attached, setAttached] = useState<File[]>([]); // picked, waiting to go with her message
  const [showMenu, setShowMenu] = useState(true); // the three start options are on screen
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const listEnd = useRef<HTMLDivElement | null>(null);
  const input = useRef<HTMLInputElement | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null); // the voice reply playing now, if any
  // Bumped when she closes the chat: anything still on its way from before is dropped.
  const round = useRef(0);

  function stopVoice() {
    audio.current?.pause();
    audio.current = null;
  }

  function play(url: string) {
    stopVoice(); // one voice at a time: a new reply cuts off the last one
    const next = new Audio(mediaUrl(url));
    audio.current = next;
    next.play().catch(() => {
      // Autoplay can be blocked; the play button on the message still works.
    });
  }

  // Closing the chat stops the voice and any recording, and starts the next chat empty.
  function closeAndReset() {
    round.current += 1;
    stopVoice();
    if (recorder.current?.state === "recording") recorder.current.stop();
    setItems([]);
    setText("");
    setAttached([]);
    setError(null);
    setHeard(false);
    setBusy(false);
    setTranscribing(false);
    setShowMenu(true);
    clearChatContext();
    closeChat();
  }

  // Leaving the page (or the widget unmounting) must not leave a voice talking either.
  useEffect(() => stopVoice, []);

  useEffect(() => {
    listEnd.current?.scrollIntoView({ block: "end" });
  }, [items, busy]);

  useEffect(() => {
    if (chatOpen) input.current?.focus();
  }, [chatOpen]);

  // Open, the chat takes the right side of the page and the page moves left to make room (globals.css).
  useEffect(() => {
    document.documentElement.classList.toggle("chat-open", chatOpen);
    return () => document.documentElement.classList.remove("chat-open");
  }, [chatOpen]);

  function showReply(reply: ChatReply, autoplay: boolean) {
    const answers: Item[] = reply.messages.map((m) => ({
      id: nextId++,
      from: "praman",
      text: m.text,
      unverified: m.unverified,
      citations: m.citations,
      audioUrl: m.audio_url,
      feedback: m.feedback,
      buttons: m.buttons,
    }));
    setItems((current) => [...current, ...answers]);
    if (reply.case_id === null) {
      renewCase(); // she deleted everything: the next message starts a new case
      setShowMenu(true);
    }
    const firstAudio = answers.find((a) => a.audioUrl)?.audioUrl;
    if (autoplay && firstAudio) play(firstAudio);
  }

  // "Did this solve it?": a Yes is counted for the distributor; a No asks for a person, who gets a brief.
  async function rate(item: Item, solved: boolean) {
    if (!item.feedback || item.rated || !sessionId) return;
    const mine = round.current;
    setItems((current) => current.map((i) => (i.id === item.id ? { ...i, rated: true } : i)));
    setError(null);
    try {
      const reply = await api<ChatReply>("/api/feedback", {
        method: "POST",
        json: { session_id: sessionId, answer_id: item.feedback.answer_id, solved, language },
      });
      if (mine === round.current) showReply(reply, false);
    } catch (err) {
      if (mine !== round.current) return;
      setItems((current) => current.map((i) => (i.id === item.id ? { ...i, rated: false } : i)));
      setError(errorText(err));
    }
  }

  async function sendText(event: FormEvent) {
    event.preventDefault();
    const message = text.trim();
    if (!sessionId || busy) return;
    if (attached.length) return sendWithFiles(attached, message);
    if (!message) return;
    await sendMessage(message);
  }

  // A start option: the backend remembers the pick and opens the conversation (for Find a policy, its first question).
  async function startJourney(option: (typeof START_OPTIONS)[number]) {
    if (!sessionId || busy) return;
    const mine = round.current;
    setItems((current) => [...current, { id: nextId++, from: "you", text: option.title }]);
    setShowMenu(false);
    setBusy(true);
    setError(null);
    try {
      const reply = await api<ChatReply>("/api/journey", {
        method: "POST",
        json: { session_id: sessionId, journey: option.journey, language },
      });
      if (mine === round.current) showReply(reply, false);
    } catch (err) {
      if (mine === round.current) setError(errorText(err));
    } finally {
      if (mine === round.current) setBusy(false);
    }
  }

  // "shown" is what her bubble says when she tapped a quick reply: the button's title, not its id.
  async function sendMessage(message: string, shown?: string) {
    const mine = round.current;
    setItems((current) => [...current, { id: nextId++, from: "you", text: shown ?? message }]);
    setText("");
    setHeard(false);
    setShowMenu(false);
    setBusy(true);
    setError(null);
    try {
      const reply = await api<ChatReply>("/api/chat", {
        method: "POST",
        json: {
          session_id: sessionId,
          text: message,
          language,
          insurer: chatContext?.insurer,
          product: chatContext?.product,
          speak,
        },
      });
      if (mine === round.current) showReply(reply, speak);
    } catch (err) {
      if (mine === round.current) setError(errorText(err));
    } finally {
      if (mine === round.current) setBusy(false);
    }
  }

  // A recording becomes words in the text box; she checks or corrects them and presses Enter to send.
  async function transcribeVoice(blob: Blob, type: string) {
    const form = new FormData();
    form.append("audio", blob, type.includes("mp4") ? "voice.mp4" : type.includes("ogg") ? "voice.ogg" : "voice.webm");
    form.append("language", language);
    const mine = round.current;
    setTranscribing(true);
    setError(null);
    try {
      const { transcript } = await api<{ transcript: string }>("/api/transcribe", { method: "POST", body: form });
      if (mine !== round.current) return;
      setText((current) => (current.trim() ? `${current.trim()} ${transcript}` : transcript));
      setHeard(true);
      setTimeout(() => input.current?.focus(), 0);
    } catch (err) {
      if (mine === round.current) setError(errorText(err));
    } finally {
      if (mine === round.current) setTranscribing(false);
    }
  }

  // Picked files wait above the box; they go with her message when she presses Enter or Send.
  function addFiles(list: FileList | null) {
    const picked = Array.from(list ?? []);
    if (fileInput.current) fileInput.current.value = "";
    if (!picked.length) return;
    setAttached((current) => [
      ...current,
      ...picked.filter((f) => !current.some((c) => c.name === f.name && c.size === f.size)),
    ]);
    setError(null);
    setTimeout(() => input.current?.focus(), 0);
  }

  async function sendWithFiles(files: File[], message: string) {
    if (!sessionId) return;
    setError(null);
    // Nothing is read without her yes: the same consent modal as the readiness page.
    if (!(await ensureConsent())) {
      setError("Your documents were not sent. They are read only after you agree.");
      return;
    }
    const form = new FormData();
    files.forEach((file) => form.append("files", file, file.name));
    form.append("session_id", sessionId);
    form.append("language", language);
    form.append("speak", String(speak));
    if (message) form.append("text", message);
    if (chatContext) {
      form.append("insurer", chatContext.insurer);
      form.append("product", chatContext.product);
    }
    const mine = round.current;
    setItems((current) => [...current, { id: nextId++, from: "you", text: message, files: files.map((f) => f.name) }]);
    setText("");
    setAttached([]);
    setHeard(false);
    setBusy(true);
    try {
      const reply = await api<ChatReply>("/api/chat/upload", { method: "POST", body: form });
      if (mine === round.current) showReply(reply, speak);
    } catch (err) {
      if (mine === round.current) setError(errorText(err));
    } finally {
      if (mine === round.current) setBusy(false);
    }
  }

  async function toggleRecording() {
    if (recording) {
      recorder.current?.stop();
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError("This browser can't record here. Type your question instead.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const preferred = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find((t) =>
        MediaRecorder.isTypeSupported(t),
      );
      const rec = preferred ? new MediaRecorder(stream, { mimeType: preferred }) : new MediaRecorder(stream);
      chunks.current = [];
      const mine = round.current;
      rec.ondataavailable = (event) => {
        if (event.data.size) chunks.current.push(event.data);
      };
      rec.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        setRecording(false);
        const type = rec.mimeType || "audio/webm";
        const blob = new Blob(chunks.current, { type });
        if (blob.size && mine === round.current) void transcribeVoice(blob, type);
      };
      recorder.current = rec;
      rec.start();
      setRecording(true);
      setError(null);
    } catch {
      setError("Microphone access was refused. Allow it in the browser, or type your question.");
    }
  }

  if (!chatOpen) {
    return (
      <button
        type="button"
        onClick={() => openChat()}
        className="fixed bottom-5 right-5 z-40 flex items-center gap-2 rounded-full bg-pine px-5 py-3 font-medium text-white shadow-lg hover:bg-pine-dark"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true" className="h-5 w-5 fill-none stroke-current" strokeWidth="2">
          <path d="M4 5h16v11H9l-5 4z" strokeLinejoin="round" />
        </svg>
        Ask Praman
      </button>
    );
  }

  return (
    <section
      aria-label="Chat with Praman"
      className="fixed inset-0 z-40 flex flex-col bg-paper shadow-2xl lg:bottom-4 lg:left-auto lg:right-4 lg:top-[calc(var(--header-height)+1rem)] lg:w-[var(--chat-width)] lg:overflow-hidden lg:rounded-xl lg:border lg:border-line"
    >
      <header className="flex items-center gap-2 border-b border-line px-4 py-3">
        <h2 className="flex-1 whitespace-nowrap text-lg font-semibold">Ask Praman</h2>
        <label className="sr-only" htmlFor="chat-language">
          Language
        </label>
        <select
          id="chat-language"
          translate="no"
          value={language}
          onChange={(event) => setLanguage(event.target.value)}
          className="min-w-0 max-w-[9.5rem] rounded-md border border-line bg-white px-2 py-1 text-sm"
        >
          {LANGUAGES.map((l) => (
            <option key={l.code} value={l.code}>
              {l.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => setSpeak((s) => !s)}
          aria-pressed={speak}
          title={speak ? "Voice replies on" : "Voice replies off"}
          className={`rounded-md border px-2 py-1 text-sm ${speak ? "border-pine bg-pine-wash text-pine-dark" : "border-line text-muted"}`}
        >
          {speak ? "Voice on" : "Voice off"}
        </button>
        <button type="button" onClick={closeAndReset} aria-label="Close chat" className="px-2 text-2xl leading-none text-muted hover:text-ink">
          ×
        </button>
      </header>

      {chatContext && (
        <p className="flex items-center justify-between gap-2 border-b border-line bg-pine-wash px-4 py-2 text-sm text-pine-dark">
          <span>Asking about: {chatContext.label}</span>
          <button type="button" onClick={clearChatContext} className="underline">
            Clear
          </button>
        </p>
      )}

      <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4" aria-live="polite">
        {items.length === 0 && (
          <p className="text-[15px] text-muted">
            Choose what you want to do, or just type. You can send any insurance policy (health, bike, car, life, travel
            or home) with the paperclip, and you can also speak: press the microphone.
          </p>
        )}
        {items.map((item) => (
          <div key={item.id} className={`flex flex-col gap-1.5 ${item.from === "you" ? "items-end" : "items-start"}`}>
            <div
              translate="no"
              className={`max-w-[85%] rounded-lg px-3 py-2 text-[15px] leading-relaxed ${
                item.from === "you" ? "bg-pine text-white" : "border border-line bg-white text-ink"
              }`}
            >
              {item.files?.map((name) => (
                <p key={name} className="mb-1 flex items-center gap-1.5 text-sm font-medium">
                  <PaperclipIcon className="h-4 w-4" />
                  <span className="break-all">{name}</span>
                </p>
              ))}
              {item.text && <p className="whitespace-pre-wrap">{item.text}</p>}
              {(item.unverified || item.audioUrl || (item.citations && item.citations.length > 0)) && (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {item.unverified && <UnverifiedBadge />}
                  {[...(item.citations ?? [])]
                    .sort((a, b) => a.page - b.page)
                    .map((c) => (
                      <span
                        key={c.label}
                        className="rounded bg-paper px-1.5 py-0.5 text-xs text-muted"
                        title={c.insurer === "Your document" ? "From the document you sent" : c.source_url || c.label}
                      >
                        {/* Her own document needs only the page; a policy wording or regulation is named. */}
                        {c.insurer === "Your document"
                          ? `Page ${c.page}`
                          : `${c.insurer}, ${c.doc_type.replace(/_/g, " ")}, page ${c.page}`}
                      </span>
                    ))}
                  {item.audioUrl && (
                    <button type="button" onClick={() => play(item.audioUrl!)} className="text-xs font-medium text-pine underline">
                      Play
                    </button>
                  )}
                </div>
              )}
            </div>
            {item.buttons && item.buttons.length > 0 && item.id === items[items.length - 1]?.id && !busy && (
              <div className="flex max-w-[95%] flex-wrap gap-2 text-sm" role="group" aria-label="Quick replies">
                {item.buttons.map((b) => (
                  <button
                    key={b.id}
                    type="button"
                    onClick={() => void sendMessage(b.id, b.title)}
                    className="rounded-full border border-pine/40 bg-white px-3 py-1.5 font-medium text-pine-dark hover:bg-pine hover:text-white"
                  >
                    {b.title}
                  </button>
                ))}
              </div>
            )}
            {item.feedback && !item.rated && (
              <div className="flex max-w-[85%] flex-wrap items-center gap-2 text-sm">
                <span className="text-muted">
                  {item.feedback.kind === "answer" ? "Did this solve it?" : "Want a person to look at this?"}
                </span>
                {item.feedback.kind === "answer" && (
                  <button
                    type="button"
                    onClick={() => void rate(item, true)}
                    className="rounded-full border border-pine/40 bg-pine-wash px-3 py-1 font-medium text-pine-dark hover:bg-pine hover:text-white"
                  >
                    Yes
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => void rate(item, false)}
                  className="rounded-full border border-line bg-white px-3 py-1 font-medium text-ink hover:border-pine hover:text-pine"
                >
                  {item.feedback.kind === "answer" ? "No, I need help" : "Yes, get me help"}
                </button>
              </div>
            )}
          </div>
        ))}
        {showMenu && (
          <div className="grid gap-2" role="group" aria-label="What would you like to do?">
            {START_OPTIONS.map((option) => (
              <button
                key={option.journey}
                type="button"
                onClick={() => void startJourney(option)}
                disabled={busy || !sessionId}
                className="rounded-lg border border-line bg-white p-3 text-left hover:border-pine disabled:opacity-50"
              >
                <span className="block font-semibold text-ink">{option.title}</span>
                <span className="block text-sm text-muted">{option.hint}</span>
              </button>
            ))}
          </div>
        )}
        {!showMenu && items.length > 0 && !busy && (
          <button type="button" onClick={() => setShowMenu(true)} className="text-sm text-muted underline hover:text-ink">
            Change what I am doing
          </button>
        )}
        {busy && <p className="text-sm text-muted">Praman is replying…</p>}
        {transcribing && <p className="text-sm text-muted">Writing down what you said…</p>}
        {error && (
          <p role="alert" className="rounded-md bg-stamp-wash px-3 py-2 text-sm text-stamp">
            {error}
          </p>
        )}
        <div ref={listEnd} />
      </div>

      {attached.length > 0 && (
        <ul className="flex flex-wrap gap-2 border-t border-line px-3 pt-3" aria-label="Documents to send">
          {attached.map((file) => (
            <li
              key={`${file.name}-${file.size}`}
              className="flex max-w-full items-center gap-1.5 rounded-md border border-line bg-white py-1 pl-2 pr-1 text-sm text-ink"
            >
              <PaperclipIcon className="h-4 w-4 text-pine" />
              <span className="max-w-[220px] truncate" title={file.name}>
                {file.name}
              </span>
              <button
                type="button"
                onClick={() => setAttached((current) => current.filter((f) => f !== file))}
                aria-label={`Remove ${file.name}`}
                className="rounded px-1.5 text-lg leading-none text-muted hover:text-stamp"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {heard && text.trim() && (
        <p className="border-t border-line bg-pine-wash px-4 py-2 text-sm text-pine-dark">
          This is what I heard. Correct it if needed, then press Enter to send.
        </p>
      )}
      <form onSubmit={sendText} className="flex items-center gap-2 border-t border-line p-3">
        <label htmlFor="chat-text" className="sr-only">
          Your message
        </label>
        <input
          id="chat-text"
          ref={input}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={
            recording
              ? "Recording… press stop when done"
              : transcribing
                ? "Writing down what you said…"
                : attached.length
                  ? "Add a message (optional), then press Enter"
                  : "Type a message"
          }
          disabled={recording || transcribing}
          className="min-w-0 flex-1 rounded-md border border-line bg-white px-3 py-2"
        />
        <input
          ref={fileInput}
          type="file"
          multiple
          accept="application/pdf,image/jpeg,image/png,image/webp,image/tiff"
          className="hidden"
          onChange={(event) => addFiles(event.target.files)}
        />
        <button
          type="button"
          onClick={() => fileInput.current?.click()}
          disabled={busy || recording || !sessionId}
          aria-label="Send a document"
          title="Send a photo or PDF of a claim document"
          className={`${ICON_BUTTON} border-line bg-white text-ink hover:border-pine hover:text-pine`}
        >
          <PaperclipIcon />
        </button>
        <button
          type="button"
          onClick={toggleRecording}
          disabled={busy || transcribing || !sessionId}
          aria-pressed={recording}
          aria-label={recording ? "Stop recording" : "Record a voice message"}
          title={recording ? "Stop recording" : "Record a voice message"}
          className={`${ICON_BUTTON} ${
            recording ? "border-stamp bg-stamp text-white" : "border-line bg-white text-ink hover:border-pine hover:text-pine"
          }`}
        >
          {recording ? <StopIcon /> : <MicIcon />}
        </button>
        <button
          type="submit"
          disabled={busy || (!text.trim() && !attached.length) || !sessionId}
          className="rounded-md bg-pine px-4 py-2 font-medium text-white hover:bg-pine-dark disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </section>
  );
}
