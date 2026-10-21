import type { CurrentSong, GuildState, Song } from "@/types";

export const INITIAL_STATE: GuildState = {
  current: null,
  queue: [],
  played: [],
  history: [],
  volume: 100,
  loop_mode: "off",
  shuffle: false,
  autoplay: false,
  speed: 1.0,
  audio_effect: "none",
  is_connected: false,
  queue_duration: 0,
};

function sameFields(a: object, b: object): boolean {
  const ak = Object.keys(a);
  if (ak.length !== Object.keys(b).length) return false;
  for (const k of ak) {
    if ((a as Record<string, unknown>)[k] !== (b as Record<string, unknown>)[k]) return false;
  }
  return true;
}

/**
 * Reuse the previous song objects (and the previous array) wherever a broadcast repeats them,
 * so memoized rows skip re-rendering. Songs are matched by webpage_url, not position.
 */
export function shareSongs<T extends Song>(prev: T[], next: T[]): T[] {
  const byUrl = new Map<string, T>();
  for (const s of prev) byUrl.set(s.webpage_url, s);
  let changed = prev.length !== next.length;
  const out = next.map((song, i) => {
    const old = byUrl.get(song.webpage_url);
    const kept = old && sameFields(old, song) ? old : song;
    if (kept !== prev[i]) changed = true;
    return kept;
  });
  return changed ? out : prev;
}

/** The live position never lives in state (see ServerPosition), so it is stripped here. */
export function normalizeCurrent(prev: CurrentSong | null, next: (CurrentSong & { position?: number }) | null): CurrentSong | null {
  if (!next) return null;
  const { position: _position, ...rest } = next;
  return prev && sameFields(prev, rest) ? prev : rest;
}

/** Merge an incoming state message into the previous state, keeping unchanged parts identical. */
export function mergeGuildState(prev: GuildState, data: Partial<GuildState>): GuildState {
  const merged = { ...prev, ...data } as GuildState;
  return {
    ...merged,
    // A backend that version-gates lists may leave them out when they did not change.
    queue: data.queue ? shareSongs(prev.queue, data.queue) : prev.queue,
    played: data.played ? shareSongs(prev.played, data.played) : prev.played,
    history: data.history ? shareSongs(prev.history, data.history) : prev.history,
    current: "current" in data ? normalizeCurrent(prev.current, data.current ?? null) : prev.current,
  };
}
