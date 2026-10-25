"use client";

import Link from "next/link";

import { HomeSection } from "./sections/HomeSection";
import { AccountSection } from "./sections/AccountSection";
import { PrivacySection } from "./sections/PrivacySection";
import { BalanceSection } from "./sections/BalanceSection";
import { AnalyticsSection } from "./sections/AnalyticsSection";
import { HistorySection } from "./sections/HistorySection";
import type { ConversationSummary } from "@/hooks/useConversations";
import type {
  AnalyticsResponse,
  Notification,
  SessionItem,
  TransactionItem,
  User,
} from "@/types/api";

export type SettingsSectionKey =
  | "home"
  | "account"
  | "privacy"
  | "balance"
  | "analytics"
  | "history";

// Accent color shared across the settings shell — taken from the chat send
// button (Tailwind `emerald-600`). Centralized here so swapping the brand
// accent later is a one-line change.
export const ACCENT_BG = "bg-emerald-600";
export const ACCENT_TEXT = "text-emerald-400";
export const ACCENT_BORDER = "border-emerald-600";

interface NavItem {
  key: SettingsSectionKey;
  label: string;
}

const NAV_ITEMS: readonly NavItem[] = [
  { key: "home", label: "Главная" },
  { key: "account", label: "Аккаунт" },
  { key: "privacy", label: "Конфиденциальность" },
  { key: "balance", label: "Баланс" },
  { key: "analytics", label: "Аналитика" },
  { key: "history", label: "История" },
];

export interface SettingsPageProps {
  user: User | null;
  activeSection: SettingsSectionKey;
  onSectionChange: (key: SettingsSectionKey) => void;

  // Profile
  displayName: string;
  displayNameError: string | null;
  isSavingDisplayName: boolean;
  displayNameSavedAt: number | null;
  onDisplayNameChange: (value: string) => void;
  onSaveDisplayName: () => void;

  fullName: string;
  fullNameError: string | null;
  isSavingFullName: boolean;
  fullNameSavedAt: number | null;
  onFullNameChange: (value: string) => void;
  onSaveFullName: () => void;

  // Appearance (stub)
  appearance: AppearanceMode;
  onAppearanceChange: (mode: AppearanceMode) => void;

  // Notifications
  notifications: Notification[];
  isLoadingNotifications: boolean;
  notificationPrefs: Record<string, boolean>;
  onToggleNotificationPref: (key: string) => void;
  onMarkAsRead: (id: string) => void;

  // Password (Yandex-only accounts)
  showPasswordSection: boolean;
  newPassword: string;
  confirmPassword: string;
  passwordError: string | null;
  isSavingPassword: boolean;
  passwordSuccess: boolean;
  onNewPasswordChange: (value: string) => void;
  onConfirmPasswordChange: (value: string) => void;
  onSetPassword: () => void;

  // Sessions
  sessions: SessionItem[];
  isLoadingSessions: boolean;
  onTerminateSession: (id: string) => void;
  onTerminateAllSessions: () => void;

  // Delete account
  deleteEmailInput: string;
  onDeleteEmailInputChange: (value: string) => void;
  isDeletingAccount: boolean;
  deleteError: string | null;
  onConfirmDeleteAccount: () => void;

  // Balance
  balance: string | null;
  isLoadingBalance: boolean;
  amount: string;
  amountError: string | null;
  isSubmittingTopUp: boolean;
  transactions: TransactionItem[];
  isLoadingTransactions: boolean;
  hasMoreTransactions: boolean;
  pendingPaymentNotice: string | null;
  onAmountChange: (value: string) => void;
  onTopUp: () => void;
  onLoadMoreTransactions: () => void;

  // Analytics
  analytics: AnalyticsResponse | null;
  isLoadingAnalytics: boolean;

  // History
  recentConversations: ConversationSummary[];
  isLoadingHistory: boolean;
}

export type AppearanceMode = "system" | "light" | "dark";

export function SettingsPage(props: SettingsPageProps) {
  return (
    <div className="flex min-h-screen flex-col bg-[var(--trinity-canvas)]">
      <Header />
      <div className="mx-auto flex w-full max-w-6xl flex-1 gap-6 px-4 pb-12">
        <SideNav
          activeSection={props.activeSection}
          onSectionChange={props.onSectionChange}
        />
        <main className="min-w-0 flex-1 py-6">
          <SectionRouter {...props} />
        </main>
      </div>
    </div>
  );
}

