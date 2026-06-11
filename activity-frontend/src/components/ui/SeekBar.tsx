import { useEffect, useRef, useState, useCallback } from "react";
import { cn, formatDuration } from "@/lib/utils";

// Progress is driven by rAF + transform from getPosition() (a ref read) —
// playback causes zero React renders.

/** Interactive seek slider: drag/touch scrubbing, keyboard arrows, ARIA slider. */
export function SeekBar({
  duration,
  getPosition,
  onSeek,
  disabled,
  className,
}: {
  duration: number;
  getPosition: () => number;
  onSeek: (seconds: number) => void;
  disabled?: boolean;
  className?: string;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const fillRef = useRef<HTMLDivElement>(null);
  const thumbRef = useRef<HTMLDivElement>(null);
  const bubbleRef = useRef<HTMLDivElement>(null);

  const scrubbingRef = useRef(false);
  const scrubFracRef = useRef(0);
  const durRef = useRef(duration);
  durRef.current = duration;
  const [scrubbing, setScrubbing] = useState(false);
  const [hovering, setHovering] = useState(false);

  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const dur = durRef.current;
      const frac = scrubbingRef.current
        ? scrubFracRef.current
        : dur > 0
          ? Math.min(getPosition() / dur, 1)
          : 0;
      if (fillRef.current) fillRef.current.style.transform = `scaleX(${frac})`;
      if (thumbRef.current) thumbRef.current.style.left = `${frac * 100}%`;
      if (scrubbingRef.current && bubbleRef.current) {
        bubbleRef.current.style.left = `${frac * 100}%`;
        bubbleRef.current.textContent = formatDuration(frac * dur);
      }
      if (trackRef.current) {
        trackRef.current.setAttribute("aria-valuenow", String(Math.floor(frac * dur)));
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [getPosition]);

  const fracFromClientX = useCallback((clientX: number) => {
    const rect = trackRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return 0;
    return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  }, []);

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (disabled || !durRef.current) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    scrubbingRef.current = true;
    scrubFracRef.current = fracFromClientX(e.clientX);
    setScrubbing(true);
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (scrubbingRef.current) {
      scrubFracRef.current = fracFromClientX(e.clientX);
    } else if (e.pointerType === "mouse" && bubbleRef.current && durRef.current > 0) {
      const frac = fracFromClientX(e.clientX);
      bubbleRef.current.style.left = `${frac * 100}%`;
      bubbleRef.current.textContent = formatDuration(frac * durRef.current);
    }
  };

  const handlePointerUp = () => {
    if (!scrubbingRef.current) return;
    scrubbingRef.current = false;
    setScrubbing(false);
    onSeek(Math.floor(scrubFracRef.current * durRef.current));
  };

  const handlePointerCancel = () => {
    scrubbingRef.current = false;
    setScrubbing(false);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (disabled || !durRef.current) return;
    const dur = durRef.current;
    const pos = getPosition();
    let next: number | null = null;
    if (e.key === "ArrowRight") next = Math.min(pos + 5, dur);
    else if (e.key === "ArrowLeft") next = Math.max(pos - 5, 0);
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = Math.max(dur - 1, 0);
    if (next !== null) {
      e.preventDefault();
      onSeek(Math.floor(next));
    }
  };

  const active = scrubbing || hovering;

  return (
    <div
      ref={trackRef}
      role="slider"
      tabIndex={disabled ? -1 : 0}
      aria-label="Seek"
      aria-valuemin={0}
      aria-valuemax={Math.floor(duration)}
      aria-valuenow={0}
      className={cn(
        "group/seek relative w-full h-5 pointer-coarse:h-6 flex items-center cursor-pointer touch-none select-none",
        disabled && "cursor-default",
        className,
      )}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerCancel}
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
      onKeyDown={handleKeyDown}
    >
      <div
        className={cn(
          "w-full rounded-full bg-white/20 overflow-hidden transition-[height] duration-150",
          active ? "h-[5px]" : "h-[3px]",
        )}
      >
        <div
          ref={fillRef}
          className="h-full w-full rounded-full bg-white/80 origin-left will-change-transform"
          style={{ transform: "scaleX(0)" }}
        />
      </div>
      <div
        ref={thumbRef}
        className={cn(
          "absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-3 h-3 rounded-full bg-white",
          "shadow-[0_0_6px_rgba(255,255,255,0.4)] transition-[opacity,scale] duration-150 pointer-events-none",
          active ? "opacity-100 scale-100" : "opacity-0 scale-0 pointer-coarse:opacity-100 pointer-coarse:scale-100",
        )}
        style={{ left: "0%" }}
      />
      {(scrubbing || hovering) && duration > 0 && (
        <div
          ref={bubbleRef}
          className="absolute -top-6 -translate-x-1/2 px-1.5 py-0.5 rounded bg-surface-2 border border-white/[0.1] text-[10px] font-mono text-white/80 pointer-events-none shadow-lg whitespace-nowrap"
          style={{ left: "0%" }}
        />
      )}
    </div>
  );
}

/** Non-interactive thin progress bar (mini player, PiP). */
export function ProgressStrip({
  duration,
  getPosition,
  className,
  fillClassName,
}: {
  duration: number;
  getPosition: () => number;
  className?: string;
  fillClassName?: string;
}) {
  const fillRef = useRef<HTMLDivElement>(null);
  const durRef = useRef(duration);
  durRef.current = duration;

  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const dur = durRef.current;
      const frac = dur > 0 ? Math.min(getPosition() / dur, 1) : 0;
      if (fillRef.current) fillRef.current.style.transform = `scaleX(${frac})`;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [getPosition]);

  return (
    <div className={cn("w-full overflow-hidden bg-white/10", className)}>
      <div
        ref={fillRef}
        className={cn("h-full w-full origin-left will-change-transform", fillClassName)}
        style={{ transform: "scaleX(0)" }}
      />
    </div>
  );
}

/** Time label that updates from a position getter without re-rendering React. */
export function LiveTime({ get, className }: { get: () => number; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    let raf = 0;
    let last = "";
    const tick = () => {
      const text = formatDuration(get());
      if (text !== last && ref.current) {
        ref.current.textContent = text;
        last = text;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [get]);
  return (
    <span ref={ref} className={className}>
      0:00
    </span>
  );
}
