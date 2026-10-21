// Mirrors AUDIO_EFFECTS in config.py: names and speed multipliers.
export const AUDIO_EFFECTS: readonly { id: string; label: string; speed: number }[] = [
  { id: "none", label: "Off", speed: 1.0 },
  { id: "bass_boost", label: "Bass Boost", speed: 1.0 },
  { id: "nightcore", label: "Nightcore", speed: 1.25 },
  { id: "vaporwave", label: "Vaporwave", speed: 0.8 },
  { id: "treble_boost", label: "Treble Boost", speed: 1.0 },
  { id: "8d", label: "8D Audio", speed: 1.0 },
];

export const SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0];

export function effectLabel(id: string): string {
  return AUDIO_EFFECTS.find(e => e.id === id)?.label ?? id;
}

export function effectSpeed(id: string): number {
  return AUDIO_EFFECTS.find(e => e.id === id)?.speed ?? 1.0;
}

/** Playback rate of the song clock: user speed times the effect's multiplier. */
export function playbackRate(speed: number, effect: string): number {
  const s = Number.isFinite(speed) && speed > 0 ? speed : 1;
  return s * effectSpeed(effect);
}

// Seeks stop this far short of the end so they can't fire `ended` (matches the server clamp).
export const SEEK_END_MARGIN = 5;

/** Clamp a seek target to [0, duration - margin]; durations of 0 (live, unknown) only clamp at 0. */
export function clampSeek(target: number, duration: number): number {
  const upper = duration > 0 ? Math.max(0, Math.floor(duration) - SEEK_END_MARGIN) : Infinity;
  return Math.max(0, Math.min(Math.round(target), upper));
}

/** Target for a relative seek, or null when it would not move in the asked direction. */
export function relativeSeekTarget(position: number, delta: number, duration: number): number | null {
  const from = Math.floor(position);
  const target = clampSeek(from + delta, duration);
  if (delta > 0 ? target <= from : target >= from) return null;
  return target;
}

export interface ServerTick {
  position: number;
  is_paused: boolean;
  // performance.now() when the tick arrived
  at: number;
}

/** The server clock's position now: the last tick advanced by the time since it arrived. */
export function extrapolatePosition(tick: ServerTick, now: number, rate: number, duration = 0): number {
  if (tick.is_paused || !(tick.at > 0)) return tick.position;
  const pos = tick.position + Math.max(0, now - tick.at) / 1000 * rate;
  return duration > 0 ? Math.min(pos, duration) : pos;
}

/** Discord presence timestamps (seconds) for a song at `position` playing at `rate`. */
export function presenceTimestamps(nowMs: number, position: number, duration: number, rate: number) {
  const r = rate > 0 ? rate : 1;
  const start = Math.floor(nowMs / 1000 - position / r);
  return { start, end: Math.floor(start + duration / r) };
}

// Server ticks advance about 1s of song time; more than this between two of them is a seek.
export const SEEK_TICK_JUMP = 3;
