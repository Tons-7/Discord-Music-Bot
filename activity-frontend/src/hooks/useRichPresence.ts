"use client";

import { useEffect, useEffectEvent, useRef } from "react";
import type { DiscordSDK } from "@discord/embedded-app-sdk";
import { presenceTimestamps } from "@/lib/playback";

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
// changes: song swap, pause/resume, a rate change, or a position jump that implies a seek.
const POSITION_JUMP_THRESHOLD = 5;
const JUMP_CHECK_MS = 3000;

function isHttpUrl(url: string | undefined): url is string {
  return !!url && /^https?:\/\//i.test(url);
}

export function useRichPresence(
  sdk: DiscordSDK | null,
  song: PresenceSong | null,
  getPosition: () => number,
  isPaused: boolean,
  // Song seconds per real second (speed times the effect's multiplier)
  rate: number,
) {
  const lastRef = useRef<{ url: string | null; paused: boolean; rate: number; posAt: number; sentAt: number }>({
    url: null, paused: false, rate: 1, posAt: 0, sentAt: 0,
  });

  const update = useEffectEvent((force: boolean) => {
    if (!sdk) return;

    if (!song) {
      if (lastRef.current.url !== null) {
        sdk.commands.setActivity({ activity: null }).catch(() => {});
        lastRef.current = { url: null, paused: false, rate: 1, posAt: 0, sentAt: 0 };
      }
      return;
    }

    const last = lastRef.current;
    const now = Date.now();
    const position = getPosition();
    const extrapolated = last.paused ? last.posAt : last.posAt + (now - last.sentAt) / 1000 * last.rate;
    const posJumped = Math.abs(extrapolated - position) > POSITION_JUMP_THRESHOLD;

    if (!force && !posJumped) return;

    const activity: ActivityPayload = {
      type: 2, // Listening
      details: song.title,
      instance: true,
    };
    // The song title in profiles and the member list opens the track.
    if (isHttpUrl(song.webpage_url)) activity.details_url = song.webpage_url;
    if (song.uploader) activity.state = song.uploader;

    if (!song.is_live && !isPaused && song.duration > 0) {
      activity.timestamps = presenceTimestamps(now, position, song.duration, rate);
    }

    if (song.thumbnail) {
      activity.assets = { large_image: song.thumbnail, large_text: song.title };
      if (isHttpUrl(song.webpage_url)) activity.assets.large_url = song.webpage_url;
    }

    sdk.commands.setActivity({ activity }).catch(() => {});
    lastRef.current = { url: song.webpage_url, paused: isPaused, rate, posAt: position, sentAt: now };
  });

  // Refire on meaningful changes (song swap, pause/resume, rate change, clear on stop)
  useEffect(() => {
    if (!sdk) return;
    const last = lastRef.current;
    update(!song || last.url !== song.webpage_url || last.paused !== isPaused || last.rate !== rate);
  }, [sdk, song, isPaused, rate]);

  // Position is not React state, so seeks are detected by polling. The
  // interval survives song/pause changes; update() reads the latest values.
  useEffect(() => {
    if (!sdk) return;
    const interval = setInterval(() => update(false), JUMP_CHECK_MS);
    return () => clearInterval(interval);
  }, [sdk]);
}
