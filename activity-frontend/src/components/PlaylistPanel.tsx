"use client";

import { useState, useEffect, useRef } from "react";
import useSWR, { useSWRConfig } from "swr";
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
import MarqueeText from "./MarqueeText";
import { useGuildState } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { apiFetch } from "@/lib/api";
import { maybeAutoPlay } from "@/lib/queueActions";
import { formatDuration, proxyImg, cn } from "@/lib/utils";
import EmptyState from "./EmptyState";
import IconButton from "./ui/IconButton";
import { SongRowSkeleton } from "./ui/Skeleton";
import { Spinner, BackIcon, PlusIcon, PlayIcon, TrashIcon, CloseIcon, CheckIcon, SearchIcon, NoteIcon } from "./ui/icons";
import type { Playlist, PlaylistSong, Member, Collab } from "@/types";

type View = "list" | "detail" | "collabs";

export default function PlaylistPanel() {
  const { guildId, state } = useGuildState();
  const { toast } = useToast();
  const { mutate } = useSWRConfig();

  const [view, setView] = useState<View>("list");
  const [globalMode, setGlobalMode] = useState(false);
  const [selectedName, setSelectedName] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [queueingUrl, setQueueingUrl] = useState<string | null>(null);
  const [addedUrls, setAddedUrls] = useState<Set<string>>(new Set());
  // Row whose long-press drag is arming (touch delay constraint) — visual cue
  const [armingUrl, setArmingUrl] = useState<string | null>(null);
  // dnd-kit fires a click on the drag's mouseup — suppress row-click adds after a drag
  const dragOccurred = useRef(false);

  const [memberSearch, setMemberSearch] = useState("");
  const [members, setMembers] = useState<Member[]>([]);
  const [memberSearching, setMemberSearching] = useState(false);
  const searchDebounce = useRef<ReturnType<typeof setTimeout>>(undefined);
  const searchAbort = useRef<AbortController | null>(null);

  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const gm = `global_mode=${globalMode}`;
  const listKey = `/api/guild/${guildId}/playlists?${gm}`;
  const detailKey = view === "detail" && selectedName
    ? `/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}?${gm}`
    : null;
  const collabsKey = view === "collabs" && selectedName
    ? `/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/collabs?${gm}`
    : null;

  const { data: listData, isLoading: listLoading } = useSWR<{ playlists: Playlist[] }>(listKey);
  const { data: detailData, isLoading: detailLoading } = useSWR<{ songs: PlaylistSong[] }>(detailKey);
  const { data: collabsData, isLoading: collabLoading } = useSWR<{ collaborators: Collab[] }>(collabsKey);
  const playlists = listData?.playlists ?? [];
  const songs = detailData?.songs ?? [];
  const collabs = collabsData?.collaborators ?? [];

  useEffect(() => {
    if (!confirmDelete) return;
    const t = setTimeout(() => setConfirmDelete(null), 3000);
    return () => clearTimeout(t);
  }, [confirmDelete]);

  useEffect(() => () => {
    if (searchDebounce.current) clearTimeout(searchDebounce.current);
    searchAbort.current?.abort();
  }, []);

  const openPlaylist = (name: string) => {
    setSelectedName(name);
    setView("detail");
  };

  const handleCreate = async () => {
    if (!newName.trim()) return;
    try {
      await apiFetch(`/api/guild/${guildId}/playlists`, { method: "POST", body: JSON.stringify({ name: newName.trim(), global_mode: globalMode }) });
      toast(`Created "${newName.trim()}"`, "success"); setNewName(""); setShowCreate(false);
      mutate(listKey);
    } catch (e: any) { toast(e.message, "error"); }
  };

  const handleDelete = async (name: string) => {
    if (confirmDelete !== name) { setConfirmDelete(name); return; }
    setConfirmDelete(null);
    mutate(listKey, { playlists: playlists.filter(p => p.name !== name) }, { revalidate: false });
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}?${gm}`, { method: "DELETE" });
      toast(`Deleted "${name}"`, "success"); if (view !== "list") setView("list");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      mutate(listKey);
    }
  };

  const handleLoad = async (name: string) => {
    try {
      const r = await apiFetch<{ added: number; total: number; auto_play?: boolean }>(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/load`, { method: "POST", body: JSON.stringify({ global_mode: globalMode }) });
      maybeAutoPlay(guildId, r, !!state.current);
      toast(`Loaded ${r.added} of ${r.total} songs`, "success");
    } catch (e: any) { toast(e.message, "error"); }
  };

  const handleAddCurrent = async (name: string) => {
    if (!state.current) { toast("Nothing playing", "error"); return; }
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/add`, { method: "POST", body: JSON.stringify({ song_url: state.current.webpage_url, global_mode: globalMode }) });
      toast(`Saved to "${name}"`, "success");
      mutate(listKey);
      if (detailKey && selectedName === name) mutate(detailKey);
    } catch (e: any) { toast(e.message, "error"); }
  };

  const handleRemoveSong = async (pos: number) => {
    if (!detailKey) return;
    mutate(detailKey, { songs: songs.filter((_, i) => i !== pos) }, { revalidate: false });
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/${pos}?${gm}`, { method: "DELETE" });
      toast("Removed", "success");
    } catch (e: any) { toast(e.message, "error"); }
    finally { mutate(detailKey); mutate(listKey); }
  };

  const handleQueueSong = async (song: PlaylistSong) => {
    if (dragOccurred.current) return;
    if (queueingUrl || addedUrls.has(song.webpage_url)) return;
    setQueueingUrl(song.webpage_url);
    try {
      const res = await apiFetch<{ ok: boolean; duplicate?: boolean; auto_play?: boolean }>(
        `/api/guild/${guildId}/queue/add`,
        { method: "POST", body: JSON.stringify({ query: song.webpage_url }) },
      );
      if (res.duplicate) {
        toast(`"${song.title}" is already in queue`, "error");
        return;
      }
      maybeAutoPlay(guildId, res, !!state.current);
      toast(`Added "${song.title}"`, "success");
      setAddedUrls(prev => new Set(prev).add(song.webpage_url));
      setTimeout(() => setAddedUrls(prev => { const n = new Set(prev); n.delete(song.webpage_url); return n; }), 2000);
    } catch (e: any) { toast(e.message, "error"); }
    finally { setQueueingUrl(null); }
  };

  // Only cue delay-based (long-press) arming — the MouseSensor's distance
  // constraint would flash the cue on every click.
  const handleSongDragPending = (event: DragPendingEvent) => {
    if ("delay" in event.constraint) setArmingUrl(String(event.id));
  };
  const handleSongDragAbort = () => setArmingUrl(null);

  const handleSongDragStart = () => { setArmingUrl(null); dragOccurred.current = true; };

  const handleSongDragEnd = async (event: DragEndEvent) => {
    setTimeout(() => { dragOccurred.current = false; }, 100);
    const { active, over } = event;
    if (!detailKey || !over || active.id === over.id) return;
    const from = songs.findIndex(s => s.webpage_url === active.id);
    const to = songs.findIndex(s => s.webpage_url === over.id);
    if (from < 0 || to < 0) return;
    mutate(detailKey, { songs: arrayMove(songs, from, to) }, { revalidate: false });
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/move`, { method: "POST", body: JSON.stringify({ from_pos: from, to_pos: to, global_mode: globalMode }) });
      mutate(listKey); // first song may have changed -> playlist cover updates
    } catch (e: any) {
      toast(e.message, "error");
      mutate(detailKey);
    }
  };

  const openCollabs = (name: string) => {
    setSelectedName(name);
    setView("collabs");
    setMemberSearch("");
    setMembers([]);
  };

  const searchMembers = async (q: string) => {
    searchAbort.current?.abort();
    const ctrl = new AbortController();
    searchAbort.current = ctrl;
    setMemberSearching(true);
    try {
      const d = await apiFetch<{ members: Member[] }>(
        `/api/guild/${guildId}/playlists/members?q=${encodeURIComponent(q)}`,
        { signal: ctrl.signal },
      );
      const collabIds = new Set(collabs.map(c => c.id));
      setMembers(d.members.filter(m => !collabIds.has(m.id)));
      setMemberSearching(false);
    } catch (e: any) {
      if (e?.name === "AbortError") return;
      setMembers([]);
      setMemberSearching(false);
    }
  };

  const handleMemberInput = (val: string) => {
    setMemberSearch(val);
    if (searchDebounce.current) clearTimeout(searchDebounce.current);
    if (val.trim()) {
      searchDebounce.current = setTimeout(() => searchMembers(val), 300);
    } else {
      setMembers([]);
    }
  };

  const handleAddCollab = async (member: Member) => {
    if (!collabsKey) return;
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/collabs`, { method: "POST", body: JSON.stringify({ user_id: member.id, global_mode: globalMode }) });
      toast(`Added ${member.display_name}`, "success");
      mutate(collabsKey, { collaborators: [...collabs, { id: member.id, display_name: member.display_name, avatar: member.avatar }] }, { revalidate: false });
      setMembers(prev => prev.filter(m => m.id !== member.id));
      setMemberSearch("");
    } catch (e: any) { toast(e.message, "error"); }
  };

  const handleRemoveCollab = async (collab: Collab) => {
    if (!collabsKey) return;
    mutate(collabsKey, { collaborators: collabs.filter(c => c.id !== collab.id) }, { revalidate: false });
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/collabs/${collab.id}?${gm}`, { method: "DELETE" });
      toast(`Removed ${collab.display_name}`, "success");
    } catch (e: any) {
      toast(e.message, "error");
      mutate(collabsKey);
    }
  };

  // ── Collabs view ──────────────────────────────────────────────
  if (view === "collabs") {
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center gap-2 px-4 py-3 border-b border-white/[0.06] flex-shrink-0">
          <IconButton label="Back" onClick={() => openPlaylist(selectedName)}>
            <BackIcon />
          </IconButton>
          <h3 className="text-sm font-semibold text-white truncate flex-1">{selectedName}</h3>
          <span className="text-[10px] text-muted">Collaborators</span>
        </div>

        <div className="px-4 py-3 border-b border-white/[0.08] flex-shrink-0">
          <div className="flex items-center gap-2.5 bg-surface-3/60 rounded-xl border border-white/[0.08] focus-within:border-accent/40 transition-[border-color] duration-200 px-3.5 py-2.5">
            <SearchIcon className="w-4 h-4 text-white/40 flex-shrink-0" />
            <input
              type="search" value={memberSearch} onChange={(e) => handleMemberInput(e.target.value)}
              placeholder="Search members to add..."
              enterKeyHint="search" autoCorrect="off" autoCapitalize="off" spellCheck={false}
              className="flex-1 bg-transparent text-white text-base sm:text-sm outline-none placeholder:text-white/30 min-w-0 [&::-webkit-search-cancel-button]:hidden"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {memberSearch && (
            <div className="mb-3">
              {memberSearching ? (
                <div className="flex justify-center py-4"><Spinner /></div>
              ) : members.length > 0 ? (
                <div className="flex flex-col gap-1 mt-2">
                  {members.map(m => (
                    <button
                      key={m.id} onClick={() => handleAddCollab(m)}
                      className="w-full flex items-center gap-2.5 p-2 rounded-xl bg-accent/5 border border-accent/10 hover:bg-accent/10 transition-[background-color] duration-150 text-left"
                    >
                      <div className="w-8 h-8 rounded-full bg-surface-3 overflow-hidden flex-shrink-0">
                        {m.avatar ? <img src={proxyImg(m.avatar)} alt="" className="w-full h-full object-cover" /> : (
                          <div className="w-full h-full flex items-center justify-center text-muted text-[10px] font-bold">{m.display_name[0]}</div>
                        )}
                      </div>
                      <div className="flex-1 min-w-0">
                        <p className="text-xs font-medium text-white truncate">{m.display_name}</p>
                        <p className="text-[10px] text-white/40 truncate">@{m.username}</p>
                      </div>
                      <PlusIcon className="w-4 h-4 text-accent flex-shrink-0" />
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-[11px] text-muted text-center py-3">No members found</p>
              )}
            </div>
          )}

          {collabLoading ? (
            <div className="flex justify-center py-8"><Spinner className="w-5 h-5" /></div>
          ) : collabs.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-8 text-center">
              <p className="text-xs text-muted">No collaborators yet</p>
              <p className="text-[10px] text-muted/60 mt-1">Search for members above to add</p>
            </div>
          ) : (
            <div className="flex flex-col gap-1 mt-1">
              <p className="text-[10px] text-muted px-1 mb-1">{collabs.length} collaborator{collabs.length !== 1 ? "s" : ""}</p>
              {collabs.map(c => (
                <div key={c.id} className="flex items-center gap-2.5 p-2 rounded-xl bg-white/[0.02] border border-white/[0.04] group">
                  <div className="w-8 h-8 rounded-full bg-surface-3 overflow-hidden flex-shrink-0">
                    {c.avatar ? <img src={proxyImg(c.avatar)} alt="" className="w-full h-full object-cover" /> : (
                      <div className="w-full h-full flex items-center justify-center text-muted text-[10px] font-bold">{c.display_name[0]}</div>
                    )}
                  </div>
                  <p className="text-xs font-medium text-white truncate flex-1">{c.display_name}</p>
                  <IconButton
                    label={`Remove ${c.display_name}`}
                    size="xs"
                    tone="danger"
                    className="opacity-0 group-hover:opacity-100 pointer-coarse:opacity-100"
                    onClick={() => handleRemoveCollab(c)}
                  >
                    <CloseIcon className="w-3 h-3" />
                  </IconButton>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  // ── Detail view ──────────────────────────────────────────────────
  if (view === "detail") {
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center gap-1 px-4 py-2 border-b border-white/[0.06] flex-shrink-0">
          <IconButton label="Back" onClick={() => setView("list")}>
            <BackIcon />
          </IconButton>
          <h3 className="text-sm font-semibold text-white truncate flex-1 pl-1">{selectedName}</h3>
          <HeaderBtn onClick={() => openCollabs(selectedName)} title="Manage collaborators">Collabs</HeaderBtn>
          <HeaderBtn onClick={() => handleAddCurrent(selectedName)} title="Save current song" className="text-accent hover:text-accent/80">+ Save</HeaderBtn>
          <HeaderBtn onClick={() => handleLoad(selectedName)} title="Load into queue" className="text-success/80 hover:text-success">Load</HeaderBtn>
        </div>

        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {detailLoading ? (
            <SongRowSkeleton className="mt-2" />
          ) : songs.length === 0 ? (
            <div className="flex items-center justify-center py-12 text-muted text-xs">Empty playlist</div>
          ) : (
            <DndContext sensors={sensors} collisionDetection={closestCenter} onDragPending={handleSongDragPending} onDragAbort={handleSongDragAbort} onDragStart={handleSongDragStart} onDragEnd={handleSongDragEnd}>
              <SortableContext items={songs.map(s => s.webpage_url)} strategy={verticalListSortingStrategy}>
                <div className="flex flex-col gap-1 mt-2">
                  {songs.map((song, i) => (
                    <SortableSongRow
                      key={song.webpage_url}
                      song={song}
                      index={i}
                      arming={armingUrl === song.webpage_url}
                      queueing={queueingUrl === song.webpage_url}
                      added={addedUrls.has(song.webpage_url)}
                      onQueue={() => handleQueueSong(song)}
                      onRemove={() => handleRemoveSong(i)}
                    />
                  ))}
                </div>
              </SortableContext>
            </DndContext>
          )}
        </div>
      </div>
    );
  }

  // ── List view ────────────────────────────────────────────────────
  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-white/[0.08] flex-shrink-0">
        <div className="flex bg-white/[0.04] rounded-xl p-[3px] flex-1">
          <button onClick={() => setGlobalMode(false)} className={cn("flex-1 text-[11px] font-semibold py-1.5 rounded-[9px] transition-[background-color,color,box-shadow] duration-200", !globalMode ? "bg-accent text-white shadow-[0_1px_4px_rgba(88,101,242,0.3)]" : "text-white/40 hover:text-white/60")}>Server</button>
          <button onClick={() => setGlobalMode(true)} className={cn("flex-1 text-[11px] font-semibold py-1.5 rounded-[9px] transition-[background-color,color,box-shadow] duration-200", globalMode ? "bg-accent text-white shadow-[0_1px_4px_rgba(88,101,242,0.3)]" : "text-white/40 hover:text-white/60")}>Global</button>
        </div>
        <IconButton label="Create playlist" onClick={() => setShowCreate(!showCreate)}>
          <PlusIcon />
        </IconButton>
      </div>

      {showCreate && (
        <div className="px-4 py-2.5 border-b border-white/[0.08] flex gap-2">
          <input type="text" value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Playlist name..."
            onKeyDown={(e) => e.key === "Enter" && handleCreate()}
            enterKeyHint="done"
            className="flex-1 bg-surface-3/60 text-white text-base sm:text-sm rounded-xl px-3.5 py-2 outline-none border border-white/[0.08] focus:border-accent/40 placeholder:text-white/30 min-w-0" />
          <button onClick={handleCreate} disabled={!newName.trim()} className="text-xs font-semibold px-4 py-2 rounded-xl bg-accent text-white hover:bg-accent/80 disabled:opacity-40 transition-[background-color,opacity] duration-150">Create</button>
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-3 pb-3">
        {listLoading ? (
          <SongRowSkeleton className="mt-2" count={4} />
        ) : playlists.length === 0 ? (
          <EmptyState
            compact
            icon={
              <svg className="w-6 h-6 text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 010 3.75H5.625a1.875 1.875 0 010-3.75z" />
              </svg>
            }
            title="No playlists yet"
            subtitle="Create one to save songs"
          />
        ) : (
          <div className="flex flex-col gap-1.5 mt-2">
            {playlists.map(pl => (
              <div key={pl.name} className="flex items-center gap-3 p-2.5 rounded-2xl bg-white/[0.02] border border-white/[0.04] hover:bg-white/[0.06] hover:border-white/[0.08] transition-[background-color,border-color] duration-150 group">
                <button onClick={() => openPlaylist(pl.name)} className="flex-1 flex items-center gap-3 min-w-0 text-left">
                  <div className="w-10 h-10 rounded-xl bg-surface-3 overflow-hidden flex items-center justify-center flex-shrink-0">
                    {pl.thumbnail ? (
                      <img src={proxyImg(pl.thumbnail)} alt="" className="w-full h-full object-cover" loading="lazy" />
                    ) : (
                      <svg className="w-5 h-5 text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 010 3.75H5.625a1.875 1.875 0 010-3.75z" /></svg>
                    )}
                  </div>
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-white truncate">{pl.name}</p>
                    <p className="text-[10px] text-white/40">{pl.song_count} song{pl.song_count !== 1 ? "s" : ""}</p>
                  </div>
                </button>
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 pointer-coarse:opacity-100 transition-opacity flex-shrink-0">
                  <IconButton label="Save current song" tone="accent" onClick={() => handleAddCurrent(pl.name)}>
                    <PlusIcon className="w-3.5 h-3.5" />
                  </IconButton>
                  <IconButton label="Load into queue" tone="accent" onClick={() => handleLoad(pl.name)}>
                    <PlayIcon className="w-3.5 h-3.5" />
                  </IconButton>
                  <IconButton label="Collaborators" onClick={() => openCollabs(pl.name)}>
                    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M15 19.128a9.38 9.38 0 002.625.372 9.337 9.337 0 004.121-.952 4.125 4.125 0 00-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 018.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0111.964-3.07M12 6.375a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zm8.25 2.25a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z" /></svg>
                  </IconButton>
                  <IconButton
                    label={confirmDelete === pl.name ? "Tap to confirm delete" : "Delete"}
                    tone="danger"
                    className={cn(confirmDelete === pl.name && "bg-danger! text-white! animate-pulse")}
                    onClick={() => handleDelete(pl.name)}
                  >
                    <TrashIcon className="w-3.5 h-3.5" />
                  </IconButton>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function HeaderBtn({ children, onClick, title, className }: {
  children: React.ReactNode; onClick: () => void; title: string; className?: string;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={cn("text-[11px] font-medium px-2 py-1.5 rounded-lg text-muted hover:text-white hover:bg-white/[0.06] transition-colors flex-shrink-0", className)}
    >
      {children}
    </button>
  );
}

// Click adds to queue (same as Search/History/Favorites rows); drag reorders.
function SortableSongRow({ song, index, arming, queueing, added, onQueue, onRemove }: {
  song: PlaylistSong; index: number; arming: boolean; queueing: boolean; added: boolean;
  onQueue: () => void; onRemove: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: song.webpage_url,
  });

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      {...attributes}
      {...listeners}
      onClick={onQueue}
      title="Add to queue"
      className={cn(
        "flex items-center gap-2 p-2 rounded-xl border group cursor-pointer",
        "transition-[background-color,border-color,opacity,transform] duration-150",
        isDragging ? "opacity-40 border-accent/40 z-10 relative bg-white/[0.02]"
          : arming ? "scale-[0.97] border-accent/40 bg-white/[0.05]"
          : added ? "bg-success/[0.06] border-success/20"
          : "bg-white/[0.02] border-white/[0.04] hover:bg-white/[0.06]"
      )}
    >
      <span className="w-4 text-center text-[10px] tabular-nums text-muted flex-shrink-0">{index + 1}</span>
      <div className="w-9 h-9 rounded-lg overflow-hidden bg-surface-3 flex-shrink-0">
        {song.thumbnail ? (
          <img src={proxyImg(song.thumbnail)} alt="" className="w-full h-full object-cover" loading="lazy" />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            <NoteIcon className="w-3.5 h-3.5 text-muted" />
          </div>
        )}
      </div>
      <div className="flex-1 min-w-0">
        <MarqueeText className={cn("text-xs font-medium", added ? "text-success" : "text-white")}>{song.title}</MarqueeText>
        <p className="text-[10px] text-white/40 truncate">{song.uploader}</p>
      </div>
      <div className="flex items-center gap-1 flex-shrink-0">
        <IconButton
          label="Remove from playlist"
          size="xs"
          tone="danger"
          className="opacity-0 group-hover:opacity-100 pointer-coarse:opacity-100 transition-opacity"
          onClick={(e) => { e.stopPropagation(); onRemove(); }}
        >
          <CloseIcon className="w-3 h-3" />
        </IconButton>
        {queueing && <Spinner className="w-3.5 h-3.5" />}
        {added && <CheckIcon className="w-3.5 h-3.5 text-success" />}
        {!queueing && !added && (
          <span className="text-[10px] tabular-nums text-muted w-8 text-right">{song.duration > 0 ? formatDuration(song.duration) : ""}</span>
        )}
      </div>
    </div>
  );
}
