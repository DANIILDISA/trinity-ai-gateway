"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

import { ApiRequestError, apiClient, translateApiError } from "@/lib/api";
import { formatCost } from "@/lib/formatting";
import { api_string_to_micro, micro_to_cost_string } from "@/lib/microRub";
import { Spinner } from "@/components/ui/Spinner";
import { useToast } from "@/components/ui/Toast";
import { useAuth } from "@/hooks/useAuth";
import { useBalance } from "@/hooks/useBalance";
import { useNotifications } from "@/hooks/useNotifications";
import type {
  AnalyticsResponse,
  ConversationListResponse,
  ConversationResponse,
  PaymentCreateResponse,
  SessionItem,
  SessionListResponse,
  TransactionItem,
  TransactionListResponse,
  User,
} from "@/types/api";
import type { ConversationSummary } from "@/hooks/useConversations";
import {
  type AppearanceMode,
  type SettingsSectionKey,
  SettingsPage,
} from "./SettingsPage";

const RECENT_TX_COUNT = 20;
const RECENT_CONV_COUNT = 20;
const MIN_TOP_UP_RUB = 500;
const NOTIFICATION_PREFS_STORAGE_KEY = "trinity:notificationPrefs";
const APPEARANCE_STORAGE_KEY = "trinity:appearance";
const SECTION_STORAGE_KEY = "trinity:settingsSection";

const VALID_SECTIONS: ReadonlySet<SettingsSectionKey> = new Set<SettingsSectionKey>([
  "home",
  "account",
  "privacy",
  "balance",
  "analytics",
  "history",
]);

function toSummary(c: ConversationResponse): ConversationSummary {
  return {
    id: c.id,
    title: c.title,
    model_id: c.model_id,
    updated_at: c.updated_at,
  };
}

function readJson<T>(key: string, fallback: T): T {
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable */
  }
}

export default function SettingsRoute() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center">
          <Spinner size="lg" />
        </div>
      }
    >
      <SettingsController />
    </Suspense>
  );
}

