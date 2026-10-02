import type { Metadata, Viewport } from "next";
import { Anek_Bangla, Anek_Devanagari, Anek_Latin, Anek_Tamil } from "next/font/google";

import { ChatWidget } from "@/components/ChatWidget";
import { SiteHeader } from "@/components/SiteHeader";
import { PageTranslator } from "@/lib/pageTranslator";
import { SessionProvider } from "@/lib/session";

import "./globals.css";

const latin = Anek_Latin({ variable: "--font-anek-latin", subsets: ["latin"] });
const devanagari = Anek_Devanagari({ variable: "--font-anek-devanagari", subsets: ["devanagari"] });
const tamil = Anek_Tamil({ variable: "--font-anek-tamil", subsets: ["tamil"] });
const bangla = Anek_Bangla({ variable: "--font-anek-bangla", subsets: ["bengali"] });

export const metadata: Metadata = {
  title: "Praman",
  description: "Stops avoidable claim rejections before they happen, and sends every question to the party that owes the answer.",
};

export const viewport: Viewport = { themeColor: "#f6f7f4" };

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // Browser extensions (e.g. QuillBot's data-qb-installed) add attributes to <html> before React
    // loads. This ignores attribute differences on this one element only; children are still checked.
    <html
      lang="en"
      suppressHydrationWarning
      className={`${latin.variable} ${devanagari.variable} ${tamil.variable} ${bangla.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col font-sans text-[16px]">
        <SessionProvider>
          <SiteHeader />
          <main className="w-full max-w-5xl flex-1 px-4 pb-28 pt-8 sm:px-8 lg:px-12">{children}</main>
          <footer className="border-t border-line">
            <div className="max-w-5xl space-y-1 px-4 py-5 text-sm text-muted sm:px-8 lg:px-12">
              <p>Prototype for demo</p>
              {/* A plain-text credit for the event, not branding: no logo, and no claim of partnership. */}
              <p>
                Built for the Paytm Build for India hackathon (AI-Powered Financial Journeys track). Not affiliated with
                or endorsed by Paytm.
              </p>
            </div>
          </footer>
          <ChatWidget />
          <PageTranslator />
        </SessionProvider>
      </body>
    </html>
  );
}
