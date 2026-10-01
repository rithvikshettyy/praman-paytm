"use client";

// The whole site in her language. After React renders, this reads the page's visible text (and
// placeholders, tooltips and screen-reader labels), asks the backend for translations (Sarvam,
// cached there on disk and here per browser), and swaps them in. The English originals are kept,
// so switching back to English restores them. It keeps watching the page, so text that arrives
// later (a verdict, a checklist) is translated too.
//
// Left alone: anything inside [translate="no"] (chat replies already in her language, her own
// words, the language pickers, the name Praman), and text with no letters (amounts, numbers).

import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { useSession } from "@/lib/session";

const ENGLISH = "en-IN";
const SKIP = "script, style, noscript, textarea, code, pre, [translate='no'], [contenteditable='true']";
const ATTRIBUTES = ["placeholder", "title", "aria-label"] as const;
const BATCH = 100; // the backend takes up to 300 per call; smaller batches show progress sooner
const HAS_LETTER = /\p{L}/u;

type Shown = { en: string; shown: string };
const textOriginals = new WeakMap<Text, Shown>();
const attributeOriginals = new WeakMap<Element, Record<string, Shown>>();
const caches = new Map<string, Map<string, string>>(); // language -> English -> translation

function cacheFor(language: string): Map<string, string> {
  let cache = caches.get(language);
  if (!cache) {
    cache = new Map();
    try {
      const saved = window.localStorage.getItem(`praman.page.${language}`);
      if (saved) for (const [en, out] of Object.entries(JSON.parse(saved) as Record<string, string>)) cache.set(en, out);
    } catch {
      // Private windows may refuse storage; the backend's cache still saves the cost.
    }
    caches.set(language, cache);
  }
  return cache;
}

function saveCache(language: string) {
  try {
    window.localStorage.setItem(`praman.page.${language}`, JSON.stringify(Object.fromEntries(cacheFor(language))));
  } catch {
    // As above: only slower next time.
  }
}

// The English a slot holds: what React last wrote, unless it still shows our own translation.
function englishOf(current: string, seen: Shown | undefined): string {
  return seen && current === seen.shown ? seen.en : current;
}

// Leading and trailing spaces are layout, not words: translate the core, keep the spacing.
function split(text: string): [string, string, string] {
  const core = text.trim();
  const start = text.indexOf(core);
  return [text.slice(0, start), core, text.slice(start + core.length)];
}

type Slot = { en: string; write: (shown: string) => void };

function slots(): Slot[] {
  const found: Slot[] = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    const text = node;
    if (!text.parentElement || text.parentElement.closest(SKIP)) continue;
    const en = englishOf(text.nodeValue ?? "", textOriginals.get(text));
    if (!HAS_LETTER.test(en)) continue;
    found.push({
      en,
      write: (shown) => {
        textOriginals.set(text, { en, shown });
        if (text.nodeValue !== shown) text.nodeValue = shown;
      },
    });
  }
  const selector = ATTRIBUTES.map((a) => `[${a}]`).join(",");
  for (const element of Array.from(document.body.querySelectorAll(selector))) {
    if (element.closest(SKIP)) continue;
    const seen = attributeOriginals.get(element) ?? {};
    for (const attribute of ATTRIBUTES) {
      const current = element.getAttribute(attribute);
      if (current === null) continue;
      const en = englishOf(current, seen[attribute]);
      if (!HAS_LETTER.test(en)) continue;
      found.push({
        en,
        write: (shown) => {
          attributeOriginals.set(element, { ...(attributeOriginals.get(element) ?? {}), [attribute]: { en, shown } });
          if (element.getAttribute(attribute) !== shown) element.setAttribute(attribute, shown);
        },
      });
    }
  }
  return found;
}

export function PageTranslator() {
  const { language } = useSession();
  const [busy, setBusy] = useState(false);
  const current = useRef(language);
  const asked = useRef(new Set<string>()); // per language: strings already sent, so none is asked twice at once
  const tries = useRef(new Map<string, number>()); // strings that came back untranslated (say, Sarvam was busy)

  useEffect(() => {
    current.current = language;
    document.documentElement.lang = language.split("-")[0];
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function run() {
      const target = current.current;
      const page = slots();
      if (target === ENGLISH) {
        for (const slot of page) slot.write(slot.en);
        return;
      }
      const cache = cacheFor(target);
      const missing = Array.from(
        new Set(page.map((s) => split(s.en)[1]).filter((core) => !cache.has(core) && !asked.current.has(`${target}|${core}`))),
      );
      for (const slot of page) {
        const [lead, core, trail] = split(slot.en);
        const out = cache.get(core);
        if (out) slot.write(lead + out + trail);
      }
      if (!missing.length) return;
      missing.forEach((core) => asked.current.add(`${target}|${core}`));
      setBusy(true);
      try {
        for (let i = 0; i < missing.length; i += BATCH) {
          const texts = missing.slice(i, i + BATCH);
          const body = await api<{ translations: string[] }>("/api/translate", {
            method: "POST",
            json: { language: target, texts },
          });
          texts.forEach((en, n) => {
            const out = body.translations[n];
            if (out && out !== en) {
              cache.set(en, out);
              return;
            }
            // Came back in English: not final. Try again a little later, at most three times.
            const key = `${target}|${en}`;
            const tried = (tries.current.get(key) ?? 0) + 1;
            tries.current.set(key, tried);
            if (tried < 3) {
              setTimeout(() => {
                asked.current.delete(key);
                if (current.current === target) schedule(0);
              }, 15000 * tried);
            }
          });
          saveCache(target);
          if (current.current !== target) return; // she switched language while this was on its way
          schedule(0);
        }
      } catch {
        // The page stays readable in English; the next change on the page tries again.
        missing.forEach((core) => asked.current.delete(`${target}|${core}`));
      } finally {
        setBusy(false);
      }
    }

    function schedule(delay: number) {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => void run(), delay);
    }

    schedule(0);
    const observer = new MutationObserver(() => schedule(200));
    observer.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: [...ATTRIBUTES] });
    return () => {
      observer.disconnect();
      if (timer) clearTimeout(timer);
    };
  }, [language]);

  if (!busy) return null;
  return (
    <p role="status" translate="no" className="fixed bottom-5 left-5 z-50 rounded-full bg-ink px-3 py-1.5 text-sm text-white shadow">
      Translating…
    </p>
  );
}
