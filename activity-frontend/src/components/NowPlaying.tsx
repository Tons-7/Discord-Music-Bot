"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { useGuildState } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { formatDuration, cn, proxyImg } from "@/lib/utils";
import { apiFetch } from "@/lib/api";
import MarqueeText from "./MarqueeText";
import FavHeart from "./FavHeart";
import InviteButton from "./InviteButton";
import type { AudioPlayerHandle } from "@/hooks/useAudioPlayer";
import type { LoopMode } from "@/types";

const NEXT_LOOP: Record<LoopMode, LoopMode> = { off: "song", song: "queue", queue: "off" };
const AUDIO_EFFECTS: readonly [string, string][] = [
  ["none", "Off"], ["bass_boost", "Bass"], ["nightcore", "NC"],
  ["vaporwave", "Vap"], ["treble_boost", "Treb"], ["8d", "8D"],
];
const SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0];

export default function NowPlaying({ audio, compact }: { audio: AudioPlayerHandle; compact?: boolean }) {
  const { state, guildId, sendCommand } = useGuildState();
  const { toast } = useToast();
  const { current } = state;

  const displayPos = audio.ready ? audio.position : (state.is_connected ? (current?.position ?? 0) : 0);
  const totalDur = audio.ready && audio.duration > 0 ? audio.duration : (current?.duration ?? 0);
  const progress = totalDur > 0 ? Math.min((displayPos / totalDur) * 100, 100) : 0;
  const isPaused = audio.ready ? !audio.playing : (current?.is_paused ?? true);
  const hasSong = !!current;

  const [localVol, setLocalVol] = useState(state.volume);
  const [dragging, setDragging] = useState(false);
  useEffect(() => { if (!dragging) setLocalVol(state.volume); }, [state.volume, dragging]);

  const [expanded, setExpanded] = useState<"speed" | "fx" | null>(null);
  useEffect(() => {
    if (!expanded) return;
    const close = () => setExpanded(null);
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, [expanded]);

  const [loopHover, setLoopHover] = useState(false);

  const [hoverTime, setHoverTime] = useState<number | null>(null);
  const [hoverX, setHoverX] = useState(0);
  const handleProgressHover = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (!totalDur) return;
    const rect = e.currentTarget.getBoundingClientRect();
    setHoverTime(Math.floor(Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) * totalDur));
    setHoverX(e.clientX - rect.left);
  }, [totalDur]);

  const handleSkip = useCallback(async () => {
    if (state.queue.length === 0 && !state.autoplay && !state.is_connected && state.loop_mode === "off") return;
    audio.stop();
    if (state.is_connected) await sendCommand("skip");
    else await apiFetch(`/api/guild/${guildId}/play`, { method: "POST" }).catch(() => {});
  }, [state.queue.length, state.autoplay, state.is_connected, state.loop_mode, audio, sendCommand, guildId]);

  const handlePrevious = useCallback(async () => {
    audio.stop();
    await sendCommand("previous");
  }, [audio, sendCommand]);

  const handleStop = useCallback(async () => {
    audio.stop();
    await sendCommand("stop");
  }, [audio, sendCommand]);

  const handleSeek = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (!totalDur) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const seconds = Math.floor(Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) * totalDur);
    audio.seek(seconds);
    sendCommand("seek", { position: String(seconds) });
  }, [totalDur, audio, sendCommand]);

  const thumb = useMemo(() => current?.thumbnail ? proxyImg(current.thumbnail) : null, [current?.thumbnail]);
  const noNext = state.queue.length === 0 && !state.autoplay && !state.is_connected && state.loop_mode === "off";

  return (
    <div className="relative h-full w-full bg-surface-1 flex flex-col">
      {thumb && <img src={thumb} alt="" className="absolute inset-0 w-full h-full object-cover blur-3xl scale-125 opacity-20" />}
      <div className="absolute inset-0 bg-gradient-to-b from-surface-1/50 via-surface-1/30 to-surface-1/80" />

      {!compact && (
        <div className="absolute top-3 right-3 z-20">
          <InviteButton />
        </div>
      )}

      <div className="relative z-10 flex-1 flex items-center justify-center p-4 max-sm:p-3 min-h-0 min-w-0">
        <div
          className={cn(
            "relative rounded-2xl overflow-hidden shadow-[0_8px_40px_rgba(0,0,0,0.5)]",
            compact ? "w-[140px] aspect-square" : "aspect-square max-sm:w-full max-sm:max-h-full sm:h-full sm:max-w-full"
          )}
        >
          {thumb ? (
            <img src={thumb} alt="" className="w-full h-full object-cover" />
          ) : (
            <div className="w-full h-full bg-gradient-to-br from-accent/20 via-accent/5 to-transparent flex items-center justify-center">
              <svg className="w-14 h-14 text-accent/30" fill="currentColor" viewBox="0 0 24 24">
                <path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z" />
              </svg>
            </div>
          )}
          {current?.is_live && (
            <div className="absolute top-2 right-2 flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-500">
              <span className="w-1.5 h-1.5 rounded-full bg-white animate-pulse" />
              <span className="text-[9px] font-bold text-white">LIVE</span>
            </div>
          )}
        </div>
      </div>

      <div className="relative z-10 flex-shrink-0 p-4 max-sm:p-3">
        {hasSong && !current.is_live && (
          <div className="mb-2">
            <div className="group relative w-full h-4 flex items-center cursor-pointer"
              onClick={handleSeek} onMouseMove={handleProgressHover} onMouseLeave={() => setHoverTime(null)}>
              <div className="w-full h-[3px] rounded-full bg-white/20 group-hover:h-[5px] transition-all">
                <div className="h-full rounded-full bg-white/80 relative transition-[width] duration-100 ease-linear" style={{ width: `${progress}%` }}>
                  <div className="absolute right-0 top-1/2 -translate-y-1/2 w-3 h-3 rounded-full bg-white shadow-[0_0_6px_rgba(255,255,255,0.4)] opacity-0 group-hover:opacity-100 scale-0 group-hover:scale-100 transition-all duration-150" />
                </div>
              </div>
              {hoverTime !== null && (
                <div className="absolute -top-6 -translate-x-1/2 px-1.5 py-0.5 rounded bg-surface-2 border border-white/[0.1] text-[9px] font-mono text-white/80 pointer-events-none shadow-lg"
                  style={{ left: hoverX }}>{formatDuration(hoverTime)}</div>
              )}
            </div>
            <div className="flex justify-between px-0.5">
              <span className="text-[10px] font-mono tabular-nums text-white/50">{formatDuration(displayPos)}</span>
              <span className="text-[10px] font-mono tabular-nums text-white/50">{formatDuration(totalDur)}</span>
            </div>
          </div>
        )}

        <div className="rounded-xl bg-surface-3/80 backdrop-blur-lg border border-white/[0.08] px-5 py-3 max-sm:px-3 max-sm:py-2.5">
          <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3 max-sm:grid-cols-1 max-sm:gap-2 max-sm:justify-items-center">
            <div className={cn("min-w-0 max-sm:w-full max-sm:text-center", compact && "invisible")}>
              {hasSong ? (
                <>
                  <MarqueeText className="text-xs font-semibold text-white max-sm:text-sm">{current.title}</MarqueeText>
                  <p className="text-[10px] text-white/40 truncate max-sm:text-[11px]">{current.uploader}</p>
                </>
              ) : (
                <p className="text-xs text-white/30 truncate">Nothing playing</p>
              )}
            </div>

            <div className="flex items-center gap-1.5 flex-shrink-0 max-sm:gap-2">
              <Btn onClick={handlePrevious} disabled={!hasSong || state.history.length === 0}>
                <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z" /></svg>
              </Btn>
              <button onClick={audio.playPause} disabled={!hasSong}
                className={cn("w-10 h-10 rounded-full flex items-center justify-center transition-all",
                  hasSong ? "bg-white text-surface-1 hover:scale-105 active:scale-95" : "bg-white/10 text-white/20 cursor-not-allowed"
                )}>
                {isPaused
                  ? <svg className="w-5 h-5 ml-0.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>
                  : <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" /></svg>}
              </button>
              <Btn onClick={handleSkip} disabled={!hasSong || noNext}>
                <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z" /></svg>
              </Btn>
              <Btn onClick={handleStop} disabled={!hasSong}>
                <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 6h12v12H6z" /></svg>
              </Btn>
            </div>

            <div className={cn(
              "flex items-center gap-1 flex-shrink-0 justify-end max-sm:justify-center max-sm:w-full max-sm:overflow-x-auto max-sm:[scrollbar-width:none] max-sm:[&::-webkit-scrollbar]:hidden",
              compact && "invisible"
            )}>
              {hasSong && <FavHeart webpageUrl={current.webpage_url} title={current.title} duration={current.duration} thumbnail={current.thumbnail} uploader={current.uploader} size="md" />}
              <Btn onClick={() => { const v = state.volume === 0 ? 50 : 0; sendCommand("volume", { level: v }); audio.setVolume(v); }}>
                <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24">
                  {localVol === 0
                    ? <path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51A8.796 8.796 0 0021 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 003.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z" />
                    : <path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z" />}
                </svg>
              </Btn>
              <input type="range" min={0} max={100} value={localVol}
                onChange={(e) => { const v = Number(e.target.value); setDragging(true); setLocalVol(v); audio.setVolume(v); }}
                onMouseUp={() => { setDragging(false); sendCommand("volume", { level: localVol }); }}
                onTouchEnd={() => { setDragging(false); sendCommand("volume", { level: localVol }); }}
                className="w-14 h-0.5 max-sm:hidden" />
              <Sep />
              <Pill active={state.shuffle} onClick={() => { sendCommand("shuffle"); toast(state.shuffle ? "Shuffle off" : "Shuffle on"); }}>
                <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z" /></svg>
              </Pill>
              <div className="flex items-center" onMouseEnter={() => setLoopHover(true)} onMouseLeave={() => setLoopHover(false)}>
                <Pill active={state.loop_mode !== "off"} onClick={() => { const n = NEXT_LOOP[state.loop_mode]; sendCommand("loop", { mode: n }); toast(`Loop: ${n}`); }}>
                  <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z" /></svg>
                </Pill>
                <span className={cn("text-[9px] font-medium whitespace-nowrap transition-all duration-200 overflow-hidden",
                  state.loop_mode !== "off" ? "text-accent" : "text-muted",
                  loopHover ? "max-w-[40px] opacity-100 ml-0.5" : "max-w-0 opacity-0"
                )}>{state.loop_mode}</span>
              </div>
              <div className="relative" onClick={e => e.stopPropagation()}>
                <Pill active={expanded === "speed" || state.speed !== 1.0} onClick={() => setExpanded(expanded === "speed" ? null : "speed")}>
                  <span className="text-[9px]">{state.speed}x</span>
                </Pill>
                {expanded === "speed" && (
                  <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 bg-surface-2 border border-white/[0.1] rounded-xl p-1 shadow-[0_8px_32px_rgba(0,0,0,0.6)] z-20 animate-[slide-up_0.15s_ease-out] min-w-[48px]">
                    {SPEEDS.map(s => (
                      <button key={s} onClick={() => { sendCommand("speed", { rate: s }); setExpanded(null); toast(`Speed ${s}x`); }}
                        className={cn("block w-full text-center px-2 py-1 rounded-lg text-[10px] font-medium transition-colors", s === state.speed ? "bg-accent text-white" : "text-white/70 hover:bg-white/[0.08]")}>
                        {s}x</button>))}
                  </div>)}
              </div>
              <div className="relative" onClick={e => e.stopPropagation()}>
                <Pill active={expanded === "fx" || state.audio_effect !== "none"} onClick={() => setExpanded(expanded === "fx" ? null : "fx")}>
                  <span className="text-[9px]">FX</span>
                </Pill>
                {expanded === "fx" && (
                  <div className="absolute bottom-full right-0 mb-2 bg-surface-2 border border-white/[0.1] rounded-xl p-1 shadow-[0_8px_32px_rgba(0,0,0,0.6)] z-20 animate-[slide-up_0.15s_ease-out] min-w-[80px]">
                    {AUDIO_EFFECTS.map(([id,l]) => (
                      <button key={id} onClick={() => { sendCommand("effects", { effect: id }); setExpanded(null); toast(id === "none" ? "Effects off" : l); }}
                        className={cn("block w-full text-left px-3 py-1 rounded-lg text-[10px] font-medium transition-colors whitespace-nowrap", id === state.audio_effect ? "bg-accent text-white" : "text-white/70 hover:bg-white/[0.08]")}>
                        {l}</button>))}
                  </div>)}
              </div>
              <Pill active={state.autoplay} onClick={() => { sendCommand("autoplay"); toast(state.autoplay ? "Autoplay off" : "Autoplay on"); }}>
                <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z" /></svg>
              </Pill>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Btn({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled?: boolean }) {
  return (
    <button onClick={onClick} disabled={disabled}
      className={cn("w-8 h-8 rounded-full flex items-center justify-center transition-all",
        disabled ? "text-white/20 cursor-not-allowed" : "text-white/80 hover:text-white hover:bg-white/[0.12] active:scale-90"
      )}>{children}</button>
  );
}

function Pill({ children, active, onClick }: { children: React.ReactNode; active?: boolean; onClick: () => void }) {
  return (
    <button onClick={onClick}
      className={cn("h-6 px-1.5 rounded-full text-[10px] font-semibold flex items-center gap-0.5 transition-all flex-shrink-0",
        active ? "bg-accent/20 text-accent" : "text-white/60 hover:text-white/80 hover:bg-white/[0.08]"
      )}>{children}</button>
  );
}

function Sep() { return <div className="w-px h-4 bg-white/[0.15] flex-shrink-0" />; }
