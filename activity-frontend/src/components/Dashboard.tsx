"use client";

import { useState, useMemo } from "react";
import { useGuildState } from "./GuildStateProvider";
import { useAudioPlayer, type AudioPlayerHandle } from "@/hooks/useAudioPlayer";
import { useRichPresence } from "@/hooks/useRichPresence";
import { getSDK } from "@/lib/discord-sdk";
import NowPlaying from "./NowPlaying";
import QueuePanel from "./QueuePanel";
import SearchPanel from "./SearchPanel";
import HistoryPanel from "./HistoryPanel";
import PlaylistPanel from "./PlaylistPanel";
import LyricsPanel from "./LyricsPanel";
import FavoritesPanel from "./FavoritesPanel";
import StatsPanel from "./StatsPanel";
import PiPView from "./PiPView";
import { useLayoutMode, LayoutMode } from "@/hooks/useLayoutMode";
import { cn, formatDuration, proxyImg } from "@/lib/utils";

type Panel = "search" | "queue" | "history" | "playlists" | "lyrics" | "favorites" | "stats" | null;

const PANELS: { id: Exclude<Panel, null>; icon: React.ReactNode; label: string }[] = [
  {
    id: "search", label: "Search",
    icon: <svg className="w-[18px] h-[18px]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" /></svg>,
  },
  {
    id: "queue", label: "Queue",
    icon: <svg className="w-[18px] h-[18px]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 10h16M4 14h10" /></svg>,
  },
  {
    id: "playlists", label: "Playlists",
    icon: <svg className="w-[18px] h-[18px]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 010 3.75H5.625a1.875 1.875 0 010-3.75z" /></svg>,
  },
  {
    id: "lyrics", label: "Lyrics",
    icon: <svg className="w-[18px] h-[18px]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M9 9l10.5-3m0 6.553v3.75a2.25 2.25 0 01-1.632 2.163l-1.32.377a1.803 1.803 0 11-.99-3.467l2.31-.66a2.25 2.25 0 001.632-2.163zm0 0V2.25L9 5.25v10.303m0 0v3.75a2.25 2.25 0 01-1.632 2.163l-1.32.377a1.803 1.803 0 01-.99-3.467l2.31-.66A2.25 2.25 0 009 15.553z" /></svg>,
  },
  {
    id: "favorites", label: "Favorites",
    icon: <svg className="w-[18px] h-[18px]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z" /></svg>,
  },
  {
    id: "stats", label: "Stats",
    icon: <svg className="w-[18px] h-[18px]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" /></svg>,
  },
  {
    id: "history", label: "History",
    icon: <svg className="w-[18px] h-[18px]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>,
  },
];

