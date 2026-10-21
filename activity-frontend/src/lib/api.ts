let _token: string | null = null;
let _session: { guildId: string; token: string; expiresAt: number } | null = null;

export function setApiToken(token: string) {
  _token = token;
}

/** Thrown for any failed request; `status` is 0 when the server could not be reached. */
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function errorStatus(e: unknown): number | undefined {
  return e instanceof ApiError ? e.status : undefined;
}

export function errorMessage(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback;
}

/**
 * Signed, guild-scoped session token for query-string contexts (<audio> src, WebSocket).
 * Kept in memory only, so a token from another launch, guild or account is never reused.
 */
export function getSessionToken(guildId: string): string | null {
  if (!_session || _session.guildId !== guildId || Date.now() >= _session.expiresAt) return null;
  return _session.token;
}

export function setSessionToken(guildId: string, token: string, expiresInSeconds: number) {
  // Expire locally a minute early so a token is never used right at its deadline.
  _session = { guildId, token, expiresAt: Date.now() + Math.max(0, expiresInSeconds - 60) * 1000 };
}

export function clearSessionToken() {
  _session = null;
}

/** Milliseconds until the session token for this guild should be treated as expired, 0 if none. */
export function sessionTokenRemainingMs(guildId: string): number {
  if (!_session || _session.guildId !== guildId) return 0;
  return Math.max(0, _session.expiresAt - Date.now());
}

export async function fetchSessionToken(guildId: string): Promise<string> {
  const resp = await apiFetch<{ token: string; expires_in?: number }>(`/api/guild/${guildId}/session-token`);
  setSessionToken(guildId, resp.token, typeof resp.expires_in === "number" ? resp.expires_in : 21600);
  return resp.token;
}

// Copies from older builds: a stale token there used to win over the fresh one.
export function forgetLegacyTokens() {
  try {
    localStorage.removeItem("activity_session_token");
    localStorage.removeItem("activity_token");
  } catch {
    // storage unavailable — nothing to clean up
  }
}

export async function apiFetch<T = unknown>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const headers: Record<string, string> = {
    ...(options.headers as Record<string, string>),
  };

  if (_token) {
    headers["Authorization"] = `Bearer ${_token}`;
  }

  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  let resp: Response;
  try {
    resp = await fetch(path, { ...options, headers });
  } catch (e) {
    if ((e as DOMException)?.name === "AbortError") throw e;
    throw new ApiError("Couldn't reach the server. Check your connection.", 0);
  }

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    const detail = typeof err?.detail === "string" ? err.detail : "";
    throw new ApiError(detail || `Request failed (${resp.status})`, resp.status);
  }

  return resp.json();
}
