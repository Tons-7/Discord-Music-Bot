"use client";

import { useRef, useState, useCallback } from "react";
import useSWR, { useSWRConfig } from "swr";
import { useGuildState } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { apiFetch } from "@/lib/api";
import IconButton from "./ui/IconButton";
import Popover from "./ui/Popover";
import { cn } from "@/lib/utils";

export interface PlaylistCandidate {
  webpage_url: string;
  title: string;
  duration?: number;
  thumbnail?: string;
  uploader?: string;
  url?: string;
}

interface Playlist {
  name: string;
  song_count?: number;
}

function PlaylistAddIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h11M4 11h11M4 16h6m5 2h6m-3-3v6" />
    </svg>
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
  const [open, setOpen] = useState(false);
  const anchorRef = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);

  return (
    <>
      <IconButton
        ref={anchorRef}
        label="Save to playlist"
        size={size}
        tone="accent"
        className={className}
        onClick={(e) => { e.stopPropagation(); setOpen(o => !o); }}
      >
        <PlaylistAddIcon className={size === "md" ? "w-4 h-4" : "w-3.5 h-3.5"} />
      </IconButton>
      <Popover open={open} onClose={close} anchorRef={anchorRef} className="w-56 p-2">
        {/* Mounted only while open, so the playlist fetch is on demand */}
        <PlaylistPicker song={song} onDone={close} />
      </Popover>
    </>
  );
}

function PlaylistPicker({ song, onDone }: { song: PlaylistCandidate; onDone: () => void }) {
  const { guildId } = useGuildState();
  const { toast } = useToast();
  const { mutate } = useSWRConfig();

  const [globalMode, setGlobalMode] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);

  const listKey = `/api/guild/${guildId}/playlists?global_mode=${globalMode}`;
  const { data, isLoading } = useSWR<{ playlists: Playlist[] }>(listKey);
  const playlists = data?.playlists ?? [];

  const add = async (name: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/add`, {
        method: "POST",
        body: JSON.stringify({
          song_url: song.webpage_url,
          global_mode: globalMode,
          song: {
            title: song.title,
            duration: song.duration ?? 0,
            thumbnail: song.thumbnail ?? "",
            uploader: song.uploader ?? "",
            url: song.url ?? "",
          },
        }),
      });
      toast(`Saved to "${name}"`, "success");
      mutate(listKey);
      mutate(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}?global_mode=${globalMode}`);
      onDone();
    } catch (e: any) {
      toast(e?.message || "Could not save", "error");
    } finally {
      setBusy(false);
    }
  };

  const createAndAdd = async () => {
    const name = newName.trim();
    if (!name || busy) return;
    setBusy(true);
    try {
      await apiFetch(`/api/guild/${guildId}/playlists`, {
        method: "POST",
        body: JSON.stringify({ name, global_mode: globalMode }),
      });
      setNewName("");
      setCreating(false);
    } catch (e: any) {
      toast(e?.message || "Could not create playlist", "error");
      setBusy(false);
      return;
    }
    setBusy(false);
    await add(name);
  };

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-1 p-0.5 rounded-lg bg-white/[0.04]">
        {[false, true].map(g => (
          <button
            key={String(g)}
            onClick={() => { setGlobalMode(g); setCreating(false); }}
            className={cn(
              "flex-1 h-6 rounded-md text-[10px] font-semibold transition-colors",
              globalMode === g ? "bg-accent text-white" : "text-white/50 hover:text-white/80"
            )}
          >
            {g ? "Global" : "Server"}
          </button>
        ))}
      </div>

      <div className="max-h-56 overflow-y-auto flex flex-col gap-0.5">
        {isLoading && <p className="text-[11px] text-muted px-2 py-2">Loading…</p>}
        {!isLoading && playlists.length === 0 && (
          <p className="text-[11px] text-muted px-2 py-2">No {globalMode ? "global" : "server"} playlists yet</p>
        )}
        {playlists.map(p => (
          <button
            key={p.name}
            disabled={busy}
            onClick={() => add(p.name)}
            className="flex items-center justify-between gap-2 px-2 py-1.5 rounded-lg text-left text-[11px] text-white/80 hover:bg-white/[0.08] disabled:opacity-50"
          >
            <span className="truncate">{p.name}</span>
            {typeof p.song_count === "number" && (
              <span className="text-[10px] text-muted tabular-nums flex-shrink-0">{p.song_count}</span>
            )}
          </button>
        ))}
      </div>

      {creating ? (
        <div className="flex items-center gap-1">
          <input
            autoFocus
            value={newName}
            onChange={e => setNewName(e.target.value)}
            onKeyDown={e => {
              if (e.key === "Enter") createAndAdd();
              if (e.key === "Escape") { setCreating(false); setNewName(""); }
            }}
            placeholder="Playlist name"
            maxLength={50}
            className="flex-1 min-w-0 h-7 px-2 rounded-lg bg-white/[0.06] text-base sm:text-[11px] text-white placeholder:text-muted outline-none focus:bg-white/[0.1]"
          />
          <button
            onClick={createAndAdd}
            disabled={!newName.trim() || busy}
            className="h-7 px-2 rounded-lg bg-accent text-white text-[10px] font-semibold disabled:opacity-40"
          >
            Save
          </button>
        </div>
      ) : (
        <button
          onClick={() => setCreating(true)}
          className="px-2 py-1.5 rounded-lg text-left text-[11px] font-medium text-accent hover:bg-accent/10"
        >
          + New playlist
        </button>
      )}
    </div>
  );
}
