"use client";

import Link from "next/link";

export interface AdminSidebarLink {
  href: string;
  label: string;
  badge?: number;
}

const LINKS: AdminSidebarLink[] = [
  { href: "/admin", label: "Дашборд" },
  { href: "/admin/users", label: "Пользователи" },
  { href: "/admin/tariffs", label: "Тарифы" },
  { href: "/admin/requests", label: "Запросы" },
  { href: "/admin/keys", label: "Ключи" },
  { href: "/admin/alerts", label: "Алерты" },
  { href: "/admin/audit", label: "Аудит" },
  { href: "/admin/export", label: "Экспорт" },
  { href: "/admin/settings", label: "Настройки" },
  { href: "/admin/monitoring", label: "Мониторинг" },
];

export interface AdminSidebarProps {
  currentPath: string;
  displayName: string;
  alertsBadge?: number;
}

function isActive(href: string, currentPath: string): boolean {
  if (href === "/admin") return currentPath === "/admin";
  return currentPath === href || currentPath.startsWith(`${href}/`);
}

export function AdminSidebar({
  currentPath,
  displayName,
  alertsBadge = 0,
}: AdminSidebarProps) {
  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-neutral-200 bg-neutral-900 text-neutral-100">
      <div className="border-b border-neutral-800 px-4 py-4">
        <p className="text-xs uppercase tracking-wide text-neutral-400">
          Trinity
        </p>
        <h2 className="text-lg font-semibold">Admin Panel</h2>
      </div>

      <nav className="flex-1 px-2 py-3">
        <ul className="flex flex-col gap-0.5">
          {LINKS.map((link) => {
            const active = isActive(link.href, currentPath);
            const badge =
              link.href === "/admin/alerts" && alertsBadge > 0
                ? alertsBadge
                : link.badge;
            return (
              <li key={link.href}>
                <Link
                  href={link.href}
                  aria-current={active ? "page" : undefined}
                  className={`flex items-center justify-between rounded px-3 py-2 text-sm transition-colors ${
                    active
                      ? "bg-neutral-800 font-medium text-white"
                      : "text-neutral-300 hover:bg-neutral-800/60 hover:text-white"
                  }`}
                >
                  <span>{link.label}</span>
                  {badge && badge > 0 ? (
                    <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-rose-600 px-1 text-xs font-semibold text-white">
                      {badge > 99 ? "99+" : badge}
                    </span>
                  ) : null}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      <div className="border-t border-neutral-800 px-4 py-3 text-xs">
        <Link
          href="/settings"
          className="block truncate text-neutral-400 hover:text-white"
          title={displayName}
        >
          {displayName}
        </Link>
        <Link
          href="/chat"
          className="mt-1 block text-neutral-500 hover:text-neutral-200"
        >
          ← Вернуться в приложение
        </Link>
      </div>
    </aside>
  );
}

export default AdminSidebar;
