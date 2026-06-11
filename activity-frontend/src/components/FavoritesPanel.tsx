"use client";

import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { useGuildState } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { apiFetch } from "@/lib/api";
import { maybeAutoPlay } from "@/lib/queueActions";
import { formatDuration } from "@/lib/utils";
import SongRow from "./SongRow";
import EmptyState from "./EmptyState";
import IconButton from "./ui/IconButton";
import { SongRowSkeleton } from "./ui/Skeleton";
import { CloseIcon } from "./ui/icons";
import { FAVORITES_KEY, type FavSong } from "./FavHeart";

export default function FavoritesPanel() {
  const { guildId, state } = useGuildState();
  const { toast } = useToast();
  const { mutate } = useSWRConfig();
  const { data, isLoading } = useSWR<{ favorites: FavSong[] }>(FAVORITES_KEY);
  const favorites = data?.favorites ?? [];
  const [addingSet, setAddingSet] = useState<Set<string>>(new Set());

  const handleAdd = async (fav: FavSong) => {
    if (addingSet.has(fav.webpage_url)) return;
    setAddingSet(prev => new Set(prev).add(fav.webpage_url));
    try {
      const res = await apiFetch<{ ok: boolean; auto_play?: boolean }>(`/api/guild/${guildId}/queue/add`, {
        method: "POST", body: JSON.stringify({ query: fav.webpage_url }),
      });
      maybeAutoPlay(guildId, res, !!state.current);
      toast(`Added "${fav.title}"`, "success");
    } catch (e: any) { toast(e.message, "error"); }
    finally {
      setTimeout(() => setAddingSet(prev => { const n = new Set(prev); n.delete(fav.webpage_url); return n; }), 1500);
    }
  };

  const handleRemove = async (fav: FavSong, pos: number) => {
    mutate(
      FAVORITES_KEY,
      (d?: { favorites: FavSong[] }) =>
        d ? { favorites: d.favorites.filter(f => f.webpage_url !== fav.webpage_url) } : d,
      { revalidate: false },
    );
    try {
      await apiFetch(`/api/favorites/${pos + 1}`, { method: "DELETE" }); // bot uses 1-based
      toast("Removed from favorites", "success");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      mutate(FAVORITES_KEY);
    }
  };

  if (isLoading) {
    return <SongRowSkeleton className="px-3 mt-2" />;
  }

  if (favorites.length === 0) {
    return (
      <EmptyState
        compact
        icon={
          <svg className="w-6 h-6 text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z" />
          </svg>
        }
        title="No favorites yet"
        subtitle="Use /favorite while a song is playing"
      />
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-2.5 border-b border-white/[0.06] flex-shrink-0">
        <span className="text-[11px] text-muted">{favorites.length} favorite{favorites.length !== 1 ? "s" : ""}</span>
      </div>
      <div className="flex-1 overflow-y-auto px-3 pb-3">
        <div className="flex flex-col gap-1.5 mt-2">
          {favorites.map((fav, i) => {
            const isAdding = addingSet.has(fav.webpage_url);
            return (
              <SongRow
                key={fav.webpage_url}
                title={fav.title}
                subtitle={fav.uploader}
                thumbnail={fav.thumbnail}
                marquee
                group
                state={isAdding ? "added" : "default"}
                onClick={() => handleAdd(fav)}
                disabled={isAdding}
                trailing={
                  <>
                    <IconButton
                      label="Remove from favorites"
                      size="xs"
                      tone="danger"
                      className="opacity-0 group-hover:opacity-100 pointer-coarse:opacity-100"
                      onClick={(e) => { e.stopPropagation(); handleRemove(fav, i); }}
                    >
                      <CloseIcon className="w-3 h-3" />
                    </IconButton>
                    <span className="text-[11px] tabular-nums text-muted w-8 text-right">
                      {fav.duration > 0 ? formatDuration(fav.duration) : ""}
                    </span>
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
