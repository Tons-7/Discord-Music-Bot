"use client";

import { useState, useEffect, useCallback } from "react";
import { useGuildState } from "./GuildStateProvider";
import { apiFetch } from "@/lib/api";
import { proxyImg, cn } from "@/lib/utils";

type Tab = "me" | "leaderboard";

interface MyStats {
  play_count: number;
  total_seconds: number;
  top_songs: { title: string; plays: number }[];
  top_artists: { name: string; plays: number }[];
}

interface LeaderboardEntry {
  user_id: string;
  display_name: string;
  avatar_url: string | null;
  play_count: number;
  total_seconds: number;
}

function formatListenTime(seconds: number): string {
  if (seconds <= 0) return "0m";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export default function StatsPanel() {
  const { guildId } = useGuildState();
  const [tab, setTab] = useState<Tab>("me");
  const [myStats, setMyStats] = useState<MyStats | null>(null);
  const [leaderboard, setLeaderboard] = useState<LeaderboardEntry[]>([]);
  const [yourId, setYourId] = useState<string>("");
  const [loading, setLoading] = useState(true);

  const fetchMyStats = useCallback(async () => {
    try {
      const d = await apiFetch<MyStats>(`/api/guild/${guildId}/stats/me`);
      setMyStats(d);
    } catch { setMyStats(null); }
  }, [guildId]);

  const fetchLeaderboard = useCallback(async () => {
    try {
      const d = await apiFetch<{ entries: LeaderboardEntry[]; your_id: string }>(
        `/api/guild/${guildId}/stats/leaderboard`
      );
      setLeaderboard(d.entries);
      setYourId(d.your_id);
    } catch { setLeaderboard([]); }
  }, [guildId]);

  useEffect(() => {
    setLoading(true);
    Promise.all([fetchMyStats(), fetchLeaderboard()]).finally(() => setLoading(false));
  }, [fetchMyStats, fetchLeaderboard]);

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="w-5 h-5 border-2 border-accent border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Tab bar */}
      <div className="px-3 pt-3 pb-1 flex-shrink-0">
        <div className="flex bg-white/[0.04] rounded-xl p-[3px]">
          <button
            onClick={() => setTab("me")}
            className={cn(
              "flex-1 py-1.5 rounded-[9px] text-[11px] font-semibold transition-[background-color,color,box-shadow] duration-200",
              tab === "me"
                ? "bg-accent text-white shadow-[0_1px_4px_rgba(88,101,242,0.3)]"
                : "text-white/40 hover:text-white/60"
            )}
          >
            My Stats
          </button>
          <button
            onClick={() => setTab("leaderboard")}
            className={cn(
              "flex-1 py-1.5 rounded-[9px] text-[11px] font-semibold transition-[background-color,color,box-shadow] duration-200",
              tab === "leaderboard"
                ? "bg-accent text-white shadow-[0_1px_4px_rgba(88,101,242,0.3)]"
                : "text-white/40 hover:text-white/60"
            )}
          >
            Leaderboard
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-3 pb-3">
        {tab === "me" ? (
          <MyStatsView stats={myStats} />
        ) : (
          <LeaderboardView entries={leaderboard} yourId={yourId} />
        )}
      </div>
    </div>
  );
}

