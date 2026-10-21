/** Response of POST /api/guild/{id}/queue/add. */
export interface QueueAddResult {
  ok: boolean;
  added: number;
  skipped?: number;
  duplicate?: boolean;
  playing?: boolean;
  position?: number;
  title?: string;
  playlist?: boolean;
  in_background?: boolean;
  total?: number;
  capped?: boolean;
  // Why the list was cut short: Spotify's 100-track embed, the queue's size cap, or a very long list
  capped_reason?: "spotify" | "queue" | "list" | null;
}

const CAP_NOTES = {
  spotify: "Spotify only shares the first 100",
  queue: "the queue is full",
  list: "only the first part of a very long list",
} as const;

function capNote(res: QueueAddResult): string {
  if (!res.capped) return "";
  return ` (${CAP_NOTES[res.capped_reason ?? "spotify"] ?? CAP_NOTES.spotify})`;
}

/** Plain-language notice for a song that was not added because it is already queued or playing. */
export function duplicateMessage(res: QueueAddResult, title: string): string {
  if (res.playing) return `"${title}" is playing now`;
  if (res.position) return `"${title}" is already in the queue (#${res.position})`;
  return `"${title}" is already in the queue`;
}

/** Toast text for adding a whole playlist or album link. */
export function bulkAddMessage(res: QueueAddResult, shown: number): string {
  if (res.duplicate) return `All ${res.skipped ?? shown} songs are already in the queue`;
  if (res.in_background) {
    return `Adding ${res.total ?? shown} songs${capNote(res)}, they'll join the queue as they're found`;
  }
  const skipped = res.skipped ?? 0;
  const added = `Added ${res.added} song${res.added === 1 ? "" : "s"}${capNote(res)}`;
  return skipped > 0 ? `${added} (${skipped} already in the queue)` : added;
}