function SettingsController() {
  const searchParams = useSearchParams();
  const toast = useToast();
  const { user, refreshUser } = useAuth();
  const {
    balance,
    balanceRaw,
    isLoading: isLoadingBalance,
    refresh: refreshBalance,
  } = useBalance();
  const {
    notifications,
    isLoading: isLoadingNotifications,
    markAsRead,
  } = useNotifications();

  // ── Section navigation ──
  const initialSection = useMemo<SettingsSectionKey>(() => {
    const fromQuery = searchParams.get("section") as SettingsSectionKey | null;
    if (fromQuery && VALID_SECTIONS.has(fromQuery)) return fromQuery;
    const fromStorage =
      typeof window === "undefined"
        ? null
        : (window.localStorage.getItem(SECTION_STORAGE_KEY) as SettingsSectionKey | null);
    if (fromStorage && VALID_SECTIONS.has(fromStorage)) return fromStorage;
    return "home";
  }, [searchParams]);
  const [activeSection, setActiveSection] = useState<SettingsSectionKey>(initialSection);

  const onSectionChange = useCallback((key: SettingsSectionKey) => {
    setActiveSection(key);
    if (typeof window !== "undefined") {
      try {
        window.localStorage.setItem(SECTION_STORAGE_KEY, key);
      } catch {
        /* ignore */
      }
    }
  }, []);

  // ── Profile state ──
  const [displayName, setDisplayName] = useState("");
  const [displayNameError, setDisplayNameError] = useState<string | null>(null);
  const [isSavingDisplayName, setIsSavingDisplayName] = useState(false);
  const [displayNameSavedAt, setDisplayNameSavedAt] = useState<number | null>(null);

  const [fullName, setFullName] = useState("");
  const [fullNameError, setFullNameError] = useState<string | null>(null);
  const [isSavingFullName, setIsSavingFullName] = useState(false);
  const [fullNameSavedAt, setFullNameSavedAt] = useState<number | null>(null);

  // Hydrate from user once it arrives.
  useEffect(() => {
    if (user) {
      if (displayName === "") setDisplayName(user.display_name);
      if (fullName === "" && user.full_name) setFullName(user.full_name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  // ── Appearance (local stub) ──
  const [appearance, setAppearance] = useState<AppearanceMode>(() =>
    readJson<AppearanceMode>(APPEARANCE_STORAGE_KEY, "dark"),
  );
  const onAppearanceChange = useCallback((mode: AppearanceMode) => {
    setAppearance(mode);
    writeJson(APPEARANCE_STORAGE_KEY, mode);
  }, []);

  // ── Notification prefs (local-only) ──
  const [notificationPrefs, setNotificationPrefs] = useState<Record<string, boolean>>(
    () =>
      readJson<Record<string, boolean>>(NOTIFICATION_PREFS_STORAGE_KEY, {
        response_done: true,
        balance_low: true,
        system_news: true,
      }),
  );
  const onToggleNotificationPref = useCallback((key: string) => {
    setNotificationPrefs((prev) => {
      const next = { ...prev, [key]: !(prev[key] ?? true) };
      writeJson(NOTIFICATION_PREFS_STORAGE_KEY, next);
      return next;
    });
  }, []);

  // ── Password (Yandex-only accounts) ──
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [isSavingPassword, setIsSavingPassword] = useState(false);
  const [passwordSuccess, setPasswordSuccess] = useState(false);

  // ── Sessions ──
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [isLoadingSessions, setIsLoadingSessions] = useState(true);

  const loadSessions = useCallback(async () => {
    setIsLoadingSessions(true);
    try {
      const res = await apiClient.get<SessionListResponse>(
        "/api/v1/users/me/sessions",
      );
      setSessions(res.items);
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) return;
    } finally {
      setIsLoadingSessions(false);
    }
  }, []);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  // ── Delete account ──
  const [deleteEmailInput, setDeleteEmailInput] = useState("");
  const [isDeletingAccount, setIsDeletingAccount] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // ── Balance + transactions ──
  const [amount, setAmount] = useState<string>(String(MIN_TOP_UP_RUB));
  const [amountError, setAmountError] = useState<string | null>(null);
  const [isSubmittingTopUp, setIsSubmittingTopUp] = useState(false);

  const [transactions, setTransactions] = useState<TransactionItem[]>([]);
  const [txPage, setTxPage] = useState(1);
  const [txTotal, setTxTotal] = useState(0);
  const [isLoadingTransactions, setIsLoadingTransactions] = useState(true);
  const hasMoreTransactions = transactions.length < txTotal;

  const [pendingPaymentNotice, setPendingPaymentNotice] = useState<string | null>(null);
  const topUpBaselineRef = useRef<string | null>(null);

  const loadTxPage = useCallback(async (pageNum: number) => {
    setIsLoadingTransactions(true);
    try {
      const res = await apiClient.get<TransactionListResponse>(
        `/api/v1/users/me/transactions?page=${pageNum}&per_page=${RECENT_TX_COUNT}`,
      );
      setTxTotal(res.total);
      setTransactions((prev) =>
        pageNum === 1 ? res.items : [...prev, ...res.items],
      );
      setTxPage(pageNum);
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) return;
    } finally {
      setIsLoadingTransactions(false);
    }
  }, []);

  useEffect(() => {
    void loadTxPage(1);
  }, [loadTxPage]);

  // Return-from-YooKassa: surface notice, capture baseline, refresh.
  useEffect(() => {
    const paymentId = searchParams.get("payment_id");
    const status = searchParams.get("status");
    if (paymentId || status === "success") {
      setPendingPaymentNotice(
        "Платёж обрабатывается. Баланс обновится в течение минуты.",
      );
      if (topUpBaselineRef.current === null && balanceRaw !== null) {
        topUpBaselineRef.current = balanceRaw;
      }
      refreshBalance();
      void loadTxPage(1);
    }
  }, [searchParams, refreshBalance, loadTxPage, balanceRaw]);

  useEffect(() => {
    const baseline = topUpBaselineRef.current;
    if (baseline === null || balanceRaw === null) return;
    const diff = api_string_to_micro(balanceRaw) - api_string_to_micro(baseline);
    if (diff > 0n) {
      const diffApiStr = micro_to_cost_string(Number(diff));
      toast.success(`Баланс пополнен на ${formatCost(diffApiStr)}`);
      topUpBaselineRef.current = null;
      setPendingPaymentNotice(null);
    }
  }, [balanceRaw, toast]);

  // ── Analytics ──
  const [analytics, setAnalytics] = useState<AnalyticsResponse | null>(null);
  const [isLoadingAnalytics, setIsLoadingAnalytics] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await apiClient.get<AnalyticsResponse>("/api/v1/users/me/analytics");
        if (!cancelled) setAnalytics(res);
      } catch (err) {
        if (err instanceof ApiRequestError && err.status === 401) return;
      } finally {
        if (!cancelled) setIsLoadingAnalytics(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ── History (recent conversations) ──
  const [recentConversations, setRecentConversations] = useState<ConversationSummary[]>(
    [],
  );
  const [isLoadingHistory, setIsLoadingHistory] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await apiClient.get<ConversationListResponse>(
          `/api/v1/chat/conversations?page=1&per_page=${RECENT_CONV_COUNT}`,
        );
        if (!cancelled) setRecentConversations(res.items.map(toSummary));
      } catch (err) {
        if (err instanceof ApiRequestError && err.status === 401) return;
      } finally {
        if (!cancelled) setIsLoadingHistory(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ── Handlers ──
  const handleSaveDisplayName = useCallback(async () => {
    setDisplayNameError(null);
    const value = displayName.trim();
    if (value.length < 2) {
      setDisplayNameError("Минимум 2 символа.");
      return;
    }
    if (value.length > 100) {
      setDisplayNameError("Не более 100 символов.");
      return;
    }
    setIsSavingDisplayName(true);
    try {
      await apiClient.patch<User>("/api/v1/users/me", { display_name: value });
      setDisplayNameSavedAt(Date.now());
      await refreshUser();
      toast.success("Настройки сохранены");
    } catch (err) {
      const msg = translateApiError(err);
      setDisplayNameError(msg);
      toast.error(msg);
    } finally {
      setIsSavingDisplayName(false);
    }
  }, [displayName, refreshUser, toast]);

  const handleSaveFullName = useCallback(async () => {
    setFullNameError(null);
    const value = fullName.trim();
    if (value.length > 150) {
      setFullNameError("Не более 150 символов.");
      return;
    }
    setIsSavingFullName(true);
    try {
      await apiClient.patch<User>("/api/v1/users/me", { full_name: value });
      setFullNameSavedAt(Date.now());
      await refreshUser();
      toast.success("Имя сохранено");
    } catch (err) {
      const msg = translateApiError(err);
      setFullNameError(msg);
      toast.error(msg);
    } finally {
      setIsSavingFullName(false);
    }
  }, [fullName, refreshUser, toast]);

  const handleSetPassword = useCallback(async () => {
    setPasswordError(null);
    setPasswordSuccess(false);
    if (newPassword.length < 8) {
      setPasswordError("Минимум 8 символов.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordError("Пароли не совпадают.");
      return;
    }
    setIsSavingPassword(true);
    try {
      await apiClient.post("/api/v1/auth/set-password", { password: newPassword });
      setPasswordSuccess(true);
      setNewPassword("");
      setConfirmPassword("");
      toast.success("Пароль успешно установлен");
    } catch (err) {
      const msg = translateApiError(err);
      setPasswordError(msg);
      toast.error(msg);
    } finally {
      setIsSavingPassword(false);
    }
  }, [newPassword, confirmPassword, toast]);

  const handleTerminateSession = useCallback(
    async (id: string) => {
      try {
        await apiClient.delete(`/api/v1/users/me/sessions/${id}`);
        toast.success("Сессия завершена");
        await loadSessions();
      } catch (err) {
        toast.error(translateApiError(err));
      }
    },
    [loadSessions, toast],
  );

  const handleTerminateAllSessions = useCallback(async () => {
    if (
      typeof window !== "undefined" &&
      !window.confirm("Завершить все сессии, включая текущую?")
    ) {
      return;
    }
    try {
      await apiClient.post("/api/v1/users/me/sessions/terminate-all");
      toast.success("Все сессии завершены");
      // The current access token is now blacklisted server-side; bounce to
      // login so the user re-authenticates cleanly.
      window.location.assign("/login");
    } catch (err) {
      toast.error(translateApiError(err));
    }
  }, [toast]);

  const handleConfirmDeleteAccount = useCallback(async () => {
    setDeleteError(null);
    setIsDeletingAccount(true);
    try {
      await apiClient.post("/api/v1/users/me/delete", {
        email_confirmation: deleteEmailInput,
      });
      toast.success("Аккаунт помечен на удаление");
      window.location.assign("/login");
    } catch (err) {
      const msg = translateApiError(err);
      setDeleteError(msg);
    } finally {
      setIsDeletingAccount(false);
    }
  }, [deleteEmailInput, toast]);

  const handleTopUp = useCallback(async () => {
    setAmountError(null);
    const parsed = Number(amount);
    if (!Number.isFinite(parsed) || !Number.isInteger(parsed) || parsed < MIN_TOP_UP_RUB) {
      setAmountError(`Минимальная сумма пополнения — ${MIN_TOP_UP_RUB} ₽`);
      return;
    }
    setIsSubmittingTopUp(true);
    try {
      const res = await apiClient.post<PaymentCreateResponse>(
        "/api/v1/payments/create",
        { amount: parsed },
      );
      window.location.href = res.confirmation_url;
    } catch (err) {
      const message = translateApiError(err);
      setAmountError(message);
      toast.error(message);
      setIsSubmittingTopUp(false);
    }
  }, [amount, toast]);

  const handleLoadMoreTransactions = useCallback(() => {
    if (!isLoadingTransactions && hasMoreTransactions) void loadTxPage(txPage + 1);
  }, [isLoadingTransactions, hasMoreTransactions, loadTxPage, txPage]);

  const showPasswordSection = user?.has_password === false;

  return (
    <SettingsPage
      user={user}
      activeSection={activeSection}
      onSectionChange={onSectionChange}
      displayName={displayName}
      displayNameError={displayNameError}
      isSavingDisplayName={isSavingDisplayName}
      displayNameSavedAt={displayNameSavedAt}
      onDisplayNameChange={setDisplayName}
      onSaveDisplayName={handleSaveDisplayName}
      fullName={fullName}
      fullNameError={fullNameError}
      isSavingFullName={isSavingFullName}
      fullNameSavedAt={fullNameSavedAt}
      onFullNameChange={setFullName}
      onSaveFullName={handleSaveFullName}
      appearance={appearance}
      onAppearanceChange={onAppearanceChange}
      notifications={notifications}
      isLoadingNotifications={isLoadingNotifications}
      notificationPrefs={notificationPrefs}
      onToggleNotificationPref={onToggleNotificationPref}
      onMarkAsRead={(id) => void markAsRead(id)}
      showPasswordSection={showPasswordSection}
      newPassword={newPassword}
      confirmPassword={confirmPassword}
      passwordError={passwordError}
      isSavingPassword={isSavingPassword}
      passwordSuccess={passwordSuccess}
      onNewPasswordChange={setNewPassword}
      onConfirmPasswordChange={setConfirmPassword}
      onSetPassword={handleSetPassword}
      sessions={sessions}
      isLoadingSessions={isLoadingSessions}
      onTerminateSession={(id) => void handleTerminateSession(id)}
      onTerminateAllSessions={() => void handleTerminateAllSessions()}
      deleteEmailInput={deleteEmailInput}
      onDeleteEmailInputChange={setDeleteEmailInput}
      isDeletingAccount={isDeletingAccount}
      deleteError={deleteError}
      onConfirmDeleteAccount={() => void handleConfirmDeleteAccount()}
      balance={balance}
      isLoadingBalance={isLoadingBalance}
      amount={amount}
      amountError={amountError}
      isSubmittingTopUp={isSubmittingTopUp}
      transactions={transactions}
      isLoadingTransactions={isLoadingTransactions}
      hasMoreTransactions={hasMoreTransactions}
      pendingPaymentNotice={pendingPaymentNotice}
      onAmountChange={setAmount}
      onTopUp={() => void handleTopUp()}
      onLoadMoreTransactions={handleLoadMoreTransactions}
      analytics={analytics}
      isLoadingAnalytics={isLoadingAnalytics}
      recentConversations={recentConversations}
      isLoadingHistory={isLoadingHistory}
    />
  );
}
