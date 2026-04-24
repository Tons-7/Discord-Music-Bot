"use client";

import { useRef, useEffect, useState, useCallback } from "react";
import { apiFetch } from "@/lib/api";

interface AudioPlayerState {
  playing: boolean;
  paused: boolean;
  ready: boolean;
  position: number;
  duration: number;
}

export type AudioPlayerHandle = AudioPlayerState & {
  playPause: () => void;
  seek: (seconds: number) => void;
  stop: () => void;
  setVolume: (v: number) => void;
  positionRef: React.RefObject<number>;
};

// Effect speed multipliers — must match config.py AUDIO_EFFECTS
const EFFECT_SPEED_MULT: Record<string, number> = {
  none: 1.0, bass_boost: 1.0, nightcore: 1.25, vaporwave: 0.8, treble_boost: 1.0, "8d": 1.0,
};
const PITCH_EFFECTS = new Set(["nightcore", "vaporwave"]);

export function useAudioPlayer(
  guildId: string,
  currentWebpageUrl: string | null,
  currentDuration: number,
  serverPosition: number,
  serverIsPaused: boolean,
  eventVersion: number,
  speed: number,
  audioEffect: string,
  volume: number,
) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [state, setState] = useState<AudioPlayerState>({
    playing: false, paused: false, ready: false, position: 0, duration: 0,
  });
  const positionRef = useRef(0);
  const hasSyncedRef = useRef(false);
  const stoppingRef = useRef(false);
  const recoveringRef = useRef(false);
  const lastPositionSetRef = useRef(0);
  // When the local user seeks, suppress remote-position correction briefly so
  // the server's older position doesn't immediately snap us backward.
  const lastLocalSeekAtRef = useRef(0);

  const buildStreamUrl = useCallback(() => {
    const token = localStorage.getItem("activity_token") || "";
    return `/api/guild/${guildId}/stream?token=${encodeURIComponent(token)}&t=${Date.now()}`;
  }, [guildId]);

  useEffect(() => {
    if (!audioRef.current) {
      const audio = new Audio();
      audio.preload = "auto";
      audioRef.current = audio;
    }
    return () => {
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current.src = "";
      }
    };
  }, []);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || !currentWebpageUrl) return;

    if (audio.dataset.songUrl !== currentWebpageUrl) {
      audio.dataset.songUrl = currentWebpageUrl;
      hasSyncedRef.current = false;
      stoppingRef.current = false;

      audio.src = buildStreamUrl();

      const onCanPlay = () => {
        audio.removeEventListener("canplay", onCanPlay);
        audio.play().catch(() => {});
      };
      audio.addEventListener("canplay", onCanPlay);

      audio.load();
      setState(s => ({ ...s, ready: false }));
    }
  }, [currentWebpageUrl, eventVersion, buildStreamUrl]);

  // Sync to server position on first load (e.g. joining mid-song)
  const serverPosRef = useRef(serverPosition);
  serverPosRef.current = serverPosition;

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || !state.ready || hasSyncedRef.current) return;

    if (serverPosRef.current > 2) {
      audio.currentTime = serverPosRef.current;
    }
    hasSyncedRef.current = true;
  }, [state.ready]);

  // Follow remote pause/resume — when another user (or slash command) toggles
  // playback, the server broadcasts is_paused and every client's audio snaps
  // to match so everyone hears the same thing. Must skip when the element has
  // ended (onEnded hasn't flipped ready yet) or we'd restart the finished song.
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || !state.ready || stoppingRef.current || audio.ended) return;
    if (serverIsPaused && !audio.paused) {
      audio.pause();
    } else if (!serverIsPaused && audio.paused) {
      audio.play().catch(() => {});
    }
  }, [serverIsPaused, state.ready]);

  // Correct drift against the server clock. POSITION_UPDATE fires every second;
  // we only snap when the gap is large enough to imply a remote seek (not normal
  // playback drift). Suppressed briefly after a local seek, and always during
  // stream-error recovery (the recovery flow is about to reseek to resumeAt).
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || !state.ready || !currentWebpageUrl) return;
    if (stoppingRef.current || recoveringRef.current) return;
    if (Date.now() - lastLocalSeekAtRef.current < 3000) return;
    if (Math.abs(audio.currentTime - serverPosition) > 2.5) {
      audio.currentTime = serverPosition;
    }
  }, [serverPosition, state.ready, currentWebpageUrl]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const mult = EFFECT_SPEED_MULT[audioEffect] ?? 1.0;
    audio.playbackRate = speed * mult;
    audio.preservesPitch = !PITCH_EFFECTS.has(audioEffect);
  }, [speed, audioEffect]);

  useEffect(() => {
    const audio = audioRef.current;
    if (audio) audio.volume = Math.max(0, Math.min(1, volume / 100));
  }, [volume]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const onTimeUpdate = () => {
      if (stoppingRef.current) return;
      positionRef.current = audio.currentTime;
      const now = Date.now();
      if (now - lastPositionSetRef.current < 500) return;
      lastPositionSetRef.current = now;
      setState(s => ({ ...s, position: audio.currentTime }));
    };
    const onPlaying = () => {
      if (stoppingRef.current) return;
      positionRef.current = audio.currentTime;
      lastPositionSetRef.current = Date.now();
      setState({ playing: true, paused: false, ready: true, position: audio.currentTime, duration: audio.duration || currentDuration });
    };
    const onPause = () => {
      if (stoppingRef.current) return;
      // Sync position immediately so the frozen display reflects the true pause point,
      // not the last throttled setState (which can be up to 500ms stale).
      positionRef.current = audio.currentTime;
      lastPositionSetRef.current = Date.now();
      setState(s => ({ ...s, playing: false, paused: true, position: audio.currentTime }));
    };
    const onEnded = () => {
      if (!audio.dataset.songUrl) return; // stop() already handled this
      audio.dataset.songUrl = ""; // allow reload for song loop mode
      positionRef.current = 0;
      setState({ playing: false, paused: false, ready: false, position: 0, duration: 0 });
      apiFetch(`/api/guild/${guildId}/play`, { method: "POST" }).catch(() => {});
    };
    const onDurationChange = () => setState(s => ({ ...s, duration: audio.duration || currentDuration }));
    const onError = () => {
      if (stoppingRef.current || recoveringRef.current) return;
      if (!audio.dataset.songUrl) return;

      // Recover from mid-stream upstream drops (e.g. socket timeout during long pause).
      recoveringRef.current = true;
      const resumeAt = audio.currentTime || 0;
      const wasPlaying = !audio.paused;
      const cleanup = () => {
        audio.removeEventListener("loadedmetadata", onLoaded);
        audio.removeEventListener("error", onReloadError);
        recoveringRef.current = false;
      };
      const onLoaded = () => {
        cleanup();
        if (resumeAt > 0) {
          try { audio.currentTime = resumeAt; } catch { /* noop */ }
        }
        if (wasPlaying) audio.play().catch(() => {});
      };
      const onReloadError = () => { cleanup(); };
      audio.addEventListener("loadedmetadata", onLoaded);
      audio.addEventListener("error", onReloadError);
      audio.src = buildStreamUrl();
      audio.load();
    };

    audio.addEventListener("timeupdate", onTimeUpdate);
    audio.addEventListener("playing", onPlaying);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("ended", onEnded);
    audio.addEventListener("durationchange", onDurationChange);
    audio.addEventListener("error", onError);
    return () => {
      audio.removeEventListener("timeupdate", onTimeUpdate);
      audio.removeEventListener("playing", onPlaying);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("ended", onEnded);
      audio.removeEventListener("durationchange", onDurationChange);
      audio.removeEventListener("error", onError);
    };
  }, [guildId, currentDuration]);

  const playPause = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      audio.play().catch(() => {});
      apiFetch(`/api/guild/${guildId}/resume`, { method: "POST" }).catch(() => {});
    } else {
      audio.pause();
      apiFetch(`/api/guild/${guildId}/pause`, { method: "POST" }).catch(() => {});
    }
  }, [guildId]);

  const setVolume = useCallback((vol: number) => {
    const audio = audioRef.current;
    if (audio) audio.volume = Math.max(0, Math.min(1, vol / 100));
  }, []);

  const seek = useCallback((seconds: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = seconds;
    positionRef.current = seconds;
    lastLocalSeekAtRef.current = Date.now();
  }, []);

  const stop = useCallback(() => {
    stoppingRef.current = true;
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.src = "";
      audio.load(); // abort pending loads
      audio.dataset.songUrl = "";
    }
    hasSyncedRef.current = false;
    positionRef.current = 0;
    setState({ playing: false, paused: false, ready: false, position: 0, duration: 0 });
    setTimeout(() => { stoppingRef.current = false; }, 0);
  }, []);

  return { ...state, playPause, seek, stop, setVolume, positionRef };
}
