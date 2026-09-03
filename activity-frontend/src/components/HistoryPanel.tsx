"use client";

import { useState, useEffect, useMemo } from "react";
import { useGuildState } from "./GuildStateProvider";
import { apiFetch } from "@/lib/api";
import { maybeAutoPlay } from "@/lib/queueActions";
import { formatDuration, cn } from "@/lib/utils";
import { useToast } from "./Toast";
import FavHeart from "./FavHeart";
import SongRow from "./SongRow";
import AddToPlaylistButton from "./AddToPlaylistButton";
import EmptyState from "./EmptyState";

export default function HistoryPanel() {
  const { state, guildId } = useGuildState();
  const { history } = state;
  const [addedSet, setAddedSet] = useState<Set<string>>(new Set());
  const [pendingSet, setPendingSet] = useState<Set<string>>(new Set());
  const [confirmClear, setConfirmClear] = useState(false);
  const { toast } = useToast();

  useEffect(() => {
    if (!confirmClear) return;
    const t = setTimeout(() => setConfirmClear(false), 3000);
    return () => clearTimeout(t);
  }, [confirmClear]);

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
      maybeAutoPlay(guildId, res, !!state.current);

      setAddedSet(prev => new Set(prev).add(webpageUrl));
      toast(`Added "${title}"`, "success");
      setTimeout(() => setAddedSet(prev => { const n = new Set(prev); n.delete(webpageUrl); return n; }), 2500);
    } catch {
      setPendingSet(prev => { const n = new Set(prev); n.delete(webpageUrl); return n; });
    }
  };

  const reversed = useMemo(() => [...history].reverse(), [history]);

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
              if (!confirmClear) { setConfirmClear(true); return; }
              setConfirmClear(false);
              try {
                await apiFetch(`/api/guild/${guildId}/history/clear`, { method: "POST" });
                toast("History cleared", "success");
              } catch (e: any) {
                toast(e?.message || "Could not clear history", "error");
              }
            }}
            className={cn(
              "text-[11px] font-medium px-2 py-1 -my-1 rounded-md transition-colors",
              confirmClear ? "text-white bg-danger" : "text-danger/60 hover:text-danger"
            )}
          >
            {confirmClear ? "Tap to confirm" : "Clear"}
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
                    <AddToPlaylistButton song={song} />
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
