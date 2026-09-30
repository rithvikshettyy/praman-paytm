"use client";

// The chat on every page. Text and voice both go to the backend, which runs the same
// conversation as WhatsApp; this widget only shows what comes back.

import { useEffect, useRef, useState, type FormEvent } from "react";

import { UnverifiedBadge } from "@/components/Status";
import { api, errorText, mediaUrl } from "@/lib/api";
import { LANGUAGES, useSession } from "@/lib/session";
import type { ChatMessage, ChatReply } from "@/lib/types";

interface Item {
  id: number;
  from: "you" | "praman";
  text: string;
  unverified?: boolean;
  citations?: ChatMessage["citations"];
  audioUrl?: string | null;
}

let nextId = 1;

function play(url: string) {
  const audio = new Audio(mediaUrl(url));
  audio.play().catch(() => {
    // Autoplay can be blocked; the play button on the message still works.
  });
}

export function ChatWidget() {
  const { sessionId, language, setLanguage, chatOpen, openChat, closeChat, chatContext, clearChatContext, renewCase } =
    useSession();
  const [items, setItems] = useState<Item[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [speak, setSpeak] = useState(true);
  const [recording, setRecording] = useState(false);
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const listEnd = useRef<HTMLDivElement | null>(null);
  const input = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    listEnd.current?.scrollIntoView({ block: "end" });
  }, [items, busy]);

  useEffect(() => {
    if (chatOpen) input.current?.focus();
  }, [chatOpen]);

  function showReply(reply: ChatReply, autoplay: boolean) {
    const answers: Item[] = reply.messages.map((m) => ({
      id: nextId++,
      from: "praman",
      text: m.text,
      unverified: m.unverified,
      citations: m.citations,
      audioUrl: m.audio_url,
    }));
    setItems((current) => [...current, ...answers]);
    if (reply.case_id === null) renewCase(); // she deleted everything: the next message starts a new case
    const firstAudio = answers.find((a) => a.audioUrl)?.audioUrl;
    if (autoplay && firstAudio) play(firstAudio);
  }

  async function sendText(event: FormEvent) {
    event.preventDefault();
    const message = text.trim();
    if (!message || !sessionId || busy) return;
    setItems((current) => [...current, { id: nextId++, from: "you", text: message }]);
    setText("");
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
      showReply(reply, speak);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function sendVoice(blob: Blob, type: string) {
    if (!sessionId) return;
    const form = new FormData();
    form.append("audio", blob, type.includes("mp4") ? "voice.mp4" : type.includes("ogg") ? "voice.ogg" : "voice.webm");
    form.append("session_id", sessionId);
    form.append("language", language);
    if (chatContext) {
      form.append("insurer", chatContext.insurer);
      form.append("product", chatContext.product);
    }
    setBusy(true);
    setError(null);
    try {
      const reply = await api<ChatReply>("/api/voice", { method: "POST", body: form });
      setItems((current) => [...current, { id: nextId++, from: "you", text: `🎙 ${reply.transcript ?? ""}` }]);
      showReply(reply, true);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
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
      rec.ondataavailable = (event) => {
        if (event.data.size) chunks.current.push(event.data);
      };
      rec.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        setRecording(false);
        const type = rec.mimeType || "audio/webm";
        const blob = new Blob(chunks.current, { type });
        if (blob.size) void sendVoice(blob, type);
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
      className="fixed inset-x-0 bottom-0 z-40 flex max-h-[85vh] flex-col border border-line bg-paper shadow-2xl sm:inset-x-auto sm:bottom-5 sm:right-5 sm:w-[400px] sm:rounded-lg"
    >
      <header className="flex items-center gap-2 border-b border-line px-4 py-3">
        <h2 className="flex-1 text-lg font-semibold">Ask Praman</h2>
        <label className="sr-only" htmlFor="chat-language">
          Language
        </label>
        <select
          id="chat-language"
          value={language}
          onChange={(event) => setLanguage(event.target.value)}
          className="rounded-md border border-line bg-white px-2 py-1 text-sm"
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
        <button type="button" onClick={closeChat} aria-label="Close chat" className="px-2 text-2xl leading-none text-muted hover:text-ink">
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
            Ask about your policy, say what went wrong with a claim, or ask what documents are still missing. You can
            also speak: press the microphone.
          </p>
        )}
        {items.map((item) => (
          <div key={item.id} className={item.from === "you" ? "flex justify-end" : "flex justify-start"}>
            <div
              className={`max-w-[85%] rounded-lg px-3 py-2 text-[15px] leading-relaxed ${
                item.from === "you" ? "bg-pine text-white" : "border border-line bg-white text-ink"
              }`}
            >
              <p className="whitespace-pre-wrap">{item.text}</p>
              {(item.unverified || item.audioUrl || (item.citations && item.citations.length > 0)) && (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {item.unverified && <UnverifiedBadge />}
                  {item.citations?.map((c) => (
                    <span key={c.label} className="rounded bg-paper px-1.5 py-0.5 text-xs text-muted" title={c.source_url || c.label}>
                      {c.insurer}, {c.doc_type.replace(/_/g, " ")}, page {c.page}
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
          </div>
        ))}
        {busy && <p className="text-sm text-muted">Praman is replying…</p>}
        {error && (
          <p role="alert" className="rounded-md bg-stamp-wash px-3 py-2 text-sm text-stamp">
            {error}
          </p>
        )}
        <div ref={listEnd} />
      </div>

      <form onSubmit={sendText} className="flex items-center gap-2 border-t border-line p-3">
        <label htmlFor="chat-text" className="sr-only">
          Your message
        </label>
        <input
          id="chat-text"
          ref={input}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={recording ? "Recording… press stop to send" : "Type a message"}
          disabled={recording}
          className="min-w-0 flex-1 rounded-md border border-line bg-white px-3 py-2"
        />
        <button
          type="button"
          onClick={toggleRecording}
          disabled={busy || !sessionId}
          aria-pressed={recording}
          aria-label={recording ? "Stop recording and send" : "Record a voice message"}
          className={`rounded-md border px-3 py-2 text-sm font-medium ${
            recording ? "border-stamp bg-stamp text-white" : "border-line bg-white text-ink"
          }`}
        >
          {recording ? "Stop" : "🎙"}
        </button>
        <button
          type="submit"
          disabled={busy || !text.trim() || !sessionId}
          className="rounded-md bg-pine px-4 py-2 font-medium text-white hover:bg-pine-dark disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </section>
  );
}
