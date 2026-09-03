import { useState, useEffect, useRef, useMemo } from "react";
import useSWR from "swr";
import { useGuildState } from "./GuildStateProvider";
import IconButton from "./ui/IconButton";
import { apiFetch } from "@/lib/api";
import { cn } from "@/lib/utils";

export interface LyricsData {
  lyrics: string;
  synced: string;
  title: string;
  artist: string;
  webpage_url: string;
}

// Lyrics are immutable per song — cache by song so reopening the panel (or a
// preload on song start) never refetches.
export const lyricsKey = (guildId: string, webpageUrl: string) => `lyrics:${guildId}:${webpageUrl}`;
// SWR hands the string key to the fetcher, so the song travels with the key and
// a song change mid-flight can never resolve under the previous song's key.
export const lyricsFetcher = (guildId: string) => (key: string) => {
  const url = key.slice(lyricsKey(guildId, "").length);
  const query = url ? `?url=${encodeURIComponent(url)}` : "";
  return apiFetch<LyricsData>(`/api/guild/${guildId}/lyrics${query}`);
};
const LYRICS_SWR_OPTS = {
  revalidateIfStale: false,
  revalidateOnFocus: false,
  revalidateOnReconnect: false,
  shouldRetryOnError: false,
} as const;

interface SyncedLine {
  time: number;
  text: string;
}

// Fractions are optional and may use "." or ":"; a line can carry several
// leading stamps ("[00:12.00][01:30.00] chorus") and repeats at each of them.
const LRC_TIMESTAMP = /\s*\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?]/g;

function parseLRC(synced: string): SyncedLine[] {
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
    if (!text) continue;
    for (const time of times) lines.push({ time, text });
  }
  lines.sort((a, b) => a.time - b.time);
  return lines;
}

const OFFSET_STEP = 0.5;
// Auto-scroll yields to a manual scroll, then takes over again after a lull
const AUTO_SCROLL_RESUME_MS = 4000;
// A scroll landing back on the active line resumes at once, but only once the
// gesture has settled — the first flick starts from the centred line.
const SETTLE_MS = 500;
const OFFSET_KEY = "lyricsOffsets";
const OFFSET_MAX = 100;

