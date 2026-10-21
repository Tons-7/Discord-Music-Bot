import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiFetch, clearSessionToken, errorStatus, getSessionToken, setApiToken, setSessionToken } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  clearSessionToken();
});

describe("session token", () => {
  it("is scoped to its guild", () => {
    setSessionToken("1", "tok", 3600);
    expect(getSessionToken("1")).toBe("tok");
    expect(getSessionToken("2")).toBeNull();
  });

  it("expires a minute early", () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    setSessionToken("1", "tok", 120);
    vi.setSystemTime(59_000);
    expect(getSessionToken("1")).toBe("tok");
    vi.setSystemTime(61_000);
    expect(getSessionToken("1")).toBeNull();
  });
});

describe("apiFetch", () => {
  it("sends the bearer token and JSON body", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    setApiToken("abc");
    await apiFetch("/x", { method: "POST", body: "{}" });
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer abc");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("throws the server's detail with the status", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "DJ role required" }), { status: 403 })));
    const err = await apiFetch("/x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toBe("DJ role required");
    expect(errorStatus(err)).toBe(403);
  });

  it("never shows a validation payload as the message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: [{ msg: "bad" }] }), { status: 422 })));
    const err = await apiFetch("/x").catch((e: unknown) => e);
    expect((err as ApiError).message).toBe("Request failed (422)");
  });

  it("reports an unreachable server as status 0 in plain words", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    const err = await apiFetch("/x").catch((e: unknown) => e);
    expect(errorStatus(err)).toBe(0);
    expect((err as ApiError).message).toMatch(/Couldn't reach the server/);
  });

  it("passes aborts through untouched", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new DOMException("aborted", "AbortError"); }));
    const err = await apiFetch("/x").catch((e: unknown) => e);
    expect((err as DOMException).name).toBe("AbortError");
  });
});
