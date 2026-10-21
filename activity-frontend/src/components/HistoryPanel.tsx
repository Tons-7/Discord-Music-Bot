"use client";

import { useGuildState } from "./GuildStateProvider";
import { apiFetch, errorMessage } from "@/lib/api";
import { useToast } from "./Toast";
import FavHeart from "./FavHeart";
import SongRow from "./SongRow";
import AddToPlaylistButton from "./AddToPlaylistButton";
import EmptyState from "./EmptyState";
import ConfirmButton from "./ui/ConfirmButton";
import { HistoryIcon } from "./ui/icons";
import { useQueueAdd } from "@/hooks/useQueueAdd";
import type { Song } from "@/types";

/** Newest first. Keys are URLs (history is de-duplicated); a repeat from an old snapshot gets a suffix. */
function keyedHistory(history: Song[]): { song: Song; key: string }[] {
  const seen = new Map<string, number>();
  const rows: { song: Song; key: string }[] = [];
  for (let i = history.length - 1; i >= 0; i--) {
    const song = history[i];
    const n = seen.get(song.webpage_url) ?? 0;
    seen.set(song.webpage_url, n + 1);
    rows.push({ song, key: n ? `${song.webpage_url}#${n}` : song.webpage_url });
  }
  return rows;
}

export default function HistoryPanel() {
  const { state, guildId } = useGuildState();
  const { history } = state;
  const { toast } = useToast();
  const queue = useQueueAdd();

  const clearHistory = () => {
    apiFetch(`/api/guild/${guildId}/history/clear`, { method: "POST" }).then(
      () => toast("History cleared", "success"),
      (e: unknown) => toast(errorMessage(e, "Couldn't clear the history"), "error"),
    );
  };

  const rows = keyedHistory(history);

  if (rows.length === 0) {
    return (
      <EmptyState
        icon={<HistoryIcon className="w-7 h-7" strokeWidth={1.5} />}
        title="No history yet"
        subtitle="Songs you play will appear here"
      />
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-3 border-b border-fg/[0.08] flex-shrink-0 flex items-center justify-between">
        <span className="text-xs text-muted font-medium">
          {history.length} song{history.length !== 1 ? "s" : ""} played
        </span>
        <ConfirmButton label="Clear" confirmLabel="Clear history?" onConfirm={clearHistory} />
      </div>

      <div className="flex-1 overflow-y-auto px-3 pb-3">
        <div className="flex flex-col gap-1.5 mt-2">
          {rows.map(({ song, key }) => {
            const status = queue.status(song.webpage_url);
            return (
              <SongRow
                key={key}
                title={song.title}
                subtitle={song.uploader}
                thumbnail={song.thumbnail}
                actionLabel={`Add "${song.title}" to the queue`}
                state={status}
                onClick={() => queue.add(song.webpage_url, song.title)}
                disabled={status !== "default"}
                duration={song.duration}
                trailing={
                  <>
                    <AddToPlaylistButton song={song} />
                    <FavHeart webpageUrl={song.webpage_url} title={song.title} duration={song.duration} thumbnail={song.thumbnail} uploader={song.uploader} />
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
