"use client";

import { Activity, useState, useMemo, useEffect, useCallback, memo } from "react";
import { preload } from "swr";
import { useGuildState, useServerPosition } from "./GuildStateProvider";
import { lyricsKey, lyricsFetcher } from "./LyricsPanel";
import { apiFetch } from "@/lib/api";
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
import { useLocalVolume } from "@/hooks/useLocalVolume";
import { ProgressStrip, LiveTime } from "./ui/SeekBar";
import { PlayIcon, PauseIcon, NoteIcon } from "./ui/icons";
import { cn, proxyImg } from "@/lib/utils";

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

// Mobile bottom nav: 4 primary tabs + a "More" sheet (7 tabs don't fit a phone)
const MOBILE_PRIMARY: Exclude<Panel, null>[] = ["search", "queue", "playlists", "lyrics"];
const MOBILE_MORE: Exclude<Panel, null>[] = ["favorites", "stats", "history"];

export default function Dashboard() {
  const [activePanel, setActivePanel] = useState<Panel>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const { state, guildId, eventVersion, sendCommand } = useGuildState();
  const serverPos = useServerPosition();
  const { current } = state;

  const layoutMode = useLayoutMode();
  const [userVolume, setUserVolume] = useLocalVolume();

  const audio = useAudioPlayer(
    guildId,
    current?.webpage_url ?? null,
    current?.duration ?? 0,
    serverPos,
    eventVersion,
    state.speed,
    state.audio_effect,
    userVolume,
    state.is_connected,
  );

  // Fetch + refresh a signed session token for query-string contexts
  // (<audio> stream URLs, WebSocket). Keeps the raw OAuth token out of URLs.
  // guildId is set once Discord auth completes, so it doubles as the auth-ready signal.
  useEffect(() => {
    if (!guildId) return;
    let cancelled = false;
    let refreshTimer: ReturnType<typeof setTimeout> | null = null;

    const fetchToken = async () => {
      try {
        const resp = await apiFetch<{ token: string; expires_in: number }>(
          `/api/guild/${guildId}/session-token`,
        );
        if (cancelled || !resp?.token) return;
        localStorage.setItem("activity_session_token", resp.token);
        const expiresIn = typeof resp.expires_in === "number" ? resp.expires_in : 21600;
        const refreshMs = Math.max(60_000, expiresIn * 0.8 * 1000);
        refreshTimer = setTimeout(fetchToken, refreshMs);
      } catch {
        // Retry shortly if the token fetch fails.
        if (!cancelled) refreshTimer = setTimeout(fetchToken, 60_000);
      }
    };

    fetchToken();
    return () => {
      cancelled = true;
      if (refreshTimer) clearTimeout(refreshTimer);
    };
  }, [guildId]);

  // Warm the lyrics cache shortly after a song starts (after the stream kicks
  // off), so opening the Lyrics panel is instant.
  useEffect(() => {
    const url = current?.webpage_url;
    if (!guildId || !url) return;
    const t = setTimeout(() => {
      preload(lyricsKey(guildId, url), lyricsFetcher(guildId)).catch(() => {});
    }, 1500);
    return () => clearTimeout(t);
  }, [guildId, current?.webpage_url]);

  const getDisplayPosition = useCallback(
    () => (audio.ready ? audio.positionRef.current : serverPos.ref.current.position),
    [audio.ready, audio.positionRef, serverPos],
  );

  useRichPresence(
    getSDK(),
    current ?? null,
    getDisplayPosition,
    audio.ready ? !audio.playing : (current?.is_paused ?? true),
  );

  const handleLyricsSeek = useCallback(
    (seconds: number) => {
      audio.seek(seconds);
      sendCommand("seek", { position: String(Math.floor(seconds)) }).catch(() => {});
    },
    [audio.seek, sendCommand],
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
          <NowPlaying audio={audio} volume={userVolume} onVolumeChange={setUserVolume} />
        </div>
      </div>

      <div className={cn(
        "flex-shrink-0 overflow-hidden transition-[width,opacity] duration-300 ease-[cubic-bezier(0.4,0,0.2,1)]",
        panelOpen
          ? "sm:w-[min(50%,420px)] sm:border-l max-sm:flex-1 opacity-100 border-white/[0.06]"
          : "sm:w-0 max-sm:hidden opacity-0 border-transparent"
      )}>
        {/* Always mounted: panels hide via <Activity> so their local state
            (search results, scroll position) survives tab switches and close. */}
        <div className={cn(
          "h-full w-full bg-surface-2 flex flex-col",
          panelOpen && "animate-[slide-in_0.25s_ease-out]",
        )}>
          {/* Centered: Discord's mobile chrome overlays bot name (left) and Leave (right) */}
          {panelOpen && (
            <div className="hidden max-sm:flex items-center justify-center px-4 py-3 border-b border-white/[0.08] flex-shrink-0">
              <span className="text-sm font-semibold text-white capitalize">{activePanel}</span>
            </div>
          )}
          <div className="flex-1 overflow-hidden">
            <Panels panel={activePanel} getPosition={getDisplayPosition} onSeek={handleLyricsSeek} />
          </div>
        </div>
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
        {panelOpen && current && (
          <MobileMiniPlayer audio={audio} onExpand={() => setActivePanel(null)} />
        )}
        <div className="flex items-center justify-around px-1 pt-1.5 pb-1">
          {MOBILE_PRIMARY.map(id => {
            const { icon, label } = PANELS.find(p => p.id === id)!;
            return (
              <MobileNavBtn key={id} active={activePanel === id} label={label} onClick={() => togglePanel(id)}>
                {icon}
              </MobileNavBtn>
            );
          })}
          <MobileNavBtn
            active={MOBILE_MORE.includes(activePanel as Exclude<Panel, null>)}
            label="More"
            onClick={() => setMoreOpen(true)}
          >
            <svg className="w-[18px] h-[18px]" fill="currentColor" viewBox="0 0 24 24"><path d="M6 10c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm12 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm-6 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z" /></svg>
          </MobileNavBtn>
        </div>
      </div>

      {moreOpen && (
        <MoreSheet
          active={activePanel}
          onSelect={panel => { setMoreOpen(false); togglePanel(panel); }}
          onClose={() => setMoreOpen(false)}
        />
      )}
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
      aria-pressed={active}
    >
      {children}
      <span className="absolute right-full mr-2 px-2 py-1 rounded-lg bg-surface-3 text-[11px] text-white font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity shadow-lg">
        {label}
      </span>
    </button>
  );
}

