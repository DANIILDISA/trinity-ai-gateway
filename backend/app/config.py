from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    # Literal so a typo like "Prod" is a hard construction failure (whose error
    # carries only the offending app_env value, never other secrets) rather than
    # silently routing through the permissive branch of the SECRET_KEY guard below.
    app_env: Literal["development", "production"] = "development"
    app_version: str = "0.0.1"
    secret_key: str = ""
    cors_origins: str = "http://localhost:3000"
    frontend_url: str = "http://localhost:3000"

    # Database
    database_url: str = ""
    # Per-worker pool. Budget invariants (tr-4fy.3):
    #   1. client-conn headroom: (workers + N_cron) × (pool_size + max_overflow)
    #      ≤ PgBouncer max_client_conn(500). Worst case (4 workers + 3 cron) × 40
    #      = 280 ≤ 500 (≈1.8× headroom). Cron tasks (cleanup/exchange_rate/
    #      reconciliation) pass settings.* explicitly, so they ALSO run at 25/15.
    #   2. Postgres throughput ceiling: PgBouncer default_pool_size(100) +
    #      reserve_pool_size(20) = 120 real server conns (transaction pooling),
    #      under Postgres max_connections=200. Above 3 workers, surplus client
    #      conns queue at PgBouncer — adding workers does NOT raise Postgres
    #      concurrency; raise default_pool_size first.
    database_pool_size: int = 25
    database_max_overflow: int = 15
    # Seconds to wait for a free pooled connection before failing fast with a
    # 503 (was hardcoded in database.py; now configurable, tr-3hs).
    database_pool_timeout: int = 10
    # Direct (non-PgBouncer) URL for Alembic. PgBouncer transaction pooling is
    # fine for the app's short transactions but a poor fit for migrations, so
    # alembic/env.py prefers this when set. Empty → fall back to database_url.
    database_url_direct: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    # Per-worker Redis connection pool ceiling (tr-8dy3). Every authed request makes a few
    # Redis calls (CSRF + blocklist + last_active); a burst of concurrent requests must not
    # exhaust the pool (which surfaced as 500s at ~300 concurrent). Redis maxclients (10000)
    # covers several workers × this. Fail-open/503-backstop keep it graceful if still hit.
    redis_max_connections: int = 500

    # Auth
    jwt_access_token_expire_minutes: int = 15
    # Sliding inactivity window: each refresh extends expiry by this many days.
    jwt_refresh_token_expire_days: int = 7
    # Hard cap: no session lives longer than this many days after login.
    jwt_refresh_token_absolute_expire_days: int = 14
    bcrypt_rounds: int = 12

    # Yandex OAuth
    yandex_client_id: str = ""
    yandex_client_secret: str = ""
    yandex_redirect_uri: str = ""

    # OpenRouter
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_default_api_key: str = ""
    # Overall HTTP timeout (seconds) for OpenRouter completion calls. Slow
    # reasoning / large-context models can take minutes to first byte and
    # between chunks, so this must be generous to avoid premature
    # APITimeoutError. Override via the OPENROUTER_TIMEOUT_SECONDS env var.
    # NB: the effective read timeout is max(this, the model's per-chunk
    # stream_timeout_seconds), so per-model reasoning windows are never cut short.
    openrouter_timeout_seconds: int = 180

    # YooKassa
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_return_url: str = ""

    # Email (Yandex SMTP — legacy, retained for backward compat)
    smtp_host: str = "smtp.yandex.ru"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    # Email (Resend HTTP API — current)
    resend_api_key: str = ""
    resend_from_email: str = "Trinity <noreply@trinity.local>"
    # Recipient for critical alert emails (BILLING_LOGIC.md §14.1, Task 9.4).
    admin_email: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Backups
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "trinity-backups"

    # Backpressure: a request still running after this many seconds is treated as
    # stuck and gets a 503 (the SSE chat stream is exempt). Set well above normal
    # P99 (~3s) so it only trips on genuinely hung requests, not slow-but-healthy
    # ones under peak load.
    request_timeout_seconds: int = 30

    # A token refresh that blocks on a row lock longer than this aborts with a
    # fast, retryable 503 instead of hanging until request_timeout_seconds — so a
    # wedged transaction holding the refresh_tokens/users lock can't turn every
    # /auth/refresh into a 30s stall (tr-wvs / tr-dus).
    refresh_lock_timeout_ms: int = 3000

    # Monitoring
    health_check_interval_seconds: int = 300
    openrouter_balance_threshold_usd: float = 5.0

    # Demo
    demo_mode: bool = False
    # Independent override so email can go live (false) while the rest of the
    # platform stays in demo (true). Defaults to True so a missing env var keeps
    # behaviour matching the global demo_mode story.
    email_demo_mode: bool = True
    # Real OpenRouter calls are the default now: the live key comes from the
    # api_keys table (primary, active), not from env.
    openrouter_demo_mode: bool = False
    # Independent override so admin alerts reach Telegram (false) while the rest
    # of the platform stays in demo (true). Defaults to True so a missing env var
    # keeps Telegram sends suppressed.
    telegram_demo_mode: bool = True

    # Sandbox code-execution service (epic tr-s3y1). Reachable only over the
    # internal docker network; never published to the host.
    sandbox_url: str = "http://sandbox:9000"
    # Per-run wall-clock the executor asks the sandbox for (the sandbox caps it at
    # its own SANDBOX_WALL_SECONDS regardless).
    sandbox_exec_timeout_seconds: int = 30
    # Ruble price of one code execution (epic tr-s3y1 Phase 2). Decision A
    # (spec 2026-06-26 §1): code execution is billed via TOKENS ONLY — the code
    # and its stdout/stderr already flow through the model and are token-billed, so
    # there is no separate sandbox fee (parity with ChatLLM / Claude-consumer feel).
    # These knobs stay (flat per-run + per-wall-second, in micro-rubles, 1 ₽ = 1e6 μ₽)
    # so a principled cost-recovery fee can be switched on later if scaling demands.
    sandbox_run_base_micro_rub: int = 0
    sandbox_sec_micro_rub: int = 0
    # Production resilience (spec 2026-06-26 §2). Per-user concurrent run_python cap
    # (backend, Redis) + a safety TTL so a crashed worker can't leak a slot.
    sandbox_max_concurrent_per_user: int = 2
    sandbox_conc_slot_ttl_seconds: int = 60

    # Concurrency / backpressure (tr-3hs, audit §H1/H2)
    worker_limit_concurrency: int = 200
    auth_backpressure_limit: int = 40
    conversation_backpressure_limit: int = 50

    # Login brute-force throttling (tr-dfem). Per-account counters in Redis;
    # thresholds/steps are stricter for admin accounts, which also fail closed
    # when Redis is unavailable (regular users fail open behind the nginx
    # per-IP limit).
    login_fail_window_seconds: int = 900
    login_fail_threshold: int = 5
    login_fail_threshold_admin: int = 3
    login_block_steps_seconds: list[int] = [60, 300, 900, 3600]
    login_block_steps_admin_seconds: list[int] = [300, 1800, 3600, 3600]
    # Empty → security events are logged only; set to receive alert emails.
    security_alert_email: str = ""


