"use client";

import { Activity, useState, useEffect, useEffectEvent, useRef, memo } from "react";
import { useSWRConfig } from "swr";
import { useGuildState, useServerPosition } from "./GuildStateProvider";
import { lyricsKey, lyricsFetcher } from "./LyricsPanel";
import { useAudioPlayer } from "@/hooks/useAudioPlayer";
import { usePlaybackView, type PlaybackView } from "@/hooks/usePlaybackView";
import { useRichPresence } from "@/hooks/useRichPresence";
import { getSDK } from "@/lib/discord-sdk";
import { playbackRate } from "@/lib/playback";
import NowPlaying from "./NowPlaying";
import QueuePanel from "./QueuePanel";
import SearchPanel from "./SearchPanel";
import HistoryPanel from "./HistoryPanel";
import PlaylistPanel from "./PlaylistPanel";
import LyricsPanel from "./LyricsPanel";
import FavoritesPanel from "./FavoritesPanel";
import StatsPanel from "./StatsPanel";
import PiPView from "./PiPView";
import ConnectionStatus from "./ConnectionStatus";
import SettingsPanel from "./SettingsPanel";
import ShortcutsButton from "./ShortcutsButton";
import StatusScreen from "./StatusScreen";
import { SidebarLabel } from "./SidebarLabel";
import { useLayoutMode, LayoutMode } from "@/hooks/useLayoutMode";
import { useLocalVolume } from "@/hooks/useLocalVolume";
import { useTheme } from "@/hooks/useTheme";
import { useTransportControls } from "@/hooks/useTransportControls";
import { useKeyboardShortcuts } from "@/hooks/useKeyboardShortcuts";
import { ProgressStrip, LiveTime } from "./ui/SeekBar";
import {
  PlayIcon, PauseIcon, NoteIcon, SearchIcon, QueueIcon, PlaylistIcon, LyricsIcon, HeartIcon, StatsIcon,
  SettingsIcon, HistoryIcon, MoreIcon, ChevronDownIcon,
} from "./ui/icons";
import { cn, proxyImg } from "@/lib/utils";

type PanelId = "search" | "queue" | "history" | "playlists" | "lyrics" | "favorites" | "stats" | "settings";
type Panel = PanelId | null;

const ICON = "w-[18px] h-[18px]";

const PANELS: { id: PanelId; icon: React.ReactNode; label: string }[] = [
  { id: "search", label: "Search", icon: <SearchIcon className={ICON} /> },
  { id: "queue", label: "Queue", icon: <QueueIcon className={ICON} /> },
  { id: "playlists", label: "Playlists", icon: <PlaylistIcon className={ICON} /> },
  { id: "lyrics", label: "Lyrics", icon: <LyricsIcon className={ICON} /> },
  { id: "favorites", label: "Favorites", icon: <HeartIcon className={ICON} /> },
  { id: "stats", label: "Stats", icon: <StatsIcon className={ICON} /> },
  { id: "settings", label: "Settings", icon: <SettingsIcon className={ICON} /> },
  { id: "history", label: "History", icon: <HistoryIcon className={ICON} /> },
];

const panelMeta = (id: PanelId) => PANELS.find(p => p.id === id)!;

// Mobile bottom nav: 4 primary tabs + a "More" sheet (7 tabs don't fit a phone)
const MOBILE_PRIMARY: PanelId[] = ["search", "queue", "playlists", "lyrics"];
const MOBILE_MORE: PanelId[] = ["favorites", "stats", "history", "settings"];

// Lyrics are warmed shortly after a song starts (after the stream kicks off).
const LYRICS_PRELOAD_MS = 1500;

