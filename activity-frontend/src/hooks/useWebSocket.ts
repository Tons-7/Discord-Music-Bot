"use client";

import { useEffect, useEffectEvent, useReducer, useRef, useState } from "react";
import type { GuildState } from "@/types";
import { INITIAL_STATE, mergeGuildState } from "@/lib/guildState";
import {
  clearSessionToken, errorMessage, errorStatus, fetchSessionToken, getSessionToken, sessionTokenRemainingMs,
} from "@/lib/api";
import {
  closeAction, fatalCloseMessage, PING_INTERVAL_MS, PONG_TIMEOUT_MS, reconnectDelay, VISIBLE_PONG_TIMEOUT_MS,
} from "@/lib/ws";

// POSITION_UPDATE is handled outside the reducer (position store + subscribers).
type Action = { type: "STATE_UPDATE" | "QUEUE_UPDATE" | "PLAYBACK_STATE"; data: Partial<GuildState> };

const STATE_TYPES = new Set(["STATE_UPDATE", "QUEUE_UPDATE", "PLAYBACK_STATE"]);

interface InternalState {
  guild: GuildState;
  eventVersion: number; // increments on STATE_UPDATE/PLAYBACK_STATE, not on POSITION_UPDATE
  hasState: boolean; // a state message has arrived since launch
}

function reducer(state: InternalState, action: Action): InternalState {
  const guild = mergeGuildState(state.guild, action.data);
  if (action.type === "QUEUE_UPDATE") return { ...state, guild, hasState: true };
  return { guild, eventVersion: state.eventVersion + 1, hasState: true };
}

export interface PositionSlice {
  position: number;
  is_paused: boolean;
  // performance.now() when this value arrived; 0 before the first one
  at: number;
  // The song the value belongs to, when known
  webpage_url?: string | null;
  seq?: number;
}

// Position tick as a store + subscription instead of React state — POSITION_UPDATE
// must never cause re-renders. Display reads the ref via rAF; logic subscribes.
export interface ServerPosition {
  ref: React.RefObject<PositionSlice>;
  subscribe: (fn: (p: PositionSlice) => void) => () => void;
}

interface PositionStore extends ServerPosition {
  publish: (p: PositionSlice) => void;
  // The current song per the latest state message; ticks without their own URL belong to it
  song: () => { url: string | null; seq?: number };
  setSong: (song: { url: string | null; seq?: number }) => void;
}

function createPositionStore(): PositionStore {
  const ref = { current: { position: 0, is_paused: false, at: 0 } as PositionSlice };
  const listeners = new Set<(p: PositionSlice) => void>();
  let song: { url: string | null; seq?: number } = { url: null };
  return {
    ref,
    song: () => song,
    setSong(next) { song = next; },
    subscribe(fn) {
      listeners.add(fn);
      return () => { listeners.delete(fn); };
    },
    publish(p) {
      ref.current = p;
      listeners.forEach(fn => fn(p));
    },
  };
}

export type SocketStatus = "connecting" | "open" | "reconnecting" | "fatal";

interface UseWebSocketReturn {
  state: GuildState;
  connected: boolean;
  status: SocketStatus;
  // Plain-language reason when the server refused this listener for good
  fatal: string | null;
  hasState: boolean;
  eventVersion: number;
  serverPosition: ServerPosition;
  retry: () => void;
}

