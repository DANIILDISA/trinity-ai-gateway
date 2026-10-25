"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiRequestError } from "@/lib/api";
import { getUsers } from "@/lib/adminApi";
import type {
  AdminUserSortBy,
  AdminUserSortOrder,
  AdminUserSummary,
} from "@/types/admin";
import { UsersPage } from "./UsersPage";

const PAGE_SIZE = 20;
const DEBOUNCE_MS = 300;

export default function AdminUsersRoute() {
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [sortBy, setSortBy] = useState<AdminUserSortBy>("created_at");
  const [sortOrder, setSortOrder] = useState<AdminUserSortOrder>("desc");
  const [page, setPage] = useState(1);
  const [users, setUsers] = useState<AdminUserSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Debounce the search input → searchQuery.
  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (debounceTimer.current) clearTimeout(debounceTimer.current);
    debounceTimer.current = setTimeout(() => {
      setSearchQuery(searchInput.trim());
      setPage(1);
    }, DEBOUNCE_MS);
    return () => {
      if (debounceTimer.current) clearTimeout(debounceTimer.current);
    };
  }, [searchInput]);

  // Fetch whenever query state changes.
  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    getUsers({
      search: searchQuery || undefined,
      page,
      per_page: PAGE_SIZE,
      sort_by: sortBy,
      sort_order: sortOrder,
    })
      .then((res) => {
        if (cancelled) return;
        setUsers(res.items);
        setTotal(res.total);
        setError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        const msg =
          err instanceof ApiRequestError
            ? err.message
            : "Не удалось загрузить пользователей.";
        setError(msg);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [searchQuery, sortBy, sortOrder, page]);

  const handleSort = useCallback(
    (column: AdminUserSortBy) => {
      if (column === sortBy) {
        setSortOrder((prev) => (prev === "asc" ? "desc" : "asc"));
      } else {
        setSortBy(column);
        setSortOrder("desc");
      }
      setPage(1);
    },
    [sortBy],
  );

  return (
    <UsersPage
      users={users}
      total={total}
      page={page}
      perPage={PAGE_SIZE}
      searchQuery={searchInput}
      sortBy={sortBy}
      sortOrder={sortOrder}
      isLoading={isLoading}
      error={error}
      onSearchChange={setSearchInput}
      onSort={handleSort}
      onPageChange={setPage}
    />
  );
}
