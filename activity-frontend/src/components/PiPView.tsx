"use client";

import { formatDuration, proxyImg, cn } from "@/lib/utils";
import { ProgressStrip, LiveTime } from "./ui/SeekBar";
import { PlayIcon, NoteIcon } from "./ui/icons";
import type { PlaybackView } from "@/hooks/usePlaybackView";

export default function PiPView({ view, blocked, onPlayPause }: {
  view: PlaybackView;
  blocked: boolean;
  onPlayPause: () => void;
}) {
  const { current, totalDur, getPosition } = view;
  const isPaused = view.isPaused || blocked;
  const thumb = current?.thumbnail ? proxyImg(current.thumbnail) : null;

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
          <NoteIcon className="w-16 h-16 text-accent/30" />
        </div>
      )}

      {/* Text block only when the PiP tile is tall enough; tiny tiles stay clean */}
      <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 via-black/40 to-transparent pt-10 pb-4 px-4 z-10 hidden [@media(min-height:150px)]:block">
        {current ? (
          <>
            <p className="text-sm font-semibold text-white truncate leading-tight drop-shadow-lg">
              {current.title}
            </p>
            <p className="text-[11px] text-white/70 truncate mt-0.5 drop-shadow-lg">
              {current.uploader}
            </p>

            {!current.is_live && totalDur > 0 && (
              <div className="flex justify-between mt-1.5">
                <LiveTime get={getPosition} className="text-[10px] font-mono tabular-nums text-white/70 drop-shadow" />
                <span className="text-[10px] font-mono tabular-nums text-white/70 drop-shadow">
                  {formatDuration(totalDur)}
                </span>
              </div>
            )}

            {current.is_live && (
              <div className="flex items-center gap-1 mt-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-danger animate-pulse" />
                <span className="text-[10px] font-bold text-danger">LIVE</span>
              </div>
            )}
          </>
        ) : (
          <p className="text-xs text-white/60 text-center py-4">Nothing playing</p>
        )}
      </div>

      {/* Always-visible slim progress strip on the bottom edge */}
      {current && !current.is_live && totalDur > 0 && (
        <ProgressStrip
          duration={totalDur}
          getPosition={getPosition}
          className="absolute bottom-0 inset-x-0 h-[3px] z-10 bg-white/15"
          fillClassName="bg-accent"
        />
      )}

      <button
        type="button"
        onClick={onPlayPause}
        disabled={!current}
        className="absolute inset-0 z-20"
        aria-label={blocked ? "Tap to play" : isPaused ? "Play" : "Pause"}
      />

      {current && (
        <div className="absolute inset-0 z-30 flex items-center justify-center pointer-events-none">
          <div className={cn(
            "w-16 h-16 rounded-full bg-black/60 backdrop-blur-sm flex items-center justify-center transition-opacity duration-300",
            isPaused ? "opacity-80" : "opacity-0",
          )}>
            <PlayIcon className="w-7 h-7 ml-0.5 text-white" />
          </div>
        </div>
      )}
    </div>
  );
}
