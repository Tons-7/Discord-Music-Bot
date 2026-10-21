// SWR keys for playlist data, built in one place so every mutate() matches the hook that reads it.

export function playlistListKey(guildId: string, globalMode: boolean): string {
  return `/api/guild/${guildId}/playlists?global_mode=${globalMode}`;
}

export function playlistDetailKey(guildId: string, name: string, globalMode: boolean, ownerId: string | null): string {
  const owner = ownerId ? `&owner_id=${ownerId}` : "";
  return `/api/guild/${guildId}/playlists/${encodeURIComponent(name)}?global_mode=${globalMode}${owner}`;
}

/** Matches the detail view of `name` with or without an owner_id, but not its /collabs view. */
export function isPlaylistDetailKey(key: unknown, guildId: string, name: string, globalMode: boolean): boolean {
  return typeof key === "string"
    && key.startsWith(`/api/guild/${guildId}/playlists/${encodeURIComponent(name)}?global_mode=${globalMode}`);
}

export function collabsKey(guildId: string, name: string, globalMode: boolean): string {
  return `/api/guild/${guildId}/playlists/${encodeURIComponent(name)}/collabs?global_mode=${globalMode}`;
}

export const PLAYLIST_NAME_MAX = 50;

/** Why a new playlist name can't be used, or null when it's fine. Mirrors the slash command's rules. */
export function playlistNameError(raw: string): string | null {
  const name = raw.trim();
  if (!name) return "Enter a name";
  if (name.length > PLAYLIST_NAME_MAX) return `Use ${PLAYLIST_NAME_MAX} characters or fewer`;
  if (name.includes("/")) return "Names can't contain /";
  return null;
}
