"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiRequestError } from "@/lib/api";
import {
  activateKey,
  addKey,
  blockAccount,
  getKeys,
  unblockAccount,
  updateKeyStatus,
} from "@/lib/adminApi";
import type { ApiKey } from "@/types/admin";
import { KeysPage } from "./KeysPage";

export default function AdminKeysRoute() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [addLabel, setAddLabel] = useState("");
  const [addKeyValue, setAddKeyValue] = useState("");
  const [addAccountName, setAddAccountName] = useState("");
  const [isAdding, setIsAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [addSuccess, setAddSuccess] = useState<string | null>(null);

  const [busyKeyId, setBusyKeyId] = useState<string | null>(null);
  const [busyAccount, setBusyAccount] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const list = await getKeys();
      setKeys(list);
      setLoadError(null);
    } catch (err) {
      setLoadError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось загрузить список ключей.",
      );
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    setAddError(null);
    setAddSuccess(null);
    if (addLabel.trim().length === 0) {
      setAddError("Укажите метку.");
      return;
    }
    if (addKeyValue.trim().length < 8) {
      setAddError("Ключ должен содержать минимум 8 символов.");
      return;
    }
    setIsAdding(true);
    try {
      const account = addAccountName.trim();
      const created = await addKey(
        addLabel.trim(),
        addKeyValue.trim(),
        account.length > 0 ? account : undefined,
      );
      setAddSuccess(`Ключ ${created.label} добавлен.`);
      setAddLabel("");
      setAddKeyValue("");
      setAddAccountName("");
      await refresh();
    } catch (err) {
      setAddError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось добавить ключ.",
      );
    } finally {
      setIsAdding(false);
    }
  };

  const runAction = async (id: string, action: () => Promise<unknown>) => {
    setBusyKeyId(id);
    try {
      await action();
      await refresh();
    } catch (err) {
      setLoadError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось выполнить действие.",
      );
    } finally {
      setBusyKeyId(null);
    }
  };

  const runAccountAction = async (
    accountName: string,
    action: () => Promise<unknown>,
  ) => {
    setBusyAccount(accountName);
    try {
      await action();
      await refresh();
    } catch (err) {
      setLoadError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось выполнить действие.",
      );
    } finally {
      setBusyAccount(null);
    }
  };

  return (
    <KeysPage
      keys={keys}
      isLoading={isLoading}
      loadError={loadError}
      addLabel={addLabel}
      addKeyValue={addKeyValue}
      addAccountName={addAccountName}
      isAdding={isAdding}
      addError={addError}
      addSuccess={addSuccess}
      busyKeyId={busyKeyId}
      busyAccount={busyAccount}
      onAddLabelChange={setAddLabel}
      onAddKeyChange={setAddKeyValue}
      onAddAccountNameChange={setAddAccountName}
      onAddSubmit={(e) => void handleAdd(e)}
      onActivate={(id) => void runAction(id, () => activateKey(id))}
      onDisable={(id) =>
        void runAction(id, () => updateKeyStatus(id, "disabled"))
      }
      onEnable={(id) =>
        void runAction(id, () => updateKeyStatus(id, "active"))
      }
      onBlockAccount={(account) =>
        void runAccountAction(account, () => blockAccount(account))
      }
      onUnblockAccount={(account) =>
        void runAccountAction(account, () => unblockAccount(account))
      }
    />
  );
}
