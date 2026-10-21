"use client";

import { useEffect, useEffectEvent, useRef, useState } from "react";
import { apiFetch, fetchSessionToken, getSessionToken } from "@/lib/api";
import { extrapolatePosition, playbackRate, SEEK_TICK_JUMP } from "@/lib/playback";
import type { PositionSlice, ServerPosition } from "@/hooks/useWebSocket";
import type { CurrentSong } from "@/types";

interface AudioPlayerState {
  playing: boolean;
  // The element has the current song loaded and in sync with the room
  ready: boolean;
  duration: number;
  // play() rejected by the autoplay policy — UI shows tap-to-play, resume() clears
  blocked: boolean;
  // The stream kept failing; UI offers Retry and Skip
  error: string | null;
}

export interface AudioPlayerHandle {
  playing: boolean;
  /** True while the local element is the source of truth (Activity-only, loaded, playable). */
  ready: boolean;
  duration: number;
  blocked: boolean;
  error: string | null;
  /** What play/pause controls should show. */
  isPaused: boolean;
  // Live position; not React state — read via rAF for display
  positionRef: React.RefObject<number>;
  /** Unified position: the element while it plays, else the server clock advanced to now. */
  getPosition: () => number;
  playPause: () => Promise<void>;
  seek: (seconds: number) => void;
  /** Stops local audio; with `expectedUrl` (and `expectedSeq`), only if that play is still the one loaded. */
  stop: (expectedUrl?: string, expectedSeq?: number) => void;
  setVolume: (v: number) => void;
  // Must be called from a user gesture
  resume: () => void;
  retry: () => void;
}

const PITCH_EFFECTS = new Set(["nightcore", "vaporwave"]);
// Effects needing a Web Audio chain (browser equivalents of the FFmpeg filters
// in config.py: bass=g=10:f=110, treble=g=5:f=3000, apulsator=hz=0.09)
const WEBAUDIO_EFFECTS = new Set(["bass_boost", "treble_boost", "8d"]);
// A local pause/resume is in flight this long; ticks sent before the server saw it must not undo it.
const TOGGLE_GRACE_MS = 1500;
const LOCAL_SEEK_GRACE_MS = 3000;
const RETRY_DELAYS_MS = [500, 2000, 6000];
const MAX_ERRORS_PER_SONG = 8;
// The server clock passed the end this long ago with no local playback: advance the room.
const STUCK_END_GRACE_S = 2;
const FX_SUSPENDED_GRACE_MS = 1000;

const IDLE: AudioPlayerState = { playing: false, ready: false, duration: 0, blocked: false, error: null };

interface FxChain {
  ctx: AudioContext;
  bass: BiquadFilterNode;
  treble: BiquadFilterNode;
  lfoGain: GainNode;
}

type PitchAudio = HTMLAudioElement & { webkitPreservesPitch?: boolean; mozPreservesPitch?: boolean };

function setPreservesPitch(audio: PitchAudio, value: boolean) {
  audio.preservesPitch = value;
  // Older WebKit (iOS before 17.2) only knows the prefixed property.
  if ("webkitPreservesPitch" in audio) audio.webkitPreservesPitch = value;
  if ("mozPreservesPitch" in audio) audio.mozPreservesPitch = value;
}

