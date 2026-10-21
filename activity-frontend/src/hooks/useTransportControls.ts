"use client";

import { useGuildState } from "@/components/GuildStateProvider";
import { useToast } from "@/components/Toast";
import { apiFetch, errorMessage } from "@/lib/api";
import { clampSeek, relativeSeekTarget } from "@/lib/playback";
import type { AudioPlayerHandle } from "./useAudioPlayer";
import type { PlaybackView } from "./usePlaybackView";
import { lastAudibleVolume, rememberAudibleVolume } from "./useLocalVolume";

// Shared voice volume before muting, kept apart from the listener's own volume memory.
let sharedVolumeBeforeMute = 0;

// Repeated seeks (a held arrow key) move local audio at once but reach the server once,
// with the final target, after the keys go quiet.
const SEEK_SEND_DELAY_MS = 300;
let pendingSeek: { timer: ReturnType<typeof setTimeout>; target: number } | null = null;

export interface TransportControls {
  playPause: () => void;
  /** Seek everyone; `defer` batches rapid repeats into one server request. */
  seekTo: (seconds: number, defer?: boolean) => void;
  seekBy: (deltaSeconds: number) => void;
  next: () => void;
  previous: () => void;
  /** Returns the new volume level. */
  toggleMute: () => number;
}

/**
 * Transport actions shared by every control surface — skip is a forced /play in
 * Activity-only mode, seeks stop 5s short of the end, and volume is server-wide
 * only while the bot is in a voice channel.
 */
export function useTransportControls(
  audio: AudioPlayerHandle,
  view: PlaybackView,
  volume: number,
  onVolumeChange: (v: number) => void,
): TransportControls {
  const { state, guildId, sendCommand } = useGuildState();
  const { toast } = useToast();
  const { current, totalDur, noNext } = view;

  const playPause = () => {
    if (!current) return;
    audio.playPause().catch((e: unknown) => toast(errorMessage(e, "Couldn't change playback"), "error"));
  };

  const sendSeek = (target: number) => {
    sendCommand("seek", { position: String(target) })
      .catch((e: unknown) => toast(errorMessage(e, "Seek failed"), "error"));
  };

  const seekTo = (seconds: number, defer = false) => {
    if (!current || current.is_live) return;
    const target = clampSeek(seconds, totalDur);
    audio.seek(target);
    if (pendingSeek) clearTimeout(pendingSeek.timer);
    if (!defer) {
      pendingSeek = null;
      sendSeek(target);
      return;
    }
    pendingSeek = {
      target,
      timer: setTimeout(() => {
        pendingSeek = null;
        sendSeek(target);
      }, SEEK_SEND_DELAY_MS),
    };
  };

  const seekBy = (deltaSeconds: number) => {
    if (!current || current.is_live) return;
    // Repeats build on the pending target: in voice mode the server position lags behind.
    const from = pendingSeek ? pendingSeek.target : view.getPosition();
    const target = relativeSeekTarget(from, deltaSeconds, totalDur);
    if (target !== null) seekTo(target, true);
  };

  const next = async () => {
    if (!current || noNext) return;
    const skipped = current.webpage_url;
    const skippedSeq = current.seq;
    try {
      if (state.is_connected) await sendCommand("skip");
      // force=true marks a user-initiated skip; plain /play no-ops while playing
      else await apiFetch(`/api/guild/${guildId}/play?force=true`, { method: "POST" });
    } catch (e: unknown) {
      toast(errorMessage(e, "Couldn't skip"), "error");
      return;
    }
    // The new song's broadcast may already have arrived and started loading; keep it.
    audio.stop(skipped, skippedSeq);
  };

  // Back to the last played song of the queue (or of the history); it works after the queue ended too.
  const previous = async () => {
    if (state.played.length === 0 && (!current || state.history.length === 0)) return;
    const left = current?.webpage_url;
    const leftSeq = current?.seq;
    try {
      await sendCommand("previous");
    } catch (e: unknown) {
      toast(errorMessage(e, "Couldn't go back"), "error");
      return;
    }
    if (left) audio.stop(left, leftSeq);
  };

  const toggleMute = () => {
    const sharedVolume = state.is_connected;
    const currentLevel = sharedVolume ? state.volume : volume;
    let level: number;
    if (sharedVolume) {
      if (currentLevel > 0) sharedVolumeBeforeMute = currentLevel;
      level = currentLevel === 0 ? sharedVolumeBeforeMute || 100 : 0;
      sendCommand("volume", { level })
        .catch((e: unknown) => toast(errorMessage(e, "Couldn't change the volume"), "error"));
    } else {
      rememberAudibleVolume(currentLevel);
      level = currentLevel === 0 ? lastAudibleVolume() : 0;
      audio.setVolume(level);
      onVolumeChange(level);
    }
    return level;
  };

  return { playPause, seekTo, seekBy, next, previous, toggleMute };
}