export function useWebSocket(
  guildId: string | null,
  ids: { channelId?: string | null; instanceId?: string | null } = {},
): UseWebSocketReturn {
  const [internal, dispatch] = useReducer(reducer, { guild: INITIAL_STATE, eventVersion: 0, hasState: false });
  const [status, setStatus] = useState<SocketStatus>("connecting");
  const [fatal, setFatal] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const [store] = useState(createPositionStore);
  const everOpenedRef = useRef(false);
  const { channelId, instanceId } = ids;

  const handleMessage = useEffectEvent((event: MessageEvent) => {
    let msg: { type?: string; data?: Record<string, unknown> };
    try {
      msg = JSON.parse(event.data);
    } catch {
      return; // ignore malformed messages
    }
    if (!msg?.type) return;
    const now = performance.now();

    if (msg.type === "POSITION_UPDATE") {
      const d = msg.data;
      if (d && typeof d.position === "number") {
        store.publish({
          position: d.position,
          is_paused: !!d.is_paused,
          at: now,
          webpage_url: typeof d.webpage_url === "string" ? d.webpage_url : store.song().url,
          seq: typeof d.seq === "number" ? d.seq : store.song().seq,
        });
      }
      return;
    }

    if (!STATE_TYPES.has(msg.type) || !msg.data) return;
    const data = msg.data as Partial<GuildState>;
    dispatch({ type: msg.type as Action["type"], data });
    if (!("current" in data)) return;
    // Seed position from state messages so remote seeks correct drift at once.
    const cur = data.current as (GuildState["current"] & { position?: number }) | null;
    store.setSong({ url: cur?.webpage_url ?? null, seq: cur?.seq });
    if (cur && typeof cur.position === "number") {
      store.publish({ position: cur.position, is_paused: !!cur.is_paused, at: now, webpage_url: cur.webpage_url, seq: cur.seq });
    }
  });

  useEffect(() => {
    if (!guildId) return;
    let stopped = false;
    let ws: WebSocket | null = null;
    let retries = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    let pingTimer: ReturnType<typeof setInterval> | undefined;
    let pongTimer: ReturnType<typeof setTimeout> | undefined;
    let awaitingReply = false;
    let attempts = 0;

    const dropSocket = () => {
      clearInterval(pingTimer);
      clearTimeout(pongTimer);
      awaitingReply = false;
      const old = ws;
      ws = null;
      if (old) {
        old.onopen = old.onmessage = old.onclose = old.onerror = null;
        try { old.close(); } catch { /* already closed */ }
      }
    };

    const retry = () => {
      if (stopped) return;
      setStatus(everOpenedRef.current ? "reconnecting" : "connecting");
      clearTimeout(reconnectTimer);
      const delay = reconnectDelay(retries);
      retries += 1;
      reconnectTimer = setTimeout(open, delay);
    };

    const fail = (message: string) => {
      stopped = true;
      clearTimeout(reconnectTimer);
      clearTimeout(refreshTimer);
      dropSocket();
      setFatal(message);
      setStatus("fatal");
    };

    // Any message proves the socket is alive; a missing reply means it went half-open.
    const ping = (timeout: number) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      try {
        ws.send(JSON.stringify({ type: "PING" }));
      } catch {
        dropSocket();
        retry();
        return;
      }
      awaitingReply = true;
      clearTimeout(pongTimer);
      pongTimer = setTimeout(() => {
        if (!awaitingReply) return;
        dropSocket();
        retry();
      }, timeout);
    };

    // Stream URLs read the token when a song loads, so it is renewed well before it expires.
    function refresh() {
      fetchSessionToken(guildId!).then(scheduleRefresh, () => {
        if (!stopped) refreshTimer = setTimeout(refresh, 60_000);
      });
    }
    function scheduleRefresh() {
      if (stopped) return;
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(refresh, Math.max(60_000, sessionTokenRemainingMs(guildId!) * 0.8));
    }

    async function open() {
      if (stopped || !guildId) return;
      clearTimeout(reconnectTimer);
      // A wake-up while the token is being fetched supersedes this attempt instead of opening a second socket.
      attempts += 1;
      const attempt = attempts;
      let token = getSessionToken(guildId);
      if (!token) {
        try {
          token = await fetchSessionToken(guildId);
        } catch (e) {
          if (stopped || attempt !== attempts) return;
          const code = errorStatus(e);
          // The same checks the socket makes, answered in plain words (banned, not a member, no bot).
          if (code === 403 || code === 404) return fail(errorMessage(e, "You can't use the music bot here."));
          if (code === 401) return fail("Your Discord sign-in expired. Close and reopen the Activity.");
          return retry();
        }
        if (stopped || attempt !== attempts) return;
        scheduleRefresh();
      } else if (refreshTimer === undefined) {
        scheduleRefresh();
      }

      const params = new URLSearchParams({ token });
      if (instanceId) params.set("instance_id", instanceId);
      if (channelId) params.set("channel_id", channelId);
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(`${protocol}//${window.location.host}/ws/guild/${guildId}?${params}`);
      ws = socket;
      let opened = false;

      socket.onopen = () => {
        opened = true;
        everOpenedRef.current = true;
        retries = 0;
        setStatus("open");
        // An idle socket (paused, or nothing playing) sends no traffic, and
        // proxies drop it silently — no onclose fires. Ping so the drop surfaces.
        clearInterval(pingTimer);
        pingTimer = setInterval(() => ping(PONG_TIMEOUT_MS), PING_INTERVAL_MS);
      };

      socket.onmessage = (event) => {
        awaitingReply = false;
        clearTimeout(pongTimer);
        handleMessage(event);
      };

      socket.onclose = (event) => {
        if (ws !== socket) return;
        dropSocket();
        const action = closeAction(event.code);
        if (action === "fatal") return fail(fatalCloseMessage(event.code, event.reason));
        // A handshake refused before accept loses its close code, so fetch a fresh token:
        // that request reports a ban or a missing bot in plain words.
        if (action === "refresh-token" || !opened) clearSessionToken();
        retry();
      };

      socket.onerror = () => {
        socket.close();
      };
    }

    // Backgrounded phones suspend the socket without firing onclose. On return,
    // check it with a ping, or reconnect at once instead of waiting out the backoff.
    const wake = () => {
      if (stopped) return;
      if (ws?.readyState === WebSocket.OPEN) {
        ping(VISIBLE_PONG_TIMEOUT_MS);
        return;
      }
      if (ws?.readyState === WebSocket.CONNECTING) return;
      retries = 0;
      dropSocket();
      open();
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") wake();
    };

    open();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", wake);
    return () => {
      stopped = true;
      clearTimeout(reconnectTimer);
      clearTimeout(refreshTimer);
      dropSocket();
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", wake);
    };
  }, [guildId, channelId, instanceId, generation]);

  const retryConnection = () => {
    setFatal(null);
    setStatus("connecting");
    setGeneration(g => g + 1);
  };

  return {
    state: internal.guild,
    connected: status === "open",
    status,
    fatal,
    hasState: internal.hasState,
    eventVersion: internal.eventVersion,
    serverPosition: store,
    retry: retryConnection,
  };
}
