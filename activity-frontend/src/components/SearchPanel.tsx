"use client";

import { useState, useEffect, useRef } from "react";
import useSWR from "swr";
import { useGuildId } from "./GuildStateProvider";
import { apiFetch, errorMessage } from "@/lib/api";
import { bulkAddMessage, type QueueAddResult } from "@/lib/queueActions";
import { cn } from "@/lib/utils";
import { useToast } from "./Toast";
import SongRow from "./SongRow";
import AddToPlaylistButton, { AddLinkToPlaylistButton } from "./AddToPlaylistButton";
import EmptyState, { ErrorState } from "./EmptyState";
import { SongRowSkeleton } from "./ui/Skeleton";
import IconButton from "./ui/IconButton";
import { AddIcon, CloseIcon, SearchIcon, Spinner } from "./ui/icons";
import { useQueueAdd } from "@/hooks/useQueueAdd";
import type { SearchResult } from "@/types";

// Server accepts up to 25 (activity/routes/search_routes.py)
const SEARCH_LIMIT = 13;
const DEBOUNCE_MS = 300;

// A search runs yt-dlp: never repeat it on its own (focus, reconnect, remount, error).
const SEARCH_SWR_OPTS = {
  keepPreviousData: true,
  revalidateIfStale: false,
  revalidateOnFocus: false,
  revalidateOnReconnect: false,
  shouldRetryOnError: false,
} as const;

const isUrl = (q: string) => /^https?:\/\//i.test(q);

export default function SearchPanel() {
  const { guildId } = useGuildId();
  const { toast } = useToast();
  const [query, setQuery] = useState("");
  // Re-armed whenever the panel is shown again, so a search hidden mid-debounce still runs.
  const [debounced, setDebounced] = useState("");
  const [addingAll, setAddingAll] = useState(false);
  const queue = useQueueAdd();
  const inputRef = useRef<HTMLInputElement>(null);

  // Back to an empty box, ready for the next search.
  const clearQuery = () => {
    setQuery("");
    inputRef.current?.focus();
  };

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query.trim()), DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [query]);

  const key = debounced
    ? `/api/guild/${guildId}/search?q=${encodeURIComponent(debounced)}&limit=${SEARCH_LIMIT}`
    : null;
  const { data, error, isLoading, mutate } = useSWR<{ results: SearchResult[]; list_url?: string }>(
    key, SEARCH_SWR_OPTS,
  );

  const trimmed = query.trim();
  const searching = !!trimmed && (trimmed !== debounced || isLoading);
  const failed = !searching && !!error;
  const results = trimmed && !failed ? data?.results ?? [] : [];
  // Set when the search was a playlist or album URL — enables "Add all", which ships
  // the URL straight to /queue/add (the backend expands the playlist).
  const playlistUrl = !searching && isUrl(debounced) && results.length > 1 ? debounced : null;
  // A video opened from a playlist or Mix: the song is the result, and its list can still be added whole.
  const listUrl = !searching && !playlistUrl && data?.list_url ? data.list_url : null;
  const bulkUrl = playlistUrl ?? listUrl;

  const handleAddAll = () => {
    if (!bulkUrl || addingAll) return;
    setAddingAll(true);
    const shown = playlistUrl ? results.map(r => r.webpage_url) : [];
    apiFetch<QueueAddResult>(`/api/guild/${guildId}/queue/add`, {
      method: "POST",
      body: JSON.stringify({ query: bulkUrl, as_list: !!listUrl }),
    }).then(
      (res) => {
        setAddingAll(false);
        toast(bulkAddMessage(res, shown.length || res.added || 0), res.duplicate ? "info" : "success");
        if (!res.duplicate) queue.markAdded(shown);
      },
      (e: unknown) => {
        setAddingAll(false);
        toast(errorMessage(e, "Couldn't add these songs"), "error");
      },
    );
  };

  return (
    <div className="flex flex-col h-full">
      {/* Search input */}
      <div className="px-4 py-3 flex-shrink-0 border-b border-fg/[0.08]">
        <div className="flex items-center gap-2.5 bg-surface-3/60 rounded-xl border border-fg/[0.08] focus-within:border-accent/40 transition-[border-color] duration-200 px-3.5 py-2.5">
          <SearchIcon className="w-4 h-4 text-muted flex-shrink-0" />
          <input
            ref={inputRef}
            type="search" value={query} onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Escape" && query) { e.preventDefault(); clearQuery(); } }}
            placeholder="Search or paste a link…"
            aria-label="Search songs"
            enterKeyHint="search" autoCorrect="off" autoCapitalize="off" spellCheck={false}
            // 16px on mobile — iOS zooms the page when focusing smaller inputs
            className="flex-1 bg-transparent text-fg text-base sm:text-sm outline-none placeholder:text-muted min-w-0 [&::-webkit-search-cancel-button]:hidden"
          />
          {query && (
            <IconButton label="Clear search" size="xs" onClick={clearQuery}>
              <CloseIcon className="w-3 h-3" />
            </IconButton>
          )}
        </div>
      </div>

      {/* Results */}
      <div className="flex-1 overflow-y-auto px-3 pb-3" aria-busy={searching || undefined}>
        {searching && results.length === 0 && <SongRowSkeleton className="mt-2" />}

        {failed && <ErrorState message="Search failed" onRetry={() => mutate()} />}

        {!trimmed && (
          <EmptyState
            icon={<SearchIcon className="w-7 h-7" />}
            title="Search for songs"
            subtitle="By name, or paste a link"
          />
        )}

        {!searching && !failed && data && trimmed && results.length === 0 && (
          <EmptyState
            icon={<SearchIcon className="w-7 h-7" />}
            title="No results found"
            subtitle="Try other words, or paste a link"
          />
        )}

        {bulkUrl && (
          <div className="sticky top-0 z-20 -mx-3 px-3 pt-3 pb-2 bg-gradient-to-b from-surface-2 via-surface-2 to-transparent flex items-center gap-2">
            <button
              type="button"
              onClick={handleAddAll}
              disabled={addingAll}
              aria-busy={addingAll || undefined}
              className={cn(
                "flex-1 min-w-0 flex items-center justify-center gap-2 py-2.5 px-4 rounded-2xl text-sm font-medium",
                "bg-accent/15 text-accent border border-accent/30",
                "transition-[background-color,opacity] duration-200",
                "hover:bg-accent/25 disabled:opacity-60 disabled:cursor-not-allowed"
              )}
            >
              {addingAll ? (
                <>
                  <Spinner label="Adding" />
                  <span>Adding songs…</span>
                </>
              ) : (
                <>
                  <AddIcon className="w-4 h-4" />
                  <span>{playlistUrl ? `Add all ${results.length} songs` : "Add the whole playlist"}</span>
                </>
              )}
            </button>
            <AddLinkToPlaylistButton url={bulkUrl} asList={!!listUrl} />
          </div>
        )}

        {results.length > 0 && (
          // Results of the previous search stay, dimmed, until the new ones arrive.
          <div className={cn("flex flex-col gap-1.5 mt-2 transition-opacity duration-200", searching && "opacity-60")}>
            {results.map((result, i) => {
              const status = queue.status(result.webpage_url);
              return (
                <SongRow
                  key={`${result.webpage_url}-${i}`}
                  title={result.title}
                  subtitle={result.uploader}
                  thumbnail={result.thumbnail}
                  actionLabel={`Add "${result.title}" to the queue`}
                  state={status}
                  onClick={() => queue.add(result.webpage_url, result.title)}
                  disabled={status !== "default"}
                  duration={result.duration}
                  trailing={<AddToPlaylistButton song={result} />}
                />
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
