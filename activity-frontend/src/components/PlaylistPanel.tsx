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
import { apiFetch, errorMessage } from "@/lib/api";
import {
  collabsKey as buildCollabsKey, isPlaylistDetailKey, playlistDetailKey, playlistListKey, playlistNameError,
  PLAYLIST_NAME_MAX,
} from "@/lib/playlists";
import { proxyImg, cn, joinDetails } from "@/lib/utils";
import EmptyState, { ErrorState } from "./EmptyState";
import IconButton from "./ui/IconButton";
import SegmentedControl from "./ui/SegmentedControl";
import { useConfirm } from "./ui/ConfirmButton";
import { SongRowSkeleton } from "./ui/Skeleton";
import { Spinner, BackIcon, PlusIcon, PlayIcon, TrashIcon, CloseIcon, SearchIcon, NoteIcon, PlaylistIcon, UsersIcon } from "./ui/icons";
import FavHeart from "./FavHeart";
import AddToPlaylistButton, { CopyPlaylistButton } from "./AddToPlaylistButton";
import { StatusSlot } from "./SongRow";
import { songAnnouncements } from "./QueuePanel";
import { useQueueAdd, type AddStatus } from "@/hooks/useQueueAdd";
import { PLAYLIST_PERMISSIONS, playlistCan } from "@/types";
import type { Playlist, PlaylistSong, Member, Collab, PlaylistPermission } from "@/types";

type View = "list" | "detail" | "collabs";

const SCOPES = [{ value: false, label: "Server" }, { value: true, label: "Global" }];
const KEYBOARD_CODES = { start: ["Space"], cancel: ["Escape"], end: ["Space", "Enter"] };
const MEMBER_DEBOUNCE_MS = 300;

const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

