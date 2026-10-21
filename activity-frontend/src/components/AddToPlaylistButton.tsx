"use client";

import { useRef, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { useGuildId } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { apiFetch, errorMessage } from "@/lib/api";
import { isPlaylistDetailKey, playlistListKey, playlistNameError, PLAYLIST_NAME_MAX } from "@/lib/playlists";
import IconButton from "./ui/IconButton";
import Popover from "./ui/Popover";
import SegmentedControl from "./ui/SegmentedControl";
import { CopyIcon, PlaylistAddIcon } from "./ui/icons";
import { cn } from "@/lib/utils";
import { playlistCan, type Playlist } from "@/types";

export interface PlaylistCandidate {
  webpage_url: string;
  title: string;
  duration?: number;
  thumbnail?: string;
  uploader?: string;
}

// Resolves true when the popover should close (i.e. the save succeeded).
type PickHandler = (name: string, globalMode: boolean, ownerId: string | null) => Promise<boolean>;

interface BulkSaveResult {
  added: number;
  skipped: number;
  dropped?: number;
  capped?: boolean;
}

function savedMessage(verb: "Added" | "Copied", res: BulkSaveResult, name: string): string {
  if (res.added === 0 && res.skipped > 0 && !res.dropped) {
    return `Nothing new — all ${res.skipped} song${res.skipped === 1 ? " is" : "s are"} already in "${name}"`;
  }
  const parts = [];
  if (res.skipped > 0) parts.push(`${res.skipped} already there`);
  if (res.dropped) parts.push(`${res.dropped} dropped, playlist full`);
  if (res.capped) parts.push("Spotify only shares the first 100");
  const note = parts.length ? ` (${parts.join(", ")})` : "";
  return `${verb} ${res.added} song${res.added === 1 ? "" : "s"} to "${name}"${note}`;
}

function PickerButton({ label, size, className, disabled, exclude, onPick, children }: {
  label: string;
  size: "xs" | "sm" | "md";
  className?: string;
  disabled?: boolean;
  exclude?: { name: string; globalMode: boolean; ownerId?: string | null };
  onPick: PickHandler;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const anchorRef = useRef<HTMLButtonElement>(null);
  const close = () => setOpen(false);

  return (
    <>
      <IconButton
        ref={anchorRef}
        label={label}
        size={size}
        tone="accent"
        className={className}
        disabled={disabled}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={(e) => { e.stopPropagation(); if (!disabled) setOpen(o => !o); }}
      >
        {children}
      </IconButton>
      <Popover open={open} onClose={close} anchorRef={anchorRef} label={label} className="w-60 p-2">
        {/* Mounted only while open, so the playlist fetch is on demand */}
        <PlaylistPicker onPick={onPick} onDone={close} exclude={exclude} />
      </Popover>
    </>
  );
}

/**
 * Save any song — search result, queue/history/favorites row, now playing — to
 * a playlist of the user's choice, without it having to be the current song.
 */
export default function AddToPlaylistButton({ song, size = "xs", className }: {
  song: PlaylistCandidate;
  size?: "xs" | "sm" | "md";
  className?: string;
}) {
  const { guildId } = useGuildId();
  const { toast } = useToast();

  const onPick: PickHandler = (name, globalMode, ownerId) =>
    apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/add`, {
      method: "POST",
      body: JSON.stringify({
        song_url: song.webpage_url,
        global_mode: globalMode,
        owner_id: ownerId,
        song: {
          title: song.title,
          duration: song.duration ?? 0,
          thumbnail: song.thumbnail ?? "",
          uploader: song.uploader ?? "",
        },
      }),
    }).then(
      () => {
        toast(`Saved to "${name}"`, "success");
        return true;
      },
      (e: unknown) => {
        toast(errorMessage(e, "Couldn't save it"), "error");
        return false;
      },
    );

  return (
    <PickerButton label="Save to playlist" size={size} className={className} onPick={onPick}>
      <PlaylistAddIcon className={size === "md" ? "w-4 h-4" : "w-3.5 h-3.5"} />
    </PickerButton>
  );
}

/**
 * Append one playlist's songs into another of the user's choice; the picker's
 * Server/Global tab selects the destination scope, duplicates are skipped.
 */
export function CopyPlaylistButton({ sourceName, sourceGlobal, sourceOwnerId, size = "sm", className }: {
  sourceName: string;
  sourceGlobal: boolean;
  sourceOwnerId?: string | null;
  size?: "xs" | "sm" | "md";
  className?: string;
}) {
  const { guildId } = useGuildId();
  const { toast } = useToast();

  const onPick: PickHandler = (name, globalMode, targetOwnerId) =>
    apiFetch<BulkSaveResult>(`/api/guild/${guildId}/playlists/${encodeURIComponent(sourceName)}/copy`, {
      method: "POST",
      body: JSON.stringify({
        target: name,
        global_mode: sourceGlobal,
        target_global_mode: globalMode,
        owner_id: sourceOwnerId ?? null,
        target_owner_id: targetOwnerId,
      }),
    }).then(
      (res) => {
        toast(savedMessage("Copied", res, name), "success");
        return true;
      },
      (e: unknown) => {
        toast(errorMessage(e, "Couldn't copy the playlist"), "error");
        return false;
      },
    );

  return (
    <PickerButton
      label="Copy to playlist"
      size={size}
      className={className}
      exclude={{ name: sourceName, globalMode: sourceGlobal, ownerId: sourceOwnerId }}
      onPick={onPick}
    >
      <CopyIcon className="w-4 h-4" />
    </PickerButton>
  );
}

/**
 * Save the whole session (current song + queue) to a playlist, mirroring the
 * bot's `/playlist add-all-queue`: duplicates against the playlist are skipped.
 */
export function AddQueueToPlaylistButton({ disabled, size = "sm", className }: {
  disabled?: boolean;
  size?: "xs" | "sm" | "md";
  className?: string;
}) {
  const { guildId } = useGuildId();
  const { toast } = useToast();

  const onPick: PickHandler = (name, globalMode, ownerId) =>
    apiFetch<BulkSaveResult>(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/add-queue`, {
      method: "POST",
      body: JSON.stringify({ global_mode: globalMode, owner_id: ownerId }),
    }).then(
      (res) => {
        toast(savedMessage("Added", res, name), "success");
        return true;
      },
      (e: unknown) => {
        toast(errorMessage(e, "Couldn't save the queue"), "error");
        return false;
      },
    );

  return (
    <PickerButton label="Save queue to playlist" size={size} className={className} disabled={disabled} onPick={onPick}>
      <PlaylistAddIcon className="w-4 h-4" />
    </PickerButton>
  );
}

/** Save every song from a playlist or album link (e.g. a search result) to a playlist. */
export function AddLinkToPlaylistButton({ url, asList = false, size = "md", className }: {
  url: string;
  // url is the playlist or Mix a searched video came from (search's list_url)
  asList?: boolean;
  size?: "xs" | "sm" | "md";
  className?: string;
}) {
  const { guildId } = useGuildId();
  const { toast } = useToast();

  const onPick: PickHandler = (name, globalMode, ownerId) =>
    apiFetch<BulkSaveResult>(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/add-url`, {
      method: "POST",
      body: JSON.stringify({ url, global_mode: globalMode, owner_id: ownerId, as_list: asList }),
    }).then(
      (res) => {
        toast(savedMessage("Added", res, name), "success");
        return true;
      },
      (e: unknown) => {
        toast(errorMessage(e, "Couldn't save these songs"), "error");
        return false;
      },
    );

  return (
    <PickerButton label="Save all to playlist" size={size} className={className} onPick={onPick}>
      <PlaylistAddIcon className="w-4 h-4" />
    </PickerButton>
  );
}

const SCOPES = [{ value: false, label: "Server" }, { value: true, label: "Global" }];

function PlaylistPicker({ onPick, onDone, exclude }: {
  onPick: PickHandler;
  onDone: () => void;
  exclude?: { name: string; globalMode: boolean; ownerId?: string | null };
}) {
  const { guildId } = useGuildId();
  const { toast } = useToast();
  const { mutate } = useSWRConfig();

  const [globalMode, setGlobalMode] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const listKey = playlistListKey(guildId, globalMode);
  const { data, error, isLoading, mutate: reload } = useSWR<{ playlists: Playlist[] }>(listKey);
  // Every picker target is written to, so a view-only share is not a candidate.
  const playlists = (data?.playlists ?? []).filter(p =>
    playlistCan(p.permission, "append")
    && !(exclude && p.name === exclude.name && globalMode === exclude.globalMode
      && (!exclude.ownerId || p.owner_id === exclude.ownerId))
  );

  const pick = (name: string, ownerId: string | null) => {
    if (busy) return;
    setBusy(true);
    onPick(name, globalMode, ownerId).then((ok) => {
      setBusy(false);
      if (!ok) return;
      mutate(listKey);
      // The open detail view's key carries an owner_id; match it with or without one.
      mutate((key: unknown) => isPlaylistDetailKey(key, guildId, name, globalMode));
      onDone();
    });
  };

  const createAndPick = () => {
    const problem = playlistNameError(newName);
    if (problem) {
      setNameError(problem);
      return;
    }
    if (busy) return;
    const name = newName.trim();
    setBusy(true);
    apiFetch(`/api/guild/${guildId}/playlists`, {
      method: "POST",
      body: JSON.stringify({ name, global_mode: globalMode }),
    }).then(
      () => {
        setBusy(false);
        setNewName("");
        setCreating(false);
        pick(name, null);
      },
      (e: unknown) => {
        setBusy(false);
        toast(errorMessage(e, "Couldn't create the playlist"), "error");
      },
    );
  };

  return (
    <div className="flex flex-col gap-1.5">
      <SegmentedControl
        label="Playlist scope"
        size="sm"
        options={SCOPES}
        value={globalMode}
        onChange={g => { setGlobalMode(g); setCreating(false); }}
      />

      <div className="max-h-56 overflow-y-auto flex flex-col gap-0.5" aria-busy={isLoading || undefined}>
        {isLoading && (
          <div className="flex flex-col gap-1 py-1 animate-pulse" aria-hidden="true">
            {[0, 1, 2].map(i => <div key={i} className="h-8 rounded-lg bg-fg/[0.04]" />)}
          </div>
        )}
        {!isLoading && error && !data && (
          <div className="flex items-center justify-between gap-2 px-2 py-2">
            <p className="text-xs text-muted">Couldn&apos;t load playlists</p>
            <button type="button" onClick={() => reload()} className="text-xs font-semibold text-accent hover:underline">
              Try again
            </button>
          </div>
        )}
        {!isLoading && data && playlists.length === 0 && (
          <p className="text-xs text-muted px-2 py-2">No {globalMode ? "global" : "server"} playlists yet</p>
        )}
        {playlists.map(p => (
          <button
            key={`${p.name}:${p.owner_id}`}
            type="button"
            disabled={busy}
            onClick={() => pick(p.name, p.owner_id)}
            className="flex items-center justify-between gap-2 px-2 py-1.5 min-h-9 rounded-lg text-left text-fg/90 hover:bg-fg/[0.08] disabled:opacity-50"
          >
            <span className="min-w-0">
              <span className="block text-sm truncate">{p.name}</span>
              {p.owner && <span className="block text-xs text-muted truncate">Shared by {p.owner}</span>}
            </span>
            {typeof p.song_count === "number" && (
              <span className="text-xs text-muted tabular-nums flex-shrink-0">{p.song_count}</span>
            )}
          </button>
        ))}
      </div>

      {creating ? (
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-1">
            <input
              autoFocus
              value={newName}
              onChange={e => { setNewName(e.target.value); setNameError(null); }}
              onKeyDown={e => {
                if (e.key === "Enter") createAndPick();
                if (e.key === "Escape") { e.stopPropagation(); setCreating(false); setNewName(""); setNameError(null); }
              }}
              placeholder="Playlist name"
              aria-label="New playlist name"
              aria-invalid={!!nameError || undefined}
              maxLength={PLAYLIST_NAME_MAX}
              className={cn(
                "flex-1 min-w-0 h-8 px-2 rounded-lg bg-fg/[0.06] text-base sm:text-sm text-fg placeholder:text-muted outline-none focus:bg-fg/[0.1]",
                nameError && "ring-1 ring-danger/60",
              )}
            />
            <button
              type="button"
              onClick={createAndPick}
              disabled={!newName.trim() || busy}
              className="h-8 px-2.5 rounded-lg bg-accent text-white text-xs font-semibold disabled:opacity-40"
            >
              Save
            </button>
          </div>
          {nameError && <p className="text-xs text-danger px-1" role="alert">{nameError}</p>}
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="px-2 py-1.5 min-h-8 rounded-lg text-left text-xs font-medium text-accent hover:bg-accent/10"
        >
          + New playlist
        </button>
      )}
    </div>
  );
}
