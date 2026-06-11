"use client";

import { useRef, useEffect, useState, useCallback, useMemo } from "react";
import { apiFetch } from "@/lib/api";
import type { ServerPosition } from "@/hooks/useWebSocket";

interface AudioPlayerState {
  playing: boolean;
  paused: boolean;
  ready: boolean;
  duration: number;
  blocked: boolean; // play() rejected by autoplay policy — UI shows tap-to-play, resume() clears
}

export type AudioPlayerHandle = AudioPlayerState & {
  playPause: () => void;
  seek: (seconds: number) => void;
  stop: () => void;
  setVolume: (v: number) => void;
  resume: () => void;
  // Live position; not React state — read via rAF for display
  positionRef: React.RefObject<number>;
};

// Effect speed multipliers — must match config.py AUDIO_EFFECTS
const EFFECT_SPEED_MULT: Record<string, number> = {
  none: 1.0, bass_boost: 1.0, nightcore: 1.25, vaporwave: 0.8, treble_boost: 1.0, "8d": 1.0,
};
const PITCH_EFFECTS = new Set(["nightcore", "vaporwave"]);
// Effects needing a Web Audio chain (browser equivalents of the FFmpeg filters
// in config.py: bass=g=10:f=110, treble=g=5:f=3000, apulsator=hz=0.09)
const WEBAUDIO_EFFECTS = new Set(["bass_boost", "treble_boost", "8d"]);

interface FxChain {
  ctx: AudioContext;
  bass: BiquadFilterNode;
  treble: BiquadFilterNode;
  lfoGain: GainNode;
}

