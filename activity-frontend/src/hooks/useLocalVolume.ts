"use client";

import { useCallback, useState } from "react";

const KEY = "activity_volume";
const DEFAULT_VOLUME = 100;

function read(): number {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw === null) return DEFAULT_VOLUME;
    const n = Number(raw);
    return Number.isFinite(n) && n >= 0 && n <= 100 ? n : DEFAULT_VOLUME;
  } catch {
    return DEFAULT_VOLUME;
  }
}

/**
 * Listener-local volume. Activity audio plays in each viewer's own <audio>
 * element, so volume is per user and never sent to the server — otherwise one
 * person's slider would change everyone else's. Voice-channel playback is the
 * exception (a single shared stream) and still goes through the volume command.
 */
export function useLocalVolume(): [number, (v: number) => void] {
  const [volume, setVolume] = useState(read);

  const persist = useCallback((v: number) => {
    const clamped = Math.max(0, Math.min(100, Math.round(v)));
    setVolume(clamped);
    try {
      localStorage.setItem(KEY, String(clamped));
    } catch {
      // private mode / storage disabled — keep it in memory for this session
    }
  }, []);

  return [volume, persist];
}
