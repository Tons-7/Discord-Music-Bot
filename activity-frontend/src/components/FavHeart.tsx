"use client";

import { useState, useEffect } from "react";
import { apiFetch } from "@/lib/api";
import { useToast } from "./Toast";
import { cn } from "@/lib/utils";

// Cache favorite URLs to avoid re-fetching per component
let _favUrls: Set<string> | null = null;
let _favList: Array<{ webpage_url: string }> = [];
const _subscribers = new Set<() => void>();

async function loadFavs(): Promise<Set<string>> {
  if (_favUrls) return _favUrls;
  try {
    const d = await apiFetch<{ favorites: Array<{ webpage_url: string }> }>("/api/favorites");
    _favList = d.favorites;
    _favUrls = new Set(d.favorites.map(f => f.webpage_url));
  } catch { _favUrls = new Set(); _favList = []; }
  return _favUrls;
}

export function invalidateFavCache() {
  _favUrls = null;
  _subscribers.forEach(fn => fn());
}

// Silent cache bust for internal use — forces the next loadFavs() to refetch
// without notifying subscribers. Used before a mutation when we need fresh
// data locally but shouldn't flicker every heart with pre-mutation state.
function _clearFavCache() { _favUrls = null; }

export default function FavHeart({ webpageUrl, title, url, duration, thumbnail, uploader, size = "sm" }: {
  webpageUrl: string; title: string; url?: string; duration?: number; thumbnail?: string; uploader?: string;
  size?: "sm" | "md";
}) {
  const { toast } = useToast();
  const [isFav, setIsFav] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const check = () => { loadFavs().then(s => { if (!cancelled) setIsFav(s.has(webpageUrl)); }); };
    check();
    _subscribers.add(check);
    return () => { cancelled = true; _subscribers.delete(check); };
  }, [webpageUrl]);

  const toggle = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (loading) return;
    setLoading(true);
    try {
      if (isFav) {
        _clearFavCache();
        await loadFavs();
        const idx = _favList.findIndex(f => f.webpage_url === webpageUrl);
        if (idx >= 0) {
          await apiFetch(`/api/favorites/${idx + 1}`, { method: "DELETE" });
          invalidateFavCache();
        }
        setIsFav(false);
        toast("Unfavorited", "info");
      } else {
        await apiFetch("/api/favorites", {
          method: "POST",
          body: JSON.stringify({ title, url: url || "", duration: duration || 0, thumbnail: thumbnail || "", uploader: uploader || "", webpage_url: webpageUrl }),
        });
        invalidateFavCache();
        setIsFav(true);
        toast("Favorited", "success");
      }
    } catch (e: any) { toast(e.message || "Failed", "error"); }
    finally { setLoading(false); }
  };

  const s = size === "md" ? "w-7 h-7" : "w-5 h-5";
  const iconSize = size === "md" ? "w-3.5 h-3.5" : "w-3 h-3";

  return (
    <button onClick={toggle} disabled={loading}
      className={cn(s, "rounded-md flex items-center justify-center transition-all flex-shrink-0",
        isFav ? "text-red-500 hover:text-red-400" : "text-white/30 hover:text-red-400"
      )}>
      {isFav ? (
        <svg className={iconSize} fill="currentColor" viewBox="0 0 24 24"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z" /></svg>
      ) : (
        <svg className={iconSize} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z" /></svg>
      )}
    </button>
  );
}
