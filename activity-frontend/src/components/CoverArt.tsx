"use client";

import { useEffect, useRef, useState } from "react";
import { artworkSources, contentBox, frameRatio } from "@/lib/artwork";
import { proxyImg } from "@/lib/utils";
import { NoteIcon } from "./ui/icons";

// Bars are looked for on a copy this wide: plenty for a straight edge, cheap to scan.
const SCAN_WIDTH = 160;

interface Shown {
  src: string;
  ratio: number;
  // An object URL of the picture cut out of its bars, released when replaced
  owned: boolean;
}

function load(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = url;
  });
}

/** The picture of `img` without black bars, with the frame shape it needs. Same-origin (proxied) images only. */
async function withoutBars(img: HTMLImageElement, url: string): Promise<Shown> {
  const { naturalWidth: width, naturalHeight: height } = img;
  const scale = Math.min(1, SCAN_WIDTH / width);
  const scan = document.createElement("canvas");
  scan.width = Math.max(1, Math.round(width * scale));
  scan.height = Math.max(1, Math.round(height * scale));
  const ctx = scan.getContext("2d", { willReadFrequently: true });
  if (!ctx) return { src: url, ratio: frameRatio(width, height), owned: false };
  ctx.drawImage(img, 0, 0, scan.width, scan.height);
  const box = contentBox(ctx.getImageData(0, 0, scan.width, scan.height).data, scan.width, scan.height);
  if (box.width === scan.width && box.height === scan.height) {
    return { src: url, ratio: frameRatio(width, height), owned: false };
  }

  const crop = document.createElement("canvas");
  crop.width = Math.round(box.width / scale);
  crop.height = Math.round(box.height / scale);
  crop.getContext("2d")?.drawImage(img, box.x / scale, box.y / scale, crop.width, crop.height, 0, 0, crop.width, crop.height);
  const blob = await new Promise<Blob | null>(resolve => crop.toBlob(resolve, "image/jpeg", 0.92));
  if (!blob) return { src: url, ratio: frameRatio(width, height), owned: false };
  return { src: URL.createObjectURL(blob), ratio: frameRatio(crop.width, crop.height), owned: true };
}

/**
 * The song's artwork in a frame of its own shape (16:9 for videos, square for album covers), as large as the
 * surrounding size container allows. YouTube's HD frame is used when it exists, and black bars are cut off.
 * The previous artwork stays until the next one is ready, so changing songs never flashes an empty frame.
 */
export default function CoverArt({ thumbnail, children }: { thumbnail?: string | null; children?: React.ReactNode }) {
  const [shown, setShown] = useState<Shown | null>(null);
  // The object URL on screen, released once it is replaced or the player goes away
  const ownedRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const sources = artworkSources(thumbnail).map(proxyImg);
    const next = async (): Promise<Shown | null> => {
      for (const url of sources) {
        const img = await load(url).catch(() => null);
        if (cancelled) return null;
        if (img) return withoutBars(img, url).catch(() => ({ src: url, ratio: frameRatio(img.naturalWidth, img.naturalHeight), owned: false }));
      }
      return null;
    };
    next().then(result => {
      if (cancelled) {
        if (result?.owned) URL.revokeObjectURL(result.src);
        return;
      }
      if (ownedRef.current) URL.revokeObjectURL(ownedRef.current);
      ownedRef.current = result?.owned ? result.src : null;
      setShown(result);
    });
    return () => { cancelled = true; };
  }, [thumbnail]);

  useEffect(() => () => {
    if (ownedRef.current) URL.revokeObjectURL(ownedRef.current);
  }, []);

  const ratio = shown?.ratio ?? 1;
  return (
    <div
      className="relative rounded-2xl overflow-hidden shadow-[0_8px_40px_rgba(0,0,0,0.5)]"
      style={{ aspectRatio: ratio, width: `min(100cqw, ${(ratio * 100).toFixed(2)}cqh)` }}
    >
      {shown ? (
        <img src={shown.src} alt="" className="w-full h-full object-cover" />
      ) : (
        <div className="w-full h-full bg-gradient-to-br from-accent/20 via-accent/5 to-transparent flex items-center justify-center">
          <NoteIcon className="w-14 h-14 text-accent/30" />
        </div>
      )}
      {children}
    </div>
  );
}
