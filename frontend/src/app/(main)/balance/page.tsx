"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

import { ApiRequestError, apiClient, translateApiError } from "@/lib/api";
import { dispatchBalanceRefresh } from "@/lib/balanceEvents";
import { formatCost } from "@/lib/formatting";
import { api_string_to_micro, micro_to_cost_string } from "@/lib/microRub";
import { Spinner } from "@/components/ui/Spinner";
import { useToast } from "@/components/ui/Toast";
import { useBalance } from "@/hooks/useBalance";
import type {
  PaymentCreateResponse,
  TransactionItem,
  TransactionListResponse,
} from "@/types/api";
import { BalancePage } from "./BalancePage";

const PAGE_SIZE = 20;
const MIN_TOP_UP_RUB = 500;

export default function BalanceRoute() {
  return (
    <Suspense fallback={<Spinner size="lg" />}>
      <BalanceRouteInner />
    </Suspense>
  );
}

function BalanceRouteInner() {
  const searchParams = useSearchParams();
  const toast = useToast();
  const {
    balance,
    balanceRaw,
    isLoading: isLoadingBalance,
    refresh: refreshBalance,
  } = useBalance();

  const [amount, setAmount] = useState<string>(String(MIN_TOP_UP_RUB));
  const [amountError, setAmountError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const [transactions, setTransactions] = useState<TransactionItem[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [isLoadingTx, setIsLoadingTx] = useState(true);

  const [pendingPaymentNotice, setPendingPaymentNotice] = useState<
    string | null
  >(null);
  // Baseline balance captured the moment the user returns from YooKassa.
  // The polling effect below compares against it to detect the credit and
  // pop a "Баланс пополнен на X₽" toast exactly once.
  const topUpBaselineRef = useRef<string | null>(null);

  const hasMore = transactions.length < total;

  const loadPage = useCallback(async (pageNum: number) => {
    setIsLoadingTx(true);
    try {
      const res = await apiClient.get<TransactionListResponse>(
        `/api/v1/users/me/transactions?page=${pageNum}&per_page=${PAGE_SIZE}`,
      );
      setTotal(res.total);
      setTransactions((prev) =>
        pageNum === 1 ? res.items : [...prev, ...res.items],
      );
      setPage(pageNum);
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) return;
      // Transient: leave existing list.
    } finally {
      setIsLoadingTx(false);
    }
  }, []);

  useEffect(() => {
    void loadPage(1);
  }, [loadPage]);

  // Return-from-YooKassa: show notice, capture baseline, trigger refresh.
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
      void loadPage(1);
    }
  }, [searchParams, refreshBalance, loadPage, balanceRaw]);

  // Watch for the balance increase that confirms a top-up landed. Fires once
  // per YooKassa return; subsequent polls leave the toast alone.
  useEffect(() => {
    const baseline = topUpBaselineRef.current;
    if (baseline === null || balanceRaw === null) return;
    const diff = api_string_to_micro(balanceRaw) - api_string_to_micro(baseline);
    if (diff > 0n) {
      const diffApiStr = micro_to_cost_string(Number(diff));
      toast.success(`Баланс пополнен на ${formatCost(diffApiStr)}`);
      topUpBaselineRef.current = null;
      setPendingPaymentNotice(null);
      // tr-hye6: the top-up has landed — signal every other balance consumer
      // (the sidebar in particular) to update instantly, not on next reload.
      dispatchBalanceRefresh();
    }
  }, [balanceRaw, toast]);

  const handleTopUp = async (e: React.FormEvent) => {
    e.preventDefault();
    setAmountError(null);

    const parsed = Number(amount);
    if (!Number.isFinite(parsed) || !Number.isInteger(parsed) || parsed < MIN_TOP_UP_RUB) {
      setAmountError(`Минимальная сумма пополнения — ${MIN_TOP_UP_RUB} ₽`);
      return;
    }

    setIsSubmitting(true);
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
      setIsSubmitting(false);
    }
  };

  const handleLoadMore = useCallback(() => {
    if (!isLoadingTx && hasMore) void loadPage(page + 1);
  }, [isLoadingTx, hasMore, loadPage, page]);

  return (
    <BalancePage
      balance={balance}
      isLoadingBalance={isLoadingBalance}
      amount={amount}
      amountError={amountError}
      isSubmitting={isSubmitting}
      transactions={transactions}
      isLoadingTx={isLoadingTx}
      hasMore={hasMore}
      pendingPaymentNotice={pendingPaymentNotice}
      onAmountChange={setAmount}
      onTopUp={handleTopUp}
      onLoadMore={handleLoadMore}
    />
  );
}
