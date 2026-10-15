"use client";

import { useGuildState } from "@/components/GuildStateProvider";
import { useToast } from "@/components/Toast";
import { apiFetch } from "@/lib/api";
import type { AudioPlayerHandle } from "./useAudioPlayer";
import { lastAudibleVolume, rememberAudibleVolume } from "./useLocalVolume";

// Shared voice volume before muting, kept apart from the listener's own volume memory.
let sharedVolumeBeforeMute = 0;

export interface TransportControls {
  playPause: () => void;
  seekBy: (deltaSeconds: number) => void;
  next: () => void;
  previous: () => void;
  /** Returns the new volume level. */
  toggleMute: () => number;
}

/**
 * Transport actions with the same semantics as the NowPlaying buttons — skip is
 * a forced /play in Activity-only mode, seeks stop 5s short of the end, and
 * volume is server-wide only while the bot is in a voice channel.
 */
export function useTransportControls(
  audio: AudioPlayerHandle,
  volume: number,
  onVolumeChange: (v: number) => void,
  getPosition: () => number,
): TransportControls {
  const { state, guildId, sendCommand } = useGuildState();
  const { toast } = useToast();
  const { current } = state;

  const totalDur = audio.ready && audio.duration > 0 ? audio.duration : (current?.duration ?? 0);
  const noNext = state.queue.length === 0 && !state.autoplay && !state.is_connected && state.loop_mode === "off";

  const playPause = () => {
    if (current) audio.playPause();
  };

  const seekBy = (deltaSeconds: number) => {
    if (!current || current.is_live) return;
    const target = Math.floor(getPosition()) + deltaSeconds;
    const clamped = Math.max(
      0,
      totalDur > 0 ? Math.min(target, Math.max(0, Math.floor(totalDur) - 5)) : target,
    );
    audio.seek(clamped);
    sendCommand("seek", { position: String(clamped) })
      .catch((e: any) => toast(e?.message || "Seek failed", "error"));
  };

  const next = async () => {
    if (!current || noNext) return;
    try {
      if (state.is_connected) await sendCommand("skip");
      // force=true marks a user-initiated skip; plain /play no-ops while playing
      else await apiFetch(`/api/guild/${guildId}/play?force=true`, { method: "POST" });
    } catch (e: any) {
      toast(e?.message || "Could not skip", "error");
      return;
    }
    audio.stop();
  };

  const previous = async () => {
    if (!current || state.history.length === 0) return;
    try {
      await sendCommand("previous");
    } catch (e: any) {
      toast(e?.message || "Could not go back", "error");
      return;
    }
    audio.stop();
  };

  const toggleMute = () => {
    const sharedVolume = state.is_connected;
    const currentLevel = sharedVolume ? state.volume : volume;
    let level: number;
    if (sharedVolume) {
      if (currentLevel > 0) sharedVolumeBeforeMute = currentLevel;
      level = currentLevel === 0 ? sharedVolumeBeforeMute || 100 : 0;
    } else {
      rememberAudibleVolume(currentLevel);
      level = currentLevel === 0 ? lastAudibleVolume() : 0;
    }
    audio.setVolume(level);
    if (sharedVolume) sendCommand("volume", { level }).catch(() => {});
    else onVolumeChange(level);
    return level;
  };

  return { playPause, seekBy, next, previous, toggleMute };
}
