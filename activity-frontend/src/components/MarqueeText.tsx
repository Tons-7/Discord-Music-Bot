"use client";

import { useRef, useState, useEffect, memo } from "react";
import { cn } from "@/lib/utils";
import { useMediaQuery } from "@/hooks/useMediaQuery";

// Overflowing text scrolls on hover (mouse) or automatically (touch, where
// hover doesn't exist); otherwise it ellipsizes. A row that covers the text with
// its own button passes `hovered` in, since the text then never sees the pointer.
const MarqueeText = memo(function MarqueeText({ children, className, hovered }: {
  children: string;
  className?: string;
  hovered?: boolean;
}) {
  const outerRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLSpanElement>(null);
  const [overflows, setOverflows] = useState(false);
  const [selfHover, setSelfHover] = useState(false);
  const coarse = useMediaQuery("(pointer: coarse)");
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");

  useEffect(() => {
    const outer = outerRef.current;
    const measure = measureRef.current;
    if (!outer || !measure) return;
    const check = () => setOverflows(measure.offsetWidth > outer.clientWidth);
    check();
    const ro = new ResizeObserver(check);
    ro.observe(outer);
    return () => ro.disconnect();
  }, [children]);

  const hovering = hovered ?? selfHover;
  const animating = overflows && !reducedMotion && (coarse || hovering);

  return (
    <div
      ref={outerRef}
      className={cn("overflow-hidden whitespace-nowrap", !animating && "text-ellipsis", className)}
      onMouseEnter={hovered === undefined ? () => setSelfHover(true) : undefined}
      onMouseLeave={hovered === undefined ? () => setSelfHover(false) : undefined}
    >
      {/* Hidden measurer, always a single copy. Its zero-size clipped box keeps the full-width text from
          widening whatever scrolls around the row (overflow-hidden here doesn't clip absolute children). */}
      <span aria-hidden="true" className="invisible absolute w-0 h-0 overflow-hidden">
        <span ref={measureRef} className="inline-block whitespace-nowrap">{children}</span>
      </span>

      {animating ? (
        <span className="inline-block pr-16" style={{ animation: "marquee 14s linear infinite" }}>
          {children}
          {/* The loop's second copy is visual only */}
          <span className="pl-16" aria-hidden="true">{children}</span>
        </span>
      ) : (
        children
      )}
    </div>
  );
});

export default MarqueeText;
