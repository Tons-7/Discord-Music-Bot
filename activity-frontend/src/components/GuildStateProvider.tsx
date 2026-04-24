"use client";

import { createContext, useContext, useCallback, useMemo } from "react";
import { useWebSocket } from "@/hooks/useWebSocket";
import { apiFetch } from "@/lib/api";
import type { GuildState } from "@/types";

interface GuildStateContextValue {
  state: GuildState;
  guildId: string;
  connected: boolean;
  eventVersion: number;
  sendCommand: (action: string, data?: Record<string, unknown>) => Promise<void>;
}

const GuildStateContext = createContext<GuildStateContextValue | null>(null);

export function GuildStateProvider({
  guildId,
  accessToken,
  children,
}: {
  guildId: string;
  accessToken: string;
  children: React.ReactNode;
}) {
  const { state, connected, eventVersion } = useWebSocket(guildId, accessToken);

  const sendCommand = useCallback(
    async (action: string, data?: Record<string, unknown>) => {
      const method = "POST";
      const path = `/api/guild/${guildId}/${action}`;
      let body: string | undefined;

      if (data) {
        body = JSON.stringify(data);
      }

      await apiFetch(path, { method, body });
    },
    [guildId]
  );

  const value = useMemo(
    () => ({ state, guildId, connected, eventVersion, sendCommand }),
    [state, guildId, connected, eventVersion, sendCommand]
  );

  return (
    <GuildStateContext.Provider value={value}>
      {children}
    </GuildStateContext.Provider>
  );
}

export function useGuildState() {
  const ctx = useContext(GuildStateContext);
  if (!ctx) throw new Error("useGuildState must be used within GuildStateProvider");
  return ctx;
}
