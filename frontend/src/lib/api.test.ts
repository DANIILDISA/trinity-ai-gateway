import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiRequestError } from "@/lib/api";

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
  } as unknown as Response;
}

describe("ApiClient — 401 refresh-and-retry", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("on a 401, runs the unauthorized handler then retries the request once", async () => {
    const client = new ApiClient("http://x");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(401, { detail: "expired" }))
      .mockResolvedValueOnce(jsonResponse(200, { id: "u1" }));
    vi.stubGlobal("fetch", fetchMock);

    const handler = vi.fn().mockResolvedValue(true);
    client.setUnauthorizedHandler(handler);

    const res = await client.get<{ id: string }>("/api/v1/users/me");

    expect(handler).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(res).toEqual({ id: "u1" });
  });

  it("does NOT retry when the handler reports it could not refresh (genuine logout)", async () => {
    const client = new ApiClient("http://x");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(401, { detail: "no session" }));
    vi.stubGlobal("fetch", fetchMock);

    const handler = vi.fn().mockResolvedValue(false);
    client.setUnauthorizedHandler(handler);

    await expect(client.get("/api/v1/users/me")).rejects.toMatchObject({
      status: 401,
    });
    expect(handler).toHaveBeenCalledTimes(1);
    // Original attempt only — no retry after a failed refresh.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("only retries once: a second 401 after refresh is surfaced, not looped", async () => {
    const client = new ApiClient("http://x");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(401, { detail: "still expired" }));
    vi.stubGlobal("fetch", fetchMock);

    const handler = vi.fn().mockResolvedValue(true);
    client.setUnauthorizedHandler(handler);

    await expect(client.get("/api/v1/users/me")).rejects.toMatchObject({
      status: 401,
    });
    // Handler runs once; fetch runs twice (original + single retry), no loop.
    expect(handler).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("never refresh-loops the /auth/refresh call itself", async () => {
    const client = new ApiClient("http://x");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(401, { detail: "no cookie" }));
    vi.stubGlobal("fetch", fetchMock);

    const handler = vi.fn().mockResolvedValue(true);
    client.setUnauthorizedHandler(handler);

    await expect(
      client.post("/api/v1/auth/refresh"),
    ).rejects.toBeInstanceOf(ApiRequestError);
    expect(handler).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not invoke the handler on a successful request", async () => {
    const client = new ApiClient("http://x");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(200, { id: "u1" }));
    vi.stubGlobal("fetch", fetchMock);

    const handler = vi.fn().mockResolvedValue(true);
    client.setUnauthorizedHandler(handler);

    await client.get("/api/v1/users/me");
    expect(handler).not.toHaveBeenCalled();
  });
});
