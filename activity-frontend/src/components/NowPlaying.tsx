"use client";

import { useState, useRef } from "react";
import { useGuildState } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { formatDuration, cn, proxyImg } from "@/lib/utils";
import { errorMessage } from "@/lib/api";
import { openExternal } from "@/lib/discord-sdk";
import { AUDIO_EFFECTS, SPEEDS, effectLabel } from "@/lib/playback";
import MarqueeText from "./MarqueeText";
import FavHeart from "./FavHeart";
import AddToPlaylistButton from "./AddToPlaylistButton";
import InviteButton from "./InviteButton";
import ListenersBar from "./ListenersBar";
import Popover from "./ui/Popover";
import { useConfirm } from "./ui/ConfirmButton";
import { SeekBar, LiveTime } from "./ui/SeekBar";
import { NowPlayingTextSkeleton } from "./ui/Skeleton";
import {
  PlayIcon, PauseIcon, NoteIcon, InfinityIcon, StopIcon, ShuffleIcon, SkipPrevIcon, SkipNextIcon,
  RepeatIcon, RepeatOneIcon, VolumeIcon, VolumeOffIcon, AlertIcon,
} from "./ui/icons";
import type { AudioPlayerHandle } from "@/hooks/useAudioPlayer";
import type { PlaybackView } from "@/hooks/usePlaybackView";
import type { TransportControls } from "@/hooks/useTransportControls";
import type { LoopMode } from "@/types";

const NEXT_LOOP: Record<LoopMode, LoopMode> = { off: "song", song: "queue", queue: "off" };
const LOOP_LABEL: Record<LoopMode, string> = { off: "Off", song: "This song", queue: "Queue" };
const LASTFM_URL = "https://www.last.fm/";

