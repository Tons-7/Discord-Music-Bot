"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useGuildState, useServerPosition } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { formatDuration, cn, proxyImg } from "@/lib/utils";
import { apiFetch } from "@/lib/api";
import MarqueeText from "./MarqueeText";
import FavHeart from "./FavHeart";
import InviteButton from "./InviteButton";
import Popover from "./ui/Popover";
import { SeekBar, LiveTime } from "./ui/SeekBar";
import { PlayIcon, PauseIcon, NoteIcon } from "./ui/icons";
import type { AudioPlayerHandle } from "@/hooks/useAudioPlayer";
import type { LoopMode } from "@/types";

const NEXT_LOOP: Record<LoopMode, LoopMode> = { off: "song", song: "queue", queue: "off" };
const AUDIO_EFFECTS: readonly [string, string][] = [
  ["none", "Off"], ["bass_boost", "Bass"], ["nightcore", "NC"],
  ["vaporwave", "Vap"], ["treble_boost", "Treb"], ["8d", "8D"],
];
const SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0];

export default function NowPlaying({ audio }: { audio: AudioPlayerHandle }) {
  const { state, guildId, sendCommand } = useGuildState();
  const serverPos = useServerPosition();
  const { toast } = useToast();
  const { current } = state;

  const isConnected = state.is_connected;
  const getDisplayPosition = useCallback(
    () => (audio.ready ? audio.positionRef.current : (isConnected ? serverPos.ref.current.position : 0)),
    [audio.ready, audio.positionRef, isConnected, serverPos],
  );

  const totalDur = audio.ready && audio.duration > 0 ? audio.duration : (current?.duration ?? 0);
  const isPaused = audio.ready ? !audio.playing : (current?.is_paused ?? true);
  const hasSong = !!current;

  const [localVol, setLocalVol] = useState(state.volume);
  const [dragging, setDragging] = useState(false);
  useEffect(() => { if (!dragging) setLocalVol(state.volume); }, [state.volume, dragging]);

  const [expanded, setExpanded] = useState<"speed" | "fx" | "volume" | null>(null);
  const speedAnchorRef = useRef<HTMLButtonElement>(null);
  const fxAnchorRef = useRef<HTMLButtonElement>(null);
  const volAnchorRef = useRef<HTMLButtonElement>(null);
  const closeExpanded = useCallback(() => setExpanded(null), []);

  const noNext = state.queue.length === 0 && !state.autoplay && !state.is_connected && state.loop_mode === "off";

  const handleSkip = useCallback(async () => {
    if (noNext) return;
    audio.stop();
    if (state.is_connected) await sendCommand("skip");
    // force=true marks a user-initiated skip; plain /play no-ops while playing
    else await apiFetch(`/api/guild/${guildId}/play?force=true`, { method: "POST" }).catch(() => {});
  }, [noNext, state.is_connected, audio, sendCommand, guildId]);

  const handlePrevious = useCallback(async () => {
    audio.stop();
    await sendCommand("previous");
  }, [audio, sendCommand]);

  const handleStop = useCallback(async () => {
    audio.stop();
    await sendCommand("stop");
  }, [audio, sendCommand]);

  const handleSeek = useCallback((seconds: number) => {
    // Match the server clamp (duration - 5) so seeking to the bar's end can't
    // fire 'ended' and skip the song
    const clamped = totalDur > 0
      ? Math.min(seconds, Math.max(0, Math.floor(totalDur) - 5))
      : seconds;
    audio.seek(clamped);
    sendCommand("seek", { position: String(clamped) })
      .catch((e: any) => toast(e?.message || "Seek failed", "error"));
  }, [audio, sendCommand, totalDur, toast]);

  const commitVolume = useCallback((v: number) => {
    sendCommand("volume", { level: v }).catch(() => {});
  }, [sendCommand]);

  const thumb = useMemo(() => current?.thumbnail ? proxyImg(current.thumbnail) : null, [current?.thumbnail]);

  return (
    <div className="relative h-full w-full bg-surface-1 flex flex-col">
      {thumb && <img src={thumb} alt="" className="absolute inset-0 w-full h-full object-cover blur-3xl scale-125 opacity-20" />}
      <div className="absolute inset-0 bg-gradient-to-b from-surface-1/50 via-surface-1/30 to-surface-1/80" />

      <div className="absolute top-3 right-3 z-20">
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
            <div className="absolute top-2 right-2 flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-500">
              <span className="w-1.5 h-1.5 rounded-full bg-white animate-pulse" />
              <span className="text-[9px] font-bold text-white">LIVE</span>
            </div>
          )}
          {audio.blocked && hasSong && !state.is_connected && (
            <button
              onClick={audio.resume}
              className="absolute inset-0 z-20 bg-black/60 backdrop-blur-sm flex flex-col items-center justify-center gap-2"
            >
              <div className="w-14 h-14 rounded-full bg-white text-surface-1 flex items-center justify-center">
                <PlayIcon className="w-7 h-7 ml-0.5" />
              </div>
              <span className="text-xs font-medium text-white">Tap to play</span>
            </button>
          )}
        </div>
      </div>

      <div className="relative z-10 flex-shrink-0 p-4 max-sm:p-3 @container">
        <div className="w-full max-w-4xl mx-auto rounded-2xl bg-surface-3/80 backdrop-blur-lg border border-white/[0.08] px-8 py-6 @max-[42rem]:px-4 @max-[42rem]:py-3.5 flex flex-col gap-3">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              {hasSong ? (
                <>
                  <MarqueeText className="text-sm font-semibold text-white select-text">{current.title}</MarqueeText>
                  <p className="text-[11px] text-white/50 truncate mt-0.5">{current.uploader}</p>
                </>
              ) : (
                <p className="text-sm text-white/50 truncate">Nothing playing</p>
              )}
            </div>
            {hasSong && <FavHeart webpageUrl={current.webpage_url} title={current.title} duration={current.duration} thumbnail={current.thumbnail} uploader={current.uploader} size="md" />}
          </div>

          {hasSong && !current.is_live && (
            <div className="flex items-center gap-2.5">
              <LiveTime get={getDisplayPosition} className="text-[10px] font-mono tabular-nums text-white/50 w-9 flex-shrink-0" />
              <SeekBar
                duration={totalDur}
                getPosition={getDisplayPosition}
                onSeek={handleSeek}
                disabled={!totalDur}
                className="flex-1"
              />
              <span className="text-[10px] font-mono tabular-nums text-white/50 w-9 text-right flex-shrink-0">{formatDuration(totalDur)}</span>
            </div>
          )}

          <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-2 @max-[42rem]:grid-cols-1 @max-[42rem]:justify-items-center">
            {/* Secondary controls */}
            <div className="flex items-center gap-2 min-w-0 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden @max-[42rem]:order-2 @max-[42rem]:w-full @max-[42rem]:justify-center">
              <Pill ref={speedAnchorRef} active={expanded === "speed" || state.speed !== 1.0} label="Playback speed"
                onClick={() => setExpanded(expanded === "speed" ? null : "speed")}>
                {state.speed}x
              </Pill>
              <Popover open={expanded === "speed"} onClose={closeExpanded} anchorRef={speedAnchorRef} className="min-w-[48px]">
                {SPEEDS.map(s => (
                  <button key={s} onClick={() => { sendCommand("speed", { rate: s }); setExpanded(null); toast(`Speed ${s}x`); }}
                    className={cn("block w-full text-center px-2 py-1.5 rounded-lg text-[10px] font-medium transition-colors", s === state.speed ? "bg-accent text-white" : "text-white/70 hover:bg-white/[0.08]")}>
                    {s}x</button>))}
              </Popover>
              <Pill ref={fxAnchorRef} active={expanded === "fx" || state.audio_effect !== "none"} label="Audio effects"
                onClick={() => setExpanded(expanded === "fx" ? null : "fx")}>
                FX
              </Pill>
              <Popover open={expanded === "fx"} onClose={closeExpanded} anchorRef={fxAnchorRef} className="min-w-[80px]">
                {AUDIO_EFFECTS.map(([id, l]) => (
                  <button key={id} onClick={() => { sendCommand("effects", { effect: id }); setExpanded(null); toast(id === "none" ? "Effects off" : l); }}
                    className={cn("block w-full text-left px-3 py-1.5 rounded-lg text-[10px] font-medium transition-colors whitespace-nowrap", id === state.audio_effect ? "bg-accent text-white" : "text-white/70 hover:bg-white/[0.08]")}>
                    {l}</button>))}
              </Popover>
              <Pill active={state.autoplay} label="Autoplay" onClick={() => { sendCommand("autoplay"); toast(state.autoplay ? "Autoplay off" : "Autoplay on"); }}>
                <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z" /></svg>
              </Pill>
              <Pill active={false} label="Stop" onClick={handleStop}>
                <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 6h12v12H6z" /></svg>
              </Pill>
            </div>

            {/* Transport: shuffle · prev · play · next · loop */}
            <div className="flex items-center gap-1.5 @max-[42rem]:order-1 @max-[42rem]:gap-2.5">
              <ToggleBtn
                active={state.shuffle}
                label="Shuffle"
                onClick={() => { sendCommand("shuffle"); toast(state.shuffle ? "Shuffle off" : "Shuffle on"); }}
              >
                <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z" /></svg>
              </ToggleBtn>
              <Btn onClick={handlePrevious} disabled={!hasSong || state.history.length === 0} label="Previous">
                <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z" /></svg>
              </Btn>
              <button onClick={audio.playPause} disabled={!hasSong}
                aria-label={isPaused ? "Play" : "Pause"}
                className={cn("w-12 h-12 rounded-full flex items-center justify-center transition-all",
                  hasSong ? "bg-white text-surface-1 hover:scale-105 active:scale-95" : "bg-white/10 text-white/20 cursor-not-allowed"
                )}>
                {isPaused ? <PlayIcon className="w-6 h-6 ml-0.5" /> : <PauseIcon className="w-6 h-6" />}
              </button>
              <Btn onClick={handleSkip} disabled={!hasSong || noNext} label="Skip">
                <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z" /></svg>
              </Btn>
              <ToggleBtn
                active={state.loop_mode !== "off"}
                label={`Loop: ${state.loop_mode}`}
                onClick={() => { const n = NEXT_LOOP[state.loop_mode]; sendCommand("loop", { mode: n }); toast(`Loop: ${n}`); }}
              >
                {state.loop_mode === "song" ? (
                  <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4zm-4-2V9h-1l-2 1v1h1.5v4H13z" /></svg>
                ) : (
                  <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z" /></svg>
                )}
              </ToggleBtn>
            </div>

            {/* Volume */}
            <div className="flex items-center gap-1.5 justify-end @max-[42rem]:hidden">
              <Btn label={localVol === 0 ? "Unmute" : "Mute"} onClick={() => { const v = state.volume === 0 ? 50 : 0; setLocalVol(v); commitVolume(v); audio.setVolume(v); }}>
                <VolumeIcon muted={localVol === 0} />
              </Btn>
              <input type="range" min={0} max={100} value={localVol} aria-label="Volume"
                onChange={(e) => { const v = Number(e.target.value); setDragging(true); setLocalVol(v); audio.setVolume(v); }}
                onMouseUp={() => { setDragging(false); commitVolume(localVol); }}
                onTouchEnd={() => { setDragging(false); commitVolume(localVol); }}
                className="w-16 h-0.5" />
            </div>

            {/* Narrow layout: volume as a popover */}
            <div className="hidden @max-[42rem]:flex items-center @max-[42rem]:order-3">
              <button
                ref={volAnchorRef}
                onClick={() => setExpanded(expanded === "volume" ? null : "volume")}
                aria-label="Volume"
                className="h-7 px-2.5 rounded-full text-[10px] font-semibold flex items-center gap-1 text-white/60 active:bg-white/[0.08]"
              >
                <VolumeIcon muted={localVol === 0} />
                {localVol}%
              </button>
              <Popover open={expanded === "volume"} onClose={closeExpanded} anchorRef={volAnchorRef} className="px-3 py-2.5">
                <input
                  type="range" min={0} max={100} value={localVol} aria-label="Volume"
                  onChange={(e) => { const v = Number(e.target.value); setDragging(true); setLocalVol(v); audio.setVolume(v); }}
                  onPointerUp={() => { setDragging(false); commitVolume(localVol); }}
                  className="w-36"
                />
              </Popover>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function VolumeIcon({ muted }: { muted: boolean }) {
  return (
    <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24">
      {muted
        ? <path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51A8.796 8.796 0 0021 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 003.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z" />
        : <path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z" />}
    </svg>
  );
}

function Btn({ children, onClick, disabled, label }: { children: React.ReactNode; onClick: () => void; disabled?: boolean; label: string }) {
  return (
    <button onClick={onClick} disabled={disabled} aria-label={label} title={label}
      className={cn("relative w-10 h-10 rounded-full flex items-center justify-center transition-all",
        "pointer-coarse:after:absolute pointer-coarse:after:-inset-1 pointer-coarse:after:content-['']",
        disabled ? "text-white/20 cursor-not-allowed" : "text-white/80 hover:text-white hover:bg-white/[0.12] active:scale-90"
      )}>{children}</button>
  );
}

// Transport toggle (shuffle/loop): accent color + dot underneath when active
function ToggleBtn({ children, active, onClick, label }: {
  children: React.ReactNode; active: boolean; onClick: () => void; label: string;
}) {
  return (
    <button onClick={onClick} aria-label={label} title={label} aria-pressed={active}
      className={cn("relative w-9 h-9 rounded-full flex items-center justify-center transition-all active:scale-90",
        "pointer-coarse:after:absolute pointer-coarse:after:-inset-1 pointer-coarse:after:content-['']",
        active ? "text-accent" : "text-white/60 hover:text-white hover:bg-white/[0.08]"
      )}>
      {children}
      {active && <span className="absolute bottom-0.5 left-1/2 -translate-x-1/2 w-1 h-1 rounded-full bg-accent" />}
    </button>
  );
}

function Pill({ children, active, onClick, label, ref }: {
  children: React.ReactNode; active?: boolean; onClick: () => void; label: string;
  ref?: React.Ref<HTMLButtonElement>;
}) {
  return (
    <button ref={ref} onClick={onClick} aria-label={label} title={label} aria-pressed={!!active}
      className={cn("relative h-7 px-2.5 rounded-full text-[11px] font-semibold flex items-center gap-1 transition-all flex-shrink-0",
        "pointer-coarse:after:absolute pointer-coarse:after:-inset-y-1.5 pointer-coarse:after:-inset-x-0.5 pointer-coarse:after:content-['']",
        active ? "bg-accent/20 text-accent" : "text-white/60 hover:text-white/80 hover:bg-white/[0.08]"
      )}>{children}</button>
  );
}