function MobileNavBtn({ children, active, label, onClick }: {
  children: React.ReactNode; active: boolean; label: string; onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex flex-col items-center justify-center gap-0.5 flex-1 min-w-0 h-11 rounded-xl transition-colors",
        active ? "text-accent bg-accent/10" : "text-muted active:bg-white/[0.06]"
      )}
      aria-pressed={active}
      aria-label={label}
    >
      {children}
      <span className="text-[10px] font-medium leading-none">{label}</span>
    </button>
  );
}

function MoreSheet({ active, onSelect, onClose }: {
  active: Panel;
  onSelect: (panel: Exclude<Panel, null>) => void;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-40 sm:hidden" role="dialog" aria-label="More panels">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div
        className="absolute inset-x-0 bottom-0 bg-surface-2 border-t border-white/[0.08] rounded-t-2xl p-3 animate-[slide-up_0.2s_ease-out]"
        style={{ paddingBottom: "calc(env(safe-area-inset-bottom) + 12px)" }}
      >
        <div className="w-9 h-1 rounded-full bg-white/15 mx-auto mb-3" />
        {MOBILE_MORE.map(id => {
          const { icon, label } = PANELS.find(p => p.id === id)!;
          return (
            <button
              key={id}
              onClick={() => onSelect(id)}
              className={cn(
                "w-full flex items-center gap-3 px-3 h-12 rounded-xl transition-colors",
                active === id ? "text-accent bg-accent/10" : "text-white/80 active:bg-white/[0.06]"
              )}
            >
              {icon}
              <span className="text-sm font-medium">{label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// Memoized so the parent's re-renders don't re-render the open panel
// (props are stable; panels read guild state via context).
// Every panel stays mounted inside <Activity>: hidden ones are display:none
// with state preserved and effects unmounted (so hidden panels don't fetch).
const Panels = memo(function Panels({ panel, getPosition, onSeek }: {
  panel: Panel; getPosition: () => number; onSeek: (seconds: number) => void;
}) {
  const mode = (id: Exclude<Panel, null>) => (panel === id ? "visible" : "hidden");
  return (
    <>
      <Activity mode={mode("queue")}><QueuePanel /></Activity>
      <Activity mode={mode("search")}><SearchPanel /></Activity>
      <Activity mode={mode("history")}><HistoryPanel /></Activity>
      <Activity mode={mode("playlists")}><PlaylistPanel /></Activity>
      <Activity mode={mode("lyrics")}><LyricsPanel getPosition={getPosition} onSeek={onSeek} /></Activity>
      <Activity mode={mode("favorites")}><FavoritesPanel /></Activity>
      <Activity mode={mode("stats")}><StatsPanel /></Activity>
    </>
  );
});

function MobileMiniPlayer({ audio, onExpand }: { audio: AudioPlayerHandle; onExpand: () => void }) {
  const { state } = useGuildState();
  const serverPos = useServerPosition();
  const { current } = state;
  const thumb = useMemo(() => current?.thumbnail ? proxyImg(current.thumbnail) : null, [current?.thumbnail]);

  const getPosition = useCallback(
    () => (audio.ready ? audio.positionRef.current : serverPos.ref.current.position),
    [audio.ready, audio.positionRef, serverPos],
  );

  if (!current) return null;

  const totalDur = audio.ready && audio.duration > 0 ? audio.duration : (current.duration ?? 0);
  const isPaused = audio.ready ? !audio.playing : (current.is_paused ?? true);

  return (
    <div
      className="relative border-b border-white/[0.06] cursor-pointer active:bg-white/[0.03]"
      role="button"
      tabIndex={0}
      aria-label="Back to player"
      onClick={onExpand}
      onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onExpand(); } }}
    >
      {!current.is_live && totalDur > 0 && (
        <ProgressStrip
          duration={totalDur}
          getPosition={getPosition}
          className="absolute top-0 inset-x-0 h-[2px]"
          fillClassName="bg-accent"
        />
      )}

      <div className="flex items-center gap-2.5 px-3 py-2">
        <div className="w-10 h-10 rounded-lg overflow-hidden bg-surface-3 flex-shrink-0">
          {thumb ? (
            <img src={thumb} alt="" className="w-full h-full object-cover" />
          ) : (
            <div className="w-full h-full flex items-center justify-center">
              <NoteIcon className="w-4 h-4 text-muted" />
            </div>
          )}
        </div>

        <div className="flex-1 min-w-0">
          <p className="text-[12px] font-semibold text-white truncate leading-tight">{current.title}</p>
          <p className="text-[10px] text-white/50 truncate mt-0.5 leading-tight">
            {current.is_live
              ? "LIVE"
              : totalDur > 0
                ? <LiveTime get={getPosition} className="font-mono tabular-nums" />
                : current.uploader}
          </p>
        </div>

        <button
          onClick={e => { e.stopPropagation(); audio.playPause(); }}
          className="w-10 h-10 rounded-full bg-white text-surface-1 flex items-center justify-center flex-shrink-0 active:scale-95 transition-transform"
          aria-label={isPaused ? "Play" : "Pause"}
        >
          {isPaused ? <PlayIcon className="w-5 h-5 ml-0.5" /> : <PauseIcon className="w-5 h-5" />}
        </button>
      </div>
    </div>
  );
}
