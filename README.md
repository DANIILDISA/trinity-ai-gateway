# Trinity — AI Model Gateway with Ruble Billing

A production-grade SaaS that gives users in Russia access to dozens of AI models (GPT, Claude, Gemini, Llama and more) with a **ruble balance and pay-per-token billing** — top up via SBP, chat in the browser, pay only for what you use.

> **Status:** feature-complete MVP prepared for commercial launch. Development is paused; the code is published as a portfolio piece.

## Highlights

- **Financially safe billing.** Every chat request runs through a **reserve → finalize** cycle against an immutable ledger; concurrent requests can't overspend a balance; a daily in-process **reconciliation** job checks four financial-integrity invariants and reports to Telegram. Displayed price == charged price is a structural invariant.
- **Real payments.** YooKassa / SBP top-ups with webhook verification and idempotency.
- **Streaming chat (SSE)** with multimodal image input, persistent gallery, optional web search, reasoning ("thinking") display, message branching and version history.
- **Code interpreter tool** — a hardened **Python sandbox** (separate service, fresh process per run, seccomp filters, resource limits, isolated network) with per-user caps; billed through tokens.
- **Automatic pricing.** OpenRouter model catalogue sync + daily USD/RUB rate from the Central Bank of Russia × configurable markup.
- **Admin panel** — dashboard, users, tariffs, API keys, alerts, audit log, request inspector, monitoring, storage, exports, settings.
- **Built for scale.** gunicorn × 4 Uvicorn workers behind PgBouncer, Redis-based rate limiting, CSRF, token revocation and **leader election** for background loops; designed for ~1,000+ DAU on a single VPS.
- **Load-tested** with Locust: ~900 concurrent users, 100k+ requests at **0.03 % failures** on a single dev worker ([results](load-tests/results/)).
- **Security** — JWT + DB-backed refresh tokens, bcrypt, AES-GCM for stored secrets, nginx with TLS 1.2/1.3, HSTS, CSP and per-IP rate limits.

## Architecture

```
                         Internet
                            │
              ┌─────────────▼─────────────┐
              │ nginx — TLS, rate limits, │
              │ SSE pass-through          │
              └──────┬─────────────┬──────┘
                 /   │             │  /api
                     ▼             ▼
        ┌──────────────────┐  ┌──────────────────────────┐      ┌──────────────┐
        │ Next.js 15       │  │ FastAPI (gunicorn × 4)    │─────▶│ OpenRouter   │
        │ React 19, SSR    │  │ auth · chat · billing ·   │      │ (AI models)  │
        └──────────────────┘  │ payments · admin · tasks  │      └──────────────┘
                              └───┬─────────┬─────────┬───┘
                                  │         │         │      ┌──────────────┐
                        ┌─────────▼──┐  ┌───▼────┐  ┌─▼───────▤ Python       │
                        │ PgBouncer  │  │ Redis 7│  │ sandbox │ (seccomp)    │
                        │ → Postgres │  │        │  └─────────┴──────────────┘
                        │ 16         │  └────────┘
                        └────────────┘   YooKassa · CBR rates · Resend · Telegram
```

## Tech stack

| Layer | Technologies |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic, gunicorn/Uvicorn, uvloop, tiktoken |
| Data | PostgreSQL 16, PgBouncer, Redis 7 |
| Frontend | Next.js 15 (App Router), React 19, TypeScript, Tailwind CSS, react-markdown |
| Integrations | OpenRouter (OpenAI SDK), YooKassa REST v3, Central Bank of Russia XML, Resend, Telegram Bot API |
| Infra | Docker Compose (dev + prod), nginx, cron jobs, `pg_dump` backups to S3-compatible storage, deploy/rollback scripts |
| Quality | ~990 backend tests (pytest), ~70 frontend test suites (Vitest), Locust load tests |

~25k lines of Python, ~36k lines of TypeScript, 84 API endpoints, 23 database migrations.

## Quick start (development)

```bash
git clone https://github.com/DANIILDISA/trinity-ai-gateway.git
cd trinity-ai-gateway
cp .env.example .env          # fill in OpenRouter / YooKassa keys (optional for UI exploration)
docker compose up -d          # postgres, redis, pgbouncer, backend, sandbox, frontend
```

Frontend: http://localhost:3000 · API docs: http://localhost:8000/api/docs

## Documentation

- [SPEC.md](SPEC.md) — full technical specification
- [SCHEMA.md](SCHEMA.md) — database schema
- [BILLING_LOGIC.md](BILLING_LOGIC.md) — billing, reservations, reconciliation
- [DESIGN.md](DESIGN.md) — design system
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — cron jobs, backups, restore
- [load-tests/](load-tests/) — Locust scenarios and results

## Author

**Daniil Burnashov** — product, architecture, backend, frontend and infrastructure.
