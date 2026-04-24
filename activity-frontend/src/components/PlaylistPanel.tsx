"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import MarqueeText from "./MarqueeText";
import { useGuildState } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { apiFetch } from "@/lib/api";
import { formatDuration, proxyImg, cn } from "@/lib/utils";
import EmptyState from "./EmptyState";
import type { Playlist, PlaylistSong, Member, Collab } from "@/types";

type View = "list" | "detail" | "collabs";

export default function PlaylistPanel() {
  const { guildId, state } = useGuildState();
  const { toast } = useToast();

  const [view, setView] = useState<View>("list");
  const [playlists, setPlaylists] = useState<Playlist[]>([]);
  const [globalMode, setGlobalMode] = useState(false);
  const [loading, setLoading] = useState(false);
  const [selectedName, setSelectedName] = useState("");
  const [songs, setSongs] = useState<PlaylistSong[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");

  // Collab state
  const [collabs, setCollabs] = useState<Collab[]>([]);
  const [collabLoading, setCollabLoading] = useState(false);
  const [memberSearch, setMemberSearch] = useState("");
  const [members, setMembers] = useState<Member[]>([]);
  const [memberSearching, setMemberSearching] = useState(false);
  const searchDebounce = useRef<ReturnType<typeof setTimeout>>(undefined);

  const gm = `global_mode=${globalMode}`;

  const fetchPlaylists = useCallback(async () => {
    setLoading(true);
    try {
      const d = await apiFetch<{ playlists: Playlist[] }>(`/api/guild/${guildId}/playlists?${gm}`);
      setPlaylists(d.playlists);
    } catch { setPlaylists([]); }
    finally { setLoading(false); }
  }, [guildId, gm]);

  useEffect(() => { fetchPlaylists(); }, [fetchPlaylists]);

  const openPlaylist = async (name: string) => {
    setSelectedName(name); setView("detail"); setDetailLoading(true);
    try {
      const d = await apiFetch<{ songs: PlaylistSong[] }>(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}?${gm}`);
      setSongs(d.songs);
    } catch { setSongs([]); }
    finally { setDetailLoading(false); }
  };

  const handleCreate = async () => {
    if (!newName.trim()) return;
    try {
      await apiFetch(`/api/guild/${guildId}/playlists`, { method: "POST", body: JSON.stringify({ name: newName.trim(), global_mode: globalMode }) });
      toast(`Created "${newName.trim()}"`, "success"); setNewName(""); setShowCreate(false); fetchPlaylists();
    } catch (e: any) { toast(e.message, "error"); }
  };

  const handleDelete = async (name: string) => {
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}?${gm}`, { method: "DELETE" });
      toast(`Deleted "${name}"`, "success"); if (view !== "list") setView("list"); fetchPlaylists();
    } catch (e: any) { toast(e.message, "error"); }
  };

  const handleLoad = async (name: string) => {
    try {
      const r = await apiFetch<{ added: number; total: number; auto_play?: boolean }>(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/load`, { method: "POST", body: JSON.stringify({ global_mode: globalMode }) });
      if (r.auto_play && !state.current) {
        apiFetch(`/api/guild/${guildId}/play`, { method: "POST" }).catch(() => {});
      }
      toast(`Loaded ${r.added} of ${r.total} songs`, "success");
    } catch (e: any) { toast(e.message, "error"); }
  };

  const handleAddCurrent = async (name: string) => {
    if (!state.current) { toast("Nothing playing", "error"); return; }
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/add`, { method: "POST", body: JSON.stringify({ song_url: state.current.webpage_url, global_mode: globalMode }) });
      toast(`Saved to "${name}"`, "success"); if (view === "detail" && selectedName === name) openPlaylist(name);
    } catch (e: any) { toast(e.message, "error"); }
  };

  const handleAddQueue = async (name: string) => {
    try {
      const r = await apiFetch<{ added: number }>(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/add-queue`, { method: "POST", body: JSON.stringify({ global_mode: globalMode }) });
      toast(`Added ${r.added} songs`, "success"); if (view === "detail" && selectedName === name) openPlaylist(name);
    } catch (e: any) { toast(e.message, "error"); }
  };

  const handleRemoveSong = async (pos: number) => {
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/${pos}?${gm}`, { method: "DELETE" });
      toast("Removed", "success"); openPlaylist(selectedName);
    } catch (e: any) { toast(e.message, "error"); }
  };

  const handleMoveSong = async (from: number, to: number) => {
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/move`, { method: "POST", body: JSON.stringify({ from_pos: from, to_pos: to, global_mode: globalMode }) });
      setSongs(prev => { const n = [...prev]; const [m] = n.splice(from, 1); n.splice(to, 0, m); return n; });
    } catch (e: any) { toast(e.message, "error"); }
  };

  const openCollabs = async (name: string) => {
    setSelectedName(name); setView("collabs"); setCollabLoading(true);
    setMemberSearch(""); setMembers([]);
    try {
      const d = await apiFetch<{ collaborators: Collab[] }>(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/collabs?${gm}`);
      setCollabs(d.collaborators);
    } catch { setCollabs([]); }
    finally { setCollabLoading(false); }
  };

  const searchMembers = async (q: string) => {
    setMemberSearching(true);
    try {
      const d = await apiFetch<{ members: Member[] }>(`/api/guild/${guildId}/playlists/members?q=${encodeURIComponent(q)}`);
      // Filter out existing collabs
      const collabIds = new Set(collabs.map(c => c.id));
      setMembers(d.members.filter(m => !collabIds.has(m.id)));
    } catch { setMembers([]); }
    finally { setMemberSearching(false); }
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
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/collabs`, { method: "POST", body: JSON.stringify({ user_id: member.id, global_mode: globalMode }) });
      toast(`Added ${member.display_name}`, "success");
      setCollabs(prev => [...prev, { id: member.id, display_name: member.display_name, avatar: member.avatar }]);
      setMembers(prev => prev.filter(m => m.id !== member.id));
      setMemberSearch("");
    } catch (e: any) { toast(e.message, "error"); }
  };

  const handleRemoveCollab = async (collab: Collab) => {
    try {
      await apiFetch(`/api/guild/${guildId}/playlists/${encodeURIComponent(selectedName)}/collabs/${collab.id}?${gm}`, { method: "DELETE" });
      toast(`Removed ${collab.display_name}`, "success");
      setCollabs(prev => prev.filter(c => c.id !== collab.id));
    } catch (e: any) { toast(e.message, "error"); }
  };

  // ── Collabs view ──────────────────────────────────────────────
  if (view === "collabs") {
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center gap-2 px-4 py-3 border-b border-white/[0.06] flex-shrink-0">
          <button onClick={() => openPlaylist(selectedName)} className="w-7 h-7 rounded-lg flex items-center justify-center text-muted hover:text-white hover:bg-white/[0.06] transition-colors">
            <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z" /></svg>
          </button>
          <h3 className="text-sm font-semibold text-white truncate flex-1">{selectedName}</h3>
          <span className="text-[10px] text-muted">Collaborators</span>
        </div>

        {/* Search for members */}
        <div className="px-4 py-3 border-b border-white/[0.08] flex-shrink-0">
          <div className="flex items-center gap-2.5 bg-surface-3/60 rounded-xl border border-white/[0.08] focus-within:border-accent/40 transition-[border-color] duration-200 px-3.5 py-2.5">
            <svg className="w-4 h-4 text-white/40 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input
              type="text" value={memberSearch} onChange={(e) => handleMemberInput(e.target.value)}
              placeholder="Search members to add..."
              className="flex-1 bg-transparent text-white text-sm outline-none placeholder:text-white/30 min-w-0"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {/* Search results */}
          {memberSearch && (
            <div className="mb-3">
              {memberSearching ? (
                <div className="flex justify-center py-4"><div className="w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin" /></div>
              ) : members.length > 0 ? (
                <div className="flex flex-col gap-1 mt-2">
                  {members.map(m => (
                    <button
                      key={m.id} onClick={() => handleAddCollab(m)}
                      className="w-full flex items-center gap-2.5 p-2 rounded-xl bg-accent/5 border border-accent/10 hover:bg-accent/10 transition-[background-color] duration-150 text-left"
                    >
                      <div className="w-8 h-8 rounded-full bg-surface-3 overflow-hidden flex-shrink-0">
                        {m.avatar ? <img src={proxyImg(m.avatar)} className="w-full h-full object-cover" /> : (
                          <div className="w-full h-full flex items-center justify-center text-muted text-[10px] font-bold">{m.display_name[0]}</div>
                        )}
                      </div>
                      <div className="flex-1 min-w-0">
                        <p className="text-xs font-medium text-white truncate">{m.display_name}</p>
                        <p className="text-[10px] text-white/30 truncate">@{m.username}</p>
                      </div>
                      <svg className="w-4 h-4 text-accent flex-shrink-0" fill="currentColor" viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z" /></svg>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-[10px] text-muted text-center py-3">No members found</p>
              )}
            </div>
          )}

          {/* Current collaborators */}
          {collabLoading ? (
            <div className="flex justify-center py-8"><div className="w-5 h-5 border-2 border-accent border-t-transparent rounded-full animate-spin" /></div>
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
                    {c.avatar ? <img src={proxyImg(c.avatar)} className="w-full h-full object-cover" /> : (
                      <div className="w-full h-full flex items-center justify-center text-muted text-[10px] font-bold">{c.display_name[0]}</div>
                    )}
                  </div>
                  <p className="text-xs font-medium text-white truncate flex-1">{c.display_name}</p>
                  <button onClick={() => handleRemoveCollab(c)} className="w-6 h-6 rounded-md flex items-center justify-center text-muted hover:text-danger opacity-0 group-hover:opacity-100 transition-[color,opacity] duration-150">
                    <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" /></svg>
                  </button>
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
        <div className="flex items-center gap-2 px-4 py-3 border-b border-white/[0.06] flex-shrink-0">
          <button onClick={() => { setView("list"); fetchPlaylists(); }} className="w-7 h-7 rounded-lg flex items-center justify-center text-muted hover:text-white hover:bg-white/[0.06] transition-colors">
            <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z" /></svg>
          </button>
          <h3 className="text-sm font-semibold text-white truncate flex-1">{selectedName}</h3>
          <button onClick={() => openCollabs(selectedName)} className="text-[10px] font-medium text-muted hover:text-white transition-colors" title="Manage collaborators">
            Collabs
          </button>
          <button onClick={() => handleAddCurrent(selectedName)} className="text-[10px] font-medium text-accent hover:text-accent/80 transition-colors" title="Save current song">
            + Save
          </button>
          <button onClick={() => handleLoad(selectedName)} className="text-[10px] font-medium text-success/80 hover:text-success transition-colors" title="Load into queue">
            Load
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {detailLoading ? (
            <div className="flex justify-center py-12"><div className="w-5 h-5 border-2 border-accent border-t-transparent rounded-full animate-spin" /></div>
          ) : songs.length === 0 ? (
            <div className="flex items-center justify-center py-12 text-muted text-xs">Empty playlist</div>
          ) : (
            <div className="flex flex-col gap-1 mt-2">
              {songs.map((song, i) => (
                <div key={song.webpage_url} className="flex items-center gap-2 p-2 rounded-xl bg-white/[0.02] border border-white/[0.04] hover:bg-white/[0.06] transition-[background-color,border-color] duration-150 group">
                  <span className="w-4 text-center text-[10px] tabular-nums text-muted flex-shrink-0">{i + 1}</span>
                  <div className="flex-1 min-w-0">
                    <MarqueeText className="text-xs font-medium text-white">{song.title}</MarqueeText>
                    <p className="text-[10px] text-white/30 truncate">{song.uploader}</p>
                  </div>
                  {/* Fixed right section: buttons + duration */}
                  <div className="flex items-center gap-0.5 flex-shrink-0">
                    <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                      {i > 0 && <MicroBtn onClick={() => handleMoveSong(i, i - 1)} title="Up"><svg className="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M7.41 15.41L12 10.83l4.59 4.58L18 14l-6-6-6 6z" /></svg></MicroBtn>}
                      {i < songs.length - 1 && <MicroBtn onClick={() => handleMoveSong(i, i + 1)} title="Down"><svg className="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z" /></svg></MicroBtn>}
                      <MicroBtn onClick={() => handleRemoveSong(i)} title="Remove" danger><svg className="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" /></svg></MicroBtn>
                    </div>
                    <span className="text-[10px] tabular-nums text-muted w-8 text-right">{song.duration > 0 ? formatDuration(song.duration) : ""}</span>
                  </div>
                </div>
              ))}
            </div>
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
        <button onClick={() => setShowCreate(!showCreate)} className="w-7 h-7 rounded-lg flex items-center justify-center text-muted hover:text-white hover:bg-white/[0.06] transition-colors" title="Create playlist">
          <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z" /></svg>
        </button>
      </div>

      {showCreate && (
        <div className="px-4 py-2.5 border-b border-white/[0.08] flex gap-2">
          <input type="text" value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Playlist name..."
            onKeyDown={(e) => e.key === "Enter" && handleCreate()}
            className="flex-1 bg-surface-3/60 text-white text-sm rounded-xl px-3.5 py-2 outline-none border border-white/[0.08] focus:border-accent/40 placeholder:text-white/30" />
          <button onClick={handleCreate} disabled={!newName.trim()} className="text-xs font-semibold px-4 py-2 rounded-xl bg-accent text-white hover:bg-accent/80 disabled:opacity-40 transition-[background-color,opacity] duration-150">Create</button>
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-3 pb-3">
        {loading ? (
          <div className="flex justify-center py-12"><div className="w-5 h-5 border-2 border-accent border-t-transparent rounded-full animate-spin" /></div>
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
                  <div className="w-10 h-10 rounded-xl bg-surface-3 flex items-center justify-center flex-shrink-0">
                    <svg className="w-5 h-5 text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 010 3.75H5.625a1.875 1.875 0 010-3.75z" /></svg>
                  </div>
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-white truncate">{pl.name}</p>
                    <p className="text-[10px] text-white/30">{pl.song_count} song{pl.song_count !== 1 ? "s" : ""}</p>
                  </div>
                </button>
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0">
                  <SmallBtn onClick={() => handleAddCurrent(pl.name)} title="Save current song" color="accent"><svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z" /></svg></SmallBtn>
                  <SmallBtn onClick={() => handleLoad(pl.name)} title="Load into queue" color="accent"><svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg></SmallBtn>
                  <SmallBtn onClick={() => openCollabs(pl.name)} title="Collaborators" color="white"><svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M15 19.128a9.38 9.38 0 002.625.372 9.337 9.337 0 004.121-.952 4.125 4.125 0 00-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 018.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0111.964-3.07M12 6.375a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zm8.25 2.25a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z" /></svg></SmallBtn>
                  <SmallBtn onClick={() => handleDelete(pl.name)} title="Delete" color="danger"><svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" /></svg></SmallBtn>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function MicroBtn({ children, onClick, title, danger }: { children: React.ReactNode; onClick: () => void; title?: string; danger?: boolean }) {
  return (
    <button onClick={onClick} title={title} className={`w-5 h-5 rounded-md flex items-center justify-center transition-colors ${danger ? "text-muted hover:text-danger hover:bg-danger/10" : "text-muted hover:text-white hover:bg-white/[0.06]"}`}>
      {children}
    </button>
  );
}

function SmallBtn({ children, onClick, title, color }: { children: React.ReactNode; onClick: () => void; title?: string; color: "accent" | "danger" | "white" }) {
  const colors = { accent: "hover:text-accent hover:bg-accent/10", danger: "hover:text-danger hover:bg-danger/10", white: "hover:text-white hover:bg-white/[0.06]" };
  return (
    <button onClick={onClick} title={title} className={`w-7 h-7 rounded-lg flex items-center justify-center text-muted transition-colors ${colors[color]}`}>
      {children}
    </button>
  );
}

