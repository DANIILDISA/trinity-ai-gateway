"use client";

import { useCallback, useState } from "react";

import { AdminDashboard } from "@/components/admin/AdminDashboard";
import { useAdminPolling } from "@/hooks/useAdminPolling";
import {
  getDashboard,
  getRevenueByModel,
  updateOpenRouterBalance,
} from "@/lib/adminApi";
import type { DashboardPeriod } from "@/types/admin";

export default function AdminDashboardPage() {
  const [period, setPeriod] = useState<DashboardPeriod>("today");

  // Re-create the closures whenever `period` changes; the polling hook
  // tracks the function via ref and re-polls on dependency change via the
  // re-render path (the initial fetch runs immediately on mount; for period
  // switches we also kick a manual refresh from onPeriodChange).
  const fetchDashboard = useCallback(() => getDashboard(period), [period]);
  const fetchRevenueByModel = useCallback(
    () => getRevenueByModel(period),
    [period],
  );

  const dashboard = useAdminPolling(fetchDashboard, 30_000);
  const models = useAdminPolling(fetchRevenueByModel, 30_000);

  const onPeriodChange = (next: DashboardPeriod) => {
    setPeriod(next);
    // Trigger immediate fetch for the new period; useAdminPolling will
    // see the new closure and re-arm naturally on the next render.
    setTimeout(() => {
      dashboard.refresh();
      models.refresh();
    }, 0);
  };

  const onSaveBalance = async (balanceUsd: number) => {
    await updateOpenRouterBalance(balanceUsd);
    dashboard.refresh();
  };

  return (
    <AdminDashboard
      data={dashboard.data}
      revenueByModel={models.data}
      isLoading={dashboard.isLoading}
      isLoadingModels={models.isLoading}
      error={dashboard.error}
      modelsError={models.error}
      period={period}
      onPeriodChange={onPeriodChange}
      onSaveBalance={onSaveBalance}
    />
  );
}