function MyStatsView({ stats }: { stats: MyStats | null }) {
  if (!stats || stats.play_count === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center px-6">
        <div className="w-12 h-12 rounded-2xl bg-white/[0.03] flex items-center justify-center mb-2">
          <svg className="w-6 h-6 text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
          </svg>
        </div>
        <p className="text-xs font-medium text-white/60">No listening data yet</p>
        <p className="text-[10px] text-muted mt-1">Play some music to start tracking</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 mt-2">
      {/* Overview cards */}
      <div className="grid grid-cols-2 gap-2">
        <div className="bg-white/[0.03] rounded-xl p-3 border border-white/[0.04]">
          <p className="text-[10px] text-muted uppercase tracking-wider font-medium">Songs Played</p>
          <p className="text-xl font-bold text-white mt-1">{stats.play_count.toLocaleString()}</p>
        </div>
        <div className="bg-white/[0.03] rounded-xl p-3 border border-white/[0.04]">
          <p className="text-[10px] text-muted uppercase tracking-wider font-medium">Listen Time</p>
          <p className="text-xl font-bold text-white mt-1">{formatListenTime(stats.total_seconds)}</p>
        </div>
      </div>

      {/* Top Songs */}
      {stats.top_songs.length > 0 && (
        <div>
          <h3 className="text-[11px] font-semibold text-white/50 uppercase tracking-wider px-1 mb-1.5">
            Top Songs
          </h3>
          <div className="flex flex-col gap-1">
            {stats.top_songs.map((song, i) => (
              <div
                key={song.title}
                className="flex items-center gap-2.5 p-2 rounded-xl bg-white/[0.02] border border-white/[0.04]"
              >
                <span className={cn(
                  "w-5 text-center text-xs font-bold flex-shrink-0",
                  i === 0 ? "text-yellow-400" : i === 1 ? "text-gray-300" : i === 2 ? "text-amber-600" : "text-muted"
                )}>
                  {i + 1}
                </span>
                <span className="text-sm text-white truncate flex-1">{song.title}</span>
                <span className="text-[11px] text-muted tabular-nums flex-shrink-0">
                  {song.plays} play{song.plays !== 1 ? "s" : ""}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Top Artists */}
      {stats.top_artists.length > 0 && (
        <div>
          <h3 className="text-[11px] font-semibold text-white/50 uppercase tracking-wider px-1 mb-1.5">
            Top Artists
          </h3>
          <div className="flex flex-col gap-1">
            {stats.top_artists.map((artist, i) => (
              <div
                key={artist.name}
                className="flex items-center gap-2.5 p-2 rounded-xl bg-white/[0.02] border border-white/[0.04]"
              >
                <span className={cn(
                  "w-5 text-center text-xs font-bold flex-shrink-0",
                  i === 0 ? "text-yellow-400" : i === 1 ? "text-gray-300" : i === 2 ? "text-amber-600" : "text-muted"
                )}>
                  {i + 1}
                </span>
                <span className="text-sm text-white truncate flex-1">{artist.name}</span>
                <span className="text-[11px] text-muted tabular-nums flex-shrink-0">
                  {artist.plays} play{artist.plays !== 1 ? "s" : ""}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function LeaderboardView({ entries, yourId }: { entries: LeaderboardEntry[]; yourId: string }) {
  if (entries.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center px-6">
        <div className="w-12 h-12 rounded-2xl bg-white/[0.03] flex items-center justify-center mb-2">
          <svg className="w-6 h-6 text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 18.75h-9m9 0a3 3 0 013 3h-15a3 3 0 013-3m9 0v-3.375c0-.621-.503-1.125-1.125-1.125h-.871M7.5 18.75v-3.375c0-.621.504-1.125 1.125-1.125h.872m5.007 0H9.497m5.007 0a7.454 7.454 0 01-.982-3.172M9.497 14.25a7.454 7.454 0 00.981-3.172M5.25 4.236c-.982.143-1.954.317-2.916.52A6.003 6.003 0 007.73 9.728M5.25 4.236V4.5c0 2.108.966 3.99 2.48 5.228M5.25 4.236V2.721C7.456 2.41 9.71 2.25 12 2.25c2.291 0 4.545.16 6.75.47v1.516M18.75 4.236c.982.143 1.954.317 2.916.52A6.003 6.003 0 0016.27 9.728M18.75 4.236V4.5c0 2.108-.966 3.99-2.48 5.228m4.974-5.492a10.509 10.509 0 01-1.292 4.18M7.73 9.728a6.726 6.726 0 002.748 1.35m3.043 0a6.726 6.726 0 002.749-1.35m-5.792 0a24.585 24.585 0 003.043 0" />
          </svg>
        </div>
        <p className="text-xs font-medium text-white/60">No leaderboard data</p>
        <p className="text-[10px] text-muted mt-1">Start listening to climb the ranks</p>
      </div>
    );
  }

  const maxSeconds = entries[0]?.total_seconds || 1;

  return (
    <div className="flex flex-col gap-1.5 mt-2">
      {entries.map((entry, i) => {
        const isYou = entry.user_id === yourId;
        const barWidth = Math.max(4, (entry.total_seconds / maxSeconds) * 100);

        return (
          <div
            key={entry.user_id}
            className={cn(
              "relative flex items-center gap-2.5 p-2.5 rounded-xl border transition-colors",
              isYou
                ? "bg-accent/[0.06] border-accent/20"
                : "bg-white/[0.02] border-white/[0.04]"
            )}
          >
            {/* Progress bar background */}
            <div
              className={cn(
                "absolute inset-0 rounded-xl opacity-[0.04]",
                i === 0 ? "bg-yellow-400" : i === 1 ? "bg-gray-300" : i === 2 ? "bg-amber-600" : "bg-white"
              )}
              style={{ width: `${barWidth}%` }}
            />

            {/* Rank */}
            <span className={cn(
              "w-6 text-center text-sm font-bold flex-shrink-0 relative",
              i === 0 ? "text-yellow-400" : i === 1 ? "text-gray-300" : i === 2 ? "text-amber-600" : "text-muted"
            )}>
              {i < 3 ? ["1st", "2nd", "3rd"][i] : `${i + 1}`}
            </span>

            {/* Avatar */}
            <div className="w-8 h-8 rounded-full overflow-hidden bg-surface-3 flex-shrink-0 relative">
              {entry.avatar_url ? (
                <img src={proxyImg(entry.avatar_url)} alt="" className="w-full h-full object-cover" loading="lazy" />
              ) : (
                <div className="w-full h-full flex items-center justify-center text-muted text-xs font-bold">
                  {entry.display_name.charAt(0).toUpperCase()}
                </div>
              )}
            </div>

            {/* Name & stats */}
            <div className="flex-1 min-w-0 relative">
              <p className={cn("text-sm font-medium truncate", isYou ? "text-accent" : "text-white")}>
                {entry.display_name}
                {isYou && <span className="text-[10px] text-accent/60 ml-1">(you)</span>}
              </p>
              <p className="text-[11px] text-muted">
                {formatListenTime(entry.total_seconds)} &middot; {entry.play_count.toLocaleString()} play{entry.play_count !== 1 ? "s" : ""}
              </p>
            </div>
          </div>
        );
      })}
    </div>
  );
}
