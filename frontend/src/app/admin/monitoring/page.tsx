"use client";

import { useAdminPolling } from "@/hooks/useAdminPolling";
import {
  getLatency,
  getOpenRouterStatus,
  getStorageStats,
  getSystemMonitoring,
} from "@/lib/adminApi";
import { MonitoringPage } from "./MonitoringPage";

export default function AdminMonitoringRoute() {
  const system = useAdminPolling(getSystemMonitoring, 30_000);
  const latency = useAdminPolling(getLatency, 30_000);
  const openrouter = useAdminPolling(getOpenRouterStatus, 30_000);
  const storage = useAdminPolling(getStorageStats, 60_000);

  const error = system.error ?? latency.error ?? openrouter.error;
  const isLoading = system.isLoading || latency.isLoading || openrouter.isLoading;

  return (
    <MonitoringPage
      system={system.data}
      latency={latency.data}
      openrouter={openrouter.data}
      storage={storage.data}
      storageLoading={storage.isLoading}
      isLoading={isLoading}
      error={error}
    />
  );
}
