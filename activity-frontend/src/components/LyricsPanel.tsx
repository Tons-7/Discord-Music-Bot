"use client";

import { useState, useEffect, useRef, useMemo, type MutableRefObject } from "react";
import { useGuildState } from "./GuildStateProvider";
import { apiFetch } from "@/lib/api";
import { cn } from "@/lib/utils";

interface LyricsData {
  lyrics: string;
  synced: string;
  title: string;
  artist: string;
  webpage_url: string;
}

interface SyncedLine {
  time: number;
  text: string;
}

function parseLRC(synced: string): SyncedLine[] {
  const lines: SyncedLine[] = [];
  for (const line of synced.split("\n")) {
    const match = line.match(/^\[(\d+):(\d+)\.(\d+)\]\s*(.*)$/);
    if (match) {
      const time = parseInt(match[1]) * 60 + parseInt(match[2]) + parseInt(match[3]) / 100;
      const text = match[4].trim();
      if (text) lines.push({ time, text });
    }
  }
  return lines;
}

export default function LyricsPanel({ positionRef }: { positionRef: MutableRefObject<number> }) {
  const { guildId, state } = useGuildState();
  const [lyrics, setLyrics] = useState<LyricsData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [loadedFor, setLoadedFor] = useState("");
  const [activeLine, setActiveLine] = useState(-1);
  const activeLineRef = useRef<HTMLDivElement>(null);

  const currentUrl = state.current?.webpage_url || "";

  useEffect(() => {
    if (!currentUrl) {
      setLyrics(null); setLoadedFor(""); setError(""); setLoading(false);
      return;
    }
    if (loadedFor === currentUrl) return;

    let cancelled = false;
    setLoading(true);
    setError("");

    apiFetch<LyricsData>(`/api/guild/${guildId}/lyrics`)
      .then((data) => {
        if (cancelled) return;
        setLyrics(data);
        setLoadedFor(data.webpage_url);
      })
      .catch((e: any) => {
        if (cancelled) return;
        setLyrics(null);
        setError(e.message || "Lyrics not found");
        setLoadedFor(currentUrl);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => { cancelled = true; };
  }, [currentUrl, guildId, loadedFor]);

  const syncedLines = useMemo(
    () => (lyrics?.synced ? parseLRC(lyrics.synced) : []),
    [lyrics?.synced]
  );
  const hasSynced = syncedLines.length > 0;

  useEffect(() => { setActiveLine(-1); }, [currentUrl]);

  // Poll position from ref (no re-renders in parent) to update active line
  useEffect(() => {
    if (!hasSynced) return;
    const interval = setInterval(() => {
      const pos = positionRef.current;
      let line = -1;
      for (let i = syncedLines.length - 1; i >= 0; i--) {
        if (pos >= syncedLines[i].time) { line = i; break; }
      }
      setActiveLine(prev => prev === line ? prev : line);
    }, 200);
    return () => clearInterval(interval);
  }, [hasSynced, syncedLines, positionRef]);

  useEffect(() => {
    if (activeLineRef.current) {
      activeLineRef.current.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [activeLine]);

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
        <div className="px-4 py-2 border-b border-white/[0.06] flex-shrink-0">
          <p className="text-[10px] text-muted truncate">{lyrics.title} — {lyrics.artist}</p>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4">
          <div className="flex flex-col gap-1">
            {syncedLines.map((line, i) => (
              <div
                key={i}
                ref={i === activeLine ? activeLineRef : undefined}
                className={cn(
                  "text-sm font-medium py-0.5 transition-[color,transform] duration-300",
                  i === activeLine ? "text-white scale-[1.02] origin-left"
                    : i < activeLine ? "text-white/20" : "text-white/35"
                )}
              >
                {line.text}
              </div>
            ))}
          </div>
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
        <pre className="text-xs text-white/60 whitespace-pre-wrap font-sans leading-relaxed">{lyrics.lyrics}</pre>
      </div>
    </div>
  );
}
