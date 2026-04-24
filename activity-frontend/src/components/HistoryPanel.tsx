"use client";

import { useState } from "react";
import { useGuildState } from "./GuildStateProvider";
import { apiFetch } from "@/lib/api";
import { formatDuration } from "@/lib/utils";
import { useToast } from "./Toast";
import FavHeart from "./FavHeart";
import SongRow from "./SongRow";
import EmptyState from "./EmptyState";

export default function HistoryPanel() {
  const { state, guildId } = useGuildState();
  const { history } = state;
  const [addedSet, setAddedSet] = useState<Set<string>>(new Set());
  const [pendingSet, setPendingSet] = useState<Set<string>>(new Set());
  const { toast } = useToast();

  const handleRequeue = async (webpageUrl: string, title: string) => {
    if (addedSet.has(webpageUrl) || pendingSet.has(webpageUrl)) return;

    setPendingSet(prev => new Set(prev).add(webpageUrl));
    try {
      const res = await apiFetch<{ ok: boolean; added: number; duplicate?: boolean; playing?: boolean; position?: number; auto_play?: boolean }>(
        `/api/guild/${guildId}/queue/add`,
        { method: "POST", body: JSON.stringify({ query: webpageUrl }) },
      );
      setPendingSet(prev => { const n = new Set(prev); n.delete(webpageUrl); return n; });

      if (res.duplicate) {
        const msg = res.playing
          ? `"${title}" is currently playing`
          : res.position
          ? `"${title}" is already in queue (#${res.position})`
          : `"${title}" is already in queue`;
        toast(msg, "error");
        return;
      }

      // If nothing was playing, start playback of the just-added song
      if (res.auto_play && !state.current) {
        apiFetch(`/api/guild/${guildId}/play`, { method: "POST" }).catch(() => {});
      }

      setAddedSet(prev => new Set(prev).add(webpageUrl));
      toast(`Added "${title}"`, "success");
      setTimeout(() => setAddedSet(prev => { const n = new Set(prev); n.delete(webpageUrl); return n; }), 2500);
    } catch {
      setPendingSet(prev => { const n = new Set(prev); n.delete(webpageUrl); return n; });
    }
  };

  const reversed = [...history].reverse();

  if (reversed.length === 0) {
    return (
      <EmptyState
        icon={
          <svg className="w-7 h-7 text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
        }
        title="No history yet"
        subtitle="Songs you play will appear here"
      />
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-3 border-b border-white/[0.08] flex-shrink-0 flex items-center justify-between">
        <span className="text-xs text-white/50 font-medium">
          {history.length} song{history.length !== 1 ? "s" : ""} played
        </span>
        {history.length > 0 && (
          <button
            onClick={async () => {
              await apiFetch(`/api/guild/${guildId}/history/clear`, { method: "POST" });
              toast("History cleared", "success");
            }}
            className="text-[10px] text-danger/60 hover:text-danger transition-colors"
          >
            Clear
          </button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-3 pb-3">
        <div className="flex flex-col gap-1.5">
          {reversed.map((song, i) => {
            const isAdded = addedSet.has(song.webpage_url);
            const isPending = pendingSet.has(song.webpage_url);

            return (
              <SongRow
                key={`${song.webpage_url}-${i}`}
                title={song.title}
                subtitle={song.uploader}
                thumbnail={song.thumbnail}
                state={isAdded ? "added" : isPending ? "pending" : "default"}
                onClick={() => handleRequeue(song.webpage_url, song.title)}
                disabled={isAdded || isPending}
                trailing={
                  <>
                    <FavHeart webpageUrl={song.webpage_url} title={song.title} duration={song.duration} thumbnail={song.thumbnail} uploader={song.uploader} />
                    <span className="text-[11px] tabular-nums text-muted">
                      {song.duration > 0 ? formatDuration(song.duration) : ""}
                    </span>
                    {isPending && (
                      <div className="w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                    )}
                    {isAdded && (
                      <svg className="w-4 h-4 text-success" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
                      </svg>
                    )}
                  </>
                }
              />
            );
          })}
        </div>
      </div>
    </div>
  );
}
