export interface DashboardRevenue {
  today_micro_rub: number;
  today_display: string;
  week_micro_rub: number;
  week_display: string;
  month_micro_rub: number;
  month_display: string;
}

export interface DashboardCosts {
  today_usd: number;
  week_usd: number;
  month_usd: number;
}

export interface DashboardMargin {
  today_percent: number;
  week_percent: number;
  month_percent: number;
}

export interface DashboardReconciliation {
  status: string;
  discrepancy_percent: number;
  last_check: string;
}

export interface DashboardResponse {
  revenue: DashboardRevenue;
  // Operator-absorbed overdraft and the effective revenue margin is based on
  // (revenue − overdraft) — tr-yalc.
  overdraft: DashboardRevenue;
  effective_revenue: DashboardRevenue;
  costs: DashboardCosts;
  margin: DashboardMargin;
  reconciliation: DashboardReconciliation;
  openrouter_balance_usd: number | null;
  openrouter_balance_warning: boolean;
  openrouter_balance_source: "live" | "manual" | "depleted" | "unavailable";
  active_users_today: number;
  total_requests_today: number;
}

export interface ModelRevenueRow {
  model_id: string;
  model_name: string;
  request_count: number;
  revenue_micro_rub: number;
  revenue_display: string;
  cost_micro_usd: number;
  cost_usd: number;
  margin_percent: number;
  total_input_tokens: number;
  total_output_tokens: number;
}

export interface RevenueByModelResponse {
  models: ModelRevenueRow[];
}

export type DashboardPeriod = "today" | "week" | "month";

export interface LatencyPercentiles {
  p50_ms: number | null;
  p95_ms: number | null;
  p99_ms: number | null;
  sample_count: number;
}

export interface LatencyResponse {
  stages: Record<string, LatencyPercentiles>;
}

export interface SystemMonitoringResponse {
  system: {
    cpu_percent: number;
    ram_percent: number;
    ram_used_mb: number;
    ram_total_mb: number;
    disk_percent: number;
    disk_used_gb: number;
    disk_total_gb: number;
  };
  database: {
    pool_size: number;
    checked_out: number;
    overflow: number;
    available: number;
  };
  redis: {
    used_memory: string;
    max_memory: string;
    connected_clients: number;
    hit_rate_percent: number;
    total_commands: number;
    version: string;
    uptime_seconds: number;
  };
}

export interface OpenRouterStatusResponse {
  balance_usd: number | null;
  balance_warning: boolean;
  active_key_label: string | null;
  active_key_suffix: string | null;
  recent_error_count_1h: number;
}

export interface StorageStatsResponse {
  text_size_bytes: number;
  images_size_bytes: number;
  files_size_bytes: number;
  total_images_count: number;
  total_files_count: number;
  total_conversations_count: number;
  total_messages_count: number;
}

export interface AdminUserSummary {
  id: string;
  email: string;
  display_name: string;
  role: string;
  status: string;
  balance: string;
  balance_micro_rub: number;
  created_at: string;
  last_active_at: string | null;
}

export interface AdminUserListResponse {
  items: AdminUserSummary[];
  total: number;
  page: number;
  per_page: number;
}

export interface AdminUserStats {
  total_topped_up_micro_rub: number;
  total_topped_up_display: string;
  total_spent_micro_rub: number;
  total_spent_display: string;
  total_requests: number;
  total_tokens: number;
  avg_request_cost_micro_rub: number;
  avg_request_cost_display: string;
  requests_today: number;
  most_used_model: string | null;
}

export interface AdminUserDetail extends AdminUserSummary {
  block_reason: string | null;
  has_password: boolean;
  has_yandex: boolean;
  sessions_count: number;
  stats: AdminUserStats;
}

export interface AdjustBalanceResponse {
  balance_micro_rub: number;
  balance: string;
  balance_display: string;
  transaction_id: string;
}

export type AdminUserSortBy =
  | "created_at"
  | "last_active_at"
  | "balance_micro_rub"
  | "email";

export type AdminUserSortOrder = "asc" | "desc";

