"use client";

import { useMemo, useState } from "react";

import { ApiRequestError } from "@/lib/api";
import {
  exportPerUser,
  exportSummary,
  exportTransactions,
} from "@/lib/adminApi";
import type { ExportSummaryResponse } from "@/types/admin";
import { ExportPage } from "./ExportPage";

function defaultRange(): { from: string; to: string } {
  const now = new Date();
  const firstOfMonth = new Date(now.getFullYear(), now.getMonth(), 1);
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  return { from: iso(firstOfMonth), to: iso(now) };
}

export default function AdminExportRoute() {
  const { from, to } = useMemo(defaultRange, []);
  const [dateFrom, setDateFrom] = useState(from);
  const [dateTo, setDateTo] = useState(to);

  const [isExportingTx, setIsExportingTx] = useState(false);
  const [isExportingPerUser, setIsExportingPerUser] = useState(false);
  const [isSummaryLoading, setIsSummaryLoading] = useState(false);

  const [summary, setSummary] = useState<ExportSummaryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const errMsg = (e: unknown, fallback: string) =>
    e instanceof ApiRequestError ? e.message : e instanceof Error ? e.message : fallback;

  const handleExportTransactions = async () => {
    setError(null);
    setIsExportingTx(true);
    try {
      await exportTransactions(dateFrom, dateTo);
    } catch (err) {
      setError(errMsg(err, "Не удалось экспортировать транзакции."));
    } finally {
      setIsExportingTx(false);
    }
  };

  const handleExportPerUser = async () => {
    setError(null);
    setIsExportingPerUser(true);
    try {
      await exportPerUser(dateFrom, dateTo);
    } catch (err) {
      setError(errMsg(err, "Не удалось экспортировать по пользователям."));
    } finally {
      setIsExportingPerUser(false);
    }
  };

  const handleExportSummary = async () => {
    setError(null);
    setIsSummaryLoading(true);
    try {
      const data = await exportSummary(dateFrom, dateTo);
      setSummary(data);
    } catch (err) {
      setError(errMsg(err, "Не удалось получить сводку."));
    } finally {
      setIsSummaryLoading(false);
    }
  };

  return (
    <ExportPage
      dateFrom={dateFrom}
      dateTo={dateTo}
      isExportingTx={isExportingTx}
      isExportingPerUser={isExportingPerUser}
      isSummaryLoading={isSummaryLoading}
      summary={summary}
      error={error}
      onDateFromChange={setDateFrom}
      onDateToChange={setDateTo}
      onExportTransactions={() => void handleExportTransactions()}
      onExportSummary={() => void handleExportSummary()}
      onExportPerUser={() => void handleExportPerUser()}
    />
  );
}