export default function NowPlaying({ audio, view, transport, volume, onVolumeChange }: {
  audio: AudioPlayerHandle;
  view: PlaybackView;
  transport: TransportControls;
  volume: number;
  onVolumeChange: (v: number) => void;
}) {
  const { state, sendCommand, channelId, hasState } = useGuildState();
  const { toast } = useToast();
  const { current, totalDur, isPaused, getPosition, noNext, hasSong } = view;
  const stopConfirm = useConfirm();

  // In a voice channel the bot plays one shared stream, so volume stays a
  // server-wide command. Activity audio is per listener — keep it local.
  const sharedVolume = state.is_connected;
  const sourceVol = sharedVolume ? state.volume : volume;
  // While the slider moves it shows its own value; a sent shared level shows until the server's changes.
  const [draft, setDraft] = useState<number | null>(null);
  const [committed, setCommitted] = useState<{ value: number; base: number } | null>(null);
  const shownVol = draft ?? (sharedVolume && committed && state.volume === committed.base ? committed.value : sourceVol);

  const [expanded, setExpanded] = useState<"speed" | "fx" | "volume" | null>(null);
  const speedAnchorRef = useRef<HTMLButtonElement>(null);
  const fxAnchorRef = useRef<HTMLButtonElement>(null);
  const volAnchorRef = useRef<HTMLButtonElement>(null);
  const closeExpanded = () => setExpanded(null);
  const toggle = (which: "speed" | "fx" | "volume") => setExpanded(expanded === which ? null : which);

  // Toasts report what the server did, not what the button showed before the click.
  const run = <T,>(action: string, data: Record<string, unknown> | undefined, done: (res: T) => string) => {
    sendCommand<T>(action, data).then(
      (res) => toast(done(res)),
      (e: unknown) => toast(errorMessage(e, "Couldn't change that"), "error"),
    );
  };

  const setSpeed = (rate: number) => {
    setExpanded(null);
    run<{ speed?: number }>("speed", { rate }, r => `Speed ${r.speed ?? rate}x`);
  };

  const setEffect = (effect: string) => {
    setExpanded(null);
    run<{ audio_effect?: string }>("effects", { effect }, r => {
      const applied = r.audio_effect ?? effect;
      return applied === "none" ? "Effects off" : `${effectLabel(applied)} on`;
    });
  };

  const handleStop = () => {
    if (!stopConfirm.press(true)) return;
    const stopped = current?.webpage_url;
    sendCommand("stop").then(
      // The broadcast tears down every listener's audio; this only spares the wait.
      () => audio.stop(stopped),
      (e: unknown) => toast(errorMessage(e, "Couldn't stop"), "error"),
    );
  };

  const commitVolume = () => {
    if (draft === null) return;
    const v = draft;
    setDraft(null);
    if (!sharedVolume) {
      onVolumeChange(v);
      return;
    }
    setCommitted({ value: v, base: state.volume });
    sendCommand("volume", { level: v }).catch((e: unknown) => {
      setCommitted(null);
      toast(errorMessage(e, "Couldn't change the volume"), "error");
    });
  };

  const volumeInput = (className: string) => (
    <input
      type="range" min={0} max={100} value={shownVol} aria-label="Volume" aria-valuetext={`${shownVol}%`}
      onChange={(e) => {
        const v = Number(e.target.value);
        setDraft(v);
        audio.setVolume(v);
      }}
      onPointerUp={commitVolume}
      onPointerCancel={commitVolume}
      onKeyUp={commitVolume}
      onBlur={commitVolume}
      className={className}
    />
  );

  const thumb = current?.thumbnail ? proxyImg(current.thumbnail) : null;
  const elsewhere = state.is_connected && !!state.voice_channel_id && !!channelId && state.voice_channel_id !== channelId;
  const autoplayPick = current?.requested_by === "Autoplay" && current.autoplay_source !== "youtube";

  return (
    <div className="relative h-full w-full bg-surface-1 flex flex-col">
      {thumb && <img src={thumb} alt="" className="absolute inset-0 w-full h-full object-cover blur-3xl scale-125 opacity-20" />}
      <div className="absolute inset-0 bg-gradient-to-b from-surface-1/50 via-surface-1/30 to-surface-1/80" />

      {/* Below Discord's top-corner overlay on phones */}
      <div className="absolute top-3 max-sm:top-12 right-3 z-20 flex items-center gap-2">
        <ListenersBar />
        <InviteButton />
      </div>

      {/* Size container: art stays square, sized by the smaller available axis */}
      <div className="relative z-10 flex-1 flex items-center justify-center p-4 max-sm:p-3 min-h-0 min-w-0 [container-type:size]">
        <div className="relative aspect-square w-[min(100cqw,100cqh)] rounded-2xl overflow-hidden shadow-[0_8px_40px_rgba(0,0,0,0.5)]">
          {thumb ? (
            <img src={thumb} alt="" className="w-full h-full object-cover" />
          ) : (
            <div className="w-full h-full bg-gradient-to-br from-accent/20 via-accent/5 to-transparent flex items-center justify-center">
              <NoteIcon className="w-14 h-14 text-accent/30" />
            </div>
          )}
          {current?.is_live && (
            <div className="absolute top-2 right-2 flex items-center gap-1 px-2 py-0.5 rounded-full bg-danger">
              <span className="w-1.5 h-1.5 rounded-full bg-white animate-pulse" />
              <span className="text-[11px] font-bold text-white">LIVE</span>
            </div>
          )}
          {hasSong && audio.error && (
            <div className="absolute inset-0 z-20 bg-black/70 backdrop-blur-sm flex flex-col items-center justify-center gap-3 px-6 text-center" role="alert">
              <AlertIcon className="w-8 h-8 text-white/80" strokeWidth={1.5} />
              <p className="text-sm font-medium text-white">{audio.error}</p>
              <div className="flex gap-2">
                <button type="button" onClick={audio.retry} className="min-h-8 px-4 rounded-full bg-white text-black text-xs font-semibold">
                  Try again
                </button>
                {!noNext && (
                  <button type="button" onClick={transport.next} className="min-h-8 px-4 rounded-full bg-white/15 text-white text-xs font-semibold hover:bg-white/25">
                    Skip
                  </button>
                )}
              </div>
            </div>
          )}
          {hasSong && !audio.error && audio.blocked && (
            <button
              type="button"
              onClick={audio.resume}
              className="absolute inset-0 z-20 bg-black/60 backdrop-blur-sm flex flex-col items-center justify-center gap-2"
            >
              <div className="w-14 h-14 rounded-full bg-white text-black flex items-center justify-center">
                <PlayIcon className="w-7 h-7 ml-0.5" />
              </div>
              <span className="text-xs font-medium text-white">Tap to play</span>
            </button>
          )}
        </div>
      </div>

      {elsewhere && (
        <p className="relative z-10 mx-auto -mt-2 mb-1 px-3 py-1 rounded-full bg-surface-3/80 border border-fg/[0.08] text-xs text-muted text-center max-w-[calc(100%-2rem)] truncate" role="status">
          Playing in 🔊 {state.voice_channel_name || "another voice channel"}. Join it to listen.
        </p>
      )}

      <div className="relative z-10 flex-shrink-0 p-4 max-sm:p-3 @container">
        <div className="w-full max-w-4xl mx-auto rounded-2xl bg-surface-3/80 backdrop-blur-lg border border-fg/[0.08] px-8 py-6 @max-[42rem]:px-4 @max-[42rem]:py-3.5 flex flex-col gap-3">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              {current ? (
                <>
                  <MarqueeText className="text-sm font-semibold text-fg select-text">{current.title}</MarqueeText>
                  <p className="text-xs text-muted truncate mt-0.5">
                    {current.uploader}
                    {autoplayPick && (
                      <>
                        {current.uploader ? " · " : ""}
                        <button
                          type="button"
                          onClick={() => openExternal(LASTFM_URL)}
                          className="hover:text-fg hover:underline underline-offset-2"
                          title="Autoplay recommendations come from Last.fm"
                        >
                          via Last.fm
                        </button>
                      </>
                    )}
                  </p>
                </>
              ) : hasState ? (
                <p className="text-sm text-muted truncate">Nothing playing</p>
              ) : (
                <NowPlayingTextSkeleton />
              )}
            </div>
            {current && (
              <div className="flex items-center gap-1 flex-shrink-0">
                <AddToPlaylistButton song={current} size="md" />
                <FavHeart webpageUrl={current.webpage_url} title={current.title} duration={current.duration} thumbnail={current.thumbnail} uploader={current.uploader} size="md" />
              </div>
            )}
          </div>

          {current && !current.is_live && (
            <div className="flex items-center gap-2.5">
              <LiveTime get={getPosition} className="text-xs font-mono tabular-nums text-muted min-w-10 flex-shrink-0" />
              <SeekBar
                duration={totalDur}
                getPosition={getPosition}
                onSeek={transport.seekTo}
                disabled={!totalDur}
                className="flex-1"
              />
              <span className="text-xs font-mono tabular-nums text-muted min-w-10 text-right flex-shrink-0">{formatDuration(totalDur)}</span>
            </div>
          )}

          <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-2 @max-[42rem]:grid-cols-1 @max-[42rem]:justify-items-center">
            {/* Secondary controls */}
            <div className="flex items-center gap-2 min-w-0 overflow-x-auto @max-[42rem]:order-2 @max-[42rem]:w-full @max-[42rem]:justify-center">
              <Pill ref={speedAnchorRef} active={expanded === "speed" || state.speed !== 1.0}
                label={`Playback speed: ${state.speed}x`} expanded={expanded === "speed"}
                onClick={() => toggle("speed")}>
                {state.speed}x
              </Pill>
              <Popover open={expanded === "speed"} onClose={closeExpanded} anchorRef={speedAnchorRef} role="menu" label="Playback speed" className="min-w-[64px]">
                {SPEEDS.map(s => (
                  <button key={s} type="button" role="menuitemradio" aria-checked={s === state.speed}
                    onClick={() => setSpeed(s)}
                    className={cn("block w-full text-center px-2 py-1.5 rounded-lg text-sm font-medium transition-colors",
                      s === state.speed ? "bg-accent text-white" : "text-fg/80 hover:bg-fg/[0.08]")}>
                    {s}x</button>))}
              </Popover>
              <Pill ref={fxAnchorRef} active={expanded === "fx" || state.audio_effect !== "none"}
                label={`Audio effect: ${effectLabel(state.audio_effect)}`} expanded={expanded === "fx"}
                onClick={() => toggle("fx")}>
                {state.audio_effect === "none" ? "FX" : effectLabel(state.audio_effect)}
              </Pill>
              <Popover open={expanded === "fx"} onClose={closeExpanded} anchorRef={fxAnchorRef} role="menu" label="Audio effect" className="min-w-[132px]">
                {AUDIO_EFFECTS.map(({ id, label }) => (
                  <button key={id} type="button" role="menuitemradio" aria-checked={id === state.audio_effect}
                    onClick={() => setEffect(id)}
                    className={cn("block w-full text-left px-3 py-1.5 rounded-lg text-sm font-medium transition-colors whitespace-nowrap",
                      id === state.audio_effect ? "bg-accent text-white" : "text-fg/80 hover:bg-fg/[0.08]")}>
                    {label}</button>))}
              </Popover>
              <Pill active={state.autoplay} pressed={state.autoplay} label="Autoplay"
                onClick={() => run<{ autoplay?: boolean }>("autoplay", undefined, r => ((r.autoplay ?? !state.autoplay) ? "Autoplay on" : "Autoplay off"))}>
                <InfinityIcon className="w-4 h-4" />
              </Pill>
              <Pill active={stopConfirm.armed === true} danger={stopConfirm.armed === true}
                label={stopConfirm.armed ? "Press again to stop and clear the queue" : "Stop and clear the queue"}
                onClick={handleStop}>
                <StopIcon className="w-3.5 h-3.5" />
                {stopConfirm.armed && <span>Stop &amp; clear?</span>}
              </Pill>
            </div>

            {/* Transport: shuffle · prev · play · next · loop */}
            <div className="flex items-center gap-1.5 @max-[42rem]:order-1 @max-[42rem]:gap-2.5">
              <ToggleBtn
                active={state.shuffle}
                label="Shuffle"
                onClick={() => run<{ shuffle?: boolean }>("shuffle", undefined, r => ((r.shuffle ?? !state.shuffle) ? "Shuffle on" : "Shuffle off"))}
              >
                <ShuffleIcon />
              </ToggleBtn>
              <Btn onClick={transport.previous} disabled={state.played.length === 0 && (!hasSong || state.history.length === 0)} label="Previous">
                <SkipPrevIcon className="w-5 h-5" />
              </Btn>
              <button type="button" onClick={transport.playPause} disabled={!hasSong}
                aria-label={isPaused ? "Play" : "Pause"}
                className={cn("w-12 h-12 rounded-full flex items-center justify-center transition-all",
                  hasSong ? "bg-fg text-surface-1 hover:scale-105 active:scale-95" : "bg-fg/10 text-fg/20 cursor-not-allowed"
                )}>
                {isPaused ? <PlayIcon className="w-6 h-6 ml-0.5" /> : <PauseIcon className="w-6 h-6" />}
              </button>
              <Btn onClick={transport.next} disabled={!hasSong || noNext} label="Next">
                <SkipNextIcon className="w-5 h-5" />
              </Btn>
              <ToggleBtn
                active={state.loop_mode !== "off"}
                label={`Loop: ${LOOP_LABEL[state.loop_mode]}`}
                onClick={() => {
                  const mode = NEXT_LOOP[state.loop_mode];
                  run<{ loop_mode?: LoopMode }>("loop", { mode }, r => `Loop: ${LOOP_LABEL[r.loop_mode ?? mode]}`);
                }}
              >
                {state.loop_mode === "song" ? <RepeatOneIcon /> : <RepeatIcon />}
              </ToggleBtn>
            </div>

            {/* Volume */}
            <div className="flex items-center gap-1.5 justify-end @max-[42rem]:hidden">
              <Btn label={shownVol === 0 ? "Unmute" : "Mute"} onClick={transport.toggleMute}>
                {shownVol === 0 ? <VolumeOffIcon className="w-3.5 h-3.5" /> : <VolumeIcon className="w-3.5 h-3.5" />}
              </Btn>
              {volumeInput("w-16 h-0.5")}
            </div>

            {/* Narrow layout: volume as a popover */}
            <div className="hidden @max-[42rem]:flex items-center @max-[42rem]:order-3">
              <button
                ref={volAnchorRef}
                type="button"
                onClick={() => toggle("volume")}
                aria-label={`Volume: ${shownVol}%`}
                aria-haspopup="dialog"
                aria-expanded={expanded === "volume"}
                className="h-7 px-2.5 rounded-full text-xs font-semibold flex items-center gap-1 text-muted active:bg-fg/[0.08]"
              >
                {shownVol === 0 ? <VolumeOffIcon className="w-3.5 h-3.5" /> : <VolumeIcon className="w-3.5 h-3.5" />}
                {shownVol}%
              </button>
              <Popover open={expanded === "volume"} onClose={closeExpanded} anchorRef={volAnchorRef} label="Volume" className="px-3 py-2.5">
                {volumeInput("w-36")}
              </Popover>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Btn({ children, onClick, disabled, label }: { children: React.ReactNode; onClick: () => void; disabled?: boolean; label: string }) {
  return (
    <button type="button" onClick={onClick} disabled={disabled} aria-label={label} title={label}
      className={cn("relative w-10 h-10 rounded-full flex items-center justify-center transition-all",
        "pointer-coarse:after:absolute pointer-coarse:after:-inset-1 pointer-coarse:after:content-['']",
        disabled ? "text-fg/20 cursor-not-allowed" : "text-fg/80 hover:text-fg hover:bg-fg/[0.12] active:scale-90"
      )}>{children}</button>
  );
}

// Transport toggle (shuffle/loop): accent color + dot underneath when active
function ToggleBtn({ children, active, onClick, label }: {
  children: React.ReactNode; active: boolean; onClick: () => void; label: string;
}) {
  return (
    <button type="button" onClick={onClick} aria-label={label} title={label} aria-pressed={active}
      className={cn("relative w-9 h-9 rounded-full flex items-center justify-center transition-all active:scale-90",
        "pointer-coarse:after:absolute pointer-coarse:after:-inset-1 pointer-coarse:after:content-['']",
        active ? "text-accent" : "text-muted hover:text-fg hover:bg-fg/[0.08]"
      )}>
      {children}
      {active && <span className="absolute bottom-0.5 left-1/2 -translate-x-1/2 w-1 h-1 rounded-full bg-accent" />}
    </button>
  );
}

// `pressed` only for real toggles, `expanded` only for popover triggers.
function Pill({ children, active, pressed, expanded, danger, disabled, onClick, label, ref }: {
  children: React.ReactNode; active?: boolean; pressed?: boolean; expanded?: boolean; danger?: boolean;
  disabled?: boolean; onClick: () => void; label: string;
  ref?: React.Ref<HTMLButtonElement>;
}) {
  return (
    <button ref={ref} type="button" onClick={onClick} disabled={disabled} aria-label={label} title={label}
      aria-pressed={pressed}
      aria-haspopup={expanded !== undefined ? "menu" : undefined}
      aria-expanded={expanded}
      className={cn("relative h-7 px-2.5 rounded-full text-xs font-semibold flex items-center gap-1 transition-all flex-shrink-0 whitespace-nowrap disabled:opacity-40",
        "pointer-coarse:after:absolute pointer-coarse:after:-inset-y-1.5 pointer-coarse:after:-inset-x-0.5 pointer-coarse:after:content-['']",
        danger ? "bg-danger text-white"
          : active ? "bg-accent/20 text-accent" : "text-muted hover:text-fg hover:bg-fg/[0.08]"
      )}>{children}</button>
  );
}

