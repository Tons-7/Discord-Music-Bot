"use client";

import { useCallback, useState } from "react";

const KEY = "activity_volume";
const AUDIBLE_KEY = "activity_volume_unmuted";
const DEFAULT_VOLUME = 100;

function read(key: string): number {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return DEFAULT_VOLUME;
    const n = Number(raw);
    return Number.isFinite(n) && n >= 0 && n <= 100 ? n : DEFAULT_VOLUME;
  } catch {
    return DEFAULT_VOLUME;
  }
}

function write(key: string, v: number) {
  try {
    localStorage.setItem(key, String(v));
  } catch {
    // private mode / storage disabled — keep it in memory for this session
  }
}

/** The level to restore on unmute: the last non-zero volume, kept across reloads. */
export function lastAudibleVolume(): number {
  return read(AUDIBLE_KEY) || DEFAULT_VOLUME;
}

export function rememberAudibleVolume(v: number) {
  if (v > 0) write(AUDIBLE_KEY, Math.round(v));
}

/**
 * Listener-local volume. Activity audio plays in each viewer's own <audio>
 * element, so volume is per user and never sent to the server — otherwise one
 * person's slider would change everyone else's. Voice-channel playback is the
 * exception (a single shared stream) and still goes through the volume command.
 */
export function useLocalVolume(): [number, (v: number) => void] {
  const [volume, setVolume] = useState(() => read(KEY));

  const persist = useCallback((v: number) => {
    const clamped = Math.max(0, Math.min(100, Math.round(v)));
    setVolume(clamped);
    write(KEY, clamped);
    rememberAudibleVolume(clamped);
  }, []);

  return [volume, persist];
}
