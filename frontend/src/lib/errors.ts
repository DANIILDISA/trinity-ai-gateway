// Maps backend error codes (SPEC.md §9.2) to user-facing Russian strings.
// Use getErrorMessage(code, fallback) when surfacing a server error to the UI;
// use formatInsufficientFunds for the one error code that carries amounts.

export const ERROR_MESSAGES: Record<string, string> = {
  validation_error: "Ошибка валидации данных",
  auth_required: "Необходимо войти в аккаунт",
  auth_invalid: "Неверный email или пароль",
  email_already_registered: "Пользователь с таким email уже существует",
  account_blocked: "Аккаунт заблокирован",
  account_pending_deletion: "Аккаунт помечен на удаление",
  admin_required: "Доступ запрещён",
  insufficient_funds: "Недостаточно средств",
  context_too_long: "Сообщение слишком длинное для выбранной модели",
  rate_limited: "Слишком много запросов. Попробуйте позже",
  model_unavailable: "Модель временно недоступна",
  paid_models_unavailable:
    "Платные модели временно недоступны. Пожалуйста, свяжитесь с администратором.",
  openrouter_error: "Ошибка при обработке запроса",
  payment_error: "Ошибка при создании платежа",
  internal_error: "Внутренняя ошибка сервера",
  service_unavailable: "Сервис временно недоступен",
};

export const NETWORK_ERROR_MESSAGE = "Внутренняя ошибка сервера";

// Generic last-resort message for a failed send when the body carries nothing
// usable (non-JSON, empty, or no message at all). Kept short so the error panel
// stays height-bounded.
export const SEND_ERROR_MESSAGE = "Ошибка отправки сообщения";

// Parse a backend error-response BODY (raw text) into a clean, localized message
// + code. Handles the SPEC.md §9.2 envelope { error: { code, message, details:
// [{ msg, input }] } }. Preference order: details[].msg (joined) → error.message
// → generic SEND_ERROR_MESSAGE. NEVER returns raw JSON and NEVER echoes the
// per-detail `input` (which can be the user's entire ~99k-char message — tr-5zt).
export function parseErrorBody(
  text: string,
  status: number,
): { message: string; code?: string } {
  let parsed: unknown = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = null;
  }

  if (parsed && typeof parsed === "object") {
    const envelope = (parsed as Record<string, unknown>).error;
    if (envelope && typeof envelope === "object") {
      const env = envelope as Record<string, unknown>;
      const code = typeof env.code === "string" ? env.code : undefined;

      // Prefer the human-readable per-field messages (e.g. the 32000-char cap).
      // Take ONLY `msg` — never `input`, which echoes the raw user payload.
      if (Array.isArray(env.details)) {
        const msgs = env.details
          .map((d) =>
            d && typeof d === "object"
              ? (d as Record<string, unknown>).msg
              : undefined,
          )
          .filter((m): m is string => typeof m === "string" && m.length > 0);
        if (msgs.length > 0) {
          return { message: msgs.join("; "), code };
        }
      }

      if (typeof env.message === "string" && env.message.length > 0) {
        return { message: env.message, code };
      }
      if (code) return { message: SEND_ERROR_MESSAGE, code };
    }
  }

  void status;
  return { message: SEND_ERROR_MESSAGE };
}

export function getErrorMessage(
  code: string | undefined | null,
  fallback?: string,
): string {
  if (code && ERROR_MESSAGES[code]) return ERROR_MESSAGES[code];
  return fallback ?? NETWORK_ERROR_MESSAGE;
}

// Render the rate-limit message with a concrete retry window when provided.
// retry_after comes from RateLimitedError.details on the wire.
export function formatRateLimited(retryAfterSeconds?: number | string): string {
  const n =
    typeof retryAfterSeconds === "string"
      ? Number(retryAfterSeconds)
      : retryAfterSeconds;
  if (!Number.isFinite(n) || (n as number) <= 0) {
    return ERROR_MESSAGES.rate_limited;
  }
  return `Слишком много запросов. Попробуйте через ${Math.ceil(n as number)} секунд`;
}

// SPEC.md §9.2: insufficient_funds carries required/available alongside the
// generic message. Amounts are 4dp ruble strings (e.g. "12.3400") emitted by
// micro_to_api_string on the backend; format them for display by stripping
// trailing zeros after the decimal point and appending the ruble sign.
export function formatInsufficientFunds(
  required?: string,
  available?: string,
): string {
  if (!required || !available) return ERROR_MESSAGES.insufficient_funds;
  return `Недостаточно средств: нужно ${formatRubAmount(required)}, доступно ${formatRubAmount(available)}`;
}

function formatRubAmount(apiString: string): string {
  const trimmed = apiString.trim();
  if (!/^-?\d+(\.\d+)?$/.test(trimmed)) return `${trimmed} ₽`;
  const negative = trimmed.startsWith("-");
  const body = negative ? trimmed.slice(1) : trimmed;
  const [whole, frac = ""] = body.split(".");
  let fracTrimmed = frac.replace(/0+$/, "");
  if (fracTrimmed.length === 1) fracTrimmed = fracTrimmed + "0";
  const pretty = fracTrimmed ? `${whole}.${fracTrimmed}` : whole;
  return `${negative ? "-" : ""}${pretty} ₽`;
}
