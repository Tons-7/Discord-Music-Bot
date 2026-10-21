"use client";

import { useState } from "react";
import useSWR from "swr";
import { useGuildId } from "./GuildStateProvider";
import EmptyState, { ErrorState } from "./EmptyState";
import SegmentedControl from "./ui/SegmentedControl";
import { StatsSkeleton } from "./ui/Skeleton";
import { StatsIcon, TrophyIcon } from "./ui/icons";
import { proxyImg, cn, formatListenTime, joinDetails } from "@/lib/utils";

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

const TABS: { value: Tab; label: string }[] = [
  { value: "me", label: "My stats" },
  { value: "leaderboard", label: "Leaderboard" },
];

// Gold, silver, bronze for the top three, as theme tokens
const RANK_TEXT = ["text-rank-1", "text-rank-2", "text-rank-3"];
const RANK_BG = ["bg-rank-1", "bg-rank-2", "bg-rank-3"];

const plays = (n: number) => `${n.toLocaleString()} play${n !== 1 ? "s" : ""}`;

export default function StatsPanel() {
  const [tab, setTab] = useState<Tab>("me");

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 pt-3 pb-1 flex-shrink-0">
        <SegmentedControl kind="tabs" label="Stats" options={TABS} value={tab} onChange={setTab} />
      </div>

      <div className="flex-1 overflow-y-auto px-3 pb-3" role="tabpanel" aria-label={tab === "me" ? "My stats" : "Leaderboard"}>
        {tab === "me" ? <MyStatsView /> : <LeaderboardView />}
      </div>
    </div>
  );
}

function MyStatsView() {
  const { guildId } = useGuildId();
  const { data: stats, error, isLoading, mutate } = useSWR<MyStats>(`/api/guild/${guildId}/stats/me`);

  if (isLoading) return <StatsSkeleton />;
  if (error && !stats) return <ErrorState compact message="Couldn't load your stats" onRetry={() => mutate()} />;

  if (!stats || stats.play_count === 0) {
    return (
      <EmptyState
        compact
        icon={<StatsIcon className="w-6 h-6" strokeWidth={1.5} />}
        title="No listening data yet"
        subtitle="Play some music to start tracking"
      />
    );
  }

  return (
    <div className="flex flex-col gap-3 mt-2">
      {/* Overview cards */}
      <div className="grid grid-cols-2 gap-2">
        <div className="bg-fg/[0.03] rounded-xl p-3 border border-fg/[0.04]">
          <p className="text-xs text-muted uppercase tracking-wider font-medium">Songs played</p>
          <p className="text-xl font-bold text-fg mt-1 tabular-nums">{stats.play_count.toLocaleString()}</p>
        </div>
        <div className="bg-fg/[0.03] rounded-xl p-3 border border-fg/[0.04]">
          <p className="text-xs text-muted uppercase tracking-wider font-medium">Listen time</p>
          <p className="text-xl font-bold text-fg mt-1 tabular-nums">{formatListenTime(stats.total_seconds)}</p>
        </div>
      </div>

      <RankedList title="Top songs" items={stats.top_songs.map(s => ({ key: s.title, label: s.title, plays: s.plays }))} />
      <RankedList title="Top artists" items={stats.top_artists.map(a => ({ key: a.name, label: a.name, plays: a.plays }))} />
    </div>
  );
}

function RankedList({ title, items }: { title: string; items: { key: string; label: string; plays: number }[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <h3 className="text-xs font-semibold text-muted uppercase tracking-wider px-1 mb-1.5">{title}</h3>
      <ol className="flex flex-col gap-1">
        {items.map((item, i) => (
          <li key={item.key} className="flex items-center gap-2.5 p-2 rounded-xl bg-fg/[0.02] border border-fg/[0.04]">
            <span className={cn("min-w-5 text-center text-xs font-bold tabular-nums flex-shrink-0", RANK_TEXT[i] ?? "text-muted")}>
              {i + 1}
            </span>
            <span className="text-sm text-fg truncate flex-1">{item.label}</span>
            <span className="text-xs text-muted tabular-nums flex-shrink-0">{plays(item.plays)}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function LeaderboardView() {
  const { guildId } = useGuildId();
  const { data, error, isLoading, mutate } = useSWR<{ entries: LeaderboardEntry[]; your_id: string }>(
    `/api/guild/${guildId}/stats/leaderboard`
  );

  if (isLoading) return <StatsSkeleton />;
  if (error && !data) return <ErrorState compact message="Couldn't load the leaderboard" onRetry={() => mutate()} />;

  const entries = data?.entries ?? [];
  const yourId = data?.your_id ?? "";

  if (entries.length === 0) {
    return (
      <EmptyState
        compact
        icon={<TrophyIcon className="w-6 h-6" strokeWidth={1.5} />}
        title="No leaderboard data"
        subtitle="Start listening to climb the ranks"
      />
    );
  }

  const maxSeconds = entries[0]?.total_seconds || 1;

  return (
    <ol className="flex flex-col gap-1.5 mt-2">
      {entries.map((entry, i) => {
        const isYou = entry.user_id === yourId;
        const barWidth = Math.max(4, (entry.total_seconds / maxSeconds) * 100);

        return (
          <li
            key={entry.user_id}
            className={cn(
              "relative flex items-center gap-2.5 p-2.5 rounded-xl border transition-colors",
              isYou ? "bg-accent/[0.06] border-accent/20" : "bg-fg/[0.02] border-fg/[0.04]"
            )}
          >
            {/* Progress bar background */}
            <div
              aria-hidden="true"
              className={cn("absolute inset-0 rounded-xl opacity-[0.04]", RANK_BG[i] ?? "bg-fg")}
              style={{ width: `${barWidth}%` }}
            />

            {/* Rank */}
            <span className={cn("min-w-8 text-center text-sm font-bold tabular-nums flex-shrink-0 relative", RANK_TEXT[i] ?? "text-muted")}>
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
              <p className={cn("text-sm font-medium truncate", isYou ? "text-accent" : "text-fg")}>
                {entry.display_name}
                {isYou && <span className="text-xs text-accent ml-1">(you)</span>}
              </p>
              <p className="text-xs text-muted tabular-nums">
                {joinDetails(formatListenTime(entry.total_seconds), plays(entry.play_count))}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
