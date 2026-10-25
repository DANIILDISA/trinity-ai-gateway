"use client";

import { useEffect, useState } from "react";

import { ApiRequestError, apiClient } from "@/lib/api";
import type { ModelInfo, ModelListResponse } from "@/types/api";
import { PricingTable } from "./PricingTable";

export default function PricingRoute() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [lastUpdate, setLastUpdate] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const res = await apiClient.get<ModelListResponse>("/api/v1/models");
        if (cancelled) return;
        const items = res.models ?? [];
        setModels(items);
        setLastUpdate(latestUpdate(items));
        setError(null);
      } catch (err) {
        if (cancelled) return;
        const message =
          err instanceof ApiRequestError
            ? err.message
            : "Не удалось загрузить тарифы. Попробуйте позже.";
        setError(message);
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <PricingTable
      models={models}
      lastUpdate={lastUpdate}
      isLoading={isLoading}
      error={error}
    />
  );
}

function latestUpdate(items: ModelInfo[]): string | null {
  let latest: string | null = null;
  let latestMs = -Infinity;
  for (const m of items) {
    if (!m.last_updated) continue;
    const ms = Date.parse(m.last_updated);
    if (Number.isFinite(ms) && ms > latestMs) {
      latestMs = ms;
      latest = m.last_updated;
    }
  }
  return latest;
}
