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

export interface Playlist {
  name: string;
  song_count: number;
}

export interface PlaylistSong {
  title: string;
  uploader: string;
  duration: number;
  webpage_url: string;
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
}
