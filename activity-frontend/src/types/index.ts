export interface Song {
  url: string;
  title: string;
  duration: number;
  thumbnail: string;
  uploader: string;
  webpage_url: string;
  requested_by: string;
  is_live: boolean;
}

export interface CurrentSong extends Song {
  position: number;
  is_paused: boolean;
}

export type LoopMode = "off" | "song" | "queue";

export interface GuildState {
  current: CurrentSong | null;
  queue: Song[];
  history: Song[];
  volume: number;
  loop_mode: LoopMode;
  shuffle: boolean;
  autoplay: boolean;
  speed: number;
  audio_effect: string;
  is_connected: boolean;
  queue_duration: number;
}

export interface SearchResult {
  title: string;
  duration: number;
  thumbnail: string;
  uploader: string;
  webpage_url: string;
  url: string;
}

export type PlaylistPermission = "view" | "append" | "edit";
export type PlaylistAccess = PlaylistPermission | "owner";

// Mirrors PLAYLIST_PERMISSIONS / PLAYLIST_PERMISSION_RANK in config.py
export const PLAYLIST_PERMISSIONS: PlaylistPermission[] = ["view", "append", "edit"];
const PERMISSION_RANK: Record<string, number> = { view: 1, append: 2, edit: 3, owner: 4 };

export function playlistCan(level: PlaylistAccess | undefined, needed: PlaylistAccess): boolean {
  return (level ? PERMISSION_RANK[level] ?? 0 : 0) >= PERMISSION_RANK[needed];
}

export interface Playlist {
  name: string;
  song_count: number;
  thumbnail?: string;
  permission: PlaylistAccess;
  // Display name of the owner, null when it is the user's own playlist
  owner: string | null;
  // Names are unique per owner, so this is needed to address a shared playlist
  owner_id: string;
}

export interface PlaylistSong {
  title: string;
  uploader: string;
  duration: number;
  webpage_url: string;
  thumbnail?: string;
}

export interface Member {
  id: string;
  display_name: string;
  username: string;
  avatar: string | null;
}

export interface Collab {
  id: string;
  display_name: string;
  avatar: string | null;
  permission: PlaylistPermission;
}
