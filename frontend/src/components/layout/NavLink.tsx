"use client";

import Link from "next/link";

export interface NavLinkProps {
  href: string;
  currentPath: string;
  children: React.ReactNode;
  disabled?: boolean;
}

function isActive(href: string, currentPath: string): boolean {
  if (href === "/") return currentPath === "/";
  return currentPath === href || currentPath.startsWith(`${href}/`);
}

export function NavLink({
  href,
  currentPath,
  children,
  disabled = false,
}: NavLinkProps) {
  const active = isActive(href, currentPath);

  const base =
    "inline-flex h-9 items-center rounded px-3 text-sm transition-colors";
  const stateClass = active
    ? "font-semibold text-neutral-100 underline underline-offset-4"
    : "text-neutral-400 hover:text-neutral-100 hover:bg-neutral-800";

  if (disabled) {
    return (
      <span
        aria-disabled="true"
        className={`${base} pointer-events-none opacity-50 text-neutral-500`}
      >
        {children}
      </span>
    );
  }

  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={`${base} ${stateClass}`}
    >
      {children}
    </Link>
  );
}

export default NavLink;
