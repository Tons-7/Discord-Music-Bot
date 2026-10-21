"use client";

import { useState, useRef, useEffect } from "react";
import {
  DndContext,
  closestCenter,
  MouseSensor,
  TouchSensor,
  KeyboardSensor,
  useSensor,
  useSensors,
  type Announcements,
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
import { formatDuration, proxyImg, cn, joinDetails, requesterName } from "@/lib/utils";
import { apiFetch, errorMessage } from "@/lib/api";
import MarqueeText from "./MarqueeText";
import FavHeart from "./FavHeart";
import AddToPlaylistButton, { AddQueueToPlaylistButton } from "./AddToPlaylistButton";
import EmptyState from "./EmptyState";
import IconButton from "./ui/IconButton";
import ConfirmButton from "./ui/ConfirmButton";
import { StatusSlot } from "./SongRow";
import { CheckIcon, CloseIcon, MusicIcon, PlayIcon, NoteIcon, SearchIcon, Spinner } from "./ui/icons";
import type { CurrentSong, GuildState, Song } from "@/types";

// Space picks a row up (as the drag instructions say); Enter stays free for the row's buttons.
const KEYBOARD_CODES = { start: ["Space"], cancel: ["Escape"], end: ["Space", "Enter"] };
// A removed row stays hidden this long after the server confirmed, while its broadcast arrives.
const REMOVED_HIDE_MS = 3000;

export function songAnnouncements(titleOf: (id: string | number) => string): Announcements {
  return {
    onDragStart: ({ active }) => `Picked up ${titleOf(active.id)}.`,
    onDragOver: ({ active, over }) =>
      over ? `${titleOf(active.id)} is over ${titleOf(over.id)}.` : `${titleOf(active.id)} is no longer over a song.`,
    onDragEnd: ({ active, over }) =>
      over ? `${titleOf(active.id)} was dropped over ${titleOf(over.id)}.` : `${titleOf(active.id)} was dropped.`,
    onDragCancel: ({ active }) => `Moving ${titleOf(active.id)} was cancelled.`,
  };
}

export default function QueuePanel({ onSearch }: { onSearch?: () => void }) {
  const { state, guildId } = useGuildState();
  const { toast } = useToast();
  const { queue, queue_duration, played, current } = state;
  const listRef = useRef<HTMLDivElement>(null);
  const nowRef = useRef<HTMLDivElement>(null);

  // Optimistic overlays, each tied to the server queue it was made from: a new queue from
  // the server replaces the reorder, and removed rows stay hidden until they are gone.
  const [order, setOrder] = useState<{ base: Song[]; songs: Song[] } | null>(null);
  const [removed, setRemoved] = useState<ReadonlySet<string>>(new Set());
  const [skipping, setSkipping] = useState<string | null>(null);
  // Client-side only. While it's non-empty the rows no longer line up with the
  // server queue, so reordering is disabled for its duration.
  const [filter, setFilter] = useState("");
  // Row whose long-press drag is arming (touch delay constraint) — visual cue
  const [armingUrl, setArmingUrl] = useState<string | null>(null);
  // dnd-kit fires a click on the drag's mouseup — suppress button actions after a drag
  const dragOccurred = useRef(false);

  const base = order && order.base === queue ? order.songs : queue;
  const displayed = removed.size ? base.filter(s => !removed.has(s.webpage_url)) : base;

  // Like YouTube's queue, songs stay once played: they are listed above the current one.
  const shownPlayed = removed.size ? played.filter(s => !removed.has(s.webpage_url)) : played;

  const query = filter.trim().toLowerCase();
  const filtering = query.length > 0;
  const matches = (song: Song) => !filtering
    || song.title.toLowerCase().includes(query)
    || (song.uploader || "").toLowerCase().includes(query);
  // Rows carry their index in the unfiltered list, so numbering stays true
  // while filtered and drag indices never come from the filtered list.
  const rows = displayed.map((song, index) => ({ song, index })).filter(({ song }) => matches(song));
  const playedRows = shownPlayed.map((song, index) => ({ song, index })).filter(({ song }) => matches(song));

  // Open at the current song (played ones above it stay a scroll away), and follow it as songs change.
  const currentUrl = current?.webpage_url;
  useEffect(() => {
    const list = listRef.current;
    const row = nowRef.current;
    if (list && row) list.scrollTop = row.offsetTop - 4;
  }, [currentUrl]);

  const titles = new Map(displayed.map(s => [s.webpage_url, s.title]));
  const announcements = songAnnouncements(id => `"${titles.get(String(id)) ?? "song"}"`);

  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates, keyboardCodes: KEYBOARD_CODES }),
  );

  const hide = (url: string) => setRemoved(prev => new Set(prev).add(url));
  const unhide = (url: string) => setRemoved(prev => {
    if (!prev.has(url)) return prev;
    const next = new Set(prev);
    next.delete(url);
    return next;
  });

  // /queue/add always appends, so restoring the original slot takes a follow-up
  // move. The position is read back from the server rather than assumed: other
  // clients may have changed the queue while the toast was up.
  const undoRemove = (song: Song, pos: number) => {
    apiFetch<{ duplicate?: boolean }>(`/api/guild/${guildId}/queue/add`, {
      method: "POST", body: JSON.stringify({ query: song.webpage_url }),
    })
      .then(res => (res.duplicate ? null : apiFetch<GuildState>(`/api/guild/${guildId}/state`)))
      .then(server => {
        if (!server) return;
        const from = server.queue.findIndex(s => s.webpage_url === song.webpage_url);
        const to = Math.min(pos, server.queue.length - 1);
        if (from < 0 || to < 0 || from === to) return;
        return apiFetch(`/api/guild/${guildId}/queue/move`, {
          method: "POST", body: JSON.stringify({ from_pos: from, to_pos: to, webpage_url: song.webpage_url }),
        });
      })
      .catch((e: unknown) => toast(errorMessage(e, "Undo failed"), "error"));
  };

  // Positions come from what is on screen (already-removed rows excluded); the URL lets the
  // server check it is removing that song.
  const handleRemove = (song: Song, wasPlayed = false) => {
    const pos = (wasPlayed ? shownPlayed : displayed).findIndex(s => s.webpage_url === song.webpage_url);
    if (pos < 0) return;
    hide(song.webpage_url);
    const section = wasPlayed ? "&played=true" : "";
    apiFetch<{ position?: number }>(
      `/api/guild/${guildId}/queue/${pos}?url=${encodeURIComponent(song.webpage_url)}${section}`,
      { method: "DELETE" },
    ).then(
      (res) => {
        const at = typeof res.position === "number" ? res.position : pos;
        // Undo queues a song again; a played one would come back as upcoming, so it has none.
        toast(`Removed "${song.title}"`, "success",
          wasPlayed ? undefined : { label: "Undo", onClick: () => undoRemove(song, at) });
        setTimeout(() => unhide(song.webpage_url), REMOVED_HIDE_MS);
      },
      (e: unknown) => {
        unhide(song.webpage_url);
        toast(errorMessage(e, "Couldn't remove it"), "error");
      },
    );
  };

  const handleClear = () => {
    apiFetch(`/api/guild/${guildId}/queue/clear`, { method: "POST" })
      .catch((e: unknown) => toast(errorMessage(e, "Couldn't clear the queue"), "error"));
  };

  // Played songs are jumped back to, the same way: the queue keeps its order either way.
  const handleSkipTo = (song: Song, wasPlayed = false) => {
    if (dragOccurred.current || skipping) return;
    const pos = (wasPlayed ? shownPlayed : displayed).findIndex(s => s.webpage_url === song.webpage_url);
    if (pos < 0) return;
    setSkipping(song.webpage_url);
    apiFetch<{ title: string }>(`/api/guild/${guildId}/skipto`, {
      method: "POST", body: JSON.stringify({ position: pos, webpage_url: song.webpage_url, played: wasPlayed }),
    }).then(
      (r) => {
        setSkipping(null);
        toast(`${wasPlayed ? "Back to" : "Skipped to"} "${r.title}"`, "success");
      },
      (e: unknown) => {
        setSkipping(null);
        toast(errorMessage(e, "Couldn't skip to it"), "error");
      },
    );
  };

  // Only cue delay-based (long-press) arming — the MouseSensor's distance
  // constraint would flash the cue on every click.
  const handleDragPending = (event: DragPendingEvent) => {
    if ("delay" in event.constraint) setArmingUrl(String(event.id));
  };
  const handleDragAbort = () => setArmingUrl(null);

  const handleDragStart = () => { setArmingUrl(null); dragOccurred.current = true; };

  const handleDragEnd = (event: DragEndEvent) => {
    setTimeout(() => { dragOccurred.current = false; }, 100);
    if (filtering) return; // indices would be computed against a filtered list
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const from = displayed.findIndex(s => s.webpage_url === active.id);
    const to = displayed.findIndex(s => s.webpage_url === over.id);
    if (from < 0 || to < 0) return;

    setOrder({ base: queue, songs: arrayMove(displayed, from, to) });
    apiFetch(`/api/guild/${guildId}/queue/move`, {
      method: "POST",
      body: JSON.stringify({ from_pos: from, to_pos: to, webpage_url: String(active.id) }),
    }).catch((e: unknown) => {
      setOrder(null);
      toast(errorMessage(e, "Couldn't move it"), "error");
    });
  };

  const header = (
    <div className="flex items-center justify-between px-4 pt-3 pb-2 flex-shrink-0">
      <span className="text-xs text-muted font-medium">
        {filtering
          ? `${rows.length + playedRows.length} of ${displayed.length + shownPlayed.length}`
          : `${displayed.length} up next`}
        {queue_duration > 0 && displayed.length > 0 && <> · {formatDuration(queue_duration)}</>}
      </span>
      <div className="flex items-center gap-1.5">
        <AddQueueToPlaylistButton disabled={displayed.length === 0 && shownPlayed.length === 0 && !state.current} />
        {(displayed.length > 0 || shownPlayed.length > 0) && (
          <ConfirmButton label="Clear" confirmLabel="Clear queue?" onConfirm={handleClear} />
        )}
      </div>
    </div>
  );

  if (displayed.length === 0 && shownPlayed.length === 0) {
    return (
      <div className="flex flex-col h-full">
        {/* With a song playing, the session can still be saved */}
        {state.current && header}
        <EmptyState
          className="flex-1"
          icon={<MusicIcon className="w-7 h-7" strokeWidth={1.5} />}
          title="Queue is empty"
          subtitle="Search for songs to add"
          action={onSearch ? { label: "Search", onClick: onSearch } : undefined}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {header}

      <div className="px-4 pb-2.5 border-b border-fg/[0.08] flex-shrink-0">
        <div className="flex items-center gap-2 bg-surface-3/60 rounded-xl border border-fg/[0.08] focus-within:border-accent/40 transition-[border-color] duration-200 px-3 py-1.5">
          <SearchIcon className="w-3.5 h-3.5 text-muted flex-shrink-0" />
          <input
            type="text" value={filter} onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter queue…"
            enterKeyHint="done" autoCorrect="off" autoCapitalize="off" spellCheck={false}
            aria-label="Filter queue"
            className="flex-1 bg-transparent text-fg text-base sm:text-sm outline-none placeholder:text-muted min-w-0"
          />
          {filtering && (
            <IconButton label="Clear filter" size="xs" onClick={() => setFilter("")}>
              <CloseIcon className="w-3 h-3" />
            </IconButton>
          )}
        </div>
        {filtering && (
          <p className="text-xs text-muted mt-1.5">Reordering is off while filtering</p>
        )}
      </div>

      <div ref={listRef} className="relative flex-1 overflow-y-auto px-3 pb-3">
        {playedRows.length > 0 && (
          <div className="flex flex-col gap-1 mt-1" aria-label="Played">
            {playedRows.map(({ song }) => (
              <PlayedRow
                key={song.webpage_url}
                song={song}
                skipping={skipping === song.webpage_url}
                onJump={s => handleSkipTo(s, true)}
                onRemove={s => handleRemove(s, true)}
              />
            ))}
          </div>
        )}
        {current && (!filtering || matches(current)) && <NowPlayingRow ref={nowRef} song={current} />}
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          accessibility={{ announcements }}
          onDragPending={handleDragPending}
          onDragAbort={handleDragAbort}
          onDragStart={handleDragStart}
          onDragEnd={handleDragEnd}
        >
          <SortableContext items={rows.map(r => r.song.webpage_url)} strategy={verticalListSortingStrategy}>
            <div className="flex flex-col gap-1 mt-1">
              {rows.map(({ song, index }) => (
                <QueueRow
                  key={song.webpage_url}
                  song={song}
                  index={index}
                  arming={armingUrl === song.webpage_url}
                  draggable={!filtering}
                  skipping={skipping === song.webpage_url}
                  onSkipTo={handleSkipTo}
                  onRemove={handleRemove}
                />
              ))}
              {filtering && rows.length === 0 && playedRows.length === 0 && (
                <p className="text-xs text-muted text-center py-8">No songs match “{filter.trim()}”</p>
              )}
              {!filtering && displayed.length === 0 && (
                <p className="text-xs text-muted text-center py-6">Nothing up next</p>
              )}
            </div>
          </SortableContext>
        </DndContext>
      </div>
    </div>
  );
}

