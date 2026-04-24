"use client";

import { useMemo } from "react";
import { useGuildState } from "./GuildStateProvider";
import { formatDuration, proxyImg } from "@/lib/utils";
import type { AudioPlayerHandle } from "@/hooks/useAudioPlayer";

type PiPAudio = Pick<AudioPlayerHandle, "playing" | "ready" | "position" | "duration" | "playPause">;

export default function PiPView({ audio }: { audio: PiPAudio }) {
  const { state } = useGuildState();
  const { current } = state;

  const displayPos = audio.ready ? audio.position : 0;
  const totalDur = audio.ready && audio.duration > 0 ? audio.duration : (current?.duration ?? 0);
  const progress = totalDur > 0 ? Math.min((displayPos / totalDur) * 100, 100) : 0;
  const isPaused = audio.ready ? !audio.playing : (current?.is_paused ?? true);
  const thumb = useMemo(() => current?.thumbnail ? proxyImg(current.thumbnail) : null, [current?.thumbnail]);

  return (
    <div className="relative h-dvh w-full bg-surface-1 overflow-hidden">
      {thumb ? (
        <img
          src={thumb}
          alt=""
          className="absolute inset-0 w-full h-full object-cover"
        />
      ) : (
        <div className="absolute inset-0 bg-gradient-to-br from-accent/20 via-accent/5 to-transparent flex items-center justify-center">
          <svg className="w-16 h-16 text-accent/30" fill="currentColor" viewBox="0 0 24 24">
            <path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z" />
          </svg>
        </div>
      )}

      <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 via-black/40 to-transparent pt-10 pb-3 px-4 z-10">
        {current ? (
          <>
            <p className="text-sm font-semibold text-white truncate leading-tight drop-shadow-lg">
              {current.title}
            </p>
            <p className="text-[11px] text-white/70 truncate mt-0.5 drop-shadow-lg">
              {current.uploader}
            </p>

            {!current.is_live && totalDur > 0 && (
              <div className="mt-2">
                <div className="w-full h-[3px] rounded-full bg-white/20">
                  <div
                    className="h-full rounded-full bg-white/90 transition-[width] duration-200 ease-linear"
                    style={{ width: `${progress}%` }}
                  />
                </div>
                <div className="flex justify-between mt-1">
                  <span className="text-[10px] font-mono tabular-nums text-white/70 drop-shadow">
                    {formatDuration(displayPos)}
                  </span>
                  <span className="text-[10px] font-mono tabular-nums text-white/70 drop-shadow">
                    {formatDuration(totalDur)}
                  </span>
                </div>
              </div>
            )}

            {current.is_live && (
              <div className="flex items-center gap-1 mt-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />
                <span className="text-[10px] font-bold text-red-400">LIVE</span>
              </div>
            )}
          </>
        ) : (
          <p className="text-xs text-white/60 text-center py-4">Nothing playing</p>
        )}
      </div>

      <button
        onClick={audio.playPause}
        className="absolute inset-0 z-20"
        aria-label={isPaused ? "Play" : "Pause"}
      />

      {current && (
        <div className="absolute inset-0 z-30 flex items-center justify-center pointer-events-none">
          <div className={`w-16 h-16 rounded-full bg-black/60 backdrop-blur-sm flex items-center justify-center transition-opacity duration-300 ${isPaused ? "opacity-80" : "opacity-0"}`}>
            <svg className="w-7 h-7 ml-0.5 text-white" fill="currentColor" viewBox="0 0 24 24">
              <path d="M8 5v14l11-7z" />
            </svg>
          </div>
        </div>
      )}
    </div>
  );
}
