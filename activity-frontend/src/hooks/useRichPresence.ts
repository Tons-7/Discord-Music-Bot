"use client";

import { useEffect, useEffectEvent, useRef } from "react";
import type { DiscordSDK } from "@discord/embedded-app-sdk";

type ActivityPayload = NonNullable<
  Parameters<DiscordSDK["commands"]["setActivity"]>[0]["activity"]
>;

interface PresenceSong {
  webpage_url: string;
  title: string;
  uploader?: string;
  duration: number;
  thumbnail?: string;
  is_live?: boolean;
}

// Discord rate-limits setActivity (5 per 20s). We only refire on meaningful
// changes: song swap, pause/resume, or a position jump that implies a seek.
const POSITION_JUMP_THRESHOLD = 5;
const JUMP_CHECK_MS = 3000;

export function useRichPresence(
  sdk: DiscordSDK | null,
  song: PresenceSong | null,
  getPosition: () => number,
  isPaused: boolean,
) {
  const lastRef = useRef<{ url: string | null; paused: boolean; posAt: number; sentAt: number }>({
    url: null, paused: false, posAt: 0, sentAt: 0,
  });

  const update = useEffectEvent((force: boolean) => {
    if (!sdk) return;

    if (!song) {
      if (lastRef.current.url !== null) {
        sdk.commands.setActivity({ activity: null }).catch(() => {});
        lastRef.current = { url: null, paused: false, posAt: 0, sentAt: 0 };
      }
      return;
    }

    const last = lastRef.current;
    const now = Date.now();
    const position = getPosition();
    const extrapolated = last.paused ? last.posAt : last.posAt + (now - last.sentAt) / 1000;
    const posJumped = Math.abs(extrapolated - position) > POSITION_JUMP_THRESHOLD;

    if (!force && !posJumped) return;

    const activity: ActivityPayload = {
      type: 2, // Listening
      details: song.title,
      instance: true,
    };
    if (song.uploader) activity.state = song.uploader;

    if (!song.is_live && !isPaused && song.duration > 0) {
      const startSec = Math.floor(now / 1000) - Math.floor(position);
      activity.timestamps = { start: startSec, end: startSec + Math.floor(song.duration) };
    }

    if (song.thumbnail) {
      activity.assets = { large_image: song.thumbnail };
    }

    sdk.commands.setActivity({ activity }).catch(() => {});
    lastRef.current = { url: song.webpage_url, paused: isPaused, posAt: position, sentAt: now };
  });

  // Refire on meaningful changes (song swap, pause/resume, clear on stop)
  useEffect(() => {
    if (!sdk) return;
    const last = lastRef.current;
    update(!song || last.url !== song.webpage_url || last.paused !== isPaused);
  }, [sdk, song, isPaused]);

  // Position is not React state, so seeks are detected by polling. The
  // interval survives song/pause changes; update() reads the latest values.
  useEffect(() => {
    if (!sdk) return;
    const interval = setInterval(() => update(false), JUMP_CHECK_MS);
    return () => clearInterval(interval);
  }, [sdk]);
}