export default function Dashboard() {
  const [activePanel, setActivePanel] = useState<Panel>(null);
  const { state, guildId, eventVersion } = useGuildState();
  const { current } = state;

  const layoutMode = useLayoutMode();

  const audio = useAudioPlayer(
    guildId,
    current?.webpage_url ?? null,
    current?.duration ?? 0,
    current?.position ?? 0,
    current?.is_paused ?? false,
    eventVersion,
    state.speed,
    state.audio_effect,
    state.volume,
  );

  useRichPresence(
    getSDK(),
    current ?? null,
    audio.ready ? audio.position : (current?.position ?? 0),
    audio.ready ? !audio.playing : (current?.is_paused ?? true),
  );

  if (layoutMode === LayoutMode.PiP) {
    return <PiPView audio={audio} />;
  }

  const togglePanel = (panel: Exclude<Panel, null>) => {
    setActivePanel(prev => prev === panel ? null : panel);
  };

  const panelOpen = activePanel !== null;

  return (
    <div className="flex max-sm:flex-col h-dvh bg-surface-1 text-white overflow-hidden">
      <div className={cn(
        "flex flex-col flex-1 min-w-0 min-h-0",
        panelOpen && "max-sm:hidden"
      )}>
        <div className="flex-1 overflow-hidden">
          <NowPlaying audio={audio} compact={panelOpen} />
        </div>
      </div>

      <div className={cn(
        "flex-shrink-0 overflow-hidden transition-[width,opacity] duration-300 ease-[cubic-bezier(0.4,0,0.2,1)]",
        panelOpen
          ? "sm:w-[50%] sm:border-l max-sm:flex-1 opacity-100 border-white/[0.06]"
          : "sm:w-0 max-sm:hidden opacity-0 border-transparent"
      )}>
        {panelOpen && (
          <div className="h-full w-full bg-surface-2 flex flex-col animate-[slide-in_0.25s_ease-out]">
            <div className="hidden max-sm:flex items-center px-4 py-3 border-b border-white/[0.08] flex-shrink-0">
              <span className="text-sm font-semibold text-white capitalize">{activePanel}</span>
            </div>
            <div className="flex-1 overflow-hidden">
              {activePanel === "queue" && <QueuePanel />}
              {activePanel === "search" && <SearchPanel />}
              {activePanel === "history" && <HistoryPanel />}
              {activePanel === "playlists" && <PlaylistPanel />}
              {activePanel === "lyrics" && <LyricsPanel positionRef={audio.positionRef} />}
              {activePanel === "favorites" && <FavoritesPanel />}
              {activePanel === "stats" && <StatsPanel />}
            </div>
          </div>
        )}
      </div>

      <div className="max-sm:hidden flex flex-col items-center gap-1 py-3 px-1.5 bg-surface-1 flex-shrink-0">
        {PANELS.map(({ id, icon, label }) => (
          <SidebarBtn key={id} active={activePanel === id} label={label} onClick={() => togglePanel(id)}>
            {icon}
          </SidebarBtn>
        ))}
      </div>

      <div
        className="hidden max-sm:flex flex-col flex-shrink-0 bg-surface-1 border-t border-white/[0.06]"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        {panelOpen && current && <MobileMiniPlayer audio={audio} />}
        <div className="flex items-center justify-around px-1 pt-1.5 pb-1">
          {PANELS.map(({ id, icon, label }) => (
            <button
              key={id}
              onClick={() => togglePanel(id)}
              className={cn(
                "flex flex-col items-center justify-center gap-0.5 flex-1 min-w-0 h-11 rounded-xl transition-colors",
                activePanel === id ? "text-accent bg-accent/10" : "text-muted active:bg-white/[0.06]"
              )}
              aria-pressed={activePanel === id}
              aria-label={label}
            >
              {icon}
              <span className="text-[9px] font-medium leading-none">{label}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function SidebarBtn({ children, active, label, onClick }: {
  children: React.ReactNode; active: boolean; label: string; onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "w-10 h-10 rounded-xl flex items-center justify-center transition-all duration-150 group relative",
        active
          ? "bg-accent text-white shadow-[0_0_12px_var(--color-accent-glow)]"
          : "text-muted hover:text-white hover:bg-white/[0.06]"
      )}
      title={label}
    >
      {children}
      <span className="absolute right-full mr-2 px-2 py-1 rounded-lg bg-surface-3 text-[11px] text-white font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity shadow-lg">
        {label}
      </span>
    </button>
  );
}

type MiniAudio = Pick<AudioPlayerHandle, "playing" | "ready" | "position" | "duration" | "playPause">;

function MobileMiniPlayer({ audio }: { audio: MiniAudio }) {
  const { state } = useGuildState();
  const { current } = state;
  const thumb = useMemo(() => current?.thumbnail ? proxyImg(current.thumbnail) : null, [current?.thumbnail]);
  if (!current) return null;

  const displayPos = audio.ready ? audio.position : (current.position ?? 0);
  const totalDur = audio.ready && audio.duration > 0 ? audio.duration : (current.duration ?? 0);
  const progress = totalDur > 0 ? Math.min((displayPos / totalDur) * 100, 100) : 0;
  const isPaused = audio.ready ? !audio.playing : (current.is_paused ?? true);

  return (
    <div className="relative border-b border-white/[0.06]">
      {!current.is_live && totalDur > 0 && (
        <div className="absolute top-0 inset-x-0 h-[2px] bg-white/10">
          <div
            className="h-full bg-accent transition-[width] duration-200 ease-linear"
            style={{ width: `${progress}%` }}
          />
        </div>
      )}

      <div className="flex items-center gap-2.5 px-3 py-2">
        <div className="w-10 h-10 rounded-lg overflow-hidden bg-surface-3 flex-shrink-0">
          {thumb ? (
            <img src={thumb} alt="" className="w-full h-full object-cover" />
          ) : (
            <div className="w-full h-full flex items-center justify-center">
              <svg className="w-4 h-4 text-muted" fill="currentColor" viewBox="0 0 24 24"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z" /></svg>
            </div>
          )}
        </div>

        <div className="flex-1 min-w-0">
          <p className="text-[12px] font-semibold text-white truncate leading-tight">{current.title}</p>
          <p className="text-[10px] text-white/50 truncate mt-0.5 leading-tight">
            {current.is_live
              ? "LIVE"
              : totalDur > 0
                ? `${formatDuration(displayPos)} / ${formatDuration(totalDur)}`
                : current.uploader}
          </p>
        </div>

        <button
          onClick={audio.playPause}
          className="w-10 h-10 rounded-full bg-white text-surface-1 flex items-center justify-center flex-shrink-0 active:scale-95 transition-transform"
          aria-label={isPaused ? "Play" : "Pause"}
        >
          {isPaused
            ? <svg className="w-5 h-5 ml-0.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>
            : <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" /></svg>}
        </button>
      </div>
    </div>
  );
}
