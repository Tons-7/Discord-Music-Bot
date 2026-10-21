"use client";

import { createContext, useContext } from "react";
import { useWebSocket, type ServerPosition, type SocketStatus } from "@/hooks/useWebSocket";
import { apiFetch } from "@/lib/api";
import type { GuildState } from "@/types";

type SendCommand = <T = { ok: boolean }>(action: string, data?: Record<string, unknown>) => Promise<T>;

// Stable for the whole session: components that only need the guild re-render never.
interface GuildIdValue {
  guildId: string;
  // The voice channel and Activity instance this client was launched in
  channelId: string | null;
  instanceId: string | null;
  sendCommand: SendCommand;
}

interface GuildStateValue {
  state: GuildState;
  connected: boolean;
  status: SocketStatus;
  fatal: string | null;
  hasState: boolean;
  eventVersion: number;
  retryConnection: () => void;
}

const GuildIdContext = createContext<GuildIdValue | null>(null);
const GuildStateContext = createContext<GuildStateValue | null>(null);

// Stable ref + subscription (see useWebSocket) — never causes re-renders
const PositionContext = createContext<ServerPosition | null>(null);

export function GuildStateProvider({
  guildId,
  channelId = null,
  instanceId = null,
  children,
}: {
  guildId: string;
  channelId?: string | null;
  instanceId?: string | null;
  children: React.ReactNode;
}) {
  const { state, connected, status, fatal, hasState, eventVersion, serverPosition, retry } =
    useWebSocket(guildId, { channelId, instanceId });

  const sendCommand: SendCommand = (action, data) =>
    apiFetch(`/api/guild/${guildId}/${action}`, {
      method: "POST",
      body: data ? JSON.stringify(data) : undefined,
    });

  const idValue = { guildId, channelId, instanceId, sendCommand };
  const stateValue = { state, connected, status, fatal, hasState, eventVersion, retryConnection: retry };

  return (
    <GuildIdContext.Provider value={idValue}>
      <GuildStateContext.Provider value={stateValue}>
        <PositionContext.Provider value={serverPosition}>
          {children}
        </PositionContext.Provider>
      </GuildStateContext.Provider>
    </GuildIdContext.Provider>
  );
}

/** Guild id and commands only. Prefer this where live state isn't needed: it never re-renders. */
export function useGuildId(): GuildIdValue {
  const ctx = useContext(GuildIdContext);
  if (!ctx) throw new Error("useGuildId must be used within GuildStateProvider");
  return ctx;
}

export function useGuildState(): GuildStateValue & GuildIdValue {
  const ids = useGuildId();
  const ctx = useContext(GuildStateContext);
  if (!ctx) throw new Error("useGuildState must be used within GuildStateProvider");
  return { ...ctx, ...ids };
}

export function useServerPosition(): ServerPosition {
  const ctx = useContext(PositionContext);
  if (!ctx) throw new Error("useServerPosition must be used within GuildStateProvider");
  return ctx;
}
