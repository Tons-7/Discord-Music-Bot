import { useEffect, useEffectEvent, useRef, useState } from "react";
import { cn, formatDuration } from "@/lib/utils";

// Progress is driven by rAF from getPosition() (a ref read) — playback causes zero
// React renders, and the DOM is only written when a value changes.

const KEY_STEP_SECONDS = 5;

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
  /** `defer` marks keyboard steps, which may repeat quickly. */
  onSeek: (seconds: number, defer?: boolean) => void;
  disabled?: boolean;
  className?: string;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const fillRef = useRef<HTMLDivElement>(null);
  const thumbRef = useRef<HTMLDivElement>(null);
  const bubbleRef = useRef<HTMLDivElement>(null);

  const scrubbingRef = useRef(false);
  const scrubFracRef = useRef(0);
  const [scrubbing, setScrubbing] = useState(false);
  const [hovering, setHovering] = useState(false);

  // Read the latest duration inside the animation loop without restarting it.
  const frame = useEffectEvent(() => ({ dur: duration, pos: getPosition() }));

  useEffect(() => {
    let raf = 0;
    let lastFrac = -1;
    let lastSec = -1;
    const tick = () => {
      const { dur, pos } = frame();
      const frac = scrubbingRef.current
        ? scrubFracRef.current
        : dur > 0 ? Math.min(Math.max(pos / dur, 0), 1) : 0;
      if (frac !== lastFrac) {
        lastFrac = frac;
        if (fillRef.current) fillRef.current.style.transform = `scaleX(${frac})`;
        if (thumbRef.current) thumbRef.current.style.left = `${frac * 100}%`;
      }
      if (scrubbingRef.current && bubbleRef.current) {
        bubbleRef.current.style.left = `${frac * 100}%`;
        bubbleRef.current.textContent = formatDuration(frac * dur);
      }
      const sec = Math.floor(frac * dur);
      if (sec !== lastSec && trackRef.current) {
        lastSec = sec;
        trackRef.current.setAttribute("aria-valuenow", String(sec));
        trackRef.current.setAttribute("aria-valuetext", `${formatDuration(sec)} of ${formatDuration(dur)}`);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  const fracFromClientX = (clientX: number) => {
    const rect = trackRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return 0;
    return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  };

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (disabled || !duration) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    scrubbingRef.current = true;
    scrubFracRef.current = fracFromClientX(e.clientX);
    setScrubbing(true);
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (scrubbingRef.current) {
      scrubFracRef.current = fracFromClientX(e.clientX);
    } else if (e.pointerType === "mouse" && bubbleRef.current && duration > 0) {
      const frac = fracFromClientX(e.clientX);
      bubbleRef.current.style.left = `${frac * 100}%`;
      bubbleRef.current.textContent = formatDuration(frac * duration);
      bubbleRef.current.style.visibility = "visible";
    }
  };

  const handlePointerUp = () => {
    if (!scrubbingRef.current) return;
    scrubbingRef.current = false;
    setScrubbing(false);
    onSeek(Math.floor(scrubFracRef.current * duration));
  };

  const handlePointerCancel = () => {
    scrubbingRef.current = false;
    setScrubbing(false);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (disabled || !duration) return;
    const pos = getPosition();
    let next: number | null = null;
    if (e.key === "ArrowRight" || e.key === "ArrowUp") next = Math.min(pos + KEY_STEP_SECONDS, duration);
    else if (e.key === "ArrowLeft" || e.key === "ArrowDown") next = Math.max(pos - KEY_STEP_SECONDS, 0);
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = Math.max(duration - 1, 0);
    if (next !== null) {
      e.preventDefault();
      onSeek(Math.floor(next), true);
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
      aria-disabled={disabled || undefined}
      className={cn(
        "group/seek relative w-full h-5 pointer-coarse:h-6 flex items-center cursor-pointer touch-none select-none rounded-full",
        disabled && "cursor-default",
        className,
      )}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerCancel}
      // Mouse only: a tap fires compatibility mouseenter that would leave the bar "hovered".
      onPointerEnter={e => { if (e.pointerType === "mouse") setHovering(true); }}
      onPointerLeave={e => { if (e.pointerType === "mouse") setHovering(false); }}
      onKeyDown={handleKeyDown}
    >
      <div
        className={cn(
          "w-full rounded-full bg-fg/20 overflow-hidden transition-[height] duration-150",
          active ? "h-[5px]" : "h-[3px]",
        )}
      >
        <div
          ref={fillRef}
          className="h-full w-full rounded-full bg-fg/80 origin-left will-change-transform"
          style={{ transform: "scaleX(0)" }}
        />
      </div>
      <div
        ref={thumbRef}
        className={cn(
          "absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-3 h-3 rounded-full bg-fg",
          "shadow-[0_0_6px_rgba(255,255,255,0.4)] transition-[opacity,scale] duration-150 pointer-events-none",
          active ? "opacity-100 scale-100" : "opacity-0 scale-0 pointer-coarse:opacity-100 pointer-coarse:scale-100",
        )}
        style={{ left: "0%" }}
      />
      {active && duration > 0 && (
        <div
          ref={bubbleRef}
          aria-hidden="true"
          className="absolute -top-6 -translate-x-1/2 px-1.5 py-0.5 rounded bg-surface-2 border border-fg/10 text-xs font-mono text-fg/80 pointer-events-none shadow-lg whitespace-nowrap"
          // Hidden until a position is written into it
          style={{ left: "0%", visibility: scrubbing ? "visible" : "hidden" }}
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
  const frame = useEffectEvent(() => (duration > 0 ? Math.min(Math.max(getPosition() / duration, 0), 1) : 0));

  useEffect(() => {
    let raf = 0;
    let last = -1;
    const tick = () => {
      const frac = frame();
      if (frac !== last && fillRef.current) {
        fillRef.current.style.transform = `scaleX(${frac})`;
        last = frac;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  return (
    <div className={cn("w-full overflow-hidden bg-fg/10", className)}>
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
  const read = useEffectEvent(() => formatDuration(get()));
  useEffect(() => {
    let raf = 0;
    let last = "";
    const tick = () => {
      const text = read();
      if (text !== last && ref.current) {
        ref.current.textContent = text;
        last = text;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return (
    <span ref={ref} className={className}>
      0:00
    </span>
  );
}
