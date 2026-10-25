"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiRequestError } from "@/lib/api";
import { getAuditLog, getUsers } from "@/lib/adminApi";
import type { AuditLogItem } from "@/types/admin";
import { AuditPage, type AuditFilters } from "./AuditPage";

const PAGE_SIZE = 50;
const DEBOUNCE_MS = 300;

const EMPTY_FILTERS: AuditFilters = {
  action: "",
  actor_email: "",
  date_from: "",
  date_to: "",
};

export default function AdminAuditRoute() {
  const [filters, setFilters] = useState<AuditFilters>(EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<AuditLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [effectiveFilters, setEffectiveFilters] = useState(filters);

  useEffect(() => {
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(() => {
      setEffectiveFilters(filters);
      setPage(1);
    }, DEBOUNCE_MS);
    return () => {
      if (debounce.current) clearTimeout(debounce.current);
    };
  }, [filters]);

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      // Resolve actor_email → actor_id (best-effort: exact match if 1 hit).
      let actor_id: string | undefined;
      if (effectiveFilters.actor_email.trim()) {
        try {
          const users = await getUsers({
            search: effectiveFilters.actor_email.trim(),
            per_page: 5,
          });
          const exact = users.items.find(
            (u) =>
              u.email.toLowerCase() ===
              effectiveFilters.actor_email.trim().toLowerCase(),
          );
          actor_id = exact?.id ?? users.items[0]?.id;
        } catch {
          actor_id = undefined;
        }
      }

      const res = await getAuditLog({
        action: effectiveFilters.action || undefined,
        actor_id,
        date_from: effectiveFilters.date_from || undefined,
        date_to: effectiveFilters.date_to || undefined,
        page,
        per_page: PAGE_SIZE,
      });
      setItems(res.items);
      setTotal(res.total);
      setError(null);
    } catch (err) {
      setError(
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось загрузить аудит.",
      );
    } finally {
      setIsLoading(false);
    }
  }, [effectiveFilters, page]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <AuditPage
      items={items}
      isLoading={isLoading}
      error={error}
      total={total}
      page={page}
      perPage={PAGE_SIZE}
      filters={filters}
      onFilterChange={setFilters}
      onPageChange={setPage}
    />
  );
}