export default function Dashboard() {
  const [activePanel, setActivePanel] = useState<Panel>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const { state, guildId, eventVersion, fatal, retryConnection } = useGuildState();
  const serverPos = useServerPosition();
  const { mutate } = useSWRConfig();
  const { current } = state;
  const moreButtonRef = useRef<HTMLButtonElement>(null);

  const layoutMode = useLayoutMode();
  const [userVolume, setUserVolume] = useLocalVolume();
  useTheme(); // loads the listener's saved theme on launch

  const audio = useAudioPlayer({
    guildId,
    current,
    serverPos,
    eventVersion,
    speed: state.speed,
    audioEffect: state.audio_effect,
    volume: userVolume,
    isConnected: state.is_connected,
  });
  const view = usePlaybackView(audio);
  const transport = useTransportControls(audio, view, userVolume, setUserVolume);

  // Fill the lyrics cache only on success, so a failed warm-up is not replayed when the panel opens.
  const currentUrl = current?.webpage_url;
  useEffect(() => {
    if (!guildId || !currentUrl) return;
    const key = lyricsKey(guildId, currentUrl);
    const t = setTimeout(() => {
      lyricsFetcher(guildId)(key).then(data => mutate(key, data, { revalidate: false }), () => {});
    }, LYRICS_PRELOAD_MS);
    return () => clearTimeout(t);
  }, [guildId, currentUrl, mutate]);

  useRichPresence(getSDK(), current, view.getPosition, view.isPaused || audio.blocked, playbackRate(state.speed, state.audio_effect));

  const handleLyricsSeek = (seconds: number) => transport.seekTo(seconds);

  const focusSearch = () => {
    setActivePanel("search");
    // The panel is already mounted but display:none while hidden — focus once
    // the open state has painted.
    requestAnimationFrame(() => {
      const input = document.querySelector<HTMLInputElement>('[data-panel="search"] input');
      input?.focus();
      input?.select();
    });
  };

  useKeyboardShortcuts({
    ...transport,
    toggleShortcuts: () => setShortcutsOpen(open => !open),
    focusSearch,
  });

  if (fatal) {
    return <StatusScreen kind="error" message={fatal} action={{ label: "Try again", onClick: retryConnection }} />;
  }

  const togglePanel = (panel: PanelId) => {
    setActivePanel(prev => prev === panel ? null : panel);
  };

  const panelOpen = activePanel !== null;
  const pip = layoutMode === LayoutMode.PiP;

  return (
    <>
      {pip && <PiPView view={view} blocked={audio.blocked} onPlayPause={transport.playPause} />}
      {/* Kept mounted (hidden) in PiP, so panels keep their state through a minimise. */}
      <Activity mode={pip ? "hidden" : "visible"}>
        <div className="flex max-sm:flex-col h-dvh bg-surface-1 text-fg overflow-hidden">
          <div className={cn(
            "flex flex-col flex-1 min-w-0 min-h-0",
            panelOpen && "max-sm:hidden"
          )}>
            <div className="flex-1 overflow-hidden">
              <NowPlaying audio={audio} view={view} transport={transport} volume={userVolume} onVolumeChange={setUserVolume} />
            </div>
          </div>

          {/* The panel takes its width at once and only its content slides in: animating width would relayout every frame. */}
          <div className={cn(
            "flex-shrink-0 overflow-hidden",
            panelOpen
              ? "sm:w-[min(50%,420px)] sm:border-l max-sm:flex-1 border-fg/[0.06]"
              : "sm:w-0 max-sm:hidden"
          )}>
            {/* Always mounted: panels hide via <Activity> so their local state
                (search results, scroll position) survives tab switches and close. */}
            <div className={cn(
              "h-full w-full bg-surface-2 flex flex-col",
              panelOpen && "animate-[slide-in_0.2s_ease-out]",
            )}>
              {/* Centered: Discord's mobile chrome overlays bot name (left) and Leave (right) */}
              {activePanel && (
                <div className="hidden max-sm:flex items-center justify-center px-4 py-2 border-b border-fg/[0.08] flex-shrink-0">
                  <button
                    type="button"
                    onClick={() => setActivePanel(null)}
                    aria-label={`Close ${panelMeta(activePanel).label} and go back to the player`}
                    className="flex items-center gap-1 min-h-9 px-3 rounded-full text-sm font-semibold text-fg active:bg-fg/[0.06]"
                  >
                    <ChevronDownIcon className="w-4 h-4 text-muted" />
                    {panelMeta(activePanel).label}
                  </button>
                </div>
              )}
              <div className="flex-1 overflow-hidden">
                <Panels panel={activePanel} getPosition={view.getPosition} onSeek={handleLyricsSeek} onSearch={focusSearch} />
              </div>
            </div>
          </div>

          <div className="max-sm:hidden flex flex-col items-center gap-1 py-3 px-1.5 bg-surface-1 flex-shrink-0">
            {PANELS.map(({ id, icon, label }) => (
              <SidebarBtn key={id} active={activePanel === id} label={label} onClick={() => togglePanel(id)}>
                {icon}
              </SidebarBtn>
            ))}
            <ShortcutsButton open={shortcutsOpen} onOpenChange={setShortcutsOpen} />
          </div>

          <div
            className="hidden max-sm:flex flex-col flex-shrink-0 bg-surface-1 border-t border-fg/[0.06]"
            style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
          >
            {panelOpen && current && (
              <MobileMiniPlayer view={view} blocked={audio.blocked} onPlayPause={transport.playPause} onExpand={() => setActivePanel(null)} />
            )}
            <div className="flex items-center justify-around px-1 pt-1.5 pb-1">
              {MOBILE_PRIMARY.map(id => {
                const { icon, label } = panelMeta(id);
                return (
                  <MobileNavBtn key={id} active={activePanel === id} label={label} onClick={() => togglePanel(id)}>
                    {icon}
                  </MobileNavBtn>
                );
              })}
              <MobileNavBtn
                ref={moreButtonRef}
                active={MOBILE_MORE.includes(activePanel as PanelId)}
                label="More"
                expanded={moreOpen}
                onClick={() => setMoreOpen(true)}
              >
                <MoreIcon className={ICON} />
              </MobileNavBtn>
            </div>
          </div>

          <ConnectionStatus />

          {moreOpen && (
            <MoreSheet
              active={activePanel}
              onSelect={panel => { setMoreOpen(false); setActivePanel(panel); }}
              onClose={() => setMoreOpen(false)}
              returnFocusRef={moreButtonRef}
            />
          )}
        </div>
      </Activity>
    </>
  );
}