export interface TariffModel {
  model_id: string;
  model_name: string;
  provider: string;
  input_price_micro_rub: number;
  input_price_display: string;
  output_price_micro_rub: number;
  output_price_display: string;
  is_price_override: boolean;
  status: string;
}

export interface TariffsResponse {
  exchange_rate_micro_rub: number;
  exchange_rate_display: string;
  last_updated: string | null;
  next_update_allowed_at: string | null;
  markup_multiplier: number;
  models: TariffModel[];
}

export interface PricePreview {
  model_id: string;
  model_name: string;
  current_input_price: string;
  current_output_price: string;
  proposed_input_price: string;
  proposed_output_price: string;
  is_override: boolean;
}

export interface ApplyTariffResponse {
  applied: boolean;
  previous_rate_micro_rub: number;
  previous_rate_display: string;
  new_rate_micro_rub: number;
  new_rate_display: string;
  models_updated: number;
  next_update_allowed_at: string;
}

export interface SyncResult {
  added: number;
  updated: number;
  deactivated: number;
  errors: string[];
}

export interface CbrRateResponse {
  rate_usd_rub: number;
  source: string;
  fetched_at: string;
  note?: string;
}

export interface RateLimitsResponse {
  requests_per_minute: number;
  requests_per_hour: number;
}

export interface SystemSettings {
  rate_limits: {
    rate_limit_per_minute: number;
    rate_limit_per_hour: number;
    rate_limit_per_day: number;
  };
  billing: {
    reservation_buffer_multiplier: number;
    overdraft_tolerance_percent: number;
    low_balance_remaining_requests: number;
    markup_multiplier: number;
  };
  payments: {
    min_topup_amount_rub: number;
  };
  openrouter: {
    openrouter_balance_threshold_usd: number;
  };
  reconciliation: {
    reconciliation_threshold_percent: number;
  };
  exchange_rate: {
    auto_exchange_rate_enabled: boolean;
    auto_exchange_rate_interval_minutes: number;
    auto_exchange_rate_mode: string;
  };
}

export interface ExportSummaryResponse {
  period: { date_from: string; date_to: string };
  revenue: {
    total_micro_rub: number;
    total_display: string;
    credit_count: number;
    debit_count: number;
  };
  costs: {
    total_micro_usd: number;
    total_usd_display: string;
  };
  margin: {
    percent: number;
    effective_revenue_micro_rub: number;
  };
  users: {
    unique_payers: number;
    new_registrations: number;
  };
  requests: {
    total: number;
    completed: number;
    failed: number;
    partial: number;
  };
}

export type AlertSeverity = "critical" | "warning" | "info";
export type AlertStatus = "new" | "acknowledged" | "resolved";

export interface AlertItem {
  id: string;
  severity: AlertSeverity;
  status: AlertStatus;
  source: string;
  title: string;
  message: string;
  details: Record<string, unknown> | null;
  created_at: string;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
}

export interface AlertListResponse {
  items: AlertItem[];
  total: number;
  page: number;
  per_page: number;
  unread_count: number;
}

export interface AuditLogItem {
  id: string;
  actor_id: string | null;
  actor_email: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
  ip_address: string | null;
}

export interface AuditLogResponse {
  items: AuditLogItem[];
  total: number;
  page: number;
  per_page: number;
}

export interface ApiKey {
  id: string;
  name: string;
  label: string;
  key_suffix: string;
  status: string;
  is_primary: boolean;
  account_name: string | null;
  account_blocked: boolean;
  total_requests: number;
  total_cost_micro_usd: number;
  last_used_at: string | null;
  created_at: string;
}

// Request tracking (FEATURE 1)
export interface RequestRow {
  request_id: string;
  created_at: string;
  completed_at: string | null;
  user_id: string;
  user_email: string | null;
  model_id: string;
  model_name: string | null;
  status: string;
  response_received: boolean;
  input_tokens: number;
  output_tokens: number;
  prompt_length_chars: number | null;
  openrouter_cost_micro_usd: number;
  openrouter_cost_usd: number;
  our_cost_micro_rub: number;
  our_cost_display: string;
  user_cost_micro_rub: number;
  user_cost_display: string;
}

export interface RequestListResponse {
  items: RequestRow[];
  total: number;
  page: number;
  per_page: number;
}