export function useAudioPlayer(
  guildId: string,
  currentWebpageUrl: string | null,
  currentDuration: number,
  serverPos: ServerPosition,
  eventVersion: number,
  speed: number,
  audioEffect: string,
  volume: number,
  isConnected: boolean,
): AudioPlayerHandle {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [state, setState] = useState<AudioPlayerState>({
    playing: false, paused: false, ready: false, duration: 0, blocked: false,
  });
  const positionRef = useRef(0);
  const readyRef = useRef(false);
  readyRef.current = state.ready;
  const hasSyncedRef = useRef(false);
  const stoppingRef = useRef(false);
  const recoveringRef = useRef(false);
  // When the local user seeks, suppress remote-position correction briefly so
  // the server's older position doesn't immediately snap us backward.
  const lastLocalSeekAtRef = useRef(0);
  // True while the element is buffering behind the server clock; suppresses
  // drift-correction re-seeks so slow networks don't trigger forward stutter.
  const bufferingRef = useRef(false);

  // NotAllowedError = needs a user gesture; AbortError (src swap mid-load) is normal
  const tryPlay = useCallback((audio: HTMLAudioElement) => {
    audio.play().then(
      () => setState(s => (s.blocked ? { ...s, blocked: false } : s)),
      (err: unknown) => {
        if ((err as DOMException)?.name === "NotAllowedError") {
          setState(s => ({ ...s, blocked: true }));
        }
      },
    );
  }, []);

  const buildStreamUrl = useCallback(() => {
    const token =
      localStorage.getItem("activity_session_token") ||
      localStorage.getItem("activity_token") ||
      "";
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
    if (!audio) return;

    // When connected to a voice channel the bot is the source of truth; the
    // browser element must stay silent (no streaming, no src).
    if (isConnected) {
      audio.pause();
      audio.src = "";
      audio.dataset.songUrl = "";
      return;
    }

    // Remote stop: current became null but this client's element is still
    // playing — tear it down (the stopping client stops its own element).
    if (!currentWebpageUrl) {
      if (audio.dataset.songUrl) {
        stoppingRef.current = true;
        audio.pause();
        audio.src = "";
        audio.load();
        audio.dataset.songUrl = "";
        hasSyncedRef.current = false;
        positionRef.current = 0;
        setState({ playing: false, paused: false, ready: false, duration: 0, blocked: false });
        setTimeout(() => { stoppingRef.current = false; }, 0);
      }
      return;
    }

    if (audio.dataset.songUrl !== currentWebpageUrl) {
      audio.dataset.songUrl = currentWebpageUrl;
      hasSyncedRef.current = false;
      stoppingRef.current = false;

      audio.src = buildStreamUrl();

      const onCanPlay = () => {
        audio.removeEventListener("canplay", onCanPlay);
        tryPlay(audio);
      };
      audio.addEventListener("canplay", onCanPlay);

      audio.load();
      setState(s => ({ ...s, ready: false }));
    }
  }, [currentWebpageUrl, eventVersion, buildStreamUrl, isConnected, tryPlay]);

  // Sync to server position on first load (e.g. joining mid-song)
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || !state.ready || hasSyncedRef.current) return;

    const pos = serverPos.ref.current.position;
    if (pos > 2) {
      audio.currentTime = pos;
    }
    hasSyncedRef.current = true;
  }, [state.ready, serverPos]);

  // Server tick (subscription, no re-renders): follow remote pause/resume and
  // snap only on gaps large enough to imply a remote seek, not playback drift.
  useEffect(() => {
    if (isConnected) return;
    return serverPos.subscribe(({ position, is_paused }) => {
      const audio = audioRef.current;
      if (!audio || !readyRef.current || stoppingRef.current) return;

      // Pause/resume follow — must skip when the element has ended (onEnded
      // hasn't flipped ready yet) or we'd restart the finished song.
      if (!audio.ended) {
        if (is_paused && !audio.paused) {
          audio.pause();
        } else if (!is_paused && audio.paused) {
          tryPlay(audio);
        }
      }

      // Drift correction
      if (!currentWebpageUrl || recoveringRef.current || bufferingRef.current) return;
      if (Date.now() - lastLocalSeekAtRef.current < 3000) return;
      if (Math.abs(audio.currentTime - position) > 2.5) {
        audio.currentTime = position;
      }
    });
  }, [serverPos, isConnected, currentWebpageUrl, tryPlay]);

  // Returning from background: timers/socket were suspended, re-sync now
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== "visible" || isConnected) return;
      const audio = audioRef.current;
      if (!audio || !readyRef.current || stoppingRef.current || recoveringRef.current) return;
      const { position, is_paused } = serverPos.ref.current;
      if (!audio.ended && !is_paused && audio.paused) tryPlay(audio);
      if (Math.abs(audio.currentTime - position) > 2.5) {
        audio.currentTime = position;
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [serverPos, isConnected, tryPlay]);

  // Created lazily on first use of a Web Audio effect — once a
  // MediaElementSource exists the element's output is permanently routed
  // through the graph, so untouched sessions keep the plain audio path.
  const fxRef = useRef<FxChain | null>(null);
  const ensureFxChain = useCallback((): FxChain | null => {
    const audio = audioRef.current;
    if (!audio) return null;
    if (fxRef.current) return fxRef.current;
    try {
      const ctx = new AudioContext();
      const source = ctx.createMediaElementSource(audio);
      const bass = ctx.createBiquadFilter();
      bass.type = "lowshelf";
      bass.frequency.value = 110;
      bass.gain.value = 0;
      const treble = ctx.createBiquadFilter();
      treble.type = "highshelf";
      treble.frequency.value = 3000;
      treble.gain.value = 0;
      const panner = ctx.createStereoPanner();
      const lfo = ctx.createOscillator();
      lfo.frequency.value = 0.09;
      const lfoGain = ctx.createGain();
      lfoGain.gain.value = 0; // pan sweep depth; 0 = centered
      lfo.connect(lfoGain).connect(panner.pan);
      lfo.start();
      source.connect(bass).connect(treble).connect(panner).connect(ctx.destination);
      // A suspended context silences the routed element — resume on playback
      audio.addEventListener("play", () => { ctx.resume().catch(() => {}); });
      fxRef.current = { ctx, bass, treble, lfoGain };
    } catch {
      // Web Audio unavailable — effects stay voice-only
    }
    return fxRef.current;
  }, []);

  useEffect(() => {
    return () => { fxRef.current?.ctx.close().catch(() => {}); };
  }, []);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const mult = EFFECT_SPEED_MULT[audioEffect] ?? 1.0;
    audio.playbackRate = speed * mult;
    audio.preservesPitch = !PITCH_EFFECTS.has(audioEffect);

    const fx = WEBAUDIO_EFFECTS.has(audioEffect) ? ensureFxChain() : fxRef.current;
    if (fx) {
      fx.ctx.resume().catch(() => {});
      const t = fx.ctx.currentTime;
      fx.bass.gain.setTargetAtTime(audioEffect === "bass_boost" ? 10 : 0, t, 0.05);
      fx.treble.gain.setTargetAtTime(audioEffect === "treble_boost" ? 5 : 0, t, 0.05);
      fx.lfoGain.gain.setTargetAtTime(audioEffect === "8d" ? 0.85 : 0, t, 0.1);
    }
  }, [speed, audioEffect, ensureFxChain]);

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
    };
    const onPlaying = () => {
      if (stoppingRef.current) return;
      positionRef.current = audio.currentTime;
      setState({ playing: true, paused: false, ready: true, duration: audio.duration || currentDuration, blocked: false });
    };
    const onPause = () => {
      if (stoppingRef.current) return;
      positionRef.current = audio.currentTime;
      setState(s => ({ ...s, playing: false, paused: true }));
    };
    const onEnded = () => {
      if (!audio.dataset.songUrl) return; // stop() already handled this
      // Capture the ended url before clearing so the backend can de-dupe the
      // advance (ended_url) and we don't double-advance on slow event ordering.
      const endedUrl = audio.dataset.songUrl;
      audio.dataset.songUrl = ""; // allow reload for song loop mode
      positionRef.current = 0;
      setState({ playing: false, paused: false, ready: false, duration: 0, blocked: false });
      // When voice-connected the bot drives advancement; don't POST from here.
      if (isConnected) return;
      apiFetch(
        `/api/guild/${guildId}/play?ended_url=${encodeURIComponent(endedUrl)}`,
        { method: "POST" },
      ).catch(() => {});
    };
    const onWaiting = () => { bufferingRef.current = true; };
    const onStalled = () => { bufferingRef.current = true; };
    const onResumed = () => { bufferingRef.current = false; };
    const onDurationChange = () => setState(s => ({ ...s, duration: audio.duration || currentDuration }));
    const onError = () => {
      if (stoppingRef.current || recoveringRef.current) return;
      if (!audio.dataset.songUrl) return;

      // Recover from mid-stream upstream drops (e.g. socket timeout during long pause).
      recoveringRef.current = true;
      // positionRef, not currentTime: after a failed seek the element reverts
      // currentTime to the old position, while positionRef holds the seek target
      const resumeAt = positionRef.current || audio.currentTime || 0;
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
        if (wasPlaying) tryPlay(audio);
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
    audio.addEventListener("waiting", onWaiting);
    audio.addEventListener("stalled", onStalled);
    audio.addEventListener("playing", onResumed);
    audio.addEventListener("canplay", onResumed);
    audio.addEventListener("canplaythrough", onResumed);
    return () => {
      audio.removeEventListener("timeupdate", onTimeUpdate);
      audio.removeEventListener("playing", onPlaying);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("ended", onEnded);
      audio.removeEventListener("durationchange", onDurationChange);
      audio.removeEventListener("error", onError);
      audio.removeEventListener("waiting", onWaiting);
      audio.removeEventListener("stalled", onStalled);
      audio.removeEventListener("playing", onResumed);
      audio.removeEventListener("canplay", onResumed);
      audio.removeEventListener("canplaythrough", onResumed);
    };
  }, [guildId, currentDuration, isConnected, buildStreamUrl, tryPlay]);

  const playPause = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      tryPlay(audio);
      apiFetch(`/api/guild/${guildId}/resume`, { method: "POST" }).catch(() => {});
    } else {
      audio.pause();
      apiFetch(`/api/guild/${guildId}/pause`, { method: "POST" }).catch(() => {});
    }
  }, [guildId, tryPlay]);

  // Must be called from a user gesture; the next server tick re-syncs position
  const resume = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    tryPlay(audio);
  }, [tryPlay]);

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
    setState({ playing: false, paused: false, ready: false, duration: 0, blocked: false });
    setTimeout(() => { stoppingRef.current = false; }, 0);
  }, []);

  // Stable handle so children can memoize on it
  return useMemo(
    () => ({ ...state, playPause, seek, stop, setVolume, resume, positionRef }),
    [state, playPause, seek, stop, setVolume, resume],
  );
}
