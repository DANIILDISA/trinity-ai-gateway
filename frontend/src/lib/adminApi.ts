import { apiClient } from "@/lib/api";
import type {
  AdjustBalanceResponse,
  AdminUserDetail,
  AdminUserListResponse,
  AlertItem,
  AlertListResponse,
  ApiKey,
  ApplyTariffResponse,
  AuditLogResponse,
  CbrRateResponse,
  DashboardPeriod,
  DashboardResponse,
  ExportSummaryResponse,
  LatencyResponse,
  OpenRouterStatusResponse,
  PricePreview,
  RateLimitsResponse,
  RequestListResponse,
  RevenueByModelResponse,
  StorageStatsResponse,
  SyncResult,
  SystemMonitoringResponse,
  SystemSettings,
  TariffsResponse,
} from "@/types/admin";

const BASE = "/api/v1/admin";

function qs(params: Record<string, string | number | boolean | undefined>): string {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== "",
  );
  if (entries.length === 0) return "";
  const usp = new URLSearchParams();
  for (const [k, v] of entries) usp.set(k, String(v));
  return `?${usp.toString()}`;
}

export function getDashboard(
  period: DashboardPeriod = "today",
): Promise<DashboardResponse> {
  return apiClient.get<DashboardResponse>(
    `${BASE}/dashboard${qs({ period })}`,
  );
}

export function getRevenueByModel(
  period: DashboardPeriod = "today",
): Promise<RevenueByModelResponse> {
  return apiClient.get<RevenueByModelResponse>(
    `${BASE}/dashboard/revenue-by-model${qs({ period })}`,
  );
}

export function getLatency(): Promise<LatencyResponse> {
  return apiClient.get<LatencyResponse>(`${BASE}/monitoring/latency`);
}

export function getSystemMonitoring(): Promise<SystemMonitoringResponse> {
  return apiClient.get<SystemMonitoringResponse>(`${BASE}/monitoring/system`);
}

export function getOpenRouterStatus(): Promise<OpenRouterStatusResponse> {
  return apiClient.get<OpenRouterStatusResponse>(`${BASE}/monitoring/openrouter`);
}

export function getStorageStats(): Promise<StorageStatsResponse> {
  return apiClient.get<StorageStatsResponse>(`${BASE}/storage`);
}

export function getUsers(params: {
  search?: string;
  page?: number;
  per_page?: number;
  sort_by?: string;
  sort_order?: "asc" | "desc";
} = {}): Promise<AdminUserListResponse> {
  return apiClient.get<AdminUserListResponse>(
    `${BASE}/users${qs(params)}`,
  );
}

export function getUserDetail(id: string): Promise<AdminUserDetail> {
  return apiClient.get<AdminUserDetail>(`${BASE}/users/${id}`);
}

export function blockUser(id: string, reason: string) {
  return apiClient.post<unknown>(`${BASE}/users/${id}/block`, { reason });
}

export function unblockUser(id: string) {
  return apiClient.post<unknown>(`${BASE}/users/${id}/unblock`);
}

export function adjustBalance(
  id: string,
  amount_micro_rub: number,
  reason: string,
): Promise<AdjustBalanceResponse> {
  return apiClient.post<AdjustBalanceResponse>(
    `${BASE}/users/${id}/adjust-balance`,
    { amount_micro_rub, reason },
  );
}

export function getTariffs(): Promise<TariffsResponse> {
  return apiClient.get<TariffsResponse>(`${BASE}/tariffs`);
}

export function previewTariff(
  usd_rub_rate: number,
): Promise<PricePreview[]> {
  return apiClient.post<PricePreview[]>(`${BASE}/tariffs/preview`, {
    usd_rub_rate,
  });
}

export function applyTariff(usd_rub_rate: number): Promise<ApplyTariffResponse> {
  return apiClient.post<ApplyTariffResponse>(`${BASE}/tariffs/apply`, {
    usd_rub_rate,
  });
}

export function confirmExchangeRate(
  alert_id: string,
): Promise<ApplyTariffResponse> {
  return apiClient.post<ApplyTariffResponse>(`${BASE}/tariffs/confirm-rate`, {
    alert_id,
  });
}

export function overrideModelPrice(
  model_id: string,
  input_price_micro_rub: number,
  output_price_micro_rub: number,
) {
  return apiClient.patch<unknown>(`${BASE}/tariffs/models/${model_id}`, {
    input_price_micro_rub,
    output_price_micro_rub,
  });
}

export function clearModelPriceOverride(model_id: string) {
  return apiClient.delete(`${BASE}/tariffs/models/${model_id}/override`);
}

export function syncModels(): Promise<SyncResult> {
  return apiClient.post<SyncResult>(`${BASE}/models/sync`);
}

export function getCbrRate(): Promise<CbrRateResponse> {
  return apiClient.get<CbrRateResponse>(`${BASE}/tariffs/cbr-rate`);
}

