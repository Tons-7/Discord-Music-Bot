"use client";

import { useState, useEffect, useRef } from "react";
import { DiscordAuth, initDiscordSDK, startupErrorMessage } from "@/lib/discord-sdk";
import { forgetLegacyTokens, setApiToken } from "@/lib/api";

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
    forgetLegacyTokens();

    initDiscordSDK()
      .then((auth) => {
        // In memory only: REST calls send it as a header, query strings use the session token.
        setApiToken(auth.accessToken);
        setState({ ready: true, auth, error: null });
      })
      .catch((err: unknown) => {
        setState({ ready: false, auth: null, error: startupErrorMessage(err) });
      });
  }, []);

  return state;
}
