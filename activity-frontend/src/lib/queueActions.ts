import { apiFetch } from "@/lib/api";

/**
 * Shared "nudge playback" helper for the panels that add songs to the queue
 * (Search/Favorites/History) or load a playlist (Playlist).
 *
 * The queue/add and playlist/load endpoints return `auto_play: true` when the
 * guild is in Activity-only mode and nothing is currently playing. In that case
 * the frontend must kick off playback via POST /play. We skip it when something
 * is already playing (hasCurrent) since the backend keeps advancing on its own.
 *
 * Fire-and-forget: failures are swallowed (the WS broadcast is the source of truth).
 */
export async function maybeAutoPlay(
  guildId: string,
  res: { auto_play?: boolean },
  hasCurrent: boolean,
): Promise<void> {
  if (!res.auto_play || hasCurrent) return;
  try {
    await apiFetch(`/api/guild/${guildId}/play`, { method: "POST" });
  } catch {
    // swallow
  }
}
