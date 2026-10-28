"use client";

import { useToast } from "./Toast";
import { apiFetch, errorMessage } from "@/lib/api";
import SongRow from "./SongRow";
import AddToPlaylistButton from "./AddToPlaylistButton";
import EmptyState, { ErrorState } from "./EmptyState";
import IconButton from "./ui/IconButton";
import { SongRowSkeleton } from "./ui/Skeleton";
import { CloseIcon, HeartIcon } from "./ui/icons";
import { useFavorites, type FavSong } from "@/hooks/useFavorites";
import { useQueueAdd } from "@/hooks/useQueueAdd";

export default function FavoritesPanel() {
  const { toast } = useToast();
  const { data, error, isLoading, mutate } = useFavorites();
  const favorites = data?.favorites ?? [];
  const queue = useQueueAdd();

  // Favorites are ordered by added_at and there is no reorder endpoint, so an
  // undone removal comes back at the end of the list rather than its old slot.
  const undoRemove = (fav: FavSong) => {
    apiFetch("/api/favorites", {
      method: "POST",
      body: JSON.stringify({ ...fav, url: "" }),
    }).then(
      () => mutate(),
      (e: unknown) => {
        toast(errorMessage(e, "Undo failed"), "error");
        mutate();
      },
    );
  };

  // By URL, not list position: the list may have changed elsewhere since it loaded.
  const handleRemove = (fav: FavSong) => {
    const request = apiFetch(`/api/favorites?url=${encodeURIComponent(fav.webpage_url)}`, { method: "DELETE" });
    mutate(request.then(() => undefined), {
      optimisticData: d => ({ favorites: (d?.favorites ?? []).filter(f => f.webpage_url !== fav.webpage_url) }),
      rollbackOnError: true,
      populateCache: false,
      revalidate: true,
    }).then(
      () => toast("Removed from Favorites", "success", { label: "Undo", onClick: () => undoRemove(fav) }),
      (e: unknown) => toast(errorMessage(e, "Couldn't remove it from Favorites"), "error"),
    );
  };

  if (isLoading) {
    return <SongRowSkeleton className="px-3 mt-2" />;
  }

  if (error && !data) {
    return <ErrorState compact message="Couldn't load your favorites" onRetry={() => mutate()} />;
  }

  if (favorites.length === 0) {
    return (
      <EmptyState
        compact
        icon={<HeartIcon className="w-6 h-6" strokeWidth={1.5} />}
        title="No favorites yet"
        subtitle="Tap the heart on any song to save it here"
      />
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-2.5 border-b border-fg/[0.06] flex-shrink-0">
        <span className="text-xs text-muted">{favorites.length} favorite{favorites.length !== 1 ? "s" : ""}</span>
      </div>
      <div className="flex-1 overflow-y-auto px-3 pb-3">
        <div className="flex flex-col gap-1.5 mt-2">
          {favorites.map(fav => {
            const status = queue.status(fav.webpage_url);
            return (
              <SongRow
                key={fav.webpage_url}
                title={fav.title}
                subtitle={fav.uploader}
                thumbnail={fav.thumbnail}
                marquee
                actionLabel={`Add "${fav.title}" to the queue`}
                state={status}
                onClick={() => queue.add(fav.webpage_url, fav.title)}
                disabled={status !== "default"}
                duration={fav.duration}
                trailing={
                  <>
                    {/* Hover-only, so it goes first: the always-shown button stays next to the duration */}
                    <IconButton
                      label="Remove from Favorites"
                      size="xs"
                      tone="danger"
                      className="opacity-0 group-hover:opacity-100 group-has-focus-visible:opacity-100 pointer-coarse:opacity-100"
                      onClick={() => handleRemove(fav)}
                    >
                      <CloseIcon className="w-3 h-3" />
                    </IconButton>
                    <AddToPlaylistButton song={fav} />
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