# B1 (tr-pkv3): obvious low-entropy placeholders we refuse as a production key —
# length alone lets a memorable 32-char value like "changeme"×4 through.
_SECRET_PLACEHOLDER_MARKERS = (
    "changeme", "change-me", "your-", "placeholder", "example",
    "insecure", "secret-key", "xxxx",
)


def _secret_key_is_strong(secret_key: str) -> bool:
    """A production SECRET_KEY must be non-empty, >=32 chars after strip, have a
    little character diversity, and not be an obvious placeholder."""
    stripped = secret_key.strip()
    if len(stripped) < 32:
        return False
    if len(set(stripped)) < 5:  # "x"*32 and trivial repeats
        return False
    low = stripped.lower()
    return not any(marker in low for marker in _SECRET_PLACEHOLDER_MARKERS)


def _enforce_production_secret_key(s: "Settings") -> None:
    """Refuse to boot outside development with an empty/weak SECRET_KEY.

    Raises a plain RuntimeError with a STATIC message — deliberately NOT a pydantic
    ValueError/ValidationError, whose .errors() would embed the full settings input
    dict (DATABASE_URL password, YOOKASSA/SMTP/RESEND/TELEGRAM secrets) into the
    boot traceback and any log pipeline the instant SECRET_KEY is misconfigured
    (audit#1, empirically confirmed). Fail-closed: any app_env other than the exact
    "development" requires a strong key — and the Literal typing on app_env already
    turns a typo like "Prod" into a hard construction failure, so reaching here with
    a non-"development" value means "production".
    """
    if s.app_env != "development" and not _secret_key_is_strong(s.secret_key):
        raise RuntimeError(
            "SECRET_KEY must be a random value of at least 32 characters when "
            "APP_ENV is not 'development' — it signs JWTs (HS256) and derives the "
            "AES key for stored API keys. Generate one with `openssl rand -hex 32` "
            "and set SECRET_KEY in .env.prod. The server refuses to start without it."
        )


settings = Settings()
_enforce_production_secret_key(settings)
