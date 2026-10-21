export function proxyImg(url: string | undefined | null): string {
  if (!url) return "";
  return `/api/img?url=${encodeURIComponent(url)}`;
}

export function formatDuration(seconds: number): string {
  if (!seconds || seconds <= 0 || !Number.isFinite(seconds)) return "0:00";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);

  if (h > 0) {
    return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  }
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function formatListenTime(seconds: number): string {
  if (!(seconds > 0)) return "0m";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

/** Joins the parts that are present with " · " (the row-detail separator). */
export function joinDetails(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" · ");
}

/** "Requested by" text without a raw Discord mention. */
export function requesterName(requestedBy: string | undefined): string {
  if (!requestedBy || requestedBy === "Unknown") return "";
  return requestedBy.replace(/<@!?\d+>/, "").trim() || requestedBy;
}

export function cn(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}

export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