export function useAudioPlayer({
  guildId, current, serverPos, eventVersion, speed, audioEffect, volume, isConnected,
}: {
  guildId: string;
  current: CurrentSong | null;
  serverPos: ServerPosition;
  eventVersion: number;
  speed: number;
  audioEffect: string;
  volume: number;
  isConnected: boolean;
}): AudioPlayerHandle {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [state, setState] = useState<AudioPlayerState>(IDLE);
  const [fxSuspended, setFxSuspended] = useState(false);
  const positionRef = useRef(0);
  const prevServerPosRef = useRef<number | null>(null);
  // A remote seek seen while correction was held off (buffering, recovering), applied once it may be
  const remoteSeekRef = useRef(false);
  const stoppingRef = useRef(false);
  const recoveringRef = useRef(false);
  const loadRef = useRef<AbortController | null>(null);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const attemptsRef = useRef(0);
  const errorsRef = useRef(0);
  const loadedSeqRef = useRef<number | undefined>(undefined);
  const endedRef = useRef<{ url: string; seq?: number } | null>(null);
  const advanceSentRef = useRef<string | null>(null);
  // When the local user seeks, suppress remote-position correction briefly so
  // the server's older position doesn't immediately snap us backward.
  const lastLocalSeekAtRef = useRef(0);
  const lastToggleAtRef = useRef(0);
  // True while the element is buffering behind the server clock; suppresses
  // drift-correction re-seeks so slow networks don't trigger forward stutter.
  const bufferingRef = useRef(false);
  const fxRef = useRef<FxChain | null>(null);

  const currentUrl = current?.webpage_url ?? null;
  const currentSeq = current?.seq;
  const currentDuration = current?.duration ?? 0;
  const isLive = !!current?.is_live;
  const rate = playbackRate(speed, audioEffect);

  // The server clock for the current song, advanced to now.
  const serverNow = () => {
    const tick = serverPos.ref.current;
    if (tick.webpage_url && currentUrl && tick.webpage_url !== currentUrl) return 0;
    return extrapolatePosition(tick, performance.now(), rate, currentDuration);
  };

  const roomPaused = () => {
    const tick = serverPos.ref.current;
    if (tick.at > 0 && (!tick.webpage_url || tick.webpage_url === currentUrl)) return tick.is_paused;
    return current?.is_paused ?? true;
  };

  // NotAllowedError = needs a user gesture; AbortError (src swap mid-load) is normal
  const tryPlay = (audio: HTMLAudioElement) => {
    audio.play().then(
      () => setState(s => (s.blocked ? { ...s, blocked: false } : s)),
      (err: unknown) => {
        if ((err as DOMException)?.name === "NotAllowedError") {
          setState(s => ({ ...s, blocked: true, ready: false, playing: false }));
        }
      },
    );
  };

  const streamUrl = () =>
    `/api/guild/${guildId}/stream?token=${encodeURIComponent(getSessionToken(guildId) ?? "")}&t=${Date.now()}`;

  // One load of the stream into the element; its listeners die with `signal` (next load or stop).
  const startLoad = (audio: HTMLAudioElement, signal: AbortSignal, url: string, resumeAt: number | null) => {
    audio.src = streamUrl();
    audio.addEventListener("loadedmetadata", () => {
      if (audio.dataset.songUrl !== url) return;
      recoveringRef.current = false;
      audio.defaultPlaybackRate = rate;
      audio.playbackRate = rate;
      // Seek before playback starts, so a mid-song join doesn't play the intro first.
      const target = resumeAt ?? (isLive ? 0 : serverNow());
      if (target > 2) {
        try { audio.currentTime = target; } catch { /* not seekable yet */ }
      }
      positionRef.current = target > 2 ? target : 0;
      setState(s => ({ ...s, duration: audio.duration || currentDuration }));
    }, { once: true, signal });
    audio.addEventListener("canplay", () => {
      if (audio.dataset.songUrl !== url) return;
      if (roomPaused()) {
        // Loaded and paused with the room; a later resume tick starts it.
        setState(s => ({ ...s, ready: true, playing: false }));
      } else {
        tryPlay(audio);
      }
    }, { once: true, signal });
    audio.load();
  };

  const teardown = () => {
    stoppingRef.current = true;
    loadRef.current?.abort();
    loadRef.current = null;
    clearTimeout(retryTimerRef.current);
    recoveringRef.current = false;
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load(); // abort pending loads
      audio.dataset.songUrl = "";
    }
    prevServerPosRef.current = null;
    remoteSeekRef.current = false;
    positionRef.current = 0;
    setState(IDLE);
    fxRef.current?.ctx.suspend().catch(() => {});
    setFxSuspended(false);
    setTimeout(() => { stoppingRef.current = false; }, 0);
  };

  const loadSong = (url: string) => {
    const audio = audioRef.current;
    if (!audio) return;
    loadRef.current?.abort();
    const ctrl = new AbortController();
    loadRef.current = ctrl;
    clearTimeout(retryTimerRef.current);
    attemptsRef.current = 0;
    errorsRef.current = 0;
    recoveringRef.current = false;
    stoppingRef.current = false;
    endedRef.current = null;
    advanceSentRef.current = null;
    prevServerPosRef.current = null;
    remoteSeekRef.current = false;
    loadedSeqRef.current = current?.seq;
    audio.dataset.songUrl = url;
    setState(IDLE);
    startLoad(audio, ctrl.signal, url, null);
  };

  // An unrelated broadcast right after `ended` still names the finished play; only a new play reloads.
  const isFinishedPlay = useEffectEvent((url: string) => {
    const ended = endedRef.current;
    if (!ended || ended.url !== url) return false;
    if (ended.seq !== undefined && current?.seq !== undefined) return ended.seq === current.seq;
    return currentDuration > 0 && serverNow() > currentDuration - 3;
  });

  useEffect(() => {
    if (!audioRef.current) {
      const audio = new Audio();
      audio.preload = "auto";
      audioRef.current = audio;
    }
    return () => {
      loadRef.current?.abort();
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current.removeAttribute("src");
        audioRef.current.load();
      }
    };
  }, []);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    // When connected to a voice channel the bot is the source of truth; the
    // browser element must stay silent (no streaming, no src) and hold no stale state.
    // A null currentUrl is a remote stop: tear down, or this client keeps hearing audio.
    if (isConnected || !currentUrl) {
      if (audio.dataset.songUrl || audio.getAttribute("src")) teardown();
      return;
    }

    // A new play of the loaded song (queue loop with one song, a skip back to it) starts over too.
    const loaded = audio.dataset.songUrl === currentUrl;
    const newPlay = loaded && currentSeq !== undefined && loadedSeqRef.current !== undefined
      && currentSeq !== loadedSeqRef.current;
    if ((!loaded || newPlay) && !isFinishedPlay(currentUrl)) {
      loadSong(currentUrl);
    }
  }, [currentUrl, currentSeq, eventVersion, isConnected]);

  const postEnded = useEffectEvent((url: string) => {
    const seq = loadedSeqRef.current;
    const query = `ended_url=${encodeURIComponent(url)}${seq !== undefined ? `&ended_seq=${seq}` : ""}`;
    const send = () => apiFetch(`/api/guild/${guildId}/play?${query}`, { method: "POST" });
    // Idempotent on the server, so one retry is safe.
    send().catch(() => setTimeout(() => { send().catch(() => {}); }, 2000));
  });

  // Server tick handler (Effect Event: reads the latest state without making the
  // subscription below re-subscribe): follow remote pause/resume and snap only on
  // gaps large enough to imply a remote seek, not playback drift.
  const onServerTick = useEffectEvent((p: PositionSlice) => {
    const audio = audioRef.current;
    const url = audio?.dataset.songUrl;
    if (!audio || !url || stoppingRef.current) return;
    if (p.webpage_url && p.webpage_url !== url) return; // a tick for another song

    // Every tick moves the reference, so a jump is only ever measured against the tick before it
    // (a stale one after buffering would look like a seek and skip audio).
    const prev = prevServerPosRef.current;
    prevServerPosRef.current = p.position;
    if (prev !== null && Math.abs(p.position - prev) > SEEK_TICK_JUMP * Math.max(1, rate)) {
      remoteSeekRef.current = true;
    }

    if (state.error) {
      // Nobody can play this song, so nobody's `ended` will advance the room.
      if (!p.is_paused && !isLive && currentDuration > 0 && p.position >= currentDuration + STUCK_END_GRACE_S
          && advanceSentRef.current !== url) {
        advanceSentRef.current = url;
        postEnded(url);
      }
      return;
    }
    if (!state.ready) return;

    // Pause/resume follow — must skip when the element has ended (onEnded
    // hasn't flipped ready yet) or we'd restart the finished song.
    if (!audio.ended && performance.now() - lastToggleAtRef.current > TOGGLE_GRACE_MS) {
      if (p.is_paused && !audio.paused) {
        audio.pause();
      } else if (!p.is_paused && audio.paused) {
        tryPlay(audio);
      }
    }

    if (recoveringRef.current || bufferingRef.current) return;
    if (performance.now() - lastLocalSeekAtRef.current < LOCAL_SEEK_GRACE_MS) {
      remoteSeekRef.current = false; // this listener's own seek is newer
      return;
    }

    // The server clock knows nothing about buffering, so a steady gap is our own
    // drift and snapping to it would skip audio. Only a jump between consecutive
    // ticks is a real remote seek.
    if (!remoteSeekRef.current) return;
    remoteSeekRef.current = false;
    if (Math.abs(audio.currentTime - p.position) > 2.5) {
      audio.currentTime = p.position;
      positionRef.current = p.position;
    }
  });

  useEffect(() => {
    if (isConnected) return;
    return serverPos.subscribe((p) => onServerTick(p));
  }, [serverPos, isConnected]);

  // Returning from background: timers/socket were suspended, re-sync now
  const onVisible = useEffectEvent(() => {
    if (document.visibilityState !== "visible" || isConnected) return;
    const audio = audioRef.current;
    if (!audio || !state.ready || stoppingRef.current || recoveringRef.current) return;
    const paused = roomPaused();
    if (!audio.ended && !paused && audio.paused) tryPlay(audio);
    const position = serverNow();
    if (!paused && Math.abs(audio.currentTime - position) > 2.5) {
      audio.currentTime = position;
      positionRef.current = position;
    }
  });

  useEffect(() => {
    const handler = () => onVisible();
    document.addEventListener("visibilitychange", handler);
    return () => document.removeEventListener("visibilitychange", handler);
  }, []);

  // Created lazily on first use of a Web Audio effect — once a
  // MediaElementSource exists the element's output is permanently routed
  // through the graph, so untouched sessions keep the plain audio path.
  const ensureFxChain = useEffectEvent((): FxChain | null => {
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
      // A suspended context silences the routed element: resume on playback, and show
      // tap-to-play if it stays suspended (it may only start inside a user gesture).
      let check: ReturnType<typeof setTimeout> | undefined;
      ctx.onstatechange = () => {
        clearTimeout(check);
        if (ctx.state === "running") {
          setFxSuspended(false);
          return;
        }
        check = setTimeout(() => {
          setFxSuspended(ctx.state === "suspended" && !audio.paused && !!audio.dataset.songUrl);
        }, FX_SUSPENDED_GRACE_MS);
      };
      audio.addEventListener("play", () => { ctx.resume().catch(() => {}); });
      fxRef.current = { ctx, bass, treble, lfoGain };
    } catch {
      // Web Audio unavailable — effects stay voice-only
    }
    return fxRef.current;
  });

  useEffect(() => {
    return () => { fxRef.current?.ctx.close().catch(() => {}); };
  }, []);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    // defaultPlaybackRate survives the load() of the next song; playbackRate alone resets to it.
    audio.defaultPlaybackRate = rate;
    audio.playbackRate = rate;
    setPreservesPitch(audio, !PITCH_EFFECTS.has(audioEffect));

    // The element is silent in voice mode, so the effects graph has nothing to do.
    if (isConnected) {
      fxRef.current?.ctx.suspend().catch(() => {});
      return;
    }
    const fx = WEBAUDIO_EFFECTS.has(audioEffect) ? ensureFxChain() : fxRef.current;
    if (fx) {
      if (!audio.paused) fx.ctx.resume().catch(() => {});
      const t = fx.ctx.currentTime;
      fx.bass.gain.setTargetAtTime(audioEffect === "bass_boost" ? 10 : 0, t, 0.05);
      fx.treble.gain.setTargetAtTime(audioEffect === "treble_boost" ? 5 : 0, t, 0.05);
      fx.lfoGain.gain.setTargetAtTime(audioEffect === "8d" ? 0.85 : 0, t, 0.1);
    }
  }, [rate, audioEffect, isConnected]);

  // Re-applied when voice mode ends: voice-mode volume changes never touch the element.
  useEffect(() => {
    const audio = audioRef.current;
    if (audio) audio.volume = Math.max(0, Math.min(1, volume / 100));
  }, [volume, isConnected]);

  const onElementError = useEffectEvent((audio: HTMLAudioElement) => {
    const url = audio.dataset.songUrl;
    if (stoppingRef.current || isConnected || !url || !loadRef.current) return;
    errorsRef.current += 1;
    const attempt = attemptsRef.current++;
    if (attempt >= RETRY_DELAYS_MS.length || errorsRef.current > MAX_ERRORS_PER_SONG) {
      recoveringRef.current = false;
      setState(s => ({ ...s, ready: false, playing: false, error: "Couldn't load this song" }));
      return;
    }
    // Recover from mid-stream upstream drops (e.g. socket timeout during long pause).
    // positionRef, not currentTime: after a failed seek the element reverts
    // currentTime to the old position, while positionRef holds the seek target.
    recoveringRef.current = true;
    const resumeAt = positionRef.current || audio.currentTime || null;
    clearTimeout(retryTimerRef.current);
    retryTimerRef.current = setTimeout(async () => {
      if (audio.dataset.songUrl !== url) return;
      if (!getSessionToken(guildId)) await fetchSessionToken(guildId).catch(() => {});
      if (audio.dataset.songUrl !== url) return;
      loadRef.current?.abort();
      const ctrl = new AbortController();
      loadRef.current = ctrl;
      startLoad(audio, ctrl.signal, url, resumeAt);
    }, RETRY_DELAYS_MS[attempt]);
  });

  const onEnded = useEffectEvent((audio: HTMLAudioElement) => {
    const url = audio.dataset.songUrl;
    if (!url) return; // stop() already handled this
    // Remember the finished play so a broadcast that still names it doesn't reload it,
    // and clear songUrl so a loop-song repeat of the same URL does reload.
    endedRef.current = { url, seq: loadedSeqRef.current };
    audio.dataset.songUrl = "";
    positionRef.current = 0;
    setState(IDLE);
    // When voice-connected the bot drives advancement; don't POST from here.
    if (!isConnected) postEnded(url);
  });

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const onTimeUpdate = () => {
      if (stoppingRef.current || recoveringRef.current) return;
      positionRef.current = audio.currentTime;
    };
    const onPlaying = () => {
      if (stoppingRef.current || !audio.dataset.songUrl) return;
      positionRef.current = audio.currentTime;
      attemptsRef.current = 0;
      bufferingRef.current = false;
      setState(s => ({
        ...s, playing: true, ready: true, blocked: false, error: null,
        duration: audio.duration || s.duration || currentDuration,
      }));
    };
    const onPause = () => {
      if (stoppingRef.current) return;
      if (!audio.seeking) positionRef.current = audio.currentTime;
      setState(s => (s.playing ? { ...s, playing: false } : s));
    };
    const onWaiting = () => { bufferingRef.current = true; };
    const onResumed = () => { bufferingRef.current = false; };
    const onDurationChange = () => {
      if (!audio.dataset.songUrl) return;
      setState(s => ({ ...s, duration: audio.duration || currentDuration }));
    };
    const onError = () => onElementError(audio);
    const ended = () => onEnded(audio);

    audio.addEventListener("timeupdate", onTimeUpdate);
    audio.addEventListener("playing", onPlaying);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("ended", ended);
    audio.addEventListener("durationchange", onDurationChange);
    audio.addEventListener("error", onError);
    audio.addEventListener("waiting", onWaiting);
    audio.addEventListener("stalled", onWaiting);
    audio.addEventListener("canplay", onResumed);
    audio.addEventListener("canplaythrough", onResumed);
    return () => {
      audio.removeEventListener("timeupdate", onTimeUpdate);
      audio.removeEventListener("playing", onPlaying);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("ended", ended);
      audio.removeEventListener("durationchange", onDurationChange);
      audio.removeEventListener("error", onError);
      audio.removeEventListener("waiting", onWaiting);
      audio.removeEventListener("stalled", onWaiting);
      audio.removeEventListener("canplay", onResumed);
      audio.removeEventListener("canplaythrough", onResumed);
    };
  }, [currentDuration]);

  const ready = state.ready && !isConnected && !state.blocked && !state.error;
  const blocked = !isConnected && !!currentUrl && (state.blocked || fxSuspended);
  const isPaused = ready ? !state.playing : (current?.is_paused ?? true);

  const getPosition = () => {
    if (!ready) return serverNow();
    const audio = audioRef.current;
    // Read the element directly while it plays: timeupdate only fires ~4 times a second.
    if (audio && state.playing && !audio.seeking && !recoveringRef.current) return audio.currentTime;
    return positionRef.current;
  };

  // Gesture handlers resume the effects context synchronously: some webviews only allow it there.
  const unlockFx = () => {
    fxRef.current?.ctx.resume().catch(() => {});
  };

  const resume = () => {
    const audio = audioRef.current;
    if (!audio || isConnected || !audio.dataset.songUrl) return;
    unlockFx();
    if (roomPaused()) {
      setState(s => ({ ...s, blocked: false }));
      return;
    }
    const position = serverNow();
    if (!isLive && Math.abs(audio.currentTime - position) > 2.5) {
      try { audio.currentTime = position; } catch { /* not seekable yet */ }
      positionRef.current = position;
    }
    tryPlay(audio);
  };

  const retry = () => {
    const audio = audioRef.current;
    const url = audio?.dataset.songUrl;
    if (!audio || !url || isConnected) return;
    loadRef.current?.abort();
    const ctrl = new AbortController();
    loadRef.current = ctrl;
    attemptsRef.current = 0;
    errorsRef.current = 0;
    advanceSentRef.current = null;
    setState(s => ({ ...s, error: null }));
    startLoad(audio, ctrl.signal, url, null);
  };

  // Decided from the shared state, not the local element: a local element that is
  // loading, blocked or silent in voice mode must never resume the room from 0:00.
  const playPause = async () => {
    const audio = audioRef.current;
    if (!isConnected && blocked) {
      resume();
      return;
    }
    const serverPaused = roomPaused();
    const local = !isConnected && ready && audio ? audio : null;
    if (isPaused) {
      unlockFx();
      if (!serverPaused) {
        // The room is playing and only this element is behind: catch up locally.
        if (local) tryPlay(local);
        return;
      }
      if (local) {
        lastToggleAtRef.current = performance.now();
        tryPlay(local);
      }
      try {
        await apiFetch(`/api/guild/${guildId}/resume`, { method: "POST" });
      } catch (e) {
        local?.pause();
        throw e;
      }
      return;
    }
    if (serverPaused) {
      local?.pause();
      return;
    }
    if (local) {
      lastToggleAtRef.current = performance.now();
      local.pause();
    }
    try {
      await apiFetch(`/api/guild/${guildId}/pause`, { method: "POST" });
    } catch (e) {
      if (local) tryPlay(local);
      throw e;
    }
  };

  const setVolume = (vol: number) => {
    const audio = audioRef.current;
    if (audio && !isConnected) audio.volume = Math.max(0, Math.min(1, vol / 100));
  };

  const seek = (seconds: number) => {
    const audio = audioRef.current;
    if (!audio || isConnected || !audio.dataset.songUrl) return;
    try { audio.currentTime = seconds; } catch { /* not seekable yet */ }
    positionRef.current = seconds;
    lastLocalSeekAtRef.current = performance.now();
  };

  const stop = (expectedUrl?: string, expectedSeq?: number) => {
    const audio = audioRef.current;
    if (expectedUrl !== undefined && audio?.dataset.songUrl && audio.dataset.songUrl !== expectedUrl) return;
    // The same song may already be playing again (a new play), which this stop isn't about.
    if (expectedSeq !== undefined && loadedSeqRef.current !== undefined && loadedSeqRef.current !== expectedSeq) return;
    teardown();
  };

  return {
    playing: state.playing,
    ready,
    duration: state.duration,
    blocked,
    error: isConnected ? null : state.error,
    isPaused,
    positionRef,
    getPosition,
    playPause,
    seek,
    stop,
    setVolume,
    resume,
    retry,
  };
}
