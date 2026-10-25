"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { ApiRequestError } from "@/lib/api";
import {
  adjustBalance,
  blockUser,
  getUserDetail,
  unblockUser,
} from "@/lib/adminApi";
import { formatBalance } from "@/lib/formatting";
import type { AdminUserDetail } from "@/types/admin";
import { UserDetailPage } from "./UserDetailPage";

const MICRO_PER_RUB = 1_000_000;

export default function AdminUserDetailRoute() {
  const params = useParams<{ id: string }>();
  const userId = params?.id ?? "";

  const [user, setUser] = useState<AdminUserDetail | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [blockReason, setBlockReason] = useState("");
  const [isShowingBlockForm, setIsShowingBlockForm] = useState(false);
  const [isBlocking, setIsBlocking] = useState(false);
  const [isUnblocking, setIsUnblocking] = useState(false);
  const [blockError, setBlockError] = useState<string | null>(null);

  const [adjustAmount, setAdjustAmount] = useState("");
  const [adjustReason, setAdjustReason] = useState("");
  const [isAdjusting, setIsAdjusting] = useState(false);
  const [adjustError, setAdjustError] = useState<string | null>(null);
  const [adjustSuccess, setAdjustSuccess] = useState<string | null>(null);

  const refreshUser = useCallback(async () => {
    if (!userId) return;
    try {
      const detail = await getUserDetail(userId);
      setUser(detail);
      setError(null);
    } catch (err) {
      const msg =
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось загрузить пользователя.";
      setError(msg);
    } finally {
      setIsLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    void refreshUser();
  }, [refreshUser]);

  const handleBlock = async () => {
    setBlockError(null);
    if (blockReason.trim().length === 0) {
      setBlockError("Укажите причину блокировки.");
      return;
    }
    setIsBlocking(true);
    try {
      await blockUser(userId, blockReason.trim());
      setIsShowingBlockForm(false);
      setBlockReason("");
      await refreshUser();
    } catch (err) {
      setBlockError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось заблокировать пользователя.",
      );
    } finally {
      setIsBlocking(false);
    }
  };

  const handleUnblock = async () => {
    setBlockError(null);
    setIsUnblocking(true);
    try {
      await unblockUser(userId);
      await refreshUser();
    } catch (err) {
      setBlockError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось разблокировать пользователя.",
      );
    } finally {
      setIsUnblocking(false);
    }
  };

  const handleAdjust = async (e: React.FormEvent) => {
    e.preventDefault();
    setAdjustError(null);
    setAdjustSuccess(null);

    const parsed = Number(adjustAmount);
    if (!Number.isFinite(parsed) || !Number.isInteger(parsed) || parsed === 0) {
      setAdjustError("Введите целое ненулевое число.");
      return;
    }
    if (adjustReason.trim().length < 10) {
      setAdjustError("Причина — минимум 10 символов.");
      return;
    }

    setIsAdjusting(true);
    try {
      const res = await adjustBalance(
        userId,
        parsed * MICRO_PER_RUB,
        adjustReason.trim(),
      );
      setAdjustSuccess(
        `Баланс обновлён. Новый баланс: ${formatBalance(res.balance_display)}.`,
      );
      setAdjustAmount("");
      setAdjustReason("");
      await refreshUser();
    } catch (err) {
      setAdjustError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось скорректировать баланс.",
      );
    } finally {
      setIsAdjusting(false);
    }
  };

  return (
    <UserDetailPage
      user={user}
      isLoading={isLoading}
      error={error}
      blockReason={blockReason}
      isBlocking={isBlocking}
      isUnblocking={isUnblocking}
      isShowingBlockForm={isShowingBlockForm}
      blockError={blockError}
      adjustAmount={adjustAmount}
      adjustReason={adjustReason}
      isAdjusting={isAdjusting}
      adjustError={adjustError}
      adjustSuccess={adjustSuccess}
      onBlockReasonChange={setBlockReason}
      onShowBlockForm={() => setIsShowingBlockForm(true)}
      onHideBlockForm={() => {
        setIsShowingBlockForm(false);
        setBlockError(null);
        setBlockReason("");
      }}
      onBlock={() => void handleBlock()}
      onUnblock={() => void handleUnblock()}
      onAdjustAmountChange={setAdjustAmount}
      onAdjustReasonChange={setAdjustReason}
      onAdjustSubmit={(e) => void handleAdjust(e)}
    />
  );
}
