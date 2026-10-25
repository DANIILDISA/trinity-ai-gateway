"use client";

import Link from "next/link";

import { Button } from "@/components/ui/Button";
import type { User } from "@/types/api";
import { NavLink } from "./NavLink";

export interface HeaderProps {
  user: User | null;
  balance: string | null;
  isLoadingBalance: boolean;
  unreadCount?: number;
  onLogout: () => void;
  currentPath: string;
}

export function Header({
  user,
  balance,
  isLoadingBalance,
  onLogout,
  currentPath,
}: HeaderProps) {
  const blocked = user?.status === "blocked";

  return (
    <header className="sticky top-0 z-10 w-full border-b border-neutral-800 bg-neutral-950/95 backdrop-blur-sm">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-4 px-4">
        <Link
          href="/chat"
          className="text-base font-semibold tracking-tight text-neutral-100"
        >
          Trinity
        </Link>

        <nav className="ml-2 flex items-center gap-1">
          <NavLink
            href="/chat"
            currentPath={currentPath}
            disabled={blocked}
          >
            Чат
          </NavLink>
          <NavLink href="/history" currentPath={currentPath}>
            История
          </NavLink>
          <NavLink
            href="/balance"
            currentPath={currentPath}
            disabled={blocked}
          >
            Баланс
          </NavLink>
          <NavLink href="/pricing" currentPath={currentPath}>
            Тарифы
          </NavLink>
        </nav>

        <div className="ml-auto flex items-center gap-3">
          <BalanceDisplay
            balance={balance}
            isLoading={isLoadingBalance}
            blocked={blocked}
          />

          {user ? (
            <Link
              href="/settings"
              title="Настройки"
              className="hidden text-sm text-neutral-300 hover:text-neutral-100 hover:underline sm:inline"
            >
              {user.display_name}
            </Link>
          ) : null}

          <Button size="sm" variant="ghost" onClick={onLogout}>
            Выйти
          </Button>
        </div>
      </div>
    </header>
  );
}

function BalanceDisplay({
  balance,
  isLoading,
  blocked,
}: {
  balance: string | null;
  isLoading: boolean;
  blocked: boolean;
}) {
  if (isLoading && balance === null) {
    return (
      <span
        aria-label="Загрузка баланса"
        className="inline-block h-5 w-20 animate-pulse rounded bg-neutral-800"
      />
    );
  }

  const text = balance ?? "—";

  if (blocked) {
    return (
      <span
        title="Аккаунт заблокирован"
        className="text-sm font-medium text-neutral-400"
      >
        {text}
      </span>
    );
  }

  return (
    <Link
      href="/balance"
      className="font-mono text-sm font-medium text-emerald-400 hover:underline"
    >
      {text}
    </Link>
  );
}

export default Header;
