"use client";

import { useState, useEffect, useRef } from "react";
import { DiscordAuth, initDiscordSDK } from "@/lib/discord-sdk";
import { setApiToken } from "@/lib/api";

interface DiscordState {
  ready: boolean;
  auth: DiscordAuth | null;
  error: string | null;
}

export function useDiscord(): DiscordState {
  const [state, setState] = useState<DiscordState>({
    ready: false,
    auth: null,
    error: null,
  });
  const initRef = useRef(false);

  useEffect(() => {
    if (initRef.current) return;
    initRef.current = true;

    initDiscordSDK()
      .then((auth) => {
        setApiToken(auth.accessToken);
        // Store token for audio element (can't set headers on <audio src>)
        localStorage.setItem("activity_token", auth.accessToken);
        setState({ ready: true, auth, error: null });
      })
      .catch((err) => {
        setState({ ready: false, auth: null, error: err.message });
      });
  }, []);

  return state;
}
