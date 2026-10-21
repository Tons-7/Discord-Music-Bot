export interface SyncedLine {
  time: number;
  // Empty for a timestamp with no words: an instrumental break or the song's end.
  text: string;
}

// Fractions are optional and may use "." or ":"; a line can carry several
// leading stamps ("[00:12.00][01:30.00] chorus") and repeats at each of them.
const LRC_TIMESTAMP = /\s*\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?]/g;

export function parseLRC(synced: string): SyncedLine[] {
  const lines: SyncedLine[] = [];
  for (const raw of synced.split("\n")) {
    LRC_TIMESTAMP.lastIndex = 0;
    const times: number[] = [];
    let consumed = 0;
    let match: RegExpExecArray | null;
    while ((match = LRC_TIMESTAMP.exec(raw)) !== null) {
      if (match.index !== consumed) break;
      consumed = match.index + match[0].length;
      const frac = match[3];
      times.push(
        parseInt(match[1]) * 60 +
          parseInt(match[2]) +
          (frac ? parseInt(frac) / Math.pow(10, frac.length) : 0)
      );
    }
    const text = raw.slice(consumed).trim();
    for (const time of times) lines.push({ time, text });
  }
  lines.sort((a, b) => a.time - b.time);
  // Leading or repeated breaks carry nothing to highlight.
  return lines.filter((line, i) => line.text || (i > 0 && lines[i - 1].text));
}

/** Index of the line being sung at `position`, or -1 before the first one. */
export function activeLineIndex(lines: SyncedLine[], position: number): number {
  for (let i = lines.length - 1; i >= 0; i--) {
    if (position >= lines[i].time) return i;
  }
  return -1;
}

const OFFSET_KEY = "lyricsOffsets";
export const OFFSET_MAX = 100;

function readOffsets(): Record<string, number> {
  try {
    const parsed = JSON.parse(localStorage.getItem(OFFSET_KEY) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function readOffset(webpageUrl: string): number {
  const value = readOffsets()[webpageUrl];
  return Number.isFinite(value) ? value : 0;
}

export function writeOffset(webpageUrl: string, offset: number) {
  try {
    const all = readOffsets();
    // Re-inserting moves the song to the end, so eviction drops the least recently adjusted.
    delete all[webpageUrl];
    if (offset !== 0) all[webpageUrl] = offset;
    // Bounded: one key per song grew forever, and exhausting the origin quota
    // would break every other localStorage user (volume, theme).
    const keys = Object.keys(all);
    for (const stale of keys.slice(0, Math.max(0, keys.length - OFFSET_MAX))) delete all[stale];
    localStorage.setItem(OFFSET_KEY, JSON.stringify(all));
  } catch {
    // Storage unavailable — the offset just won't persist.
  }
}
