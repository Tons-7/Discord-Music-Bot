// WebSocket close codes the backend sends after accepting the socket (see contracts/frontend.md).
export const CLOSE_AUTH = 4001; // token invalid or expired: fetch a new one and reconnect
export const CLOSE_FORBIDDEN = 4003; // banned or not a member: stop retrying
export const CLOSE_NO_BOT = 4004; // the bot is not in this server: stop retrying

export type CloseAction = "retry" | "refresh-token" | "fatal";

export function closeAction(code: number): CloseAction {
  if (code === CLOSE_AUTH) return "refresh-token";
  if (code === CLOSE_FORBIDDEN || code === CLOSE_NO_BOT) return "fatal";
  return "retry";
}

export function fatalCloseMessage(code: number, reason: string): string {
  if (reason) return reason;
  if (code === CLOSE_NO_BOT) return "The music bot isn't in this server.";
  return "You can't use the music bot in this server.";
}

/**
 * Delay before reconnect attempt `retries` (0-based). Short while the server still
 * holds the room (it clears Activity playback 20s after the last viewer leaves),
 * then slower. Jitter keeps clients from reconnecting in lockstep.
 */
export function reconnectDelay(retries: number, random: () => number = Math.random): number {
  const base = retries < 6 ? Math.min(5000, 500 * 2 ** retries) : 15000;
  return Math.round(base * (0.8 + random() * 0.4));
}

// Ping often enough that idle proxies keep the socket, and give the reply this long to arrive.
export const PING_INTERVAL_MS = 25000;
export const PONG_TIMEOUT_MS = 10000;
export const VISIBLE_PONG_TIMEOUT_MS = 5000;
