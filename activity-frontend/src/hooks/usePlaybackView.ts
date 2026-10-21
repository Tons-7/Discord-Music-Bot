"use client";

import { useGuildState } from "@/components/GuildStateProvider";
import type { AudioPlayerHandle } from "./useAudioPlayer";
import type { CurrentSong } from "@/types";

export interface PlaybackView {
  current: CurrentSong | null;
  hasSong: boolean;
  /** Length of the current song: the element's when it knows, else the server's. */
  totalDur: number;
  isPaused: boolean;
  getPosition: () => number;
  /** Nothing would play after the current song, so Next is disabled. */
  noNext: boolean;
}

/** The one place the player views (Now Playing, mini player, PiP, shortcuts) derive what they show. */
export function usePlaybackView(audio: AudioPlayerHandle): PlaybackView {
  const { state } = useGuildState();
  const { current } = state;
  return {
    current,
    hasSong: !!current,
    totalDur: audio.ready && audio.duration > 0 ? audio.duration : (current?.duration ?? 0),
    isPaused: audio.isPaused,
    getPosition: audio.getPosition,
    noNext: state.queue.length === 0 && !state.autoplay && !state.is_connected && state.loop_mode === "off",
  };
}