export function getRequests(
  params: { page?: number; per_page?: number; request_id?: string } = {},
): Promise<RequestListResponse> {
  return apiClient.get<RequestListResponse>(`${BASE}/requests${qs(params)}`);
}

export function getRateLimits(): Promise<RateLimitsResponse> {
  return apiClient.get<RateLimitsResponse>(`${BASE}/settings/rate-limits`);
}

export function updateRateLimits(
  data: Record<string, unknown>,
): Promise<void> {
  return apiClient.patch(`${BASE}/settings/rate-limits`, data);
}

export function getAlerts(
  params: {
    status?: "new" | "acknowledged" | "resolved";
    severity?: "critical" | "warning" | "info";
    page?: number;
    per_page?: number;
  } = {},
): Promise<AlertListResponse> {
  return apiClient.get<AlertListResponse>(`${BASE}/alerts${qs(params)}`);
}

export function acknowledgeAlert(id: string): Promise<AlertItem> {
  return apiClient.post<AlertItem>(`${BASE}/alerts/${id}/acknowledge`);
}

export function resolveAlert(id: string): Promise<AlertItem> {
  return apiClient.post<AlertItem>(`${BASE}/alerts/${id}/resolve`);
}

export function getAuditLog(
  params: Record<string, string | number | undefined> = {},
): Promise<AuditLogResponse> {
  return apiClient.get<AuditLogResponse>(`${BASE}/audit${qs(params)}`);
}

export function getKeys(): Promise<ApiKey[]> {
  return apiClient.get<ApiKey[]>(`${BASE}/keys`);
}

export function addKey(
  label: string,
  apiKey: string,
  accountName?: string,
): Promise<ApiKey> {
  return apiClient.post<ApiKey>(`${BASE}/keys`, {
    name: label,
    key: apiKey,
    account_name: accountName ?? null,
  });
}

export function updateKeyStatus(
  id: string,
  status: "active" | "disabled" | "exhausted",
): Promise<ApiKey> {
  return apiClient.patch<ApiKey>(`${BASE}/keys/${id}`, { status });
}

export function activateKey(id: string): Promise<ApiKey> {
  return apiClient.post<ApiKey>(`${BASE}/keys/${id}/activate`);
}

export function blockAccount(
  accountName: string,
): Promise<{ affected_keys: number }> {
  return apiClient.post<{ affected_keys: number }>(
    `${BASE}/keys/accounts/${encodeURIComponent(accountName)}/block`,
  );
}

export function unblockAccount(
  accountName: string,
): Promise<{ affected_keys: number }> {
  return apiClient.post<{ affected_keys: number }>(
    `${BASE}/keys/accounts/${encodeURIComponent(accountName)}/unblock`,
  );
}

export function updateOpenRouterBalance(
  balance_usd: number,
): Promise<{ balance_usd: number; updated_at: string }> {
  return apiClient.put<{ balance_usd: number; updated_at: string }>(
    `${BASE}/settings/openrouter-balance`,
    { balance_usd },
  );
}

// ── Export + settings (Task 8.6) ─────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const target = `${name}=`;
  for (const raw of document.cookie.split(";")) {
    const c = raw.trim();
    if (c.startsWith(target)) return decodeURIComponent(c.slice(target.length));
  }
  return null;
}

async function _downloadCsv(
  path: string,
  body: Record<string, unknown>,
  fallbackFilename: string,
): Promise<void> {
  const token = apiClient.getToken();
  const csrf = readCookie("csrf_token");
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (csrf) headers["X-CSRF-Token"] = csrf;

  const res = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers,
    credentials: "include",
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`Ошибка экспорта (${res.status})`);
  }

  let filename = fallbackFilename;
  const disp = res.headers.get("content-disposition");
  const match = disp?.match(/filename="?([^";]+)"?/i);
  if (match) filename = match[1];

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function exportTransactions(
  date_from: string,
  date_to: string,
): Promise<void> {
  return _downloadCsv(
    `${BASE}/export/transactions`,
    { date_from, date_to },
    `transactions_${date_from}_${date_to}.csv`,
  );
}

export function exportPerUser(
  date_from: string,
  date_to: string,
): Promise<void> {
  return _downloadCsv(
    `${BASE}/export/per-user`,
    { date_from, date_to },
    `per_user_${date_from}_${date_to}.csv`,
  );
}

export function exportSummary(
  date_from: string,
  date_to: string,
): Promise<ExportSummaryResponse> {
  return apiClient.post<ExportSummaryResponse>(`${BASE}/export/summary`, {
    date_from,
    date_to,
  });
}

export function getSettings(): Promise<SystemSettings> {
  return apiClient.get<SystemSettings>(`${BASE}/settings`);
}

export function updateSetting(key: string, value: unknown) {
  return apiClient.put<unknown>(`${BASE}/settings/${key}`, { value });
}
