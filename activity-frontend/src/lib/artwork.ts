// Player artwork: YouTube's sharpest thumbnail, shown in its own shape without black bars.

const YOUTUBE_THUMBNAIL = /^https?:\/\/i\d?\.ytimg\.com\/vi(?:_webp)?\/([\w-]{11})\//;

/** Thumbnail URLs to try, best first: a YouTube video's 1280×720 frame, then the song's own thumbnail. */
export function artworkSources(thumbnail: string | null | undefined): string[] {
  if (!thumbnail) return [];
  const id = YOUTUBE_THUMBNAIL.exec(thumbnail)?.[1];
  const best = id ? `https://i.ytimg.com/vi/${id}/maxresdefault.jpg` : null;
  return best && best !== thumbnail ? [best, thumbnail] : [thumbnail];
}

export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

// A channel this bright or darker is bar, not picture.
const DARK = 24;
// Share of a line's pixels that may be brighter (compression noise, a channel logo) and it still counts as bar.
const NOISE = 0.03;
// One bar covers less than this share of the image; a darker edge than that is a dark picture.
const MAX_BAR = 0.3;
// Bars come in pairs of about the same size (letterbox top and bottom, pillarbox left and right).
const SYMMETRY = 0.04;

/**
 * The picture inside black letterbox or pillarbox bars of an RGBA image, or the whole image without bars.
 * YouTube's 4:3 thumbnails letterbox 16:9 videos, and Topic tracks pillarbox their square album cover.
 */
export function contentBox(data: Uint8ClampedArray, width: number, height: number): Box {
  const bright = (x: number, y: number) => {
    const i = (y * width + x) * 4;
    return Math.max(data[i], data[i + 1], data[i + 2]) > DARK;
  };
  const darkRow = (y: number) => {
    let lit = 0;
    for (let x = 0; x < width; x++) if (bright(x, y)) lit++;
    return lit <= NOISE * width;
  };
  const darkColumn = (x: number) => {
    let lit = 0;
    for (let y = 0; y < height; y++) if (bright(x, y)) lit++;
    return lit <= NOISE * height;
  };
  const bars = (size: number, dark: (i: number) => boolean): [number, number] => {
    const limit = Math.floor(size * MAX_BAR);
    let start = 0;
    while (start < limit && dark(start)) start++;
    let end = 0;
    while (end < limit && dark(size - 1 - end)) end++;
    if (start >= limit || end >= limit || Math.abs(start - end) > SYMMETRY * size) return [0, 0];
    return [start, end];
  };

  const [top, bottom] = bars(height, darkRow);
  const [left, right] = bars(width, darkColumn);
  return { x: left, y: top, width: width - left - right, height: height - top - bottom };
}

/** The frame's width/height: the picture's own, between square (album covers) and 16:9 (videos). */
export function frameRatio(width: number, height: number): number {
  if (!width || !height) return 1;
  return Math.min(16 / 9, Math.max(1, width / height));
}
