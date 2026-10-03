"use client";

import Link from "next/link";

import { useSession } from "@/lib/session";

export default function Home() {
  const { caseId, openChat } = useSession();
  const services = [
    {
      title: "Claim readiness",
      body: "Upload your policy and the hospital bill. See whether the claim will be stopped or cut, and by how much, before you file.",
      action: <Link href="/readiness" className="font-medium text-pine underline">Check a claim</Link>,
    },
    {
      title: "Ask about my policy",
      body: "Ask in your language. Answers come only from your policy and the regulations, with the page they came from.",
      action: (
        <span className="flex flex-wrap gap-x-4 gap-y-1">
          <Link href="/policies" className="font-medium text-pine underline">See my policies</Link>
          <button type="button" onClick={() => openChat()} className="font-medium text-pine underline">
            Open the chat
          </button>
        </span>
      ),
    },
    {
      title: "Document checklist",
      body: "Six documents a claim needs. Send them one at a time and see which are still missing.",
      action: caseId ? (
        <Link href={`/checklist/${caseId}`} className="font-medium text-pine underline">Open my checklist</Link>
      ) : (
        <span className="text-muted">Connecting to your case…</span>
      ),
    },
  ];

  return (
    <div className="space-y-12">
      <section className="max-w-3xl pt-4">
        <h1 className="text-[2.6rem] font-bold leading-[1.05] tracking-tight text-ink sm:text-6xl">
          Stop avoidable claim rejections before they happen.
        </h1>
      </section>

      <section aria-label="Services" className="grid gap-4 sm:grid-cols-3">
        {services.map((s) => (
          <article key={s.title} className="flex flex-col rounded-lg border border-line bg-white p-5">
            <h2 className="text-xl font-semibold">{s.title}</h2>
            <p className="mt-2 flex-1 leading-relaxed text-muted">{s.body}</p>
            <div className="mt-4">{s.action}</div>
          </article>
        ))}
      </section>

      <section className="border-t border-line pt-6">
        <p className="text-muted">
          Working for the distributor?{" "}
          <Link href="/console" className="font-medium text-pine underline">
            Open the distributor console
          </Link>{" "}
          to see the cases that need you and the complaints waiting for a person.
        </p>
      </section>
    </div>
  );
}
