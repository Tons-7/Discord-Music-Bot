"use client";

import { useEffect, useRef, useReducer, useCallback, useState } from "react";
import type { GuildState } from "@/types";

const INITIAL_STATE: GuildState = {
  current: null,
  queue: [],
  history: [],
  volume: 100,
  loop_mode: "off",
  shuffle: false,
  autoplay: false,
  speed: 1.0,
  audio_effect: "none",
  is_connected: false,
  queue_duration: 0,
};

type Action =
  | { type: "STATE_UPDATE"; data: GuildState }
  | { type: "QUEUE_UPDATE"; data: GuildState }
  | { type: "PLAYBACK_STATE"; data: GuildState }
  | { type: "POSITION_UPDATE"; data: { position: number; is_paused: boolean } }
  | { type: "RESET" };

function reducer(state: InternalState, action: Action): InternalState {
  switch (action.type) {
    case "STATE_UPDATE":
    case "PLAYBACK_STATE":
      return { guild: { ...action.data }, eventVersion: state.eventVersion + 1 };
    case "QUEUE_UPDATE":
      return { guild: { ...action.data }, eventVersion: state.eventVersion };
    case "POSITION_UPDATE":
      if (!state.guild.current) return state;
      return {
        ...state,
        guild: {
          ...state.guild,
          current: {
            ...state.guild.current,
            position: action.data.position,
            is_paused: action.data.is_paused,
          },
        },
      };
    case "RESET":
      return { guild: INITIAL_STATE, eventVersion: 0 };
    default:
      return state;
  }
}

interface InternalState {
  guild: GuildState;
  eventVersion: number; // increments on STATE_UPDATE/PLAYBACK_STATE, not on POSITION_UPDATE
}

interface UseWebSocketReturn {
  state: GuildState;
  connected: boolean;
  eventVersion: number;
}

export function useWebSocket(
  guildId: string | null,
  token: string | null
): UseWebSocketReturn {
  const [internalState, dispatch] = useReducer(reducer, { guild: INITIAL_STATE, eventVersion: 0 });
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const retriesRef = useRef(0);

  const connect = useCallback(() => {
    if (!guildId || !token) return;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const url = `${protocol}//${host}/ws/guild/${guildId}?token=${token}`;

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      retriesRef.current = 0;
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type && msg.data !== undefined) {
          dispatch(msg as Action);
        }
      } catch {
        // ignore malformed messages
      }
    };

    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;

      // Reconnect with exponential backoff
      const delay = Math.min(1000 * 2 ** retriesRef.current, 30000);
      retriesRef.current++;
      reconnectTimeoutRef.current = setTimeout(connect, delay);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [guildId, token]);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, [connect]);

  return { state: internalState.guild, connected, eventVersion: internalState.eventVersion };
}
