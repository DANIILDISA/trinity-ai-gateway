import Link from "next/link";

export default function LegalLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <main className="mx-auto min-h-screen w-full max-w-3xl px-4 py-8 sm:py-12">
      <Link
        href="/"
        className="mb-6 inline-block text-sm text-neutral-400 transition-colors hover:text-neutral-100 hover:underline"
      >
        ← Назад
      </Link>
      <article className="space-y-3 text-sm leading-relaxed text-neutral-300 [&_h1]:mb-4 [&_h1]:text-2xl [&_h1]:font-semibold [&_h1]:tracking-tight [&_h1]:text-neutral-100 [&_h2]:mt-8 [&_h2]:mb-3 [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:tracking-tight [&_h2]:text-neutral-100 [&_h3]:mt-6 [&_h3]:mb-2 [&_h3]:text-base [&_h3]:font-semibold [&_h3]:text-neutral-100 [&_ul]:my-3 [&_ul]:list-disc [&_ul]:pl-6 [&_ul]:space-y-1 [&_a]:text-sky-400 [&_a]:underline hover:[&_a]:no-underline [&_code]:rounded [&_code]:bg-neutral-800 [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-xs [&_code]:text-neutral-100 [&_strong]:font-semibold [&_strong]:text-neutral-100">
        {children}
      </article>
    </main>
  );
}
