"use client";

import { useState, useRef, useCallback } from "react";
import { useGuildState } from "./GuildStateProvider";
import { apiFetch } from "@/lib/api";
import { formatDuration, cn } from "@/lib/utils";
import { useToast } from "./Toast";
import SongRow from "./SongRow";
import EmptyState from "./EmptyState";
import type { SearchResult } from "@/types";

export default function SearchPanel() {
  const { guildId, state } = useGuildState();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [addedSet, setAddedSet] = useState<Set<string>>(new Set());
  const [pendingSet, setPendingSet] = useState<Set<string>>(new Set());
  // Set when the last successful search was a URL — enables the "Add all" path
  // that ships the URL straight to /queue/add (backend expands the playlist).
  const [playlistUrl, setPlaylistUrl] = useState<string | null>(null);
  const [addingAll, setAddingAll] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const { toast } = useToast();

  const search = useCallback(async (q: string) => {
    if (!q.trim()) { setResults([]); setPlaylistUrl(null); return; }
    setLoading(true);
    try {
      const data = await apiFetch<{ results: SearchResult[] }>(
        `/api/guild/${guildId}/search?q=${encodeURIComponent(q)}&limit=8`
      );
      setResults(data.results);
      const isUrl = /^https?:\/\//i.test(q.trim());
      setPlaylistUrl(isUrl && data.results.length > 1 ? q.trim() : null);
    } catch { setResults([]); setPlaylistUrl(null); }
    finally { setLoading(false); }
  }, [guildId]);

  const handleInput = (value: string) => {
    setQuery(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => search(value), 300);
  };

  const handleAddAll = async () => {
    if (!playlistUrl || addingAll) return;
    setAddingAll(true);
    try {
      const res = await apiFetch<{ ok: boolean; added: number; skipped?: number; duplicate?: boolean; auto_play?: boolean }>(
        `/api/guild/${guildId}/queue/add`,
        { method: "POST", body: JSON.stringify({ query: playlistUrl }) },
      );

      if (res.duplicate) {
        toast(`All ${res.skipped ?? 0} songs already in queue`, "error");
        return;
      }

      if (res.auto_play && !state.current) {
        apiFetch(`/api/guild/${guildId}/play`, { method: "POST" }).catch(() => {});
      }

      const skipped = res.skipped ?? 0;
      toast(skipped > 0 ? `Added ${res.added} songs (${skipped} duplicates skipped)` : `Added ${res.added} songs`, "success");

      // Mark every visible result as added
      setAddedSet(new Set(results.map(r => r.webpage_url || r.url)));
      setTimeout(() => setAddedSet(new Set()), 2500);
    } catch (e: any) {
      toast(e?.message || "Failed to add playlist", "error");
    } finally {
      setAddingAll(false);
    }
  };

  const handleAdd = async (result: SearchResult) => {
    const key = result.webpage_url || result.url;
    if (addedSet.has(key) || pendingSet.has(key)) return;

    setPendingSet(prev => new Set(prev).add(key));
    try {
      const res = await apiFetch<{ ok: boolean; added: number; duplicate?: boolean; title?: string; position?: number; playing?: boolean; auto_play?: boolean }>(
        `/api/guild/${guildId}/queue/add`,
        { method: "POST", body: JSON.stringify({ query: result.webpage_url || result.url }) },
      );
      setPendingSet(prev => { const n = new Set(prev); n.delete(key); return n; });

      if (res.duplicate) {
        const msg = res.playing
          ? `"${result.title}" is currently playing`
          : res.position
          ? `"${result.title}" is already in queue (#${res.position})`
          : `"${result.title}" is already in queue`;
        toast(msg, "error");
        return;
      }

      // If nothing was playing, start playback of the just-added song
      if (res.auto_play && !state.current) {
        apiFetch(`/api/guild/${guildId}/play`, { method: "POST" }).catch(() => {});
      }

      setAddedSet(prev => new Set(prev).add(key));
      toast(`Added "${result.title}"`, "success");
      setTimeout(() => setAddedSet(prev => { const n = new Set(prev); n.delete(key); return n; }), 2500);
    } catch {
      setPendingSet(prev => { const n = new Set(prev); n.delete(key); return n; });
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* Search input */}
      <div className="px-4 py-3 flex-shrink-0 border-b border-white/[0.08]">
        <div className="flex items-center gap-2.5 bg-surface-3/60 rounded-xl border border-white/[0.08] focus-within:border-accent/40 transition-[border-color] duration-200 px-3.5 py-2.5">
          <svg className="w-4 h-4 text-white/40 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            type="text" value={query} onChange={(e) => handleInput(e.target.value)}
            placeholder="Search or paste URL..."
            className="flex-1 bg-transparent text-white text-sm outline-none placeholder:text-white/30 min-w-0"
          />
        </div>
      </div>

      {/* Results */}
      <div className="flex-1 overflow-y-auto px-3 pb-3">
        {loading && (
          <div className="flex items-center justify-center py-16">
            <div className="w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin" />
          </div>
        )}

        {!loading && results.length === 0 && !query && (
          <EmptyState
            icon={
              <svg className="w-7 h-7 text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
            }
            title="Search for songs"
            subtitle="By name or paste a URL"
          />
        )}

        {!loading && results.length === 0 && query && (
          <div className="flex items-center justify-center py-16 text-muted text-sm">No results found</div>
        )}

        {!loading && playlistUrl && results.length > 1 && (
          <div className="sticky top-0 z-10 -mx-3 px-3 pt-3 pb-2 bg-gradient-to-b from-surface-1 via-surface-1 to-transparent">
            <button
              onClick={handleAddAll}
              disabled={addingAll}
              className={cn(
                "w-full flex items-center justify-center gap-2 py-2.5 px-4 rounded-2xl text-sm font-medium",
                "bg-accent/15 text-accent border border-accent/30",
                "transition-[background-color,opacity] duration-200",
                "hover:bg-accent/25 disabled:opacity-60 disabled:cursor-not-allowed"
              )}
            >
              {addingAll ? (
                <>
                  <div className="w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                  <span>Adding playlist...</span>
                </>
              ) : (
                <>
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v12m6-6H6" />
                  </svg>
                  <span>Add all {results.length} songs</span>
                </>
              )}
            </button>
          </div>
        )}

        <div className="flex flex-col gap-1.5">
          {results.map((result, i) => {
            const key = result.webpage_url || result.url;
            const isAdded = addedSet.has(key);
            const isPending = pendingSet.has(key);

            return (
              <SongRow
                key={`${key}-${i}`}
                title={result.title}
                subtitle={result.uploader}
                thumbnail={result.thumbnail}
                state={isAdded ? "added" : isPending ? "pending" : "default"}
                onClick={() => handleAdd(result)}
                disabled={isAdded || isPending}
                trailing={
                  <>
                    <span className="text-[11px] tabular-nums text-muted">
                      {result.duration > 0 ? formatDuration(result.duration) : ""}
                    </span>
                    {isPending && (
                      <div className="w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                    )}
                    {isAdded && (
                      <svg className="w-4 h-4 text-success" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
                      </svg>
                    )}
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
