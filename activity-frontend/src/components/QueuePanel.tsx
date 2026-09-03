"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  DndContext,
  closestCenter,
  MouseSensor,
  TouchSensor,
  KeyboardSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragPendingEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  verticalListSortingStrategy,
  useSortable,
  sortableKeyboardCoordinates,
  arrayMove,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useGuildState } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { formatDuration, proxyImg, cn } from "@/lib/utils";
import { apiFetch } from "@/lib/api";
import MarqueeText from "./MarqueeText";
import FavHeart from "./FavHeart";
import AddToPlaylistButton, { AddQueueToPlaylistButton } from "./AddToPlaylistButton";
import EmptyState from "./EmptyState";
import IconButton from "./ui/IconButton";
import { CloseIcon, PlayIcon, NoteIcon, SearchIcon } from "./ui/icons";
import type { GuildState, Song } from "@/types";

export default function QueuePanel() {
  const { state, guildId } = useGuildState();
  const { toast } = useToast();
  const { queue, queue_duration } = state;

  // Optimistic overlay; cleared when the server queue arrives via WS
  const [localQueue, setLocalQueue] = useState<Song[] | null>(null);
  const [removedUrls, setRemovedUrls] = useState<Set<string>>(new Set());
  const [confirmClear, setConfirmClear] = useState(false);
  // Client-side only. While it's non-empty the rows no longer line up with the
  // server queue, so reordering is disabled for its duration.
  const [filter, setFilter] = useState("");
  // Row whose long-press drag is arming (touch delay constraint) — visual cue
  const [armingUrl, setArmingUrl] = useState<string | null>(null);
  // dnd-kit fires a click on the drag's mouseup — suppress button actions after a drag
  const dragOccurred = useRef(false);

  useEffect(() => {
    setLocalQueue(null);
    setRemovedUrls(new Set());
  }, [queue]);

  useEffect(() => {
    if (!confirmClear) return;
    const t = setTimeout(() => setConfirmClear(false), 3000);
    return () => clearTimeout(t);
  }, [confirmClear]);

  const displayed = useMemo(() => {
    const base = localQueue ?? queue;
    return removedUrls.size ? base.filter(s => !removedUrls.has(s.webpage_url)) : base;
  }, [localQueue, queue, removedUrls]);

  const query = filter.trim().toLowerCase();
  const filtering = query.length > 0;
  // Rows carry their index in the unfiltered queue, so numbering stays true
  // while filtered and drag indices never come from the filtered list.
  const rows = displayed
    .map((song, index) => ({ song, index }))
    .filter(({ song }) => !filtering
      || song.title.toLowerCase().includes(query)
      || (song.uploader || "").toLowerCase().includes(query));

  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const serverIndex = useCallback(
    (url: string) => queue.findIndex(s => s.webpage_url === url),
    [queue],
  );

  // /queue/add always appends, so restoring the original slot takes a follow-up
  // move. The position is read back from the server rather than assumed: other
  // clients may have changed the queue while the toast was up.
  const undoRemove = async (song: Song, pos: number) => {
    try {
      const res = await apiFetch<{ duplicate?: boolean }>(`/api/guild/${guildId}/queue/add`, {
        method: "POST", body: JSON.stringify({ query: song.webpage_url }),
      });
      if (res.duplicate) return;
      const server = await apiFetch<GuildState>(`/api/guild/${guildId}/state`);
      const from = server.queue.findIndex(s => s.webpage_url === song.webpage_url);
      const to = Math.min(pos, server.queue.length - 1);
      if (from >= 0 && to >= 0 && from !== to) {
        await apiFetch(`/api/guild/${guildId}/queue/move`, {
          method: "POST", body: JSON.stringify({ from_pos: from, to_pos: to }),
        });
      }
    } catch (e: any) {
      toast(e.message || "Undo failed", "error");
    }
  };

  const handleRemove = (song: Song) => {
    const pos = serverIndex(song.webpage_url);
    if (pos < 0) return;
    setRemovedUrls(prev => new Set(prev).add(song.webpage_url));
    apiFetch(`/api/guild/${guildId}/queue/${pos}`, { method: "DELETE" }).then(() => {
      toast(`Removed "${song.title}"`, "success", { label: "Undo", onClick: () => undoRemove(song, pos) });
    }).catch((e: any) => {
      setRemovedUrls(prev => { const n = new Set(prev); n.delete(song.webpage_url); return n; });
      toast(e.message || "Remove failed", "error");
    });
  };

  const handleClear = () => {
    if (!confirmClear) { setConfirmClear(true); return; }
    setConfirmClear(false);
    apiFetch(`/api/guild/${guildId}/queue/clear`, { method: "POST" }).catch((e: any) => {
      toast(e.message || "Clear failed", "error");
    });
  };

  const handleSkipTo = async (song: Song) => {
    if (dragOccurred.current) return;
    const pos = serverIndex(song.webpage_url);
    if (pos < 0) return;
    try {
      const r = await apiFetch<{ title: string }>(`/api/guild/${guildId}/skipto`, {
        method: "POST", body: JSON.stringify({ position: pos }),
      });
      toast(`Skipped to "${r.title}"`, "success");
    } catch (e: any) { toast(e.message || "Failed", "error"); }
  };

  // Only cue delay-based (long-press) arming — the MouseSensor's distance
  // constraint would flash the cue on every click.
  const handleDragPending = (event: DragPendingEvent) => {
    if ("delay" in event.constraint) setArmingUrl(String(event.id));
  };
  const handleDragAbort = () => setArmingUrl(null);

  const handleDragStart = () => { setArmingUrl(null); dragOccurred.current = true; };

  const handleDragEnd = async (event: DragEndEvent) => {
    setTimeout(() => { dragOccurred.current = false; }, 100);
    if (filtering) return; // indices would be computed against a filtered list
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const from = displayed.findIndex(s => s.webpage_url === active.id);
    const to = displayed.findIndex(s => s.webpage_url === over.id);
    if (from < 0 || to < 0) return;

    setLocalQueue(arrayMove(displayed, from, to));
    try {
      await apiFetch(`/api/guild/${guildId}/queue/move`, {
        method: "POST",
        body: JSON.stringify({ from_pos: from, to_pos: to }),
      });
    } catch (e: any) {
      setLocalQueue(null);
      toast(e.message || "Move failed", "error");
    }
  };

  if (displayed.length === 0) {
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
      <div className="flex items-center justify-between px-4 pt-3 pb-2 flex-shrink-0">
        <span className="text-xs text-white/50 font-medium">
          {filtering
            ? `${rows.length} of ${displayed.length}`
            : `${displayed.length} song${displayed.length !== 1 ? "s" : ""}`}
          {queue_duration > 0 && <span className="text-white/40"> · {formatDuration(queue_duration)}</span>}
        </span>
        <div className="flex items-center gap-1.5">
          <AddQueueToPlaylistButton disabled={displayed.length === 0 && !state.current} />
          <button
            onClick={handleClear}
            className={cn(
              "text-[11px] font-medium px-2 py-1 -my-1 rounded-md transition-colors",
              confirmClear ? "text-white bg-danger" : "text-danger/60 hover:text-danger"
            )}
          >
            {confirmClear ? "Tap to confirm" : "Clear"}
          </button>
        </div>
      </div>

      <div className="px-4 pb-2.5 border-b border-white/[0.08] flex-shrink-0">
        <div className="flex items-center gap-2 bg-surface-3/60 rounded-xl border border-white/[0.08] focus-within:border-accent/40 transition-[border-color] duration-200 px-3 py-1.5">
          <SearchIcon className="w-3.5 h-3.5 text-white/40 flex-shrink-0" />
          <input
            type="text" value={filter} onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter queue..."
            enterKeyHint="done" autoCorrect="off" autoCapitalize="off" spellCheck={false}
            aria-label="Filter queue"
            className="flex-1 bg-transparent text-white text-base sm:text-sm outline-none placeholder:text-white/30 min-w-0"
          />
          {filtering && (
            <IconButton label="Clear filter" size="xs" onClick={() => setFilter("")}>
              <CloseIcon className="w-3 h-3" />
            </IconButton>
          )}
        </div>
        {filtering && (
          <p className="text-[10px] text-muted mt-1.5">Reordering is off while filtering</p>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-3 pb-3">
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragPending={handleDragPending} onDragAbort={handleDragAbort} onDragStart={handleDragStart} onDragEnd={handleDragEnd}>
          <SortableContext items={rows.map(r => r.song.webpage_url)} strategy={verticalListSortingStrategy}>
            <div className="flex flex-col gap-1 mt-1">
              {rows.map(({ song, index }) => (
                <QueueRow
                  key={song.webpage_url}
                  song={song}
                  index={index}
                  arming={armingUrl === song.webpage_url}
                  draggable={!filtering}
                  onSkipTo={() => handleSkipTo(song)}
                  onRemove={() => handleRemove(song)}
                />
              ))}
              {rows.length === 0 && (
                <p className="text-xs text-muted text-center py-8">No songs match “{filter.trim()}”</p>
              )}
            </div>
          </SortableContext>
        </DndContext>
      </div>
    </div>
  );
}

function QueueRow({ song, index, arming, draggable, onSkipTo, onRemove }: {
  song: Song; index: number; arming: boolean; draggable: boolean;
  onSkipTo: () => void; onRemove: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: song.webpage_url,
    disabled: !draggable,
  });

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      {...attributes}
      {...(draggable ? listeners : {})}
      className={cn(
        "flex items-center gap-2.5 p-2 rounded-2xl bg-white/[0.02] border group",
        draggable && "cursor-grab active:cursor-grabbing",
        "transition-[background-color,border-color,opacity,transform] duration-150",
        isDragging
          ? "opacity-40 border-accent/40 z-10 relative"
          : arming
          ? "scale-[0.97] border-accent/40 bg-white/[0.05]"
          : "border-white/[0.04] hover:bg-white/[0.05] hover:border-white/[0.08]"
      )}
    >
      <button
        onClick={(e) => { e.stopPropagation(); onSkipTo(); }}
        className="w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0 transition-colors duration-150 text-muted hover:text-accent hover:bg-accent/10"
        title="Skip to this song"
        aria-label={`Skip to "${song.title}"`}
      >
        <span className="text-[11px] tabular-nums group-hover:hidden">{index + 1}</span>
        <PlayIcon className="w-3.5 h-3.5 hidden group-hover:block" />
      </button>

      <div className="w-10 h-10 rounded-lg overflow-hidden bg-surface-3 flex-shrink-0">
        {song.thumbnail ? (
          <img src={proxyImg(song.thumbnail)} alt="" className="w-full h-full object-cover" loading="lazy" />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            <NoteIcon className="w-4 h-4 text-muted" />
          </div>
        )}
      </div>

      <div className="flex-1 min-w-0">
        <MarqueeText className="text-sm font-medium text-white">{song.title}</MarqueeText>
        <p className="text-xs text-white/40 truncate mt-0.5">
          {song.uploader}
          {song.requested_by && song.requested_by !== "Unknown" && (
            <span className="text-white/30"> · Requested by {song.requested_by.replace(/<@!?\d+>/, "").trim() || song.requested_by}</span>
          )}
        </p>
      </div>

      <div className="flex items-center gap-0.5 flex-shrink-0">
        <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 pointer-coarse:opacity-100 transition-opacity">
          <AddToPlaylistButton song={song} />
          <FavHeart webpageUrl={song.webpage_url} title={song.title} url={song.url} duration={song.duration} thumbnail={song.thumbnail} uploader={song.uploader} />
          <IconButton label="Remove" size="xs" tone="danger" onClick={(e) => { e.stopPropagation(); onRemove(); }}>
            <CloseIcon className="w-3 h-3" />
          </IconButton>
        </div>
        <span className="text-[11px] tabular-nums text-muted w-8 text-right">
          {song.duration > 0 ? formatDuration(song.duration) : ""}
        </span>
      </div>
    </div>
  );
}
