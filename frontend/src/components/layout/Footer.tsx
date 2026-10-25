import Link from "next/link";

export function Footer() {
  return (
    <footer className="mt-8 border-t border-neutral-800 bg-neutral-950">
      <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-3 px-4 py-4 text-xs text-neutral-500 sm:flex-row">
        <p>© 2026 Trinity</p>
        <nav className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <Link
            href="/legal/terms"
            className="transition-colors hover:text-neutral-200 hover:underline"
          >
            Условия использования
          </Link>
          <Link
            href="/legal/privacy"
            className="transition-colors hover:text-neutral-200 hover:underline"
          >
            Политика конфиденциальности
          </Link>
          <Link
            href="/legal/cookies"
            className="transition-colors hover:text-neutral-200 hover:underline"
          >
            Cookies
          </Link>
        </nav>
      </div>
    </footer>
  );
}

export default Footer;
