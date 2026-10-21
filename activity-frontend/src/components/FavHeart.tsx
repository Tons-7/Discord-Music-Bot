"use client";

import { useRef } from "react";
import { mutate } from "swr";
import { apiFetch, errorMessage } from "@/lib/api";
import { useToast } from "./Toast";
import IconButton from "./ui/IconButton";
import { HeartFilledIcon, HeartIcon } from "./ui/icons";
import { FAVORITES_KEY, useIsFavorite, type FavoritesData, type FavSong } from "@/hooks/useFavorites";
import { cn } from "@/lib/utils";

export { FAVORITES_KEY, type FavSong } from "@/hooks/useFavorites";

export default function FavHeart({ webpageUrl, title, duration, thumbnail, uploader, size = "sm", className }: {
  webpageUrl: string; title: string; duration?: number; thumbnail?: string; uploader?: string;
  size?: "sm" | "md";
  className?: string;
}) {
  const { toast } = useToast();
  const isFav = useIsFavorite(webpageUrl);
  // The heart flips at once, so only a second click before the server answers is ignored.
  const busy = useRef(false);

  const toggle = () => {
    if (busy.current) return;
    busy.current = true;
    const adding = !isFav;
    const song: FavSong = {
      title, uploader: uploader || "", duration: duration || 0, thumbnail: thumbnail || "", webpage_url: webpageUrl,
    };
    const request = adding
      ? apiFetch("/api/favorites", {
          method: "POST",
          body: JSON.stringify({ ...song, url: "" }),
        })
      : apiFetch(`/api/favorites?url=${encodeURIComponent(webpageUrl)}`, { method: "DELETE" });
    mutate<FavoritesData | undefined>(FAVORITES_KEY, request.then(() => undefined), {
      optimisticData: (d) => {
        const favorites = d?.favorites ?? [];
        return {
          favorites: adding
            ? [...favorites.filter(f => f.webpage_url !== webpageUrl), song]
            : favorites.filter(f => f.webpage_url !== webpageUrl),
        };
      },
      rollbackOnError: true,
      populateCache: false,
      revalidate: true,
    }).then(
      () => toast(adding ? "Added to Favorites" : "Removed from Favorites", adding ? "success" : "info"),
      (e: unknown) => toast(errorMessage(e, adding ? "Couldn't add to Favorites" : "Couldn't remove from Favorites"), "error"),
    ).finally(() => { busy.current = false; });
  };

  const iconCls = size === "md" ? "w-4 h-4" : "w-3.5 h-3.5";

  return (
    <IconButton
      label={isFav ? "Remove from Favorites" : "Add to Favorites"}
      size={size === "md" ? "md" : "xs"}
      tone="danger"
      onClick={(e) => { e.stopPropagation(); toggle(); }}
      className={cn(isFav && "text-danger", className)}
    >
      {isFav ? <HeartFilledIcon className={iconCls} /> : <HeartIcon className={iconCls} />}
    </IconButton>
  );
}