export default function PlaylistPanel() {
  const { guildId, state } = useGuildState();
  const { toast } = useToast();
  const { mutate } = useSWRConfig();
  const queue = useQueueAdd();
  const deleting = useConfirm<string>();

  const [view, setView] = useState<View>("list");
  const [globalMode, setGlobalMode] = useState(false);
  const [selectedName, setSelectedName] = useState("");
  const [selectedOwner, setSelectedOwner] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  // Row whose long-press drag is arming (touch delay constraint) — visual cue
  const [armingUrl, setArmingUrl] = useState<string | null>(null);
  // dnd-kit fires a click on the drag's mouseup — suppress row-click adds after a drag
  const dragOccurred = useRef(false);

  const [memberSearch, setMemberSearch] = useState("");
  const [memberQuery, setMemberQuery] = useState("");
  const [newCollabPermission, setNewCollabPermission] = useState<PlaylistPermission>("edit");

  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates, keyboardCodes: KEYBOARD_CODES }),
  );

  // Debounced in state, so hiding the panel mid-search can't leave it stuck.
  useEffect(() => {
    const t = setTimeout(() => setMemberQuery(memberSearch.trim()), MEMBER_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [memberSearch]);

  const gm = `global_mode=${globalMode}`;
  const listKey = playlistListKey(guildId, globalMode);
  const detailKey = view === "detail" && selectedName
    ? playlistDetailKey(guildId, selectedName, globalMode, selectedOwner)
    : null;
  const collabsKey = view === "collabs" && selectedName ? buildCollabsKey(guildId, selectedName, globalMode) : null;
  const membersKey = view === "collabs" && memberQuery
    ? `/api/guild/${guildId}/playlists/members?q=${encodeURIComponent(memberQuery)}`
    : null;

  const list = useSWR<{ playlists: Playlist[] }>(listKey);
  const detail = useSWR<{ songs: PlaylistSong[] }>(detailKey);
  const collabsQuery = useSWR<{ collaborators: Collab[]; levels: PlaylistPermission[] }>(collabsKey);
  const membersQuery = useSWR<{ members: Member[] }>(membersKey, { keepPreviousData: true, revalidateOnFocus: false });
  const playlists = list.data?.playlists ?? [];
  const songs = detail.data?.songs ?? [];
  const collabs = collabsQuery.data?.collaborators ?? [];
  const levels = collabsQuery.data?.levels ?? PLAYLIST_PERMISSIONS;
  const collabIds = new Set(collabs.map(c => c.id));
  const members = memberSearch.trim() ? (membersQuery.data?.members ?? []).filter(m => !collabIds.has(m.id)) : [];
  const memberSearching = !!memberSearch.trim() && (memberSearch.trim() !== memberQuery || membersQuery.isLoading);

  // The list carries the grant, so the detail views need no extra fetch.
  const selected = playlists.find(p => p.name === selectedName && p.owner_id === selectedOwner);
  const permission = selected?.permission ?? "view";
  const isOwner = permission === "owner";
  const canAppend = playlistCan(permission, "append");
  const canEdit = playlistCan(permission, "edit");

  const refreshDetail = (name: string, isGlobal: boolean) =>
    mutate((key: unknown) => isPlaylistDetailKey(key, guildId, name, isGlobal));

  const openPlaylist = (name: string, ownerId: string | null) => {
    setSelectedOwner(ownerId);
    setSelectedName(name);
    setView("detail");
  };

  const handleCreate = () => {
    const problem = playlistNameError(newName);
    if (problem) {
      setNameError(problem);
      return;
    }
    const name = newName.trim();
    apiFetch(`/api/guild/${guildId}/playlists`, { method: "POST", body: JSON.stringify({ name, global_mode: globalMode }) })
      .then(
        () => {
          toast(`Created "${name}"`, "success");
          setNewName("");
          setShowCreate(false);
          mutate(listKey);
        },
        (e: unknown) => toast(errorMessage(e, "Couldn't create the playlist"), "error"),
      );
  };

  const handleDelete = (name: string) => {
    if (!deleting.press(name)) return;
    const rest = playlists.filter(p => !(p.name === name && p.permission === "owner"));
    mutate(listKey, { playlists: rest }, { revalidate: false });
    apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}?${gm}`, { method: "DELETE" })
      .then(
        () => {
          toast(`Deleted "${name}"`, "success");
          if (view !== "list") setView("list");
        },
        (e: unknown) => toast(errorMessage(e, "Couldn't delete the playlist"), "error"),
      )
      .then(() => mutate(listKey));
  };

  const handleLoad = (name: string, ownerId: string | null = null) => {
    apiFetch<{ added: number; total: number }>(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/load`, {
      method: "POST", body: JSON.stringify({ global_mode: globalMode, owner_id: ownerId }),
    }).then(
      (r) => toast(`Loaded ${r.added} of ${r.total} songs`, "success"),
      (e: unknown) => toast(errorMessage(e, "Couldn't load the playlist"), "error"),
    );
  };

  const handleAddCurrent = (name: string, ownerId: string | null = null) => {
    if (!state.current) {
      toast("Nothing is playing", "info");
      return;
    }
    apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/add`, {
      method: "POST", body: JSON.stringify({ song_url: state.current.webpage_url, global_mode: globalMode, owner_id: ownerId }),
    }).then(
      () => {
        toast(`Saved to "${name}"`, "success");
        mutate(listKey);
        refreshDetail(name, globalMode);
      },
      (e: unknown) => toast(errorMessage(e, "Couldn't save it"), "error"),
    );
  };

  // The add endpoint appends, so restoring the original slot takes a follow-up
  // move. Keys are rebuilt from the captured playlist: the user may have
  // navigated elsewhere while the undo toast was up.
  const undoRemoveSong = (song: PlaylistSong, pos: number, name: string, isGlobal: boolean, ownerId: string | null) => {
    const base = `/api/guild/${guildId}/playlists/${encodeURIComponent(name)}`;
    apiFetch<{ song_count: number }>(`${base}/add`, {
      method: "POST",
      body: JSON.stringify({
        song_url: song.webpage_url,
        global_mode: isGlobal,
        owner_id: ownerId,
        song: { title: song.title, uploader: song.uploader, duration: song.duration, thumbnail: song.thumbnail || "" },
      }),
    })
      .then((res) => {
        const from = (res.song_count ?? 0) - 1;
        if (from <= pos) return;
        return apiFetch(`${base}/move`, {
          method: "POST",
          body: JSON.stringify({ from_pos: from, to_pos: pos, global_mode: isGlobal, owner_id: ownerId, webpage_url: song.webpage_url }),
        });
      })
      .catch((e: unknown) => toast(errorMessage(e, "Undo failed"), "error"))
      .then(() => {
        refreshDetail(name, isGlobal);
        mutate(playlistListKey(guildId, isGlobal));
      });
  };

  const handleRemoveSong = (song: PlaylistSong, pos: number) => {
    if (!detailKey) return;
    const key = detailKey;
    const name = selectedName;
    const isGlobal = globalMode;
    const ownerId = selectedOwner;
    mutate(key, { songs: songs.filter((_, i) => i !== pos) }, { revalidate: false });
    const owner = ownerId ? `&owner_id=${ownerId}` : "";
    apiFetch(
      `/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/${pos}?${gm}${owner}&url=${encodeURIComponent(song.webpage_url)}`,
      { method: "DELETE" },
    )
      .then(
        () => toast("Removed from the playlist", "success", { label: "Undo", onClick: () => undoRemoveSong(song, pos, name, isGlobal, ownerId) }),
        (e: unknown) => toast(errorMessage(e, "Couldn't remove it"), "error"),
      )
      .then(() => {
        mutate(key);
        mutate(listKey);
      });
  };

  const handleQueueSong = (song: PlaylistSong) => {
    if (dragOccurred.current) return;
    queue.add(song.webpage_url, song.title);
  };

  // Only cue delay-based (long-press) arming — the MouseSensor's distance
  // constraint would flash the cue on every click.
  const handleSongDragPending = (event: DragPendingEvent) => {
    if ("delay" in event.constraint) setArmingUrl(String(event.id));
  };
  const handleSongDragAbort = () => setArmingUrl(null);

  const handleSongDragStart = () => { setArmingUrl(null); dragOccurred.current = true; };

  const handleSongDragEnd = (event: DragEndEvent) => {
    setTimeout(() => { dragOccurred.current = false; }, 100);
    const { active, over } = event;
    if (!detailKey || !over || active.id === over.id) return;
    const key = detailKey;
    const from = songs.findIndex(s => s.webpage_url === active.id);
    const to = songs.findIndex(s => s.webpage_url === over.id);
    if (from < 0 || to < 0) return;
    mutate(key, { songs: arrayMove(songs, from, to) }, { revalidate: false });
    apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/move`, {
      method: "POST",
      body: JSON.stringify({ from_pos: from, to_pos: to, global_mode: globalMode, owner_id: selectedOwner, webpage_url: String(active.id) }),
    }).then(
      () => mutate(listKey), // first song may have changed -> playlist cover updates
      (e: unknown) => {
        toast(errorMessage(e, "Couldn't move it"), "error");
        mutate(key);
      },
    );
  };

  const openCollabs = (name: string, ownerId: string | null) => {
    setSelectedOwner(ownerId);
    setSelectedName(name);
    setView("collabs");
    setMemberSearch("");
    setMemberQuery("");
  };

  const handleAddCollab = (member: Member) => {
    if (!collabsKey) return;
    const key = collabsKey;
    const level = newCollabPermission;
    apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/collabs`, {
      method: "POST", body: JSON.stringify({ user_id: member.id, global_mode: globalMode, permission: level }),
    }).then(
      () => {
        toast(`Added ${member.display_name}`, "success");
        mutate(key, {
          collaborators: [...collabs, { id: member.id, display_name: member.display_name, avatar: member.avatar, permission: level }],
          levels,
        }, { revalidate: false });
        setMemberSearch("");
      },
      (e: unknown) => toast(errorMessage(e, "Couldn't add them"), "error"),
    );
  };

  const handleSetCollabPermission = (collab: Collab, level: PlaylistPermission) => {
    if (!collabsKey || collab.permission === level) return;
    const key = collabsKey;
    mutate(key, { collaborators: collabs.map(c => c.id === collab.id ? { ...c, permission: level } : c), levels }, { revalidate: false });
    apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/collabs/${collab.id}`, {
      method: "POST", body: JSON.stringify({ permission: level, global_mode: globalMode }),
    })
      .catch((e: unknown) => toast(errorMessage(e, "Couldn't change their access"), "error"))
      .then(() => mutate(key));
  };

  const handleRemoveCollab = (collab: Collab) => {
    if (!collabsKey) return;
    const key = collabsKey;
    mutate(key, { collaborators: collabs.filter(c => c.id !== collab.id), levels }, { revalidate: false });
    apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/collabs/${collab.id}?${gm}`, { method: "DELETE" })
      .then(
        () => toast(`Removed ${collab.display_name}`, "success"),
        (e: unknown) => {
          toast(errorMessage(e, "Couldn't remove them"), "error");
          mutate(key);
        },
      );
  };

  // ── Collabs view ──────────────────────────────────────────────
  if (view === "collabs") {
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center gap-2 px-4 py-3 border-b border-fg/[0.06] flex-shrink-0">
          <IconButton label="Back" onClick={() => openPlaylist(selectedName, selectedOwner)}>
            <BackIcon />
          </IconButton>
          <h3 className="text-sm font-semibold text-fg truncate flex-1">{selectedName}</h3>
          <span className="text-xs text-muted">Collaborators</span>
        </div>

        <div className="px-4 py-3 border-b border-fg/[0.08] flex-shrink-0">
          <div className="flex items-center gap-2.5 bg-surface-3/60 rounded-xl border border-fg/[0.08] focus-within:border-accent/40 transition-[border-color] duration-200 px-3.5 py-2.5">
            <SearchIcon className="w-4 h-4 text-muted flex-shrink-0" />
            <input
              type="search" value={memberSearch} onChange={(e) => setMemberSearch(e.target.value)}
              placeholder="Search members to add…"
              aria-label="Search members"
              enterKeyHint="search" autoCorrect="off" autoCapitalize="off" spellCheck={false}
              className="flex-1 bg-transparent text-fg text-base sm:text-sm outline-none placeholder:text-muted min-w-0 [&::-webkit-search-cancel-button]:hidden"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {memberSearch.trim() && (
            <div className="mb-3">
              <div className="flex items-center gap-2 mt-2 px-1">
                <span className="text-xs text-muted">Add as</span>
                <LevelPicker levels={levels} value={newCollabPermission} onChange={setNewCollabPermission} label="Access for new collaborators" />
              </div>
              {memberSearching && members.length === 0 ? (
                <div className="flex justify-center py-4"><Spinner /></div>
              ) : membersQuery.error && !membersQuery.data ? (
                <p className="text-xs text-muted text-center py-3">Couldn&apos;t search members</p>
              ) : members.length > 0 ? (
                <div className={cn("flex flex-col gap-1 mt-2", memberSearching && "opacity-60")}>
                  {members.map(m => (
                    <button
                      key={m.id} type="button" onClick={() => handleAddCollab(m)}
                      className="w-full flex items-center gap-2.5 p-2 rounded-xl bg-accent/5 border border-accent/10 hover:bg-accent/10 transition-[background-color] duration-150 text-left"
                    >
                      <Avatar url={m.avatar} name={m.display_name} />
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium text-fg truncate">{m.display_name}</p>
                        <p className="text-xs text-muted truncate">@{m.username}</p>
                      </div>
                      <PlusIcon className="w-4 h-4 text-accent flex-shrink-0" />
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-muted text-center py-3">No members found</p>
              )}
            </div>
          )}

          {collabsQuery.isLoading ? (
            <div className="flex justify-center py-8"><Spinner className="w-5 h-5" /></div>
          ) : collabsQuery.error && !collabsQuery.data ? (
            <ErrorState compact message="Couldn't load collaborators" onRetry={() => collabsQuery.mutate()} />
          ) : collabs.length === 0 ? (
            <EmptyState
              compact
              className=""
              icon={<UsersIcon className="w-6 h-6" strokeWidth={1.5} />}
              title="No collaborators yet"
              subtitle="Search for members above to add them"
            />
          ) : (
            <div className="flex flex-col gap-1 mt-1">
              <p className="text-xs text-muted px-1 mb-1">{collabs.length} collaborator{collabs.length !== 1 ? "s" : ""}</p>
              {collabs.map(c => (
                <div key={c.id} className="flex items-center gap-2 p-2 rounded-xl bg-fg/[0.02] border border-fg/[0.04] group">
                  <Avatar url={c.avatar} name={c.display_name} />
                  <p className="text-sm font-medium text-fg truncate flex-1 min-w-0">{c.display_name}</p>
                  <LevelPicker levels={levels} value={c.permission} onChange={lv => handleSetCollabPermission(c, lv)} label={`Access for ${c.display_name}`} />
                  <IconButton
                    label={`Remove ${c.display_name}`}
                    size="xs"
                    tone="danger"
                    className="opacity-0 group-hover:opacity-100 group-has-focus-visible:opacity-100 pointer-coarse:opacity-100"
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
    const titles = new Map(songs.map(s => [s.webpage_url, s.title]));
    const announcements = songAnnouncements(id => `"${titles.get(String(id)) ?? "song"}"`);
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center gap-1 px-4 py-2 border-b border-fg/[0.06] flex-shrink-0">
          <IconButton label="Back" onClick={() => setView("list")}>
            <BackIcon />
          </IconButton>
          <h3 className="text-sm font-semibold text-fg truncate flex-1 pl-1">{selectedName}</h3>
          <CopyPlaylistButton sourceName={selectedName} sourceGlobal={globalMode} sourceOwnerId={selectedOwner} />
          {isOwner && <HeaderBtn onClick={() => openCollabs(selectedName, selectedOwner)} title="Manage collaborators">Collabs</HeaderBtn>}
          {canAppend && <HeaderBtn onClick={() => handleAddCurrent(selectedName, selectedOwner)} title="Save the current song" className="text-accent hover:text-accent">+ Save</HeaderBtn>}
          <HeaderBtn onClick={() => handleLoad(selectedName, selectedOwner)} title="Add every song to the queue">Load</HeaderBtn>
        </div>

        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {detail.isLoading ? (
            <SongRowSkeleton className="mt-2" compact />
          ) : detail.error && !detail.data ? (
            <ErrorState compact message="Couldn't load this playlist" onRetry={() => detail.mutate()} />
          ) : songs.length === 0 ? (
            <EmptyState
              compact
              icon={<PlaylistIcon className="w-6 h-6" strokeWidth={1.5} />}
              title="Empty playlist"
              subtitle={canAppend ? "Save songs to it from any song's playlist button" : undefined}
              action={canAppend && state.current ? { label: "Save current song", onClick: () => handleAddCurrent(selectedName, selectedOwner) } : undefined}
            />
          ) : (
            <DndContext
              sensors={sensors}
              collisionDetection={closestCenter}
              accessibility={{ announcements }}
              onDragPending={handleSongDragPending}
              onDragAbort={handleSongDragAbort}
              onDragStart={handleSongDragStart}
              onDragEnd={handleSongDragEnd}
            >
              <SortableContext items={songs.map(s => s.webpage_url)} strategy={verticalListSortingStrategy}>
                <div className="flex flex-col gap-1 mt-2">
                  {songs.map((song, i) => (
                    <SortableSongRow
                      key={song.webpage_url}
                      song={song}
                      index={i}
                      arming={armingUrl === song.webpage_url}
                      status={queue.status(song.webpage_url)}
                      canEdit={canEdit}
                      onQueue={handleQueueSong}
                      onRemove={handleRemoveSong}
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
      <div className="flex items-center gap-2 px-4 py-3 border-b border-fg/[0.08] flex-shrink-0">
        <SegmentedControl label="Playlist scope" options={SCOPES} value={globalMode} onChange={setGlobalMode} className="flex-1" />
        <IconButton
          label="Create playlist"
          aria-expanded={showCreate}
          onClick={() => { setShowCreate(!showCreate); setNameError(null); }}
        >
          <PlusIcon />
        </IconButton>
      </div>

      {showCreate && (
        <div className="px-4 py-2.5 border-b border-fg/[0.08] flex flex-col gap-1">
          <div className="flex gap-2">
            <input
              type="text" value={newName}
              onChange={(e) => { setNewName(e.target.value); setNameError(null); }}
              placeholder="Playlist name…"
              aria-label="New playlist name"
              aria-invalid={!!nameError || undefined}
              autoFocus
              maxLength={PLAYLIST_NAME_MAX}
              onKeyDown={(e) => e.key === "Enter" && handleCreate()}
              enterKeyHint="done"
              className={cn(
                "flex-1 bg-surface-3/60 text-fg text-base sm:text-sm rounded-xl px-3.5 py-2 outline-none border border-fg/[0.08] focus:border-accent/40 placeholder:text-muted min-w-0",
                nameError && "border-danger/60",
              )}
            />
            <button type="button" onClick={handleCreate} disabled={!newName.trim()} className="text-xs font-semibold px-4 py-2 rounded-xl bg-accent text-white hover:bg-accent/80 disabled:opacity-40 transition-[background-color,opacity] duration-150">Create</button>
          </div>
          {nameError && <p className="text-xs text-danger px-1" role="alert">{nameError}</p>}
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-3 pb-3">
        {list.isLoading ? (
          <SongRowSkeleton className="mt-2" count={4} compact />
        ) : list.error && !list.data ? (
          <ErrorState compact message="Couldn't load your playlists" onRetry={() => list.mutate()} />
        ) : playlists.length === 0 ? (
          <EmptyState
            compact
            icon={<PlaylistIcon className="w-6 h-6" strokeWidth={1.5} />}
            title="No playlists yet"
            subtitle="Create one to save songs"
            action={showCreate ? undefined : { label: "New playlist", onClick: () => setShowCreate(true) }}
          />
        ) : (
          <div className="flex flex-col gap-1.5 mt-2">
            {playlists.map(pl => {
              const confirming = deleting.armed === pl.name && pl.permission === "owner";
              return (
                <div key={`${pl.name}:${pl.owner_id}`} className="relative flex items-center gap-3 p-2.5 rounded-2xl bg-fg/[0.02] border border-fg/[0.04] hover:bg-fg/[0.06] hover:border-fg/[0.08] transition-[background-color,border-color] duration-150 group">
                  <button type="button" onClick={() => openPlaylist(pl.name, pl.owner_id)} className="flex-1 flex items-center gap-3 min-w-0 text-left rounded-xl">
                    <div className="w-10 h-10 rounded-xl bg-surface-3 overflow-hidden flex items-center justify-center flex-shrink-0">
                      {pl.thumbnail ? (
                        <img src={proxyImg(pl.thumbnail)} alt="" className="w-full h-full object-cover" loading="lazy" />
                      ) : (
                        <PlaylistIcon className="w-5 h-5 text-muted" strokeWidth={1.5} />
                      )}
                    </div>
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-fg truncate">{pl.name}</p>
                      <p className="text-xs text-muted truncate">
                        {joinDetails(
                          `${pl.song_count} song${pl.song_count !== 1 ? "s" : ""}`,
                          pl.owner && `Shared by ${pl.owner}`,
                          pl.owner && capitalize(pl.permission),
                        )}
                      </p>
                    </div>
                  </button>
                  <div className={cn(
                    "flex items-center gap-1 pointer-coarse:gap-2 transition-opacity flex-shrink-0",
                    confirming ? "opacity-100" : "opacity-0 group-hover:opacity-100 group-has-focus-visible:opacity-100 pointer-coarse:opacity-100",
                  )}>
                    {playlistCan(pl.permission, "append") && (
                      <IconButton label="Save the current song" tone="accent" onClick={() => handleAddCurrent(pl.name, pl.owner_id)}>
                        <PlusIcon className="w-3.5 h-3.5" />
                      </IconButton>
                    )}
                    <IconButton label="Add every song to the queue" tone="accent" onClick={() => handleLoad(pl.name, pl.owner_id)}>
                      <PlayIcon className="w-3.5 h-3.5" />
                    </IconButton>
                    {pl.permission === "owner" && (
                      <>
                        <IconButton label="Collaborators" onClick={() => openCollabs(pl.name, pl.owner_id)}>
                          <UsersIcon className="w-3.5 h-3.5" />
                        </IconButton>
                        <IconButton
                          label={confirming ? `Press again to delete "${pl.name}"` : `Delete "${pl.name}"`}
                          tone="danger"
                          className={cn(confirming && "bg-danger! text-white!")}
                          onClick={() => handleDelete(pl.name)}
                        >
                          <TrashIcon className="w-3.5 h-3.5" />
                        </IconButton>
                      </>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function Avatar({ url, name }: { url: string | null; name: string }) {
  return (
    <div className="w-8 h-8 rounded-full bg-surface-3 overflow-hidden flex-shrink-0">
      {url ? <img src={proxyImg(url)} alt="" className="w-full h-full object-cover" /> : (
        <div className="w-full h-full flex items-center justify-center text-muted text-xs font-bold">{name[0]}</div>
      )}
    </div>
  );
}

function HeaderBtn({ children, onClick, title, className }: {
  children: React.ReactNode; onClick: () => void; title: string; className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={cn("min-h-8 text-xs font-medium px-2 rounded-lg text-muted hover:text-fg hover:bg-fg/[0.06] transition-colors flex-shrink-0", className)}
    >
      {children}
    </button>
  );
}

function LevelPicker({ levels, value, onChange, label }: {
  levels: PlaylistPermission[]; value: PlaylistPermission; onChange: (level: PlaylistPermission) => void; label: string;
}) {
  return (
    <SegmentedControl
      label={label}
      size="sm"
      className="flex-shrink-0"
      options={levels.map(lv => ({ value: lv, label: capitalize(lv) }))}
      value={value}
      onChange={onChange}
    />
  );
}

// Click or Enter adds to the queue (same as Search/History/Favorites rows); Space or drag reorders.
function SortableSongRow({ song, index, arming, status, canEdit, onQueue, onRemove }: {
  song: PlaylistSong; index: number; arming: boolean; status: AddStatus; canEdit: boolean;
  onQueue: (song: PlaylistSong) => void; onRemove: (song: PlaylistSong, index: number) => void;
}) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({
    id: song.webpage_url,
    disabled: !canEdit,
  });
  // The row is the activator: key presses on its buttons (or in their popovers) don't pick it up.
  const rowRef = (node: HTMLDivElement | null) => {
    setNodeRef(node);
    setActivatorNodeRef(node);
  };
  const added = status === "added";

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget && e.key === "Enter" && !isDragging) {
      e.preventDefault();
      if (status === "default") onQueue(song);
      return;
    }
    listeners?.onKeyDown?.(e);
  };

  return (
    <div
      ref={rowRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      {...attributes}
      {...listeners}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onClick={() => { if (status === "default") onQueue(song); }}
      aria-label={`${song.title}. Press Enter to add it to the queue`}
      aria-busy={status === "pending" || undefined}
      title="Add to queue"
      className={cn(
        "flex items-center gap-2.5 p-2 rounded-2xl border group cursor-pointer",
        "transition-[background-color,border-color,opacity,transform] duration-150",
        isDragging ? "opacity-40 border-accent/40 z-10 relative bg-fg/[0.02]"
          : arming ? "scale-[0.97] border-accent/40 bg-fg/[0.05]"
          : added ? "bg-success/[0.06] border-success/20"
          : "bg-fg/[0.02] border-fg/[0.04] hover:bg-fg/[0.06]"
      )}
    >
      <span className="min-w-6 text-center text-xs tabular-nums text-muted flex-shrink-0">{index + 1}</span>
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
        <MarqueeText className={cn("text-sm font-medium", added ? "text-success" : "text-fg")}>{song.title}</MarqueeText>
        <p className="text-xs text-muted truncate mt-0.5">{song.uploader}</p>
      </div>
      <div className="flex items-center gap-1 flex-shrink-0" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-1.5 pointer-coarse:gap-2.5 opacity-0 group-hover:opacity-100 group-has-focus-visible:opacity-100 pointer-coarse:opacity-100 transition-opacity">
          <AddToPlaylistButton song={song} />
          <FavHeart
            webpageUrl={song.webpage_url}
            title={song.title}
            duration={song.duration}
            thumbnail={song.thumbnail}
            uploader={song.uploader}
          />
          {canEdit && (
            <IconButton
              label="Remove from the playlist"
              size="xs"
              tone="danger"
              onClick={() => onRemove(song, index)}
            >
              <CloseIcon className="w-3 h-3" />
            </IconButton>
          )}
        </div>
        <StatusSlot status={status} duration={song.duration} />
      </div>
    </div>
  );
}