function SidebarBtn({ children, active, label, onClick }: {
  children: React.ReactNode; active: boolean; label: string; onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "w-10 h-10 rounded-xl flex items-center justify-center transition-all duration-150 group relative",
        active
          ? "bg-accent text-white shadow-[0_0_12px_var(--color-accent-glow)]"
          : "text-muted hover:text-fg hover:bg-fg/[0.06]"
      )}
      aria-label={label}
      aria-pressed={active}
    >
      {children}
      <SidebarLabel>{label}</SidebarLabel>
    </button>
  );
}

function MobileNavBtn({ children, active, label, onClick, expanded, ref }: {
  children: React.ReactNode; active: boolean; label: string; onClick: () => void; expanded?: boolean;
  ref?: React.Ref<HTMLButtonElement>;
}) {
  return (
    <button
      ref={ref}
      type="button"
      onClick={onClick}
      className={cn(
        "flex flex-col items-center justify-center gap-0.5 flex-1 min-w-0 h-11 rounded-xl transition-colors",
        active ? "text-accent bg-accent/10" : "text-muted active:bg-fg/[0.06]"
      )}
      aria-pressed={expanded === undefined ? active : undefined}
      aria-haspopup={expanded !== undefined ? "dialog" : undefined}
      aria-expanded={expanded}
      aria-label={label}
    >
      {children}
      <span className="text-[10px] font-medium leading-none" aria-hidden="true">{label}</span>
    </button>
  );
}