function readOffsets(): Record<string, number> {
  try {
    const parsed = JSON.parse(localStorage.getItem(OFFSET_KEY) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function readOffset(webpageUrl: string): number {
  const value = readOffsets()[webpageUrl];
  return Number.isFinite(value) ? value : 0;
}

function writeOffset(webpageUrl: string, offset: number) {
  try {
    const all = readOffsets();
    if (offset === 0) delete all[webpageUrl];
    else all[webpageUrl] = offset;
    // Bounded: one key per song grew forever, and exhausting the origin quota
    // would break every other localStorage user (volume, session token).
    const keys = Object.keys(all);
    for (const stale of keys.slice(0, Math.max(0, keys.length - OFFSET_MAX))) delete all[stale];
    localStorage.setItem(OFFSET_KEY, JSON.stringify(all));
  } catch {
    // Storage unavailable — the offset just won't persist.
  }
}

export default function LyricsPanel({ getPosition, onSeek }: {
  // Unified position: browser audio when it plays locally, server clock when voice-connected
  getPosition: () => number;
  onSeek?: (seconds: number) => void;
}) {
  const { guildId, state } = useGuildState();
  const [activeLine, setActiveLine] = useState(-1);
  // Seconds added to every LRC timestamp: YouTube uploads often carry an intro
  // the studio LRC does not know about.
  const [offset, setOffset] = useState(0);
  const activeLineRef = useRef<HTMLDivElement>(null);
  // Auto-scroll pauses while the user reads ahead. `pauseTick` restarts the
  // resume timer on every fresh interaction.
  const [autoScrollPaused, setAutoScrollPaused] = useState(false);
  const [pauseTick, setPauseTick] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastInteractionRef = useRef(0);

  const currentUrl = state.current?.webpage_url || "";

  const { data: lyrics, error, isLoading } = useSWR<LyricsData>(
    currentUrl ? lyricsKey(guildId, currentUrl) : null,
    lyricsFetcher(guildId),
    LYRICS_SWR_OPTS,
  );
  const loading = isLoading;

  const syncedLines = useMemo(
    () => (lyrics?.synced ? parseLRC(lyrics.synced) : []),
    [lyrics?.synced]
  );
  const hasSynced = syncedLines.length > 0;

  useEffect(() => { setActiveLine(-1); setAutoScrollPaused(false); }, [currentUrl]);

  useEffect(() => {
    setOffset(currentUrl ? readOffset(currentUrl) : 0);
  }, [currentUrl]);

  const adjustOffset = (delta: number) => {
    const next = delta === 0 ? 0 : Math.round((offset + delta) * 10) / 10;
    setOffset(next);
    if (currentUrl) writeOffset(currentUrl, next);
  };

  // Poll the position getter (no re-renders in parent) to update the active line
  useEffect(() => {
    if (!hasSynced) return;
    const interval = setInterval(() => {
      const pos = getPosition() - offset;
      let line = -1;
      for (let i = syncedLines.length - 1; i >= 0; i--) {
        if (pos >= syncedLines[i].time) { line = i; break; }
      }
      setActiveLine(prev => prev === line ? prev : line);
    }, 200);
    return () => clearInterval(interval);
  }, [hasSynced, syncedLines, getPosition, offset]);

  useEffect(() => {
    if (autoScrollPaused) return;
    if (activeLineRef.current) {
      activeLineRef.current.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [activeLine, autoScrollPaused]);

  useEffect(() => {
    if (!autoScrollPaused) return;
    const t = setTimeout(() => setAutoScrollPaused(false), AUTO_SCROLL_RESUME_MS);
    return () => clearTimeout(t);
  }, [autoScrollPaused, pauseTick]);

  // Wheel/touchmove fire continuously through a gesture — only re-render when
  // the pause actually needs extending.
  const pauseAutoScroll = () => {
    const now = Date.now();
    const continuing = autoScrollPaused && now - lastInteractionRef.current < 200;
    lastInteractionRef.current = now;
    if (continuing) return;
    setAutoScrollPaused(true);
    setPauseTick(t => t + 1);
  };

  // Scrolling back onto the active line is the other way out of the pause.
  const handleScroll = () => {
    if (!autoScrollPaused) return;
    if (Date.now() - lastInteractionRef.current < SETTLE_MS) return;
    const line = activeLineRef.current;
    const box = scrollRef.current;
    if (!line || !box) return;
    const lineBox = line.getBoundingClientRect();
    const viewBox = box.getBoundingClientRect();
    const distance = Math.abs(
      (lineBox.top + lineBox.height / 2) - (viewBox.top + viewBox.height / 2)
    );
    if (distance < viewBox.height * 0.25) setAutoScrollPaused(false);
  };

  if (!currentUrl) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center px-6">
        <p className="text-xs text-muted">Play a song to see lyrics</p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="w-5 h-5 border-2 border-accent border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (error || !lyrics) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center px-6 gap-2">
        <svg className="w-8 h-8 text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 9l10.5-3m0 6.553v3.75a2.25 2.25 0 01-1.632 2.163l-1.32.377a1.803 1.803 0 11-.99-3.467l2.31-.66a2.25 2.25 0 001.632-2.163zm0 0V2.25L9 5.25v10.303m0 0v3.75a2.25 2.25 0 01-1.632 2.163l-1.32.377a1.803 1.803 0 01-.99-3.467l2.31-.66A2.25 2.25 0 009 15.553z" />
        </svg>
        <p className="text-xs text-muted">Couldn't find lyrics</p>
      </div>
    );
  }

  if (hasSynced) {
    return (
      <div className="flex flex-col h-full">
        <div className="px-4 py-2 border-b border-white/[0.06] flex-shrink-0 flex items-center gap-2">
          <p className="text-[10px] text-muted truncate flex-1">{lyrics.title} — {lyrics.artist}</p>
          <div className="flex items-center gap-0.5 flex-shrink-0">
            <IconButton label="Shift lyrics earlier" size="xs" onClick={() => adjustOffset(-OFFSET_STEP)}>
              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                <path strokeLinecap="round" d="M5 12h14" />
              </svg>
            </IconButton>
            <button
              type="button"
              onClick={() => adjustOffset(0)}
              disabled={offset === 0}
              title="Reset lyrics sync"
              className={cn(
                "text-[10px] tabular-nums w-9 text-center transition-colors",
                offset === 0 ? "text-muted/60" : "text-accent hover:text-white"
              )}
            >
              {offset > 0 ? `+${offset}` : offset}s
            </button>
            <IconButton label="Shift lyrics later" size="xs" onClick={() => adjustOffset(OFFSET_STEP)}>
              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                <path strokeLinecap="round" d="M12 5v14M5 12h14" />
              </svg>
            </IconButton>
          </div>
        </div>
        <div className="relative flex-1 min-h-0">
          <div
            ref={scrollRef}
            onWheel={pauseAutoScroll}
            onTouchMove={pauseAutoScroll}
            onScroll={handleScroll}
            className="h-full overflow-y-auto px-5 py-4"
          >
            <div className="flex flex-col gap-1 select-text">
              {syncedLines.map((line, i) => (
                <div
                  key={i}
                  ref={i === activeLine ? activeLineRef : undefined}
                  role={onSeek ? "button" : undefined}
                  tabIndex={onSeek ? 0 : undefined}
                  onClick={onSeek ? () => onSeek(line.time + offset) : undefined}
                  onKeyDown={onSeek ? (e) => { if (e.key === "Enter") onSeek(line.time + offset); } : undefined}
                  className={cn(
                    "text-sm font-medium py-0.5 transition-[color,transform] duration-300",
                    onSeek && "cursor-pointer rounded-md -mx-1.5 px-1.5 hover:bg-white/[0.04]",
                    i === activeLine ? "text-white scale-[1.02] origin-left"
                      : i < activeLine ? "text-white/30" : "text-white/45"
                  )}
                >
                  {line.text}
                </div>
              ))}
            </div>
          </div>
          {autoScrollPaused && activeLine >= 0 && (
            <button
              type="button"
              onClick={() => setAutoScrollPaused(false)}
              className="absolute bottom-3 left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-full bg-accent/15 border border-accent/30 text-accent text-[11px] font-medium backdrop-blur-md shadow-[0_2px_12px_rgba(0,0,0,0.35)] hover:bg-accent/25 transition-colors"
            >
              Jump to current
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-2 border-b border-white/[0.06] flex-shrink-0">
        <p className="text-[10px] text-muted truncate">{lyrics.title} — {lyrics.artist}</p>
      </div>
      <div className="flex-1 overflow-y-auto px-5 py-4">
        <pre className="text-xs text-white/60 whitespace-pre-wrap font-sans leading-relaxed select-text">{lyrics.lyrics}</pre>
      </div>
    </div>
  );
}