function QueueRow({ song, index, arming, draggable, skipping, onSkipTo, onRemove }: {
  song: Song; index: number; arming: boolean; draggable: boolean; skipping: boolean;
  onSkipTo: (song: Song) => void; onRemove: (song: Song) => void;
}) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({
    id: song.webpage_url,
    disabled: !draggable,
  });
  // The row is the activator: key presses on its buttons (or in their popovers) don't pick it up.
  const rowRef = (node: HTMLDivElement | null) => {
    setNodeRef(node);
    setActivatorNodeRef(node);
  };
  const requester = requesterName(song.requested_by);

  return (
    <div
      ref={rowRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      {...attributes}
      {...(draggable ? listeners : {})}
      aria-label={song.title}
      className={cn(
        "flex items-center gap-2.5 p-2 rounded-2xl bg-fg/[0.02] border group",
        draggable && "cursor-grab active:cursor-grabbing",
        "transition-[background-color,border-color,opacity,transform] duration-150",
        isDragging
          ? "opacity-40 border-accent/40 z-10 relative"
          : arming
          ? "scale-[0.97] border-accent/40 bg-fg/[0.05]"
          : "border-fg/[0.04] hover:bg-fg/[0.05] hover:border-fg/[0.08]"
      )}
    >
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); onSkipTo(song); }}
        disabled={skipping}
        className="relative min-w-7 h-7 px-1 rounded-lg flex items-center justify-center flex-shrink-0 transition-colors duration-150 text-muted hover:text-accent hover:bg-accent/10 pointer-coarse:after:absolute pointer-coarse:after:-inset-1 pointer-coarse:after:content-['']"
        title="Skip to this song"
        aria-label={`Skip to "${song.title}"`}
      >
        {skipping ? <Spinner className="w-3.5 h-3.5" label="Skipping" /> : (
          <>
            <span className="text-xs tabular-nums group-hover:hidden group-has-focus-visible:hidden">{index + 1}</span>
            <PlayIcon className="w-3.5 h-3.5 hidden group-hover:block group-has-focus-visible:block" />
          </>
        )}
      </button>

      <Thumb song={song} />

      <div className="flex-1 min-w-0">
        <MarqueeText className="text-sm font-medium text-fg">{song.title}</MarqueeText>
        <p className="text-xs text-muted truncate mt-0.5">
          {joinDetails(song.uploader, requester && `Requested by ${requester}`)}
        </p>
      </div>

      <div className="flex items-center gap-1 flex-shrink-0">
        <div className="flex items-center gap-1.5 pointer-coarse:gap-2.5 opacity-0 group-hover:opacity-100 group-has-focus-visible:opacity-100 pointer-coarse:opacity-100 transition-opacity">
          <AddToPlaylistButton song={song} />
          <FavHeart webpageUrl={song.webpage_url} title={song.title} duration={song.duration} thumbnail={song.thumbnail} uploader={song.uploader} />
          <IconButton label={`Remove "${song.title}"`} size="xs" tone="danger" onClick={(e) => { e.stopPropagation(); onRemove(song); }}>
            <CloseIcon className="w-3 h-3" />
          </IconButton>
        </div>
        <StatusSlot duration={song.duration} />
      </div>
    </div>
  );
}

