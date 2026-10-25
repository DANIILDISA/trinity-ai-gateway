# SPEC.md — Trinity Technical Specification

> **Document version:** 2.0
> **Last updated:** 2026-05-27
> **Status:** Canonical reference — reconstructed from the live codebase
> **Audience:** developers working on Trinity

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Tech Stack & Versions](#2-tech-stack--versions)
3. [System Architecture](#3-system-architecture)
4. [Directory Structure](#4-directory-structure)
5. [External Dependencies](#5-external-dependencies)
6. [Authentication & Authorization](#6-authentication--authorization)
7. [API Endpoints](#7-api-endpoints)
8. [Streaming Architecture](#8-streaming-architecture)
9. [Chat, Models & Images](#9-chat-models--images)
10. [Error Handling](#10-error-handling)
11. [Admin Panel](#11-admin-panel)
12. [Background Tasks & Scheduling](#12-background-tasks--scheduling)
13. [Deployment](#13-deployment)
14. [Monitoring](#14-monitoring)
15. [Environment Variables](#15-environment-variables)
16. [Security](#16-security)
17. [Legal Pages](#17-legal-pages)
18. [Glossary](#18-glossary)
19. [Known Gaps & Discrepancies](#19-known-gaps--discrepancies)

---

## 1. System Overview

Trinity is a **ruble-denominated AI model gateway** for the Russian market. Users register, top up a ruble balance via SBP (YooKassa), and chat with AI models proxied through **OpenRouter**. Billing is pay-per-use: each request is charged per actual token consumption (plus a flat surcharge for web search), with prices derived from OpenRouter's USD pricing × a configurable USD/RUB exchange rate × a markup.

**Core flow:**
```
Browser → Next.js → FastAPI → OpenRouter → AI model
                       ↕
              PostgreSQL (balance, ledger, chat, pricing, audit)
              Redis (rate limits, caches, CSRF, leader lock, latency)
```

**As-built capabilities:** email/password auth with JWT + DB-backed refresh tokens; streaming chat (SSE) with reserve→finalize billing; **multimodal image input** with a persistent gallery; an optional **web-search** plugin; an automatically-synced **OpenRouter model catalogue**; an **automated CBR exchange-rate** updater; and a rich admin panel (dashboard, users, tariffs, keys, alerts, audit, requests, monitoring, storage, exports, settings).

**Scale posture:** multi-worker (gunicorn `-w 4`) behind PgBouncer, designed for ~1 000+ DAU on a single VPS.

---

## 2. Tech Stack & Versions

From `backend/pyproject.toml` and `frontend/package.json`:

| Component | Technology | Version | Notes |
|---|---|---|---|
| Backend | FastAPI | ≥ 0.136 | async, SSE streaming |
| Python | CPython | 3.12 | |
| ASGI server | Uvicorn + **gunicorn** | uvicorn ≥ 0.34, gunicorn ≥ 23 | gunicorn supervises 4 `TrinityUvicornWorker` processes; uvloop |
| ORM / driver | SQLAlchemy 2.0 (async) + asyncpg | sqlalchemy ≥ 2.0, asyncpg ≥ 0.30 | statement cache disabled for PgBouncer |
| Migrations | Alembic | ≥ 1.14 | hand-written, 10 revisions |
| Database | PostgreSQL | 16 | |
| Pooler | PgBouncer | latest | transaction mode (dev compose) |
| Cache | Redis | 7 | `redis[hiredis]` ≥ 5.0 |
| AI gateway | OpenRouter via **OpenAI SDK** | openai ≥ 1.50 | `base_url=https://openrouter.ai/api/v1` |
| Payments | YooKassa REST v3 | via httpx | SBP; no SDK |
| Email | **Resend HTTP API** | via httpx | Jinja2 templates; **not SMTP** |
| Alerts | Telegram Bot API | via httpx | |
| FX source | Central Bank of Russia XML | via httpx | `cbr.ru/scripts/XML_daily.asp` |
| Tokenizer | tiktoken | ≥ 0.9 | `cl100k_base` for all models |
| Crypto | bcrypt (direct) + PyJWT + cryptography (AES-GCM) | pyjwt ≥ 2.10, cryptography ≥ 43 | bcrypt used directly, **not** via passlib |
| Frontend | Next.js (App Router) + React | next 15.5.4, react 19.0.0 | react-markdown, Tailwind 3.4, TS 5 |
| Reverse proxy | nginx | 1.24-alpine | TLS, rate limit, SSE pass-through |

> `passlib[bcrypt]` and `psutil` are declared in `pyproject.toml` but **not used** — password hashing calls `bcrypt` directly, and system metrics are read from `/proc`/`statvfs`.

---

## 3. System Architecture

```
                          Internet
                             │
                ┌────────────▼────────────┐
                │   nginx (1.24)          │  TLS 1.2/1.3, HSTS+CSP, 100r/m per IP,
                │   :80/:443              │  client_max_body_size 30m,
                └──────┬───────────┬──────┘  SSE pass-through for /api/v1/chat/
                /      │           │ /api
                ▼      │           ▼
        ┌───────────┐  │   ┌──────────────────────────┐
        │ Next.js 15│  │   │ FastAPI (gunicorn -w 4)   │
        │  :3000    │  │   │  • auth (JWT, no OAuth)    │
        └───────────┘  │   │  • billing pipeline (SSE)  │
                       │   │  • OpenRouter proxy        │
                       │   │  • YooKassa webhooks       │
                       │   │  • admin API               │
                       │   │  • background loops*       │
                       │   └──────┬───────────┬─────────┘
                       │          │           │
                       │          ▼           ▼
                       │   ┌────────────┐ ┌────────────┐
                       │   │ PgBouncer  │ │  Redis 7   │
                       │   │ (txn mode) │ │ rate limits│
                       │   └─────┬──────┘ │ caches/CSRF│
                       │         ▼        │ leader lock│
                       │   ┌────────────┐ │ latency    │
                       │   │ PostgreSQL │ └────────────┘
                       │   │    16      │
                       │   └────────────┘
   External:  OpenRouter API · YooKassa (SBP) · Resend (email) · Telegram · cbr.ru
```

\* **Background loops run in exactly one worker.** On startup each worker tries to win a Redis lock (`trinity:bg-leader`, TTL 60s, renewed every 30s). The winner runs the health probe, model-sync, and exchange-rate loops; followers skip them. See [§12](#12-background-tasks--scheduling).

**Connection management:** the app opens its SQLAlchemy pool against PgBouncer (transaction mode). asyncpg prepared-statement caching is disabled and prepared statements get unique names (`connect_args`) for PgBouncer compatibility. `pool_pre_ping=True`, `pool_timeout=10` (pool exhaustion → a clean 503, not a hang). Alembic connects via `DATABASE_URL_DIRECT` (bypassing PgBouncer).

**Redis** holds only ephemeral state: rate-limit sliding windows, settings/pricing caches (TTL 60s), CSRF tokens, JWT blacklist, model-health + OpenRouter-balance caches, per-stage latency samples, the background-leader lock, and `last_active` write throttles. Data loss is non-critical.

---

## 4. Directory Structure

```
trinity/
├── docker-compose.yml            # dev: postgres + redis + pgbouncer + backend + frontend + seed
├── docker-compose.prod.yml       # prod: nginx + frontend + backend + redis (managed PG external)
├── .env.example
├── SPEC.md / SCHEMA.md / BILLING_LOGIC.md / README.md / DESIGN.md
│
├── backend/
│   ├── Dockerfile                # multi-stage: base (dev) / prod (gunicorn -w 4)
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py                # uses DATABASE_URL_DIRECT when set
│   │   └── versions/001…010_*.py
│   ├── pgbouncer/                # pgbouncer.ini + userlist.txt (mounted in dev compose)
│   ├── app/
│   │   ├── main.py               # app factory, middleware, exception handlers, lifespan + bg loops
│   │   ├── config.py             # pydantic-settings
│   │   ├── dependencies.py       # get_db/get_redis/get_current_user, require_auth/active/admin/rate_limit
│   │   ├── core/                 # constants, database, redis, security, exceptions, middleware, worker
│   │   ├── models/               # 15 ORM models + enums
│   │   ├── schemas/              # auth, user, billing, chat, admin, common
│   │   ├── api/v1/               # router, auth, chat, users, models, images, payments
│   │   │   └── admin/            # __init__ (aggregator) + dashboard, users, tariffs, keys,
│   │   │                         #   alerts, audit, export, settings, monitoring, requests,
│   │   │                         #   models, storage
│   │   ├── services/             # billing_service, billing_types, token_estimator,
│   │   │                         #   openrouter_proxy, demo_proxy, tariff_service, payment_service,
│   │   │                         #   rate_limiter, cbr_service, notification_service, email_service,
│   │   │                         #   alert_service, telegram_service, monitoring_service,
│   │   │                         #   model_sync_service, message_service, chat_image_service, auth_service
│   │   ├── tasks/                # reconciliation, cleanup, health_check, exchange_rate
│   │   └── templates/email/      # Jinja2 HTML + text templates
│   ├── scripts/                  # seed_dev, seed_demo, dev_reset, seed_free_models
│   └── tests/
│
├── frontend/
│   ├── Dockerfile                # dev / runner targets
│   └── src/
│       ├── app/                  # (auth)/{login,register,reset-password}, (auth)/callback,
│       │                         #   (main)/{chat,chat/[id],chat/gallery,history,balance,settings},
│       │                         #   pricing, admin/*, legal/{terms,privacy,cookies}
│       ├── components/           # ui, chat, admin, layout
│       ├── lib/                  # api, adminApi, auth, sse, microRub, formatting, errors
│       ├── hooks/                # useAuth, useBalance, useChat, useConversations, useNotifications, useAdminPolling
│       └── types/
│
├── nginx/  { nginx.conf, conf.d/trinity.conf }
├── docker/ { init-databases.sh }                 # creates trinity + trinity_test on first boot
└── scripts/ { backup.sh, restore.sh, deploy.sh, rollback.sh, health_cron.sh,
               init_letsencrypt.sh, check_migration.sh, setup_cron.sh }
```

> `services/reconciliation_service.py` and `api/v1/admin/router.py` exist but are **empty placeholders** — reconciliation lives in `tasks/reconciliation.py`, and admin routes are aggregated in `api/v1/admin/__init__.py`.

---

## 5. External Dependencies

### 5.1 OpenRouter (AI gateway)
- OpenAI-compatible API via the `openai` async SDK, `base_url=https://openrouter.ai/api/v1`, `HTTP-Referer = frontend_url`.
- Keys live in the `api_keys` table (AES-256-GCM encrypted). The proxy uses the **primary, active, non-account-blocked** key; none → HTTP 503. On startup the leader worker seeds `OPENROUTER_DEFAULT_API_KEY` as primary **only if it looks real** (`sk-or-…`).
- Every request sets `extra_body={"usage":{"include": true}}` so the final usage chunk carries real token counts **and** `usage.cost` (USD). Optional `plugins:[{"id":"web"}]` for web search. `max_tokens` is set from `model_pricing.max_output_tokens`.
- Retry (`proxy_to_openrouter_with_retry`): 5xx and 429 retried up to 3× (backoff `2^attempt`s; 429 honours `Retry-After`); 4xx propagates; 402 on a paid model → `PaidModelsUnavailableError`.
- Health probe hits `GET /auth/key` for the account balance (cached in Redis).

### 5.2 YooKassa (payments)
- REST v3 over httpx, SBP only. Redirect flow; webhook to `POST /api/v1/payments/webhook` verified by **HMAC-SHA256** of the raw body keyed by `YOOKASSA_SECRET_KEY`. Idempotent via the unique `transactions.idempotency_key = payment_id`. Min top-up `min_topup_amount_rub` (default 500 RUB). RUB only. See BILLING_LOGIC.md §10.

### 5.3 Resend (email)
- HTTP API over httpx with Jinja2 templates (`backend/app/templates/email/`). Helpers: password reset, low balance, top-up confirmed, tariff change, admin alert. Fire-and-forget; never raises. Suppressed when `EMAIL_DEMO_MODE` is true (default). **SMTP is not used** (legacy config retained).

### 5.4 Telegram (admin alerts)
- HTTP POST to the Bot API; `🔴`/`🟡` prefixes; `info` never sent. Suppressed when `TELEGRAM_DEMO_MODE` is true (default) or token/chat-id unset.

### 5.5 Central Bank of Russia (FX)
- `cbr_service.fetch_cbr_usd_rate()` parses the daily windows-1251 XML for USD/RUB. Drives the automated exchange-rate updater (§12). Never raises (returns `None` on failure).

### 5.6 Infrastructure
- Single VPS running the prod compose (nginx, frontend, backend, redis); **managed PostgreSQL** reached via `DATABASE_URL`. S3-compatible object storage (Yandex Object Storage) for `pg_dump` backups.

> **Yandex OAuth is configured (`YANDEX_*`) and a frontend `/callback` page exists, but there is no backend OAuth route or token-exchange code — it is not implemented.** See [§19](#19-known-gaps--discrepancies).

---

## 6. Authentication & Authorization

### 6.1 Registration — `POST /api/v1/auth/register`
Body `{email, password, display_name}`. Password: ≥ 8 chars, ≥ 1 digit, ≥ 1 letter (Cyrillic or Latin), validated via `PydanticCustomError`. Creates the user (`role='user'`, `status='active'`, balance 0, bcrypt hash offloaded to a worker thread), writes an `auth.register` audit row, returns `{access_token, token_type, user}`. **No refresh token is issued on register** (the user logs in to get one).

### 6.2 Login — `POST /api/v1/auth/login`
Verifies bcrypt; on failure writes `auth.login_failed` and returns 401. On success: create a DB-backed refresh token (random 64-hex, SHA-256 hashed at rest), set it as an httpOnly, SameSite=strict cookie (`secure` in production), write `auth.login`, return the access token + user. Blocked users **can** log in (endpoint-level guards restrict them, see §6.5).

### 6.3 Refresh — `POST /api/v1/auth/refresh`
Reads the `refresh_token` cookie, looks it up by hash. Expired → delete + 401. Valid → **rotate**: delete the old token, issue a new one **inheriting the original `expires_at`** (no indefinite extension), write `auth.token_refresh`, return a fresh access token.

### 6.4 Logout — `POST /api/v1/auth/logout`
Deletes the refresh token, blacklists the access token's `jti` in Redis for its remaining TTL, clears the cookie, writes `auth.logout`.

### 6.5 Authorization (FastAPI dependencies)
| Dependency | Check |
|---|---|
| `get_current_user` | valid JWT (HS256, 15-min) not blacklisted; user exists |
| `require_auth` | `get_current_user` + throttled `last_active_at` write (≤ once/60s via Redis `SET NX`) |
| `require_active` | `require_auth` + `status == 'active'` (else 403 "Аккаунт заблокирован") |
| `require_admin` | `require_active` + `role == 'admin'` (else 403 "Доступ запрещён") |
| `require_rate_limit` | `require_auth` + `check_rate_limit`; on 429 also runs abuse tracking |

**Blocked users** can read their profile/history but `require_active` blocks chat, payments, and image endpoints. Their balance is frozen (non-refundable per ToS).

### 6.6 Password reset
`POST /auth/reset-password/request` — always returns success (no enumeration); if the email exists, store a SHA-256-hashed `secrets.token_urlsafe(32)` token (1-hour expiry) and email a Resend link. `POST /auth/reset-password/confirm` — validate token (unused + unexpired), set the new password, mark `used_at`, **delete all the user's refresh tokens**.

### 6.7 CSRF (`CSRFMiddleware`)
Redis-backed double-submit. A `csrf_token` cookie (not httpOnly, SameSite=strict, `secure` in prod) is issued/echoed in the `X-CSRF-Token` response header; mutating methods (POST/PUT/DELETE/PATCH) must send a matching, Redis-known token in `X-CSRF-Token`. Exempt: `/api/v1/payments/webhook`, `/api/v1/payments/webhook/yookassa`, `/api/v1/health`. If Redis is unavailable, mutations get 503.

---

## 7. API Endpoints

All under `/api/v1`. Monetary values in responses are strings with 4 decimals (e.g. `"0.3600"`); the backend works in micro-rubles internally.

### 7.1 Health (no auth)
| Method | Path | Notes |
|---|---|---|
| GET | `/health` | DB + Redis check → 200/503, `{status, checks, version}` (no OpenRouter/uptime) |
| GET | `/health/live` | liveness, no I/O |
| GET | `/health/ready` | DB + Redis, cached 5s in Redis |

### 7.2 Auth (no auth unless noted)
`POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout` (auth), `POST /auth/reset-password/request`, `POST /auth/reset-password/confirm`. *(Plus test-only guards `GET /auth/test/{auth,active,admin}-only`.)*

**Register/login response:**
```json
{ "access_token": "eyJ…", "token_type": "bearer",
  "user": {"id":"…","email":"…","display_name":"…","role":"user","status":"active","balance":"0.0000"} }
```

### 7.3 Users (require_auth)
| Method | Path | Notes |
|---|---|---|
| GET | `/users/me` | `{id, email, display_name, role, status, balance}` (no has_password/has_yandex/created_at) |
| PATCH | `/users/me` | update `display_name` |
| GET | `/users/me/transactions` | paginated `{items, total, page, per_page, pages}`; item = `{id,type,amount,description,balance_after,created_at}` |
| GET | `/users/me/notifications` | paginated |
| POST | `/users/me/notifications/{id}/read` | 204 |

### 7.4 Chat (require_active)
| Method | Path | Notes |
|---|---|---|
| POST | `/chat/conversations` | create `{model_id, title?}` |
| GET | `/chat/conversations?page&per_page&archived` | list (active or archived) |
| GET | `/chat/conversations/{id}` | conversation + messages |
| PATCH | `/chat/conversations/{id}` | rename |
| DELETE | `/chat/conversations/{id}` | soft delete (archive), 204 |
| POST | `/chat/conversations/{id}/restore` | un-archive |
| DELETE | `/chat/conversations/{id}/permanent` | hard delete (archived only), 204 |
| POST | `/chat/conversations/{id}/messages` | **SSE stream** (see §8) |

**Send-message body:** `{content: str | ContentPart[], model, web_search_enabled?, excluded_image_ids?[]}`. `content` is text (≤ 32 000 chars) or a multimodal array (one text part + ≤ 4 `image_url` data-URI parts, each ≤ 8 MB). The endpoint persists the user message + images, may update the conversation's model, then runs the billing pipeline. Concurrent SSE streams are capped at **50 per worker** (excess → 503).

### 7.5 Models (no auth)
`GET /models` → `{models:[…]}`. Each model: `id, name, provider, input_price_per_m, output_price_per_m, context_window, status, supports_{image_input,file_input,tools,reasoning,web_search}, last_updated`. Prices recomputed live from USD × current rate × markup; `status` is downgraded to `unavailable` from the Redis health cache.

### 7.6 Images (require_active)
| Method | Path | Notes |
|---|---|---|
| GET | `/images/gallery?page&per_page` | thumbnails only, paginated |
| GET | `/images/conversation/{id}` | full base64 for re-injecting into context (404 if not owner) |

### 7.7 Payments
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/payments/create` | active | `{amount}` whole RUB → `{payment_id, confirmation_url, status, amount}` |
| POST | `/payments/webhook` | none* | HMAC-verified YooKassa webhook (CSRF-exempt) |
| GET | `/payments/{id}/status` | auth | 404 if the payment isn't the caller's |

### 7.8 Admin (require_admin; every action audited)
| Area | Endpoints |
|---|---|
| Dashboard | `GET /admin/dashboard`, `GET /admin/dashboard/revenue-by-model` |
| Users | `GET /admin/users`, `GET /admin/users/{id}`, `POST …/block`, `POST …/unblock`, `POST …/adjust-balance` |
| Tariffs | `GET /admin/tariffs`, `POST /admin/tariffs/preview`, `POST /admin/tariffs/apply`, `POST /admin/tariffs/confirm-rate`, `PATCH /admin/tariffs/models/{id}` (override), `DELETE /admin/tariffs/models/{id}/override`, `GET /admin/tariffs/cbr-rate` (mock) |
| Keys | `GET /admin/keys`, `POST /admin/keys`, `PATCH /admin/keys/{id}`, `POST /admin/keys/{id}/activate`, `POST /admin/keys/accounts/{name}/block`, `POST /admin/keys/accounts/{name}/unblock` |
| Alerts | `GET /admin/alerts`, `POST /admin/alerts/{id}/acknowledge`, `POST /admin/alerts/{id}/resolve` |
| Audit | `GET /admin/audit` |
| Requests | `GET /admin/requests` (usage-log tracker, search by request_id) |
| Models | `POST /admin/models/sync` (trigger catalogue sync) |
| Monitoring | `GET /admin/monitoring/system`, `GET /admin/monitoring/latency`, `GET /admin/monitoring/openrouter` |
| Storage | `GET /admin/storage` (text/image/file byte estimates + counts) |
| Export | `POST /admin/export/transactions` (CSV), `POST /admin/export/summary` (JSON), `POST /admin/export/per-user` (CSV) |
| Settings | `GET /admin/settings`, `PUT /admin/settings/{key}` (whitelisted), `PUT /admin/settings/openrouter-balance` |

**Settings whitelist** (`PUT /admin/settings/{key}`): `rate_limit_per_{minute,hour,day}`, `reservation_buffer_multiplier`, `overdraft_tolerance_percent`, `low_balance_remaining_requests`, `markup_multiplier`, `min_topup_amount_rub`, `openrouter_balance_threshold_usd`, `reconciliation_threshold_percent`, `auto_exchange_rate_enabled`, `auto_exchange_rate_interval_minutes`, `auto_exchange_rate_mode`. Any other key → 400. There are **no** dedicated `/settings/rate-limits` or `/settings/balance-warning` endpoints.

---

## 8. Streaming Architecture

Chat responses stream via SSE from `POST /chat/conversations/{id}/messages`. The endpoint returns `StreamingResponse(media_type="text/event-stream")` with `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Connection: keep-alive`. nginx passes `/api/v1/chat/` through with `proxy_buffering off` and 300s read/send timeouts.

```
Browser → FastAPI: POST …/messages
FastAPI:  persist user msg + images → reserve balance (commit) →
          stream from OpenRouter, forwarding content_delta events →
          finalize billing (commit) → message_done
```

The orchestrator (`process_chat_request`) yields `message_start`, optional `context_truncated`, many `content_delta`, then `message_done` (or a single `error`). See BILLING_LOGIC.md §2/§5/§6 for the event payloads, the per-model per-chunk timeout (`stream_timeout_seconds`, 30s default / 300s for reasoning models), client-disconnect handling, and the billing integration. There is **no heartbeat `ping`** event.

---

## 9. Chat, Models & Images

### 9.1 Model catalogue & sync
`model_sync_service.ModelSyncService.sync_models` fetches OpenRouter's `/models`, **skips** `:free` and dynamic-price/zero-price models, converts USD-per-token → μUSD/1M (Decimal), recomputes RUB prices (non-override rows only), refreshes capability flags + `stream_timeout_seconds`, marks vanished models `unavailable` (never deletes — preserves pricing history), and flags any current row with a non-positive RUB price `unavailable`. Runs on startup (leader) and every 24h, or on demand via `POST /admin/models/sync`. Capabilities are derived from `architecture.input_modalities`, `supported_parameters`, and `pricing.web_search`.

### 9.2 Multimodal images
Send-message `content` may include `image_url` data-URI parts (≤ 4, ≤ 8 MB each). `chat_image_service.save_message_images` persists each image (base64) to `chat_images`; only the **text** is stored in `messages.content`. On later turns, `build_history_with_images` re-injects stored images into the matching user messages (unless their id is in `excluded_image_ids`), so vision context carries across turns. The gallery (`/images/gallery`) lists thumbnails; `/images/conversation/{id}` returns full payloads. Image parts count a flat 1000 tokens each for estimation; actual cost comes from OpenRouter's usage.

### 9.3 Web search
When `web_search_enabled` and the model `supports_web_search`, the proxy attaches `plugins:[{"id":"web"}]`. Billing for such requests uses OpenRouter's reported USD cost (which includes the flat per-search surcharge) — see BILLING_LOGIC.md §7.

### 9.4 Demo mode
`OPENROUTER_DEMO_MODE` (default true) routes chat through `demo_proxy.DemoProxy`, which emits canned Russian responses while the full billing pipeline (reserve → finalize → debit → history) still runs — useful without a real key.

---

## 10. Error Handling

### 10.1 Response shape
`TrinityError` subclasses are rendered by a FastAPI exception handler as:
```json
{ "error": { "code": "insufficient_funds", "message": "…", "details": {"required":"1.2000","available":"0.5000"} } }
```
On the SSE chat path, the same `code`/`message` (+ flattened `details`) are delivered as an `error` event.

### 10.2 Taxonomy (`core/exceptions.py`)
| Code | HTTP | Default message (RU) |
|---|---|---|
| `auth_required` | 401 | Необходимо войти в аккаунт |
| `auth_invalid` | 401 | Неверный email или пароль |
| `email_already_registered` | 409 | Пользователь с таким email уже существует |
| `account_blocked` | 403 | Аккаунт заблокирован |
| `admin_required` | 403 | Доступ запрещён |
| `insufficient_funds` | 402 | Недостаточно средств |
| `context_too_long` | 413 | Сообщение слишком длинное для выбранной модели |
| `rate_limited` | 429 | Слишком много запросов |
| `model_unavailable` | 503 | Модель временно недоступна |
| `openrouter_error` / `openrouter_server_error` | 502 | Ошибка при обработке запроса / OpenRouter недоступен |
| `openrouter_client_error` | 400 | Ошибка запроса к OpenRouter |
| `paid_models_unavailable` | 402 | Платные модели временно недоступны |
| `openrouter_rate_limited` | 429 | Превышен лимит запросов OpenRouter |
| `payment_error` | 400 | Ошибка при создании платежа |
| `service_unavailable` | 503 | Сервис временно недоступен |
| `client_disconnected` | 499 | Соединение прервано клиентом |

### 10.3 Global handlers (`main.py`)
- `RequestValidationError` → 422 `{error:{code:"validation_error", …}}`.
- `SQLAlchemyTimeoutError` and `TimeoutError` → **503** "Сервер перегружен, попробуйте позже" (pool exhaustion / overload, not a hard failure).
- Any other `Exception` → 500 `{error:{code:"internal_error", …}}`. **This handler does not create alerts or send Telegram** — alerts are filed at specific billing/payment catch-sites instead.

### 10.4 Backpressure
`RequestTimeoutMiddleware` (pure-ASGI) 503s any non-SSE request exceeding `REQUEST_TIMEOUT_SECONDS` (30s). `BodySizeLimitMiddleware` rejects bodies over **30 MB** (for image uploads) with 413. The chat SSE path is exempt from the timeout.

---

## 11. Admin Panel

Part of the same Next.js app under `/admin/*`, backed by `/api/v1/admin/*`. Desktop-first, polling-based (`useAdminPolling`, default 30s). Every write creates an `admin_audit_log` row. Pages: **Dashboard**, **Users** (+ detail with stats and block/adjust controls), **Tariffs** (rate apply/preview, per-model override + clear, CBR confirm), **Keys** (add/activate/disable, per-account block), **Alerts** (ack/resolve, unread badge), **Audit**, **Requests** (usage-log tracker), **Monitoring** (system/latency/openrouter), **Storage**, **Export**, **Settings**. See BILLING_LOGIC.md §13 for the financial flows and SCHEMA.md §4.12 for the audit action names.

---

## 12. Background Tasks & Scheduling

Two mechanisms run periodic work:

**A. In-process loops (leader worker only).** `main.lifespan` elects one leader via Redis (`trinity:bg-leader`). The leader runs, on startup: seed primary API key → model sync → health probe; then loops:
| Loop | Interval | Action |
|---|---|---|
| health check | `HEALTH_CHECK_INTERVAL_SECONDS` (300) | probe OpenRouter `/auth/key`, cache balance + per-model health (6-min TTL), alert if below threshold |
| model sync | 24h | re-sync the OpenRouter catalogue (§9.1) |
| exchange rate | 60s | poke the self-throttling CBR updater (§8.4 of BILLING_LOGIC) |
| reservation cleanup | 60s | release expired reservations + reconcile partial costs/drift (tr-u3y5) |
| daily reconciliation | 24h (+ once on startup) | 4 financial-integrity checks + Telegram summary (BILLING_LOGIC §12, tr-376l) |
| leader renew | 30s | refresh the lock; stop if lost |

**B. Cron-invoked task modules** (`docker exec backend python -m app.tasks.<name>`), per `scripts/setup_cron.sh`:
| Schedule | Module | Purpose |
|---|---|---|
| daily 04:00 UTC | `cleanup retention` | data-retention purge (SCHEMA.md §8) |
| daily 03:00 UTC | `backup.sh` | `pg_dump` → S3 |

(Reservation cleanup and daily reconciliation moved to in-process leader loops — table A — and were removed from the host crontab to avoid the double-runner race, tr-u3y5 / tr-376l.)

Each task module can also initialize its own engine/redis to run standalone. The in-process exchange-rate loop and the cron model are complementary — the updater self-throttles regardless of how often it's poked.

---

## 13. Deployment

### 13.1 Dev (`docker-compose.yml`)
Services: **postgres** (16-alpine, `max_connections=200`, tuned shared_buffers/work_mem, seeds `trinity` + `trinity_test` via `docker/init-databases.sh`), **redis** (512 MB, allkeys-lru), **pgbouncer** (transaction mode, :6432), **backend** (`uvicorn --reload`, app traffic via PgBouncer, Alembic via `DATABASE_URL_DIRECT`), **frontend** (`next dev`), and a `seed` profile (`scripts/seed_dev.py`). Tests use `trinity_test` directly (never PgBouncer).

### 13.2 Prod (`docker-compose.prod.yml`)
Services: **nginx** (80/443, TLS, the only host-exposed ports), **frontend** (`runner` target), **backend** (`prod` target = `gunicorn -k app.core.worker.TrinityUvicornWorker -w 4 --timeout 120 --graceful-timeout 30 --keep-alive 5`), **redis**. **No postgres service** — production uses **managed PostgreSQL** via `DATABASE_URL`. The custom worker sets uvloop + `limit_concurrency=300` per worker.

### 13.3 nginx (`nginx/conf.d/trinity.conf`)
HTTP→HTTPS redirect (with ACME webroot), TLS 1.2/1.3, security headers (HSTS, X-Frame-Options DENY, X-Content-Type-Options, CSP, Referrer-Policy), per-IP `limit_req` (100 r/m, burst 20), **`client_max_body_size 30m`** (matches the app's `BodySizeLimitMiddleware`). A dedicated `location /api/v1/chat/` disables buffering and uses 300s timeouts for SSE; `/api/` uses a 60s read timeout; `/` proxies the frontend.

> **Body-size mismatch:** nginx caps requests at **10 MB** while the app middleware allows **30 MB**. A 4-image message (~27 MB) is rejected by nginx before reaching the backend. See [§19](#19-known-gaps--discrepancies).

### 13.4 Migrations & deploy
`scripts/deploy.sh`: pull → build → `alembic upgrade head` → restart backend/frontend → health check. `scripts/rollback.sh` for `git checkout HEAD~1` + `alembic downgrade -1`. Per `CLAUDE.md`, after creating a migration always run `docker compose exec backend alembic upgrade head` against dev `trinity`, and never `docker compose down -v`.

### 13.5 Backups
`scripts/backup.sh` (cron 03:00 UTC): `pg_dump` → gzip → S3 (`S3_*`), retain 30 days, Telegram report. `scripts/restore.sh` documents the restore path. Redis is not backed up (ephemeral).

---

## 14. Monitoring

`monitoring_service` powers `/admin/monitoring/*`:
- **System** — CPU from `/proc/stat`, RAM from `/proc/meminfo`, disk from `os.statvfs("/")` (no psutil); SQLAlchemy pool stats (`size/checked_out/overflow/available`); Redis `INFO` (memory, clients, hit rate, version, uptime).
- **Latency** — per-stage p50/p95/p99 from Redis sorted sets `latency:{stage}` (`estimation`, `reservation`, `openrouter_ttfb`, `openrouter_total`, `finalization`, `total`), recorded after each billed request (24h TTL, 5 000-sample cap).
- **OpenRouter** — cached balance, active key label/suffix, and a 1-hour count of `openrouter` critical/warning alerts. The dashboard resolves the displayed balance as `live` (probe > 0) → `manual` (`openrouter_manual_balance_usd`) → `unavailable`.

Metrics are computed on demand (not persisted). `/api/v1/health` (DB + Redis) is the external uptime probe.

---

## 15. Environment Variables

From `app/config.py` (defaults shown) and `.env.example`:

```bash
# Core
APP_ENV=development          APP_VERSION=0.0.1
SECRET_KEY=                  # JWT signing, CSRF, AES key (SHA-256 derived)
CORS_ORIGINS=http://localhost:3000        FRONTEND_URL=http://localhost:3000

# Database (app via PgBouncer; Alembic via the DIRECT url)
DATABASE_URL=               DATABASE_URL_DIRECT=
DATABASE_POOL_SIZE=12       DATABASE_MAX_OVERFLOW=6      # .env.example suggests 20 / 10

# Redis
REDIS_URL=redis://localhost:6379/0

# Auth
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=15   JWT_REFRESH_TOKEN_EXPIRE_DAYS=7   BCRYPT_ROUNDS=12

# Yandex OAuth (configured but NOT implemented in the backend)
YANDEX_CLIENT_ID=  YANDEX_CLIENT_SECRET=  YANDEX_REDIRECT_URI=

# OpenRouter
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1   OPENROUTER_DEFAULT_API_KEY=   # extra keys live in the DB

# YooKassa
YOOKASSA_SHOP_ID=  YOOKASSA_SECRET_KEY=  YOOKASSA_RETURN_URL=

# Email — Resend (current)
RESEND_API_KEY=  RESEND_FROM_EMAIL="Trinity <noreply@trinity.local>"  ADMIN_EMAIL=
# Email — Yandex SMTP (legacy, unused): SMTP_HOST/PORT/USER/PASSWORD/FROM

# Telegram
TELEGRAM_BOT_TOKEN=  TELEGRAM_CHAT_ID=

# Backups (S3-compatible / Yandex Object Storage)
S3_ENDPOINT=  S3_ACCESS_KEY=  S3_SECRET_KEY=  S3_BUCKET=trinity-backups

# Backpressure / monitoring
REQUEST_TIMEOUT_SECONDS=30   HEALTH_CHECK_INTERVAL_SECONDS=300   OPENROUTER_BALANCE_THRESHOLD_USD=5.0

# Demo switches (independent; all default true except DEMO_MODE)
DEMO_MODE=false  EMAIL_DEMO_MODE=true  OPENROUTER_DEMO_MODE=true  TELEGRAM_DEMO_MODE=true
```

Frontend: `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_DEMO_MODE`. Runtime-tunable behaviour (rate limits, markup, thresholds, auto-FX) lives in `system_settings`, not env vars — see SCHEMA.md §7/§4.11.

> `config.py` defaults `OPENROUTER_BALANCE_THRESHOLD_USD=5.0` while the seeded `system_settings.openrouter_balance_threshold_usd=100`; the health/dashboard threshold reads the **setting** (100) when present.

---

## 16. Security

| Area | Measure |
|---|---|
| Input validation | Pydantic v2 on every endpoint; `PydanticCustomError` for custom rules |
| SQL injection | parameterized SQLAlchemy Core/ORM; no string interpolation |
| Passwords | bcrypt (12 rounds) **directly** (not passlib), offloaded to a thread |
| JWT | HS256, 15-min access tokens with `jti`; Redis blacklist on logout |
| Sessions | DB-backed refresh tokens (SHA-256 hashed), rotation inherits TTL, instant revocation |
| CSRF | Redis-backed double-submit token (`X-CSRF-Token`); webhook + health exempt |
| API-key storage | AES-256-GCM (`encrypt_key`), SHA-256(`SECRET_KEY`) as the key |
| Rate limiting | nginx per-IP 100 r/m + Redis per-user sliding windows (configurable) |
| Transport | TLS 1.2/1.3, HSTS, CSP, X-Frame-Options DENY, nosniff (nginx) |
| Body size | nginx 10 MB; app middleware 30 MB (see §19) |
| Audit | all auth events + admin actions in `admin_audit_log` |
| Webhook | HMAC-SHA256 signature + DB idempotency |
| Financial safety | DB CHECKs (`balance>=0`, `balance>=reserved`), unique idempotency keys, row locks |

---

## 17. Legal Pages

Next.js static pages in Russian, linked from the footer and registration (precondition for YooKassa review):

| Page | Path | Basis |
|---|---|---|
| Оферта (Terms) | `/legal/terms` | ст. 437 ГК РФ — service, payment, non-refundable blocked balance |
| Privacy Policy | `/legal/privacy` | 152-ФЗ — data collected (email, usage logs, chat), rights |
| Cookie Policy | `/legal/cookies` | session/auth cookies, consent banner |

---

## 18. Glossary

| Term | Meaning |
|---|---|
| **μ₽ (micro-ruble)** | `1 RUB = 1 000 000 μ₽`; all DB money is `BIGINT` μ₽ |
| **μUSD** | `1 USD = 1 000 000 μUSD`; OpenRouter cost storage |
| **Reservation** | a temporary balance lock (`estimated_cost × 2`) held during a request |
| **Finalize / Release** | charge the actual cost (debit) / free the lock with no charge |
| **Overdraft absorbed** | the shortfall the operator eats when actual cost > balance (debit is clamped) |
| **request_id** | per-request UUID correlating reservation, transaction, usage_log, message |
| **Markup** | multiplier over OpenRouter cost (default 1.25 = +25%) |
| **Exchange rate** | μ₽ per 1 USD (e.g. 82 500 000 = 82.50 RUB/USD) |
| **Price override** | an admin-pinned RUB price exempt from rate recalcs/sync |
| **Leader** | the single worker (Redis-elected) that runs background loops |
| **Demo mode** | switches (per subsystem) that stub OpenRouter/email/Telegram while keeping core flows live |
| **Web search** | OpenRouter plugin adding live results + a flat per-search USD surcharge |

---

## 19. Known Gaps & Discrepancies

Surfaced during the audit (code is read-only for this task; these are flagged, not fixed):

1. **Yandex OAuth is not implemented.** No backend `/auth/yandex*` route or token exchange exists, yet `YANDEX_*` env vars, a frontend `(auth)/callback` page, and the `users.yandex_id` column all imply it. The callback page is effectively dead.
2. **Email is Resend, not SMTP.** SMTP config (`SMTP_*`) is retained but unused.
3. **No automatic refunds.** The `refund` transaction type / `refunded` usage-log status are never written (BILLING_LOGIC.md §11). Failed requests release the reservation; overdrafts are absorbed.
4. ~~**nginx 10 MB vs app 30 MB body limit**~~ — RESOLVED: `client_max_body_size` is now `30m`, matching `BodySizeLimitMiddleware`.
5. **Global 500 handler doesn't alert.** The old spec promised Telegram on every unhandled error; the current handler only returns 500. Billing/payment paths alert explicitly.
6. **No SSE heartbeat.** Long streams rely on disabled proxy buffering, not a `ping` event.
7. **Pricing display vs charge divergence** for override models (BILLING_LOGIC.md §8.2 / SCHEMA.md §10).
8. **Dead/declared-but-unused** dependencies and columns: `passlib`, `psutil`, `usage_logs.api_key_id`, `admin_audit_log.user_agent`, `notifications.email_sent*`.
9. **Config vs seed drift:** `rate_limit_per_day` code default 1000 vs seed 500; `OPENROUTER_BALANCE_THRESHOLD_USD` config 5.0 vs seed 100; migration-002 model prices (85 ×1.15) vs settings seed (82.5 ×1.25). The setting/DB value wins at runtime; model prices reconcile on first sync/tariff update.
10. **Test-only endpoints** (`/auth/test/*`) ship in the production router.
