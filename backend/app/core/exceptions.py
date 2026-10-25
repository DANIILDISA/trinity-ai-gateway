from typing import Any


class TrinityError(Exception):
    http_status: int = 500
    code: str = "internal_error"
    default_message: str = "Внутренняя ошибка сервера"

    def __init__(self, message: str | None = None, details: dict[str, Any] | None = None) -> None:
        self.message = message or self.default_message
        self.details = details
        super().__init__(self.message)


class AuthRequiredError(TrinityError):
    http_status = 401
    code = "auth_required"
    default_message = "Необходимо войти в аккаунт"


class AuthInvalidError(TrinityError):
    http_status = 401
    code = "auth_invalid"
    default_message = "Неверный email или пароль"


class EmailAlreadyRegisteredError(TrinityError):
    http_status = 409
    code = "email_already_registered"
    default_message = "Пользователь с таким email уже существует"


class AccountBlockedError(TrinityError):
    http_status = 403
    code = "account_blocked"
    default_message = "Аккаунт заблокирован"


class AccountPendingDeletionError(TrinityError):
    # 409 Conflict — the credentials are valid but the account is in a
    # transitional state (scheduled for deletion). The frontend swaps to a
    # recovery prompt and POSTs to /auth/recover with the same credentials.
    http_status = 409
    code = "account_pending_deletion"
    default_message = "Аккаунт помечен на удаление. Восстановить?"


class AdminRequiredError(TrinityError):
    http_status = 403
    code = "admin_required"
    default_message = "Доступ запрещён"


class DuplicateRequestError(TrinityError):
    # 409 Conflict — an identical send (same user + client_request_id) is already
    # in flight or was just processed within the idempotency window (tr-n5b). The
    # gate fires BEFORE persisting the user message and BEFORE reservation/charge,
    # so no second message/usage_log/reservation is created. Benign on the client
    # (the duplicate is silently swallowed — the original turn is the truth).
    http_status = 409
    code = "duplicate_request"
    default_message = "Запрос уже обрабатывается"


class InsufficientFundsError(TrinityError):
    http_status = 402
    code = "insufficient_funds"
    default_message = "Недостаточно средств"


class ContextTooLongError(TrinityError):
    http_status = 413
    code = "context_too_long"
    default_message = "Сообщение слишком длинное для выбранной модели"


class RateLimitedError(TrinityError):
    http_status = 429
    code = "rate_limited"
    default_message = "Слишком много запросов"

    def __init__(
        self,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, details)
        self.retry_after = retry_after


class OverloadedError(TrinityError):
    # 503 — preemptive load-shedding (tr-3hs). A per-endpoint concurrency limiter is
    # full, so reject NOW with a Retry-After instead of queueing on the DB
    # pool_timeout (10s). `code` is per-instance so each limiter
    # (overloaded_auth / overloaded_conversation) is distinguishable to clients.
    http_status = 503
    code = "overloaded"
    default_message = "Сервис перегружен, попробуйте через несколько секунд"

    def __init__(
        self,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, details)
        self.retry_after = retry_after
        if code is not None:
            self.code = code


class ModelUnavailableError(TrinityError):
    http_status = 503
    code = "model_unavailable"
    default_message = "Модель временно недоступна"


class OpenRouterError(TrinityError):
    http_status = 502
    code = "openrouter_error"
    default_message = "Ошибка при обработке запроса"


class OpenRouterServerError(TrinityError):
    http_status = 502
    code = "openrouter_server_error"
    default_message = "Сервис OpenRouter временно недоступен"


class OpenRouterClientError(TrinityError):
    http_status = 400
    code = "openrouter_client_error"
    default_message = "Ошибка запроса к OpenRouter"


class PaidModelsUnavailableError(OpenRouterClientError):
    """OpenRouter returned 402 (insufficient platform credits) for a paid model.

    Subclasses OpenRouterClientError so the retry wrapper treats it as a
    non-retryable client error and the chat orchestrator surfaces it as a
    single error SSE event. Never raised for `:free` models.
    """
    http_status = 402
    code = "paid_models_unavailable"
    default_message = (
        "Платные модели временно недоступны. "
        "Пожалуйста, свяжитесь с администратором."
    )


class OpenRouterRateLimitError(TrinityError):
    http_status = 429
    code = "openrouter_rate_limited"
    default_message = "Превышен лимит запросов OpenRouter"

    def __init__(
        self,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, details)
        self.retry_after = retry_after


class PaymentError(TrinityError):
    http_status = 400
    code = "payment_error"
    default_message = "Ошибка при создании платежа"


class ServiceUnavailableError(TrinityError):
    http_status = 503
    code = "service_unavailable"
    default_message = "Сервис временно недоступен"

    def __init__(
        self,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, details)
        self.retry_after = retry_after


class ClientDisconnectedError(TrinityError):
    """Browser closed the SSE connection mid-stream.

    Carries no HTTP status because the user is gone — the orchestrator catches
    it and decides whether to charge for received tokens (finalize_partial)
    or release the reservation.
    """
    http_status = 499  # nginx convention for client-closed connection
    code = "client_disconnected"
    default_message = "Соединение прервано клиентом"
