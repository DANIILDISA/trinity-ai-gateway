import type { ApiError } from "@/types/api";
import { getErrorMessage, NETWORK_ERROR_MESSAGE } from "./errors";

const DEFAULT_BASE_URL = "http://localhost:8000";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** A protected call that 401s should refresh the access token once and retry —
 *  EXCEPT the auth endpoints themselves, which produce 401s as a normal part of
 *  the refresh/login flow and must never trigger a recursive refresh. */
const NO_REFRESH_PATHS = ["/api/v1/auth/refresh", "/api/v1/auth/login"];

/** Resolves true when a refresh succeeded (caller should retry), false on a
 *  genuine logout / unrecoverable state (caller should surface the 401). */
type UnauthorizedHandler = () => Promise<boolean>;

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
  }
}

export class ApiClient {
  private baseUrl: string;
  private accessToken: string | null = null;
  private onUnauthorized: UnauthorizedHandler | null = null;

  constructor(baseUrl?: string) {
    this.baseUrl =
      baseUrl ?? process.env.NEXT_PUBLIC_API_URL ?? DEFAULT_BASE_URL;
  }

  setToken(token: string | null): void {
    this.accessToken = token;
  }

  getToken(): string | null {
    return this.accessToken;
  }

  /** Register the 401 recovery hook (wired up in lib/auth via DI to avoid an
   *  import cycle). When a protected call 401s, the client invokes this once,
   *  and retries the request a single time if it resolves true. */
  setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
    this.onUnauthorized = handler;
  }

  get<T>(path: string): Promise<T> {
    return this.request<T>("GET", path);
  }

  post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>("POST", path, body);
  }

  patch<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>("PATCH", path, body);
  }

  put<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>("PUT", path, body);
  }

  async delete(path: string): Promise<void> {
    await this.request<void>("DELETE", path);
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    isRetry = false,
  ): Promise<T> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (this.accessToken) {
      headers.Authorization = `Bearer ${this.accessToken}`;
    }
    if (MUTATING_METHODS.has(method)) {
      const csrf = readCookie("csrf_token");
      if (csrf) headers["X-CSRF-Token"] = csrf;
    }

    const url = `${this.baseUrl}${path}`;
    let response: Response;
    try {
      response = await fetch(url, {
        method,
        headers,
        credentials: "include",
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch {
      throw new ApiRequestError("Ошибка сети. Проверьте подключение.", 0);
    }

    if (response.status === 204 || response.status === 205) {
      return undefined as T;
    }

    const text = await response.text();
    const parsed = text ? safeJsonParse(text) : null;

    if (!response.ok) {
      // A 401 on a protected call is the access-token-expired signal: refresh
      // the token once and retry, rather than surfacing the 401 as a dead error
      // (which is what previously littered the console — see tr-qrx). Guarded so
      // it never loops (single retry) and never recurses on the auth endpoints.
      if (
        response.status === 401 &&
        !isRetry &&
        this.onUnauthorized &&
        !NO_REFRESH_PATHS.includes(path)
      ) {
        const refreshed = await this.onUnauthorized();
        if (refreshed) {
          return this.request<T>(method, path, body, true);
        }
      }
      const { message, code } = extractError(parsed, response.status);
      throw new ApiRequestError(message, response.status, code);
    }

    return (parsed as T) ?? (undefined as T);
  }
}

function extractError(
  parsed: unknown,
  status: number,
): { message: string; code?: string } {
  if (parsed && typeof parsed === "object") {
    const obj = parsed as Record<string, unknown>;
    const envelope = obj.error as Record<string, unknown> | undefined;
    if (envelope && typeof envelope === "object") {
      const m = envelope.message;
      const c = envelope.code;
      if (typeof m === "string" && m.length > 0) {
        return { message: m, code: typeof c === "string" ? c : undefined };
      }
    }
    const detail = obj.detail;
    if (typeof detail === "string" && detail.length > 0) {
      return { message: detail };
    }
  }
  return { message: `Ошибка запроса (${status})` };
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const target = `${name}=`;
  const parts = document.cookie.split(";");
  for (const raw of parts) {
    const c = raw.trim();
    if (c.startsWith(target)) return decodeURIComponent(c.slice(target.length));
  }
  return null;
}

export const apiClient = new ApiClient();

// Resolve any thrown value into a user-facing Russian string. Prefers the
// SPEC.md §9.2 mapping when the backend supplied a known error code; falls
// back to the server-provided message; finally to the generic network error.
export function translateApiError(err: unknown): string {
  if (err instanceof ApiRequestError) {
    if (err.status === 0) return NETWORK_ERROR_MESSAGE;
    return getErrorMessage(err.code, err.message);
  }
  return NETWORK_ERROR_MESSAGE;
}
