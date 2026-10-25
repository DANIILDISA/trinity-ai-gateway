# BILLING_LOGIC.md — Trinity Billing System

> **Document version:** 2.0
> **Last updated:** 2026-05-27
> **Status:** Canonical reference — reconstructed from the live billing code
> **Verified against:** `services/billing_service.py`, `token_estimator.py`, `openrouter_proxy.py`, `tariff_service.py`, `payment_service.py`, `rate_limiter.py`, `notification_service.py`, `tasks/reconciliation.py`, `tasks/cleanup.py`, `tasks/exchange_rate.py`, `cbr_service.py`

---

## Table of Contents

1. [Overview](#1-overview)
2. [Request Lifecycle](#2-request-lifecycle)
3. [Token Estimation & Context Window](#3-token-estimation--context-window)
4. [Reservation System](#4-reservation-system)
5. [Streaming & Overdraft](#5-streaming--overdraft)
6. [Finalization & Rollback](#6-finalization--rollback)
7. [Cost Calculation (incl. web search)](#7-cost-calculation-incl-web-search)
8. [Pricing & Exchange Rate](#8-pricing--exchange-rate)
9. [Rate Limiting](#9-rate-limiting)
10. [Payments (YooKassa)](#10-payments-yookassa)
11. [Refunds](#11-refunds)
12. [Reconciliation](#12-reconciliation)
13. [Admin Financial Flows](#13-admin-financial-flows)
14. [Alerts](#14-alerts)
15. [User Notifications](#15-user-notifications)
16. [Edge-Case Matrix](#16-edge-case-matrix)

---

## 1. Overview

Trinity is pay-per-use. Users top up a ruble balance (YooKassa/SBP) and are charged per chat request based on actual token consumption, billed through a **two-stage reserve → finalize** pattern.

```
RESERVE     lock estimated_cost × 2 from balance  (before the OpenRouter call)
FINALIZE    charge the actual cost, free the rest (after the stream completes)
  — or —
RELEASE     free the whole reservation             (on failure / timeout / empty disconnect)
```

This guarantees: users can't overdraw (the lock precedes the API call and is enforced by a DB CHECK + row lock); failed requests cost nothing (release creates no transaction); concurrent requests from one user can't double-spend (`SELECT … FOR UPDATE`).

### 1.1 Money flow

```
Top-up (YooKassa webhook, status=succeeded)
  → transactions.credit (+amount)            via credit_balance(idempotency_key = payment_id)
  → users.balance_micro_rub += amount

Chat request
  → reservation INSERT (status=active)       users.reserved_micro_rub += min(available, est×2)
  → OpenRouter streams the answer
  → finalize: users.balance_micro_rub -= actual
              users.reserved_micro_rub -= reserved
              transactions.debit (−actual)   idempotency_key = request_id
              usage_logs row (status=completed)

Failure after reservation
  → release: users.reserved_micro_rub -= reserved   (NO transaction, NO balance change)
             usage_logs row (status=failed)
```

### 1.2 Invariants (verified daily by reconciliation)

1. `users.balance_micro_rub == SUM(transactions.amount_micro_rub)` per user.
2. `balance_micro_rub >= 0` (DB CHECK).
3. `balance_micro_rub >= reserved_micro_rub` (DB CHECK).
4. Every `transactions` row has a unique `idempotency_key` (DB UNIQUE).
5. Every completed `usage_logs` row with `actual_cost > 0` has a matching debit transaction (`request_id`).

---

## 2. Request Lifecycle

Entry point: `billing_service.process_chat_request(...)` — an async generator that yields SSE events. Invoked by `POST /api/v1/chat/conversations/{id}/messages` after the endpoint has already persisted the user message (and any attached images) and committed.

```
STEP 1  Load user (plain read). Reject if status != 'active' (AccountBlockedError).
        check_rate_limit() (3 sliding windows). Raising here aborts before any lock.
STEP 2  Load current model_pricing row; reject if missing / status != 'available'
        (ModelUnavailableError).
STEP 3  Build context: conversation history (+ re-injected images) + the new message.
        truncate_context() to the model window (reserve 25% for output).
        estimate_request_cost_with_history() → input/output token estimate + cost.
STEP 4  create_reservation(): SELECT … FOR UPDATE on the user, check available balance,
        INSERT reservation (active), bump users.reserved_micro_rub.  COMMIT immediately
        (releases the row lock before the long stream; makes the reservation visible to
        the finalize/release locks).
STEP 5  Stream from OpenRouter (proxy.proxy_to_openrouter_with_retry), wrapped in
        stream_with_cost_monitoring. Each chunk is awaited under asyncio.wait_for with a
        per-model per-chunk timeout (stream_timeout_seconds). Disconnect is polled between
        chunks. Content deltas are forwarded as SSE; the final usage chunk is captured.
STEP 6  finalize_reservation(): compute actual cost, debit (clamped to balance), free the
        reservation, write the debit transaction + usage_log. COMMIT.
STEP 7  Persist the assistant message, emit message_done, record latency samples, run the
        low-balance notification check.
```

**SSE events emitted** (`event: <name>` + JSON `data:`):

| Event | When | Payload |
|---|---|---|
| `message_start` | step 5 start | `request_id`, `estimated_cost` |
| `context_truncated` | if history was trimmed | `original_tokens`, `truncated_to_tokens`, `messages_kept`, Russian `message` |
| `content_delta` | per content chunk | `delta` |
| `message_done` | success | `request_id`, `actual_cost`, `balance_after`, `tokens_used.{input,output}`, `overdraft_absorbed` |
| `error` | failure | `code`, `message` (+ flattened `details`, e.g. `required`/`available`) |

There is **no periodic `ping`/heartbeat event**; the SSE response sets `X-Accel-Buffering: no` and relies on nginx buffering being disabled.

---

## 3. Token Estimation & Context Window

`app/services/token_estimator.py`. Tokens are counted with `tiktoken` `cl100k_base` for all models (approximate for Anthropic; the buffers below absorb the error). Per-message overhead = 4 tokens, reply overhead = 3 tokens. Multimodal image parts count a flat **1000 tokens** each for estimation.

### 3.1 History-aware estimate (the path actually used)
`estimate_request_cost_with_history(conversation_id, new_message, model_id, max_output)`:

- **Input** = `historical_input + new_message_tokens × estimation_buffer`, where `historical_input` is the **(input + output) token count of the most recent completed/partial `usage_logs` row for the conversation** (OpenRouter's exact counts for the prior turn already reflect the full context, so the whole transcript isn't re-tokenized). `estimation_buffer` defaults to `1.3`.
- **Output** = `2 × avg(output_tokens)` over the conversation's prior completed/partial requests, or **500** if there's no history. Clamped to `[100, min(model.max_output_tokens, 8192)]`.
- **Cost** = `ceil(input × input_price_μ₽/1M) + ceil(output × output_price_μ₽/1M)`, priced from the **stored RUB columns** (`input_price_micro_rub` / `output_price_micro_rub`) — the same columns finalization charges from, so admin overrides and free models (RUB price 0) are honoured.
- **`reservation_micro_rub`** = `max(estimated_cost, int(estimated_cost × reservation_buffer_multiplier))` (setting default `2.0`). *Note: this field is informational; the actual amount locked is recomputed in `create_reservation` with a hardcoded ×2 — see §4.*

A simpler `estimate_request_cost(messages, …)` exists (re-tokenizes the full message list, output = `input × 0.5`) and is retained for tests/legacy callers.

### 3.2 Context-window truncation
`truncate_context(messages, context_window, reserve_tokens=int(0.25 × max_context))`:

1. If total tokens ≤ `context_window − reserve_tokens`, keep everything.
2. Otherwise always keep the **system message** (index 0, if present) and the **last** message; drop oldest history first, never mid-message.
3. If `[system, last]` still overflows, drop the system message.
4. If `[last]` alone overflows → raise `ContextTooLongError` (HTTP 413, "Сообщение слишком длинное для выбранной модели").

When truncation happens, a `context_truncated` SSE event tells the user history was trimmed.

---

## 4. Reservation System

`create_reservation(user_id, estimated_cost_micro_rub, model_id, …)`:

```
SELECT balance_micro_rub, reserved_micro_rub FROM users WHERE id = :uid
  FOR UPDATE  (populate_existing=True — overwrite the earlier unlocked read so a
               concurrent committed reservation is seen under the lock)
available = balance − reserved
if available < estimated_cost:  raise InsufficientFundsError(required, available)
desired           = estimated_cost × 2            # RESERVATION_BUFFER_FACTOR (hardcoded)
reservation_amount = min(available, desired)      # partial buffer if can't afford 2×
INSERT reservation(status=active, reserved_micro_rub=reservation_amount, est tokens…)
users.reserved_micro_rub += reservation_amount    # DB CHECK guards balance >= reserved
```

- **Insufficient funds** is decided against the **base estimate** (no buffer). If the user can afford the estimate but not the full 2×, the reservation is capped at the available balance (a thinner safety margin, request still proceeds).
- **Concurrency:** `FOR UPDATE` serializes a single user's concurrent reservations so two requests can't both pass the balance check against the same funds. Distinct users don't contend.
- **Commit-before-stream:** `process_chat_request` commits right after `create_reservation`, releasing the user-row lock before the (potentially minutes-long) stream and making the reservation visible to the later finalize/release locks.

### 4.1 Expiry safety net
The cleanup cron (`tasks/cleanup.release_expired_reservations`, every minute) finds reservations still `active` after **5 minutes**, sets them `expired`, refunds `users.reserved_micro_rub`, and files a warning alert. Each reservation is handled in its own savepoint so one failure doesn't abort the sweep.

---

## 5. Streaming & Overdraft

`stream_with_cost_monitoring(...)` wraps the OpenRouter chunk stream:

- Forwards `content` deltas; captures the final `usage` chunk (`input_tokens`, `output_tokens`, `cost_usd`); stops on `done`.
- Every ~200 collected chars it re-estimates output tokens and computes a running cost against the **overdraft ceiling** = `reserved × (100 + overdraft_tolerance_percent)/100` (`overdraft_tolerance_percent` constant default **7**). Crossing the ceiling is **logged only — the stream is never cut off** (cutting mid-answer is worse UX than absorbing a few kopeks).
- **Agentic tool loop (tr-rkl / audit M1):** the multi-round loop (`run_agentic_loop`) is bounded by the **same** overdraft ceiling, but with a different action. Because each round is a fresh model call (8 rounds can blow 300%+ past the reserve, not a few kopeks), and because declining the *next* round never cuts a partial answer, the loop's `budget_guard` is checked **before each round** and **HALTS** (emits `tool_loop_halted reason=budget`) once the accumulated cost — priced with the finalize formula `compute_actual_cost_micro_rub` — would exceed `reserved × (100 + overdraft_tolerance_percent)/100`. Same ceiling, halt-vs-absorb chosen per path.
- In `process_chat_request`, each `__anext__()` is awaited under `asyncio.wait_for(timeout = pricing.stream_timeout_seconds)`. This single ceiling covers both **time-to-first-byte** and **inter-chunk silence**. Default 30s; reasoning/deep-research models get 300s (assigned by the model sync). A trip raises `asyncio.TimeoutError` → release + a `timeout` error event ("Модель не ответила за N секунд. Средства не списаны.").
- Client disconnect is polled (`request.is_disconnected()`) between chunks → `ClientDisconnectedError`.

OpenRouter is always asked for usage via `extra_body={"usage":{"include": true}}` so finalization bills on **OpenRouter's reported counts**, not a local estimate — critical for web search (see §7).

---

## 6. Finalization & Rollback

### 6.1 `finalize_reservation(reservation_id, actual_cost_micro_rub, prompt_tokens, completion_tokens, …)`
Atomic, under `FOR UPDATE` on the reservation then the user:

```
other_reserved      = max(user.reserved − reservation.reserved_micro_rub, 0)  # held for OTHER active reservations
available_for_debit = max(user.balance − other_reserved, 0)                   # this request's own hold + free balance
actual_debit        = min(actual_cost, available_for_debit)                   # clamp — never spend OTHERS' holds
overdraft_absorbed  = actual_debit < actual_cost
overdraft_amount    = actual_cost − actual_debit           # operator's loss if clamped
users.balance  -= actual_debit
users.reserved  = other_reserved                           # release THIS reservation (clamped ≥ 0)
reservation: status=finalized, actual_micro_rub=actual_debit,
             refunded_micro_rub = max(reserved − actual_debit, 0)
if actual_debit > 0:  INSERT transactions.debit(−actual_debit, idempotency_key=request_id)
INSERT usage_logs(status=…, estimated/actual costs, overdraft_absorbed, timings, web_search…)
if overdraft_amount > 0:  create_alert(critical, "billing", "Overdraft absorbed…")
```

- A **zero-cost** finalize (free models) writes the usage_log + frees the reservation but creates no debit transaction.
- The pre-request estimate stored in the usage_log is the real pre-buffer cost the orchestrator passes (`estimated_cost_micro_rub`); legacy callers fall back to `reserved // 2`.
- **Sibling reservations (tr-6i8s).** The clamp is against `balance − other_reserved`, **not** the raw balance, so a request that overruns its own reservation can never debit funds still held for OTHER in-flight reservations. Charging into those would push `balance` below the still-`reserved` total and trip the non-deferrable `CHECK (balance ≥ reserved)`, rolling back the **entire** debit — the answer is delivered but never billed (lost revenue), the reservation hangs, and a spurious "unexpected billing error" critical alert fires. With no other reservations, `other_reserved == 0` and this reduces to the old `min(actual_cost, balance)` clamp. Because a sibling hold may free balance moments later when it finalizes, an amount absorbed here is **not** retroactively re-collected — per §11 overdrafts are intentionally operator-absorbed, not clawed back.
- The DB `CHECK (balance_micro_rub >= 0)` and `CHECK (balance_micro_rub >= reserved_micro_rub)` are the last-resort guards behind the clamp.

### 6.2 `release_reservation(reservation_id, failure_reason)`
Frees the lock without charging: `users.reserved -= reserved`, reservation → `released`. When a `failure_reason` is given, a `usage_logs` row with `status='failed'`, zero costs, and the error message is written (for analytics). **No transaction is created.**

### 6.3 `finalize_partial(...)` — client disconnect mid-stream
If content was already delivered, bill for what was generated: use the real usage chunk if it arrived, else estimate tokens from the collected text; then run the standard finalize with `status='partial'`. If **no** content was delivered, fall back to `release_reservation`. Either way the partial assistant message is persisted so the chat isn't empty on reload.

### 6.4 Error routing in `process_chat_request`
| Caught | Action |
|---|---|
| `asyncio.TimeoutError` | rollback → release (`openrouter_timeout`) → warning alert → `timeout` SSE error |
| `ClientDisconnectedError` | finalize_partial (if content) else release; no SSE (client gone) |
| OpenRouter errors (`OpenRouterError/ServerError/ClientError/RateLimitError`, `ServiceUnavailableError`) | rollback → release → warning alert → `error` SSE (`code`/`message` from the exception) |
| Other `TrinityError` (rate limit, context-too-long, …) | rollback → release → re-raise (endpoint translates to an `error` SSE) |
| Any other `Exception` | rollback → release → **critical** alert → re-raise |

---

## 7. Cost Calculation (incl. web search)

`compute_actual_cost_micro_rub(input_tokens, output_tokens, pricing, cost_usd, exchange_rate, markup, web_search_enabled)`:

```
token_cost = ceil(in × input_price_μ₽/1M) + ceil(out × output_price_μ₽/1M)

if not (web_search_enabled and cost_usd > 0):
    return token_cost                       # normal requests: token-based, the price users see

# Web-search requests with a real reported cost:
cost_basis = micro_usd_to_micro_rub(usd_float_to_micro_usd(cost_usd), exchange_rate)
cost_with_markup = ceil(cost_basis × round(markup×100) / 100)
return max(token_cost, cost_with_markup)    # USD-cost basis REPLACES token charge
```

**Why web search is special:** OpenRouter's web plugin adds a flat per-search surcharge (~$0.005) that it folds into `usage.cost` but **not** into token counts — a per-token price can't express it (token-only billing undercharges by ~28×). For web-search requests Trinity therefore bills on OpenRouter's USD cost × rate × markup, which already contains both the token portion and the surcharge. `max(token_cost, …)` keeps the token amount as a floor so a stale price sync can't undercut it. The web plugin is attached only when `pricing.supports_web_search` is true; otherwise the flag is ignored (logged).

---

## 8. Pricing & Exchange Rate

### 8.1 RUB price formula
`tariff_service.calculate_rub_price(usd_micro, rate_micro_rub, markup)`:
```
base       = ceil(usd_micro × rate_micro_rub / 10⁶)
markup_int = round(markup × 100)             # 1.25 → 125 (round, not int)
price      = ceil(base × markup_int / 100)
```

### 8.2 Displayed price == charged price (structural invariant)
- **Charged price:** the stored `model_pricing.input_price_micro_rub` / `output_price_micro_rub` columns. Estimation and finalization both use these.
- **Displayed price:** the public models/pricing API (`tariff_service._row_to_public`) returns **those same stored columns** — the exact price the user is billed — for every row.

Because both read the same stored columns, **displayed price == charged price is a structural invariant** for all models, including **price-override** models (whose stored columns hold the admin override) and any window where a settings change has not yet re-baselined the stored rows. A rate update or the daily model sync rewrites the stored RUB columns from USD × rate × markup for non-override models (override rows are skipped and keep their pinned price), so the displayed catalog follows those rewrites. (Historical note: `_row_to_public` previously recomputed the display **live** from USD × rate × markup, so an override — or a markup-only settings change — could display a price different from the one charged; tr-bztb removed that divergence. Whether a markup-only settings change should promptly re-baseline the stored/charged price is tracked separately in tr-2ubv.20.)

### 8.3 Manual rate update — `update_exchange_rate(new_rate, admin_id, enforce_cooldown=False)`
1. Cooldown is **disabled by default** (`enforce_cooldown=False`) — manual admin updates always apply. (The 7-day window is only enforced if a caller explicitly opts in; product removed it from the manual path.)
2. Write `exchange_rate_micro_rub` + `exchange_rate_updated_at`.
3. For each current, non-override model: archive the row (`is_current=false`, `effective_until=now`) and insert a fresh current row with recomputed RUB prices.
4. Audit log (`admin.tariff.update_rate` for an admin, `system.tariff.auto_update` for automation).
5. Fan out `tariff_change` notifications to active users (in-app only).
6. Commit, then invalidate the `pricing:all` / `pricing:current` Redis caches.

`override_model_price` and `clear_model_price_override` archive + insert similarly (the latter recomputes from the live rate and clears the override flag, re-enrolling the model in automatic updates).

### 8.4 Automated CBR updater
`tasks/exchange_rate.run_exchange_rate_update` (a background loop pokes it every 60s; it self-throttles):

- Gated by `auto_exchange_rate_enabled` (default **false**). If off → no-op.
- Throttled to `auto_exchange_rate_interval_minutes` (default **1440** = daily) via the `last_exchange_rate_update` timestamp.
- Fetches the live USD/RUB rate from the **Central Bank of Russia** daily XML (`cbr.ru/scripts/XML_daily.asp`, windows-1251, comma decimals) via `cbr_service.fetch_cbr_usd_rate()`. On any fetch/parse failure it returns `None` and applies nothing.
- `auto_exchange_rate_mode = "auto"` → apply immediately (system actor, no cooldown). `"manual_confirm"` → raise an `exchange_rate` warning alert carrying the proposed rate; an admin applies it via `POST /admin/tariffs/confirm-rate`.
- In `DEMO_MODE` it fetches but only logs, never saving.

> The admin reference endpoint `GET /admin/tariffs/cbr-rate` returns a **hardcoded mock 82.50** — the real CBR fetch lives only in the auto-updater.

---

## 9. Rate Limiting

`rate_limiter.check_rate_limit(db, redis, user_id)` — three Redis sorted-set sliding windows:

| Window | Setting | Seed | Code default |
|---|---|---|---|
| per minute | `rate_limit_per_minute` | 10 | 10 |
| per hour | `rate_limit_per_hour` | 100 | 100 |
| per day | `rate_limit_per_day` | 500 | **1000** |

Each call `ZREMRANGEBYSCORE`s expired entries, `ZADD`s the request, `ZCARD`s the window. If any `count > limit`, the just-added member is removed (so a rejected request doesn't extend its own block) and `RateLimitedError(retry_after)` is raised (HTTP 429, `Retry-After` header). Limits are read through a 60s Redis cache (`settings:{key}`), so admin changes propagate within a minute.

`check_rate_limit` runs both as the `require_rate_limit` dependency **and** inside `process_chat_request` STEP 1. On the chat path only STEP 1 applies (the SSE endpoint depends on `require_active`, not `require_rate_limit`), so a chat request counts once.

**Abuse detection** (`check_rate_limit_abuse`, called from the `require_rate_limit` dependency on rejection): counts rate-limit hits per user per hour (`rate_limit_abuse_threshold`, default 10); on crossing, a single warning alert is filed using a Redis `SET NX EX` flag to dedupe to once/hour.

---

## 10. Payments (YooKassa)

`app/services/payment_service.py`. No SDK — direct `httpx` to `api.yookassa.ru/v3`.

### 10.1 Create — `POST /api/v1/payments/create`
- Validates `amount >= min_topup_amount_rub` (default 500) → else 422.
- POSTs a payment with `method.type = "sbp"`, `amount = "{rub}.00"`, `confirmation.type = "redirect"` (`return_url = frontend_url`), `metadata.user_id`, and a fresh `Idempotency-Key`. Auth = (`shop_id`, `secret_key`).
- Returns `{payment_id, confirmation_url, status:"pending", amount}`.

### 10.2 Webhook — `POST /api/v1/payments/webhook`
- Reads the raw body + `X-YooKassa-Signature`. Verifies an **HMAC-SHA256** of the raw body keyed by `yookassa_secret_key` (`hmac.compare_digest`). Mismatch → 401 + a warning alert. **Exempt from CSRF.**
- Only `object.status == "succeeded"` credits; other statuses return `{processed: false}`.
- Computes `amount_micro_rub = Decimal(amount.value) × 10⁶`, then `credit_balance(idempotency_key = payment_id)`. The unique `idempotency_key` makes duplicate webhooks safe (returns `{duplicate: true}`, no double credit).
- On first success: fire a `topup_confirmed` notification, commit.

### 10.3 Status — `GET /api/v1/payments/{id}/status`
Fetches the payment from YooKassa; if `metadata.user_id` ≠ the caller, returns 404 (no existence leak).

---

## 11. Refunds

**There is no automatic-refund flow, and no `refund` transaction is ever written.** *(The previous version of this document specified a `create_automatic_refund` helper. It does not exist in the codebase.)*

- Failed/timed-out/empty-disconnect requests call `release_reservation`, which only frees `users.reserved_micro_rub`. Since the user was never debited, no money needs returning — there is nothing to refund.
- Overdrafts (actual cost > balance) are **absorbed by the operator**: the debit is clamped to the available balance, the shortfall is recorded in `usage_logs.overdraft_absorbed_micro_rub`, and a critical alert fires.
- `reservations.refunded_micro_rub` is bookkeeping (`reserved − actual`), not a refund transaction.
- The `transaction_type` enum includes `refund` and `usage_logs.status` allows `refunded`, but **no code path produces them.** A manual operator refund would today be done as an `adjustment` (see §13) or out-of-band via YooKassa's API.

---

## 12. Reconciliation

`tasks/reconciliation.run_daily_reconciliation` runs IN-PROCESS under the Redis leader lock (`_daily_reconciliation_loop` in `app/main.py`, leader-only: once on startup + every 24h — tr-376l), so the safety net fires even where the host crontab is never installed. It is NOT a host-cron job (a host cron plus the in-process loop is the double-runner race, tr-6qf7). Four checks; discrepancies create `alerts` and the task always sends one Telegram summary.

1. **Balance invariant** — per user, `balance_micro_rub == SUM(transactions.amount_micro_rub)`. Mismatch → **critical** alert.
2. **Stale reservations** — any `active` reservation older than **10 minutes** (the every-minute cleanup should have caught it) → **warning**.
3. **Revenue vs cost** — yesterday's debit revenue vs `SUM(usage_logs.openrouter_cost_micro_usd)` converted at the current rate. Margin = `(revenue − cost) / cost × 100`. **Pricing margin** adds `overdraft_absorbed` back to revenue so absorbed losses don't look like a pricing fault. Thresholds (hardcoded): `< 0%` → critical; `< 10%` → warning, downgraded to info if pricing margin ≥ 20%.
4. **Orphaned usage logs** — yesterday's `completed` logs with `actual_cost > 0` but no matching debit transaction → **critical**.

> The dashboard (`/admin/dashboard`) computes margin differently: `(effective_revenue − cost) / effective_revenue` where `effective_revenue = revenue − overdraft_absorbed`. Reconciliation divides by cost; the dashboard divides by revenue. Both are intentional; they answer different questions. The seeded `reconciliation_threshold_percent` setting is **not** consulted (thresholds are hardcoded).

---

## 13. Admin Financial Flows

- **Balance adjustment** — `admin_adjust_balance(admin_id, user_id, amount_micro_rub, reason)` (`POST /admin/users/{id}/adjust-balance`). Requires a reason ≥ 5 chars. Positive = credit, negative = debit; a negative amount that would overdraw is clamped to the available balance (operator absorbs nothing — it simply debits less). Writes an `adjustment` transaction (`idempotency_key = adj:{uuid4}`) + an `admin.user.adjust_balance` audit row. The request body accepts either `amount` (whole rubles, legacy) or `amount_micro_rub`.
- **Manual OpenRouter balance** — `PUT /admin/settings/openrouter-balance` stores `openrouter_manual_balance_usd`. OpenRouter doesn't expose balance to provisioned keys, so the dashboard/health threshold falls back to this admin-entered value when the live probe is 0/None.
- **Dashboard metrics** — `/admin/dashboard` returns revenue (today/week/month), OpenRouter cost (USD), margin, a per-user balance-vs-ledger reconciliation status, the resolved OpenRouter balance (`live`/`manual`/`unavailable`), and active users / total requests today. `/admin/dashboard/revenue-by-model` breaks revenue + cost + margin down per model.
- **Exports** — `/admin/export/{transactions|summary|per-user}` (CSV with UTF-8 BOM for Excel, or JSON summary). All export, tariff, key, user, alert, and settings actions write an `admin_audit_log` row.

---

## 14. Alerts

`alert_service.create_alert(severity, source, title, message?, details?)` inserts an `alerts` row (flush, not commit — caller owns the transaction) and dispatches by severity:

| Severity | Dashboard | Telegram | Email |
|---|---|---|---|
| `critical` | ✓ | ✓ | ✓ (`admin_email`) |
| `warning` | ✓ | ✓ | — |
| `info` | ✓ | — | — |

Telegram (`telegram_service`) is fire-and-forget with `🔴`/`🟡` prefixes and is suppressed when `telegram_demo_mode` is true (default) or the token/chat-id is unset. Email (`email_service.send_alert_email`) goes through Resend and is best-effort. Alert sources seen in code: `billing`, `openrouter`, `payments`, `rate_limiter`, `exchange_rate`, `system`.

**Representative triggers:** OpenRouter balance below threshold (critical); balance mismatch / negative margin / orphaned usage log in reconciliation (critical); overdraft absorbed at finalize (critical); unexpected billing error (critical); OpenRouter request failure or timeout (warning); OpenRouter reported 0 tokens (warning); expired reservation (warning); invalid YooKassa webhook signature (warning); rate-limit abuse (warning); pending CBR rate in manual-confirm mode (warning).

---

## 15. User Notifications

`notification_service` (in-app rows + fire-and-forget Resend email). Rows are written through the caller's session (no own commit).

| Type | Trigger | Channel | Limit |
|---|---|---|---|
| `low_balance` | after a finalize, when `balance // avg_cost <= low_balance_remaining_requests` | `both` | once / 24h per user |
| `topup_confirmed` | successful YooKassa webhook | `in_app` (+ email send) | every top-up |
| `tariff_change` | exchange-rate change | `in_app` only (email intentionally off) | every change |

`avg_cost` is the average `actual_cost_micro_rub` over the user's **last 100 completed** requests; users with no completed history are skipped (no notification). `low_balance` also emails a top-up link; `tariff_change` is fanned out to every active user.

---

## 16. Edge-Case Matrix

| Scenario | When | Action | User sees |
|---|---|---|---|
| Insufficient funds | before reservation | reject if `available < base estimate` | `insufficient_funds` (402) + required/available |
| Marginal funds | can afford estimate, not 2× | reserve `available` (partial buffer) | proceeds normally |
| Context too long | single message exceeds window | reject | `context_too_long` (413) |
| History too long | conversation exceeds window | truncate oldest, keep system + recent | `context_truncated` SSE |
| Free model | RUB price 0 | reserve 0, finalize with no debit txn | normal, cost 0 |
| Web search request | `web_search_enabled` + real `cost_usd` | bill `max(token_cost, USD-cost × rate × markup)` | normal cost incl. surcharge |
| OpenRouter 4xx | during stream setup | release | `openrouter_client_error` |
| OpenRouter 402 (paid model) | platform credits out | release | `paid_models_unavailable` (402) |
| OpenRouter 5xx / 429 | during stream | retry ≤ 3× (backoff `2^attempt`s; 429 honours `Retry-After`), then release | error event |
| Per-chunk timeout | TTFB or mid-stream silence > `stream_timeout_seconds` | release | `timeout`, "Средства не списаны" |
| Client disconnect, content delivered | mid-stream | finalize_partial (charge for delivered), persist partial reply | (gone) — billed for partial |
| Client disconnect, nothing delivered | mid-stream | release | (gone) — not billed |
| OpenRouter reports 0 tokens | final usage chunk | estimate from collected text + warning alert | normal charge |
| Cost > balance (overdraft) | finalize | clamp debit to balance, record `overdraft_absorbed`, critical alert | normal `message_done` |
| No active primary key | reservation/stream | 503 from proxy | `service_unavailable` |
| Reservation never finalized | crash/hang | cleanup expires after 5 min, refunds reserved | n/a |
| Duplicate payment webhook | YooKassa retry | idempotency_key dedups | n/a (`duplicate: true`) |