function Header() {
  return (
    <header className="sticky top-0 z-10 border-b border-neutral-800/80 bg-neutral-950/95 backdrop-blur-sm">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-4">
        <Link
          href="/chat"
          className="inline-flex items-center gap-2 text-sm font-medium text-neutral-300 transition-colors hover:text-neutral-50"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M15 18l-6-6 6-6"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          Настройки
        </Link>
      </div>
    </header>
  );
}

function SideNav({
  activeSection,
  onSectionChange,
}: {
  activeSection: SettingsSectionKey;
  onSectionChange: (key: SettingsSectionKey) => void;
}) {
  return (
    <aside className="hidden w-56 shrink-0 py-6 md:block">
      <nav>
        <ul className="flex flex-col gap-1">
          {NAV_ITEMS.map((item) => {
            const active = activeSection === item.key;
            return (
              <li key={item.key}>
                <button
                  type="button"
                  onClick={() => onSectionChange(item.key)}
                  aria-current={active ? "page" : undefined}
                  className={`relative flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                    active
                      ? `${ACCENT_BG} text-white shadow-sm`
                      : "text-neutral-400 hover:bg-neutral-800/60 hover:text-neutral-100"
                  }`}
                >
                  {item.label}
                </button>
              </li>
            );
          })}
        </ul>
      </nav>
    </aside>
  );
}

function SectionRouter(props: SettingsPageProps) {
  switch (props.activeSection) {
    case "home":
      return (
        <HomeSection
          displayName={props.displayName}
          displayNameError={props.displayNameError}
          isSavingDisplayName={props.isSavingDisplayName}
          displayNameSavedAt={props.displayNameSavedAt}
          onDisplayNameChange={props.onDisplayNameChange}
          onSaveDisplayName={props.onSaveDisplayName}
          fullName={props.fullName}
          fullNameError={props.fullNameError}
          isSavingFullName={props.isSavingFullName}
          fullNameSavedAt={props.fullNameSavedAt}
          onFullNameChange={props.onFullNameChange}
          onSaveFullName={props.onSaveFullName}
          user={props.user}
          appearance={props.appearance}
          onAppearanceChange={props.onAppearanceChange}
          notifications={props.notifications}
          isLoadingNotifications={props.isLoadingNotifications}
          notificationPrefs={props.notificationPrefs}
          onToggleNotificationPref={props.onToggleNotificationPref}
          onMarkAsRead={props.onMarkAsRead}
          showPasswordSection={props.showPasswordSection}
          newPassword={props.newPassword}
          confirmPassword={props.confirmPassword}
          passwordError={props.passwordError}
          isSavingPassword={props.isSavingPassword}
          passwordSuccess={props.passwordSuccess}
          onNewPasswordChange={props.onNewPasswordChange}
          onConfirmPasswordChange={props.onConfirmPasswordChange}
          onSetPassword={props.onSetPassword}
        />
      );
    case "account":
      return (
        <AccountSection
          user={props.user}
          sessions={props.sessions}
          isLoadingSessions={props.isLoadingSessions}
          onTerminateSession={props.onTerminateSession}
          onTerminateAllSessions={props.onTerminateAllSessions}
          deleteEmailInput={props.deleteEmailInput}
          onDeleteEmailInputChange={props.onDeleteEmailInputChange}
          isDeletingAccount={props.isDeletingAccount}
          deleteError={props.deleteError}
          onConfirmDeleteAccount={props.onConfirmDeleteAccount}
        />
      );
    case "privacy":
      return <PrivacySection />;
    case "balance":
      return (
        <BalanceSection
          balance={props.balance}
          isLoadingBalance={props.isLoadingBalance}
          amount={props.amount}
          amountError={props.amountError}
          isSubmittingTopUp={props.isSubmittingTopUp}
          transactions={props.transactions}
          isLoadingTransactions={props.isLoadingTransactions}
          hasMoreTransactions={props.hasMoreTransactions}
          pendingPaymentNotice={props.pendingPaymentNotice}
          onAmountChange={props.onAmountChange}
          onTopUp={props.onTopUp}
          onLoadMoreTransactions={props.onLoadMoreTransactions}
        />
      );
    case "analytics":
      return (
        <AnalyticsSection
          analytics={props.analytics}
          isLoading={props.isLoadingAnalytics}
        />
      );
    case "history":
      return (
        <HistorySection
          conversations={props.recentConversations}
          isLoading={props.isLoadingHistory}
        />
      );
  }
}

export default SettingsPage;
