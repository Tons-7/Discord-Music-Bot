"use client";

import useSWR from "swr";

export const FAVORITES_KEY = "/api/favorites";

export interface FavSong {
  title: string;
  uploader: string;
  duration: number;
  webpage_url: string;
  thumbnail: string;
}

export interface FavoritesData {
  favorites: FavSong[];
}

// One lookup set per SWR snapshot, shared by every heart in every list.
const urlSets = new WeakMap<FavoritesData, Set<string>>();

export function favoriteUrls(data: FavoritesData | undefined): Set<string> {
  if (!data) return new Set();
  let set = urlSets.get(data);
  if (!set) {
    set = new Set(data.favorites.map(f => f.webpage_url));
    urlSets.set(data, set);
  }
  return set;
}

export function useFavorites() {
  return useSWR<FavoritesData>(FAVORITES_KEY);
}

export function useIsFavorite(webpageUrl: string): boolean {
  const { data } = useFavorites();
  return favoriteUrls(data).has(webpageUrl);
}