function Thumb({ song }: { song: Song }) {
  return (
    <div className="w-10 h-10 rounded-lg overflow-hidden bg-surface-3 flex-shrink-0">
      {song.thumbnail ? (
        <img src={proxyImg(song.thumbnail)} alt="" className="w-full h-full object-cover" loading="lazy" />
      ) : (
        <div className="w-full h-full flex items-center justify-center">
          <NoteIcon className="w-4 h-4 text-muted" />
        </div>
      )}
    </div>
  );
}

// Already played: muted, and its leading button goes back to it.
function PlayedRow({ song, skipping, onJump, onRemove }: {
  song: Song; skipping: boolean; onJump: (song: Song) => void; onRemove: (song: Song) => void;
}) {
  return (
    <div className="flex items-center gap-2.5 p-2 rounded-2xl border border-transparent group hover:bg-fg/[0.04] transition-[background-color] duration-150">
      <button
        type="button"
        onClick={() => onJump(song)}
        disabled={skipping}
        className="relative min-w-7 h-7 px-1 rounded-lg flex items-center justify-center flex-shrink-0 transition-colors duration-150 text-muted hover:text-accent hover:bg-accent/10 pointer-coarse:after:absolute pointer-coarse:after:-inset-1 pointer-coarse:after:content-['']"
        title="Play this song again"
        aria-label={`Go back to "${song.title}"`}
      >
        {skipping ? <Spinner className="w-3.5 h-3.5" label="Going back" /> : (
          <>
            <CheckIcon className="w-3.5 h-3.5 group-hover:hidden group-has-focus-visible:hidden" />
            <PlayIcon className="w-3.5 h-3.5 hidden group-hover:block group-has-focus-visible:block" />
          </>
        )}
      </button>
      <div className="opacity-60 group-hover:opacity-100 group-has-focus-visible:opacity-100 transition-opacity flex items-center gap-2.5 flex-1 min-w-0">
        <Thumb song={song} />
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium text-fg truncate">{song.title}</p>
          <p className="text-xs text-muted truncate mt-0.5">{song.uploader}</p>
        </div>
      </div>
      <div className="flex items-center gap-1 flex-shrink-0">
        <div className="flex items-center gap-1.5 pointer-coarse:gap-2.5 opacity-0 group-hover:opacity-100 group-has-focus-visible:opacity-100 pointer-coarse:opacity-100 transition-opacity">
          <AddToPlaylistButton song={song} />
          <FavHeart webpageUrl={song.webpage_url} title={song.title} duration={song.duration} thumbnail={song.thumbnail} uploader={song.uploader} />
          <IconButton label={`Remove "${song.title}"`} size="xs" tone="danger" onClick={() => onRemove(song)}>
            <CloseIcon className="w-3 h-3" />
          </IconButton>
        </div>
        <StatusSlot duration={song.duration} />
      </div>
    </div>
  );
}

// The queue's position marker: played songs above it, upcoming ones below.
function NowPlayingRow({ song, ref }: { song: CurrentSong; ref: React.Ref<HTMLDivElement> }) {
  return (
    <div ref={ref} className="flex items-center gap-2.5 p-2 my-1 rounded-2xl bg-accent/10 border border-accent/30" aria-current="true">
      <span className="min-w-7 flex justify-center flex-shrink-0 text-accent" aria-hidden="true">
        <MusicIcon className="w-4 h-4" />
      </span>
      <Thumb song={song} />
      <div className="flex-1 min-w-0">
        <MarqueeText className="text-sm font-semibold text-fg">{song.title}</MarqueeText>
        <p className="text-xs text-accent truncate mt-0.5">{joinDetails("Now playing", song.uploader)}</p>
      </div>
      <StatusSlot duration={song.duration} />
    </div>
  );
}
