"use client";

import { useEffect, useRef, useReducer, useCallback, useMemo, useState } from "react";
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

// POSITION_UPDATE is handled outside the reducer (position ref + subscribers).
type Action =
  | { type: "STATE_UPDATE"; data: GuildState }
  | { type: "QUEUE_UPDATE"; data: GuildState }
  | { type: "PLAYBACK_STATE"; data: GuildState }
  | { type: "RESET" };

function reducer(state: InternalState, action: Action): InternalState {
  switch (action.type) {
    case "STATE_UPDATE":
    case "PLAYBACK_STATE":
      return { guild: { ...action.data }, eventVersion: state.eventVersion + 1 };
    case "QUEUE_UPDATE":
      return { guild: { ...action.data }, eventVersion: state.eventVersion };
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

export interface PositionSlice {
  position: number;
  is_paused: boolean;
}

// Position tick as ref + subscription instead of React state — POSITION_UPDATE
// must never cause re-renders. Display reads the ref via rAF; logic subscribes.
export interface ServerPosition {
  ref: React.RefObject<PositionSlice>;
  subscribe: (fn: (p: PositionSlice) => void) => () => void;
}

interface UseWebSocketReturn {
  state: GuildState;
  connected: boolean;
  eventVersion: number;
  serverPosition: ServerPosition;
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

  const serverPosRef = useRef<PositionSlice>({ position: 0, is_paused: false });
  const posListenersRef = useRef<Set<(p: PositionSlice) => void>>(new Set());

  const publishPosition = useCallback((p: PositionSlice) => {
    serverPosRef.current = p;
    posListenersRef.current.forEach((fn) => fn(p));
  }, []);

  const serverPosition = useMemo<ServerPosition>(
    () => ({
      ref: serverPosRef,
      subscribe: (fn) => {
        posListenersRef.current.add(fn);
        return () => {
          posListenersRef.current.delete(fn);
        };
      },
    }),
    []
  );

  const connect = useCallback(() => {
    if (!guildId || !token) return;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    // Prefer the signed session token (no Discord round-trip on the backend);
    // fall back to the raw Discord token passed in / stored locally.
    const wsToken =
      localStorage.getItem("activity_session_token") ||
      token ||
      localStorage.getItem("activity_token") ||
      "";
    const url = `${protocol}//${host}/ws/guild/${guildId}?token=${encodeURIComponent(wsToken)}`;

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      retriesRef.current = 0;
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (!msg.type) return;

        if (msg.type === "POSITION_UPDATE") {
          const d = msg.data;
          if (d && typeof d.position === "number") {
            publishPosition({ position: d.position, is_paused: !!d.is_paused });
          }
          return;
        }

        if (msg.data !== undefined) {
          dispatch(msg as Action);
          // Seed position from STATE_UPDATE so remote seeks correct drift at once.
          const cur = (msg.data as GuildState)?.current;
          if (cur && typeof cur.position === "number") {
            publishPosition({ position: cur.position, is_paused: !!cur.is_paused });
          }
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
  }, [guildId, token, publishPosition]);

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

  // Backgrounded phones suspend the socket without firing onclose — reconnect
  // immediately on return instead of waiting out the backoff timer.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return;
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      retriesRef.current = 0;
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
      connect();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [connect]);

  return { state: internalState.guild, connected, eventVersion: internalState.eventVersion, serverPosition };
}
