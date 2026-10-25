"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiRequestError } from "@/lib/api";
import {
  applyTariff,
  clearModelPriceOverride,
  getCbrRate,
  getTariffs,
  overrideModelPrice,
  previewTariff,
  syncModels,
} from "@/lib/adminApi";
import type {
  CbrRateResponse,
  PricePreview,
  SyncResult,
  TariffsResponse,
} from "@/types/admin";
import { TariffsPage } from "./TariffsPage";

const MICRO_PER_UNIT = 1_000_000;

function parseRateRub(value: string): number | null {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return null;
  return Math.round(num * MICRO_PER_UNIT);
}

function cooldownActive(nextAllowed: string | null): boolean {
  if (!nextAllowed) return false;
  const ms = Date.parse(nextAllowed);
  if (!Number.isFinite(ms)) return false;
  return ms > Date.now();
}

export default function AdminTariffsRoute() {
  const [tariffs, setTariffs] = useState<TariffsResponse | null>(null);
  const [cbrRate, setCbrRate] = useState<CbrRateResponse | null>(null);
  const [preview, setPreview] = useState<PricePreview[] | null>(null);

  const [isLoadingTariffs, setIsLoadingTariffs] = useState(true);
  const [isLoadingPreview, setIsLoadingPreview] = useState(false);
  const [isApplying, setIsApplying] = useState(false);

  const [rateInput, setRateInput] = useState("");
  const [rateInputError, setRateInputError] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applySuccess, setApplySuccess] = useState<string | null>(null);

  const [isSyncing, setIsSyncing] = useState(false);
  const [syncResult, setSyncResult] = useState<SyncResult | null>(null);
  const [syncError, setSyncError] = useState<boolean>(false);

  const refreshTariffs = useCallback(async () => {
    try {
      const data = await getTariffs();
      setTariffs(data);
      // Seed the input with the current rate (in whole rubles) on first load.
      setRateInput((prev) =>
        prev === "" ? (data.exchange_rate_micro_rub / MICRO_PER_UNIT).toFixed(4) : prev,
      );
    } finally {
      setIsLoadingTariffs(false);
    }
  }, []);

  useEffect(() => {
    void refreshTariffs();
    void getCbrRate().then(setCbrRate).catch(() => undefined);
  }, [refreshTariffs]);

  const cooldownInfo = useMemo(() => {
    const next = tariffs?.next_update_allowed_at ?? null;
    return { active: cooldownActive(next), nextAllowedAt: next };
  }, [tariffs?.next_update_allowed_at]);

  const handlePreview = async () => {
    setRateInputError(null);
    setApplyError(null);
    setApplySuccess(null);
    const rateMicro = parseRateRub(rateInput);
    if (rateMicro === null) {
      setRateInputError("Введите положительное число.");
      return;
    }
    setIsLoadingPreview(true);
    try {
      const rows = await previewTariff(rateMicro);
      setPreview(rows);
    } catch (err) {
      setRateInputError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось получить предпросмотр.",
      );
    } finally {
      setIsLoadingPreview(false);
    }
  };

  const handleApply = async () => {
    setRateInputError(null);
    setApplyError(null);
    setApplySuccess(null);
    const rateMicro = parseRateRub(rateInput);
    if (rateMicro === null) {
      setRateInputError("Введите положительное число.");
      return;
    }
    setIsApplying(true);
    try {
      const res = await applyTariff(rateMicro);
      setApplySuccess(
        `Тарифы обновлены. Обновлено моделей: ${res.models_updated}.`,
      );
      setPreview(null);
      await refreshTariffs();
    } catch (err) {
      setApplyError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось применить тариф.",
      );
    } finally {
      setIsApplying(false);
    }
  };

  const handleSyncModels = async () => {
    setSyncError(false);
    setSyncResult(null);
    setIsSyncing(true);
    try {
      const res = await syncModels();
      setSyncResult(res);
      // Reflect any catalogue changes (new/removed models, prices) in the table.
      await refreshTariffs();
    } catch {
      setSyncError(true);
    } finally {
      setIsSyncing(false);
    }
  };

  const handleOverrideModel = useCallback(
    async (
      modelId: string,
      inputMicroRub: number,
      outputMicroRub: number,
    ) => {
      try {
        await overrideModelPrice(modelId, inputMicroRub, outputMicroRub);
        await refreshTariffs();
      } catch (err) {
        throw err instanceof ApiRequestError
          ? new Error(err.message)
          : new Error("Не удалось зафиксировать цену.");
      }
    },
    [refreshTariffs],
  );

  const handleClearOverride = useCallback(
    async (modelId: string) => {
      try {
        await clearModelPriceOverride(modelId);
        await refreshTariffs();
      } catch (err) {
        throw err instanceof ApiRequestError
          ? new Error(err.message)
          : new Error("Не удалось снять фиксацию.");
      }
    },
    [refreshTariffs],
  );

  return (
    <TariffsPage
      tariffs={tariffs}
      cbrRate={cbrRate}
      preview={preview}
      rateInput={rateInput}
      rateInputError={rateInputError}
      isLoadingTariffs={isLoadingTariffs}
      isLoadingPreview={isLoadingPreview}
      isApplying={isApplying}
      applyError={applyError}
      applySuccess={applySuccess}
      cooldownInfo={cooldownInfo}
      isSyncing={isSyncing}
      syncResult={syncResult}
      syncError={syncError}
      onRateInputChange={setRateInput}
      onPreview={() => void handlePreview()}
      onApply={() => void handleApply()}
      onSyncModels={() => void handleSyncModels()}
      onOverrideModel={handleOverrideModel}
      onClearOverride={handleClearOverride}
    />
  );
}
