"use client";

import { useState, useRef } from "react";
import { useGuildState } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { formatDuration, proxyImg, cn } from "@/lib/utils";
import { apiFetch } from "@/lib/api";
import MarqueeText from "./MarqueeText";
import FavHeart from "./FavHeart";
import EmptyState from "./EmptyState";

export default function QueuePanel() {
  const { state, guildId, sendCommand } = useGuildState();
  const { toast } = useToast();
  const { queue, queue_duration } = state;

  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [overIdx, setOverIdx] = useState<number | null>(null);
  const dragNode = useRef<HTMLDivElement | null>(null);

  const handleRemove = (pos: number) => apiFetch(`/api/guild/${guildId}/queue/${pos}`, { method: "DELETE" });
  const handleClear = () => apiFetch(`/api/guild/${guildId}/queue/clear`, { method: "POST" });

  const handleSkipTo = async (pos: number) => {
    try {
      const r = await apiFetch<{ title: string }>(`/api/guild/${guildId}/skipto`, {
        method: "POST", body: JSON.stringify({ position: pos }),
      });
      toast(`Skipped to "${r.title}"`, "success");
    } catch (e: any) { toast(e.message || "Failed", "error"); }
  };

  const handleDragStart = (e: React.DragEvent, idx: number) => {
    setDragIdx(idx);
    dragNode.current = e.currentTarget as HTMLDivElement;
    e.dataTransfer.effectAllowed = "move";
    setTimeout(() => {
      if (dragNode.current) dragNode.current.style.opacity = "0.3";
    }, 0);
  };

  const handleDragEnd = async () => {
    if (dragNode.current) dragNode.current.style.opacity = "1";
    if (dragIdx !== null && overIdx !== null && dragIdx !== overIdx) {
      try {
        await apiFetch(`/api/guild/${guildId}/queue/move`, {
          method: "POST",
          body: JSON.stringify({ from_pos: dragIdx, to_pos: overIdx }),
        });
      } catch (e: any) { toast(e.message || "Move failed", "error"); }
    }
    setDragIdx(null);
    setOverIdx(null);
    dragNode.current = null;
  };

  const handleDragOver = (e: React.DragEvent, idx: number) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (overIdx !== idx) setOverIdx(idx);
  };

  if (queue.length === 0) {
    return (
      <EmptyState
        icon={
          <svg className="w-7 h-7 text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" />
          </svg>
        }
        title="Queue is empty"
        subtitle="Search for songs to add"
      />
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/[0.08] flex-shrink-0">
        <span className="text-xs text-white/50 font-medium">
          {queue.length} song{queue.length !== 1 ? "s" : ""}
          {queue_duration > 0 && <span className="text-white/30"> · {formatDuration(queue_duration)}</span>}
        </span>
        <button onClick={handleClear} className="text-[11px] text-danger/50 hover:text-danger transition-colors font-medium">Clear</button>
      </div>

      <div className="flex-1 overflow-y-auto px-3 pb-3">
        <div className="flex flex-col gap-1 mt-1">
          {queue.map((song, i) => {
            const isDragging = dragIdx === i;
            const isOver = overIdx === i && dragIdx !== null && dragIdx !== i;

            return (
              <div
                key={song.webpage_url}
                draggable
                onDragStart={(e) => handleDragStart(e, i)}
                onDragEnd={handleDragEnd}
                onDragOver={(e) => handleDragOver(e, i)}
                onDragLeave={() => { if (overIdx === i) setOverIdx(null); }}
                className={cn(
                  "flex items-center gap-2.5 p-2 rounded-2xl bg-white/[0.02] border group cursor-grab active:cursor-grabbing",
                  "transition-[background-color,border-color,opacity] duration-150",
                  isDragging ? "opacity-30 border-accent/30" :
                  isOver ? "border-accent/50 bg-accent/[0.06]" :
                  "border-white/[0.04] hover:bg-white/[0.05] hover:border-white/[0.08]"
                )}
              >
                {/* Position / Skip-to button */}
                <button
                  onClick={(e) => { e.stopPropagation(); handleSkipTo(i); }}
                  className="w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0 cursor-pointer transition-colors duration-150 text-muted hover:text-accent hover:bg-accent/10"
                  title="Skip to this song"
                >
                  <span className="text-[11px] tabular-nums group-hover:hidden">{i + 1}</span>
                  <svg className="w-3.5 h-3.5 hidden group-hover:block" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>
                </button>

                {/* Thumbnail */}
                <div className="w-10 h-10 rounded-lg overflow-hidden bg-surface-3 flex-shrink-0">
                  {song.thumbnail ? (
                    <img src={proxyImg(song.thumbnail)} alt="" className="w-full h-full object-cover" loading="lazy" />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center">
                      <svg className="w-4 h-4 text-muted" fill="currentColor" viewBox="0 0 24 24"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z" /></svg>
                    </div>
                  )}
                </div>

                {/* Title + artist */}
                <div className="flex-1 min-w-0">
                  <MarqueeText className="text-sm font-medium text-white">{song.title}</MarqueeText>
                  <p className="text-xs text-white/30 truncate mt-0.5">
                    {song.uploader}
                    {song.requested_by && song.requested_by !== "Unknown" && (
                      <span className="text-white/20"> · Requested by {song.requested_by.replace(/<@!?\d+>/, "").trim() || song.requested_by}</span>
                    )}
                  </p>
                </div>

                {/* Right: actions + duration */}
                <div className="flex items-center gap-0.5 flex-shrink-0">
                  <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                    <FavHeart webpageUrl={song.webpage_url} title={song.title} url={song.url} duration={song.duration} thumbnail={song.thumbnail} uploader={song.uploader} />
                    <button onClick={() => handleRemove(i)} title="Remove"
                      className="w-5 h-5 rounded-md flex items-center justify-center text-muted hover:text-danger hover:bg-danger/10 transition-colors">
                      <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" /></svg>
                    </button>
                  </div>
                  <span className="text-[11px] tabular-nums text-muted w-8 text-right">
                    {song.duration > 0 ? formatDuration(song.duration) : ""}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
