import { useState, useEffect, useRef } from "react";
import useSWR from "swr";
import { useGuildState } from "./GuildStateProvider";
import IconButton from "./ui/IconButton";
import EmptyState, { ErrorState } from "./EmptyState";
import { LyricsSkeleton } from "./ui/Skeleton";
import { LyricsIcon, MinusIcon, NoteIcon, PlusIcon } from "./ui/icons";
import { apiFetch, errorStatus } from "@/lib/api";
import { activeLineIndex, parseLRC, readOffset, writeOffset } from "@/lib/lyrics";
import { cn, joinDetails, prefersReducedMotion } from "@/lib/utils";

export interface LyricsData {
  lyrics: string;
  synced: string;
  title: string;
  artist: string;
  webpage_url: string;
  // The track has no words (optional until the backend sends it)
  instrumental?: boolean;
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

const OFFSET_STEP = 0.5;
const POLL_MS = 100;
// Auto-scroll yields to a manual scroll, then takes over again after a lull
const AUTO_SCROLL_RESUME_MS = 4000;
// A scroll landing back on the active line resumes at once, but only once the
// gesture has settled — the first flick starts from the centred line.
const SETTLE_MS = 500;

function formatOffset(offset: number) {
  return `${offset > 0 ? "+" : ""}${offset}s`;
}

export default function LyricsPanel({ getPosition, onSeek }: {
  // Unified position: browser audio when it plays locally, server clock otherwise
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

  const { data: lyrics, error, isLoading, mutate } = useSWR<LyricsData>(
    currentUrl ? lyricsKey(guildId, currentUrl) : null,
    lyricsFetcher(guildId),
    LYRICS_SWR_OPTS,
  );

  const syncedLines = lyrics?.synced ? parseLRC(lyrics.synced) : [];
  const hasSynced = syncedLines.some(line => line.text);

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
  const syncedKey = lyrics?.synced ?? "";
  useEffect(() => {
    if (!syncedKey) return;
    const lines = parseLRC(syncedKey);
    const interval = setInterval(() => {
      const line = activeLineIndex(lines, getPosition() - offset);
      setActiveLine(prev => prev === line ? prev : line);
    }, POLL_MS);
    return () => clearInterval(interval);
  }, [syncedKey, getPosition, offset]);

  useEffect(() => {
    if (autoScrollPaused) return;
    activeLineRef.current?.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "center" });
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
      <EmptyState
        compact
        icon={<LyricsIcon className="w-6 h-6" strokeWidth={1.5} />}
        title="Nothing is playing"
        subtitle="Play a song to see its lyrics"
      />
    );
  }

  if (isLoading) return <LyricsSkeleton />;

  if (error) {
    // 404 means LRCLIB has no lyrics; anything else may work on a second try.
    return errorStatus(error) === 404 ? (
      <EmptyState compact icon={<LyricsIcon className="w-6 h-6" strokeWidth={1.5} />} title="No lyrics for this song" />
    ) : (
      <ErrorState compact message="Couldn't load lyrics" onRetry={() => mutate()} />
    );
  }

  if (!lyrics) return null;

  const credit = joinDetails(lyrics.title, lyrics.artist);

  if (lyrics.instrumental && !hasSynced && !lyrics.lyrics) {
    return <EmptyState compact icon={<NoteIcon className="w-6 h-6" />} title="Instrumental" subtitle={credit} />;
  }

  if (hasSynced) {
    const seekTo = (time: number) => onSeek?.(Math.max(0, time + offset));
    return (
      <div className="flex flex-col h-full">
        <div className="px-4 py-2 border-b border-fg/[0.06] flex-shrink-0 flex items-center gap-2">
          <p className="text-xs text-muted truncate flex-1">{credit}</p>
          <div className="flex items-center gap-0.5 flex-shrink-0" role="group" aria-label="Lyrics sync">
            <IconButton label="Shift lyrics earlier" size="xs" onClick={() => adjustOffset(-OFFSET_STEP)}>
              <MinusIcon className="w-3 h-3" />
            </IconButton>
            <button
              type="button"
              onClick={() => adjustOffset(0)}
              disabled={offset === 0}
              title="Reset lyrics sync"
              aria-label={`Lyrics sync ${formatOffset(offset)}. Reset`}
              className={cn(
                "text-xs tabular-nums min-w-9 min-h-6 text-center rounded-md transition-colors",
                offset === 0 ? "text-muted" : "text-accent hover:text-fg"
              )}
            >
              {formatOffset(offset)}
            </button>
            <IconButton label="Shift lyrics later" size="xs" onClick={() => adjustOffset(OFFSET_STEP)}>
              <PlusIcon className="w-3 h-3" />
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
              {syncedLines.map((line, i) => {
                const isActive = i === activeLine;
                const tone = isActive ? "text-fg scale-[1.02] origin-left" : i < activeLine ? "text-muted/60" : "text-muted";
                // A break between sung lines: nothing to sing, so nothing stays highlighted.
                if (!line.text) {
                  return (
                    <div key={i} ref={isActive ? activeLineRef : undefined} aria-hidden="true"
                      className={cn("text-sm py-0.5 transition-[color,transform] duration-300", tone)}>
                      ♪
                    </div>
                  );
                }
                return (
                  <div
                    key={i}
                    ref={isActive ? activeLineRef : undefined}
                    role={onSeek ? "button" : undefined}
                    tabIndex={onSeek ? 0 : undefined}
                    aria-current={isActive || undefined}
                    onClick={onSeek ? () => seekTo(line.time) : undefined}
                    onKeyDown={onSeek ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        seekTo(line.time);
                      }
                    } : undefined}
                    className={cn(
                      "text-sm font-medium py-0.5 transition-[color,transform] duration-300",
                      onSeek && "cursor-pointer rounded-md -mx-1.5 px-1.5 hover:bg-fg/[0.04]",
                      tone,
                    )}
                  >
                    {line.text}
                  </div>
                );
              })}
            </div>
          </div>
          {autoScrollPaused && activeLine >= 0 && (
            <button
              type="button"
              onClick={() => setAutoScrollPaused(false)}
              className="absolute bottom-3 left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-full bg-accent/15 border border-accent/30 text-accent text-xs font-medium backdrop-blur-md shadow-[0_2px_12px_rgba(0,0,0,0.35)] hover:bg-accent/25 transition-colors"
            >
              Jump to current
            </button>
          )}
        </div>
      </div>
    );
  }

  if (!lyrics.lyrics) {
    return <EmptyState compact icon={<LyricsIcon className="w-6 h-6" strokeWidth={1.5} />} title="No lyrics for this song" />;
  }

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-2 border-b border-fg/[0.06] flex-shrink-0">
        <p className="text-xs text-muted truncate">{credit}</p>
      </div>
      <div className="flex-1 overflow-y-auto px-5 py-4">
        <pre className="text-sm text-fg/80 whitespace-pre-wrap font-sans leading-relaxed select-text">{lyrics.lyrics}</pre>
      </div>
    </div>
  );
}