function MoreSheet({ active, onSelect, onClose, returnFocusRef }: {
  active: Panel;
  onSelect: (panel: PanelId) => void;
  onClose: () => void;
  returnFocusRef: React.RefObject<HTMLButtonElement | null>;
}) {
  const sheetRef = useRef<HTMLDivElement>(null);
  const onEscape = useEffectEvent((e: KeyboardEvent) => {
    if (e.key === "Escape") onClose();
  });

  useEffect(() => {
    sheetRef.current?.querySelector<HTMLButtonElement>("button")?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => onEscape(e);
    document.addEventListener("keydown", onKey);
    const returnTo = returnFocusRef.current;
    return () => {
      document.removeEventListener("keydown", onKey);
      returnTo?.focus({ preventScroll: true });
    };
  }, [returnFocusRef]);

  return (
    <div className="fixed inset-0 z-40 sm:hidden" role="dialog" aria-modal="true" aria-label="More panels">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} aria-hidden="true" />
      <div
        ref={sheetRef}
        className="absolute inset-x-0 bottom-0 bg-surface-2 border-t border-fg/[0.08] rounded-t-2xl p-3 animate-[slide-up_0.2s_ease-out]"
        style={{ paddingBottom: "calc(env(safe-area-inset-bottom) + 12px)" }}
      >
        <div className="w-9 h-1 rounded-full bg-fg/15 mx-auto mb-3" aria-hidden="true" />
        {MOBILE_MORE.map(id => {
          const { icon, label } = panelMeta(id);
          return (
            <button
              key={id}
              type="button"
              onClick={() => onSelect(id)}
              aria-current={active === id ? "page" : undefined}
              className={cn(
                "w-full flex items-center gap-3 px-3 h-12 rounded-xl transition-colors",
                active === id ? "text-accent bg-accent/10" : "text-fg/80 active:bg-fg/[0.06]"
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
const Panels = memo(function Panels({ panel, getPosition, onSeek, onSearch }: {
  panel: Panel; getPosition: () => number; onSeek: (seconds: number) => void; onSearch: () => void;
}) {
  const mode = (id: PanelId) => (panel === id ? "visible" : "hidden");
  return (
    <>
      <Activity mode={mode("queue")}><QueuePanel onSearch={onSearch} /></Activity>
      {/* data-panel: the "/" shortcut focuses this panel's input from Dashboard */}
      <Activity mode={mode("search")}><div data-panel="search" className="h-full"><SearchPanel /></div></Activity>
      <Activity mode={mode("history")}><HistoryPanel /></Activity>
      <Activity mode={mode("playlists")}><PlaylistPanel /></Activity>
      <Activity mode={mode("lyrics")}><LyricsPanel getPosition={getPosition} onSeek={onSeek} /></Activity>
      <Activity mode={mode("favorites")}><FavoritesPanel /></Activity>
      <Activity mode={mode("stats")}><StatsPanel /></Activity>
      <Activity mode={mode("settings")}><SettingsPanel /></Activity>
    </>
  );
});

// The whole bar returns to the player; the play button sits above that as its own control.
function MobileMiniPlayer({ view, blocked, onPlayPause, onExpand }: {
  view: PlaybackView; blocked: boolean; onPlayPause: () => void; onExpand: () => void;
}) {
  const { current, totalDur, getPosition } = view;
  if (!current) return null;
  const isPaused = view.isPaused || blocked;
  const thumb = current.thumbnail ? proxyImg(current.thumbnail) : null;

  return (
    <div className="relative border-b border-fg/[0.06] active:bg-fg/[0.03]">
      <button
        type="button"
        onClick={onExpand}
        aria-label={`Back to the player: ${current.title}`}
        className="absolute inset-0 cursor-pointer"
      />
      {!current.is_live && totalDur > 0 && (
        <ProgressStrip
          duration={totalDur}
          getPosition={getPosition}
          className="absolute top-0 inset-x-0 h-[2px] pointer-events-none"
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
          <p className="text-sm font-semibold text-fg truncate leading-tight">{current.title}</p>
          <p className="text-xs text-muted truncate mt-0.5 leading-tight">
            {current.is_live
              ? "LIVE"
              : totalDur > 0
                ? <LiveTime get={getPosition} className="font-mono tabular-nums" />
                : current.uploader}
          </p>
        </div>

        <button
          type="button"
          onClick={onPlayPause}
          className="relative w-10 h-10 rounded-full bg-fg text-surface-1 flex items-center justify-center flex-shrink-0 active:scale-95 transition-transform"
          aria-label={blocked ? "Tap to play" : isPaused ? "Play" : "Pause"}
        >
          {isPaused ? <PlayIcon className="w-5 h-5 ml-0.5" /> : <PauseIcon className="w-5 h-5" />}
        </button>
      </div>
    </div>
  );
}

