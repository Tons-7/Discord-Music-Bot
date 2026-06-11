"use client";

import { useEffect, useRef } from "react";
import type { DiscordSDK } from "@discord/embedded-app-sdk";

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

  useEffect(() => {
    if (!sdk) return;

    if (!song) {
      if (lastRef.current.url !== null) {
        sdk.commands.setActivity({ activity: null as never }).catch(() => {});
        lastRef.current = { url: null, paused: false, posAt: 0, sentAt: 0 };
      }
      return;
    }

    const update = (force: boolean) => {
      const last = lastRef.current;
      const now = Date.now();
      const position = getPosition();
      const extrapolated = last.paused ? last.posAt : last.posAt + (now - last.sentAt) / 1000;
      const posJumped = Math.abs(extrapolated - position) > POSITION_JUMP_THRESHOLD;

      if (!force && !posJumped) return;

      const activity: Record<string, unknown> = {
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

      sdk.commands.setActivity({ activity: activity as never }).catch(() => {});
      lastRef.current = { url: song.webpage_url, paused: isPaused, posAt: position, sentAt: now };
    };

    const last = lastRef.current;
    update(last.url !== song.webpage_url || last.paused !== isPaused);

    // Position is no longer React state, so seeks are detected by polling
    const interval = setInterval(() => update(false), JUMP_CHECK_MS);
    return () => clearInterval(interval);
  }, [sdk, song, isPaused, getPosition]);
}
