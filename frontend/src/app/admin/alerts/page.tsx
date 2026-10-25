"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiRequestError } from "@/lib/api";
import {
  acknowledgeAlert,
  confirmExchangeRate,
  getAlerts,
  resolveAlert,
} from "@/lib/adminApi";
import type {
  AlertItem,
  AlertSeverity,
  AlertStatus,
} from "@/types/admin";
import { AlertsPage } from "./AlertsPage";

const PAGE_SIZE = 20;
const POLL_MS = 30_000;

export default function AdminAlertsRoute() {
  const [statusFilter, setStatusFilter] = useState<AlertStatus | "all">("all");
  const [severityFilter, setSeverityFilter] = useState<AlertSeverity | "all">(
    "all",
  );
  const [page, setPage] = useState(1);

  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [total, setTotal] = useState(0);
  const [unreadCount, setUnreadCount] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchAlerts = useCallback(async () => {
    try {
      const res = await getAlerts({
        status: statusFilter === "all" ? undefined : statusFilter,
        severity: severityFilter === "all" ? undefined : severityFilter,
        page,
        per_page: PAGE_SIZE,
      });
      setAlerts(res.items);
      setTotal(res.total);
      setUnreadCount(res.unread_count);
      setError(null);
    } catch (err) {
      setError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось загрузить алерты.",
      );
    } finally {
      setIsLoading(false);
    }
  }, [statusFilter, severityFilter, page]);

  useEffect(() => {
    setIsLoading(true);
    void fetchAlerts();
    const id = setInterval(() => void fetchAlerts(), POLL_MS);
    return () => clearInterval(id);
  }, [fetchAlerts]);

  const handleAcknowledge = async (id: string) => {
    setAlerts((prev) =>
      prev.map((a) =>
        a.id === id ? { ...a, status: "acknowledged" } : a,
      ),
    );
    try {
      await acknowledgeAlert(id);
      void fetchAlerts();
    } catch (err) {
      setError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось подтвердить алерт.",
      );
      void fetchAlerts();
    }
  };

  const handleResolve = async (id: string) => {
    setAlerts((prev) =>
      prev.map((a) => (a.id === id ? { ...a, status: "resolved" } : a)),
    );
    try {
      await resolveAlert(id);
      void fetchAlerts();
    } catch (err) {
      setError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось решить алерт.",
      );
      void fetchAlerts();
    }
  };

  const handleConfirmRate = async (id: string) => {
    try {
      await confirmExchangeRate(id);
      void fetchAlerts();
    } catch (err) {
      setError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось применить курс ЦБ.",
      );
      void fetchAlerts();
    }
  };

  return (
    <AlertsPage
      alerts={alerts}
      unreadCount={unreadCount}
      total={total}
      page={page}
      perPage={PAGE_SIZE}
      statusFilter={statusFilter}
      severityFilter={severityFilter}
      isLoading={isLoading}
      error={error}
      onStatusFilter={(s) => {
        setStatusFilter(s);
        setPage(1);
      }}
      onSeverityFilter={(s) => {
        setSeverityFilter(s);
        setPage(1);
      }}
      onPageChange={setPage}
      onAcknowledge={(id) => void handleAcknowledge(id)}
      onResolve={(id) => void handleResolve(id)}
      onConfirmRate={(id) => void handleConfirmRate(id)}
    />
  );
}
