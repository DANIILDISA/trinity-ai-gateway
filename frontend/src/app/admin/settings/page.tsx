"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiRequestError } from "@/lib/api";
import { getSettings, updateSetting } from "@/lib/adminApi";
import type { SystemSettings } from "@/types/admin";
import { SettingsPage } from "./SettingsPage";

export default function AdminSettingsRoute() {
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [errorByKey, setErrorByKey] = useState<Record<string, string | null>>({});
  const [successByKey, setSuccessByKey] = useState<
    Record<string, number | null>
  >({});

  const refresh = useCallback(async () => {
    try {
      const data = await getSettings();
      setSettings(data);
      setLoadError(null);
    } catch (err) {
      setLoadError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось загрузить настройки.",
      );
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleSave = async (key: string, value: number | boolean | string) => {
    setSavingKey(key);
    setErrorByKey((prev) => ({ ...prev, [key]: null }));
    try {
      await updateSetting(key, value);
      setSuccessByKey((prev) => ({ ...prev, [key]: Date.now() }));
      await refresh();
    } catch (err) {
      setErrorByKey((prev) => ({
        ...prev,
        [key]:
          err instanceof ApiRequestError
            ? err.message
            : "Не удалось сохранить значение.",
      }));
    } finally {
      setSavingKey(null);
    }
  };

  return (
    <SettingsPage
      settings={settings}
      isLoading={isLoading}
      loadError={loadError}
      savingKey={savingKey}
      errorByKey={errorByKey}
      successByKey={successByKey}
      onSave={(k, v: number | boolean | string) => void handleSave(k, v)}
    />
  );
}
