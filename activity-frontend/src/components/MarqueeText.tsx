"use client";

import { useRef, useState, useEffect, memo } from "react";
import { cn } from "@/lib/utils";

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia(query);
    setMatches(mq.matches);
    const fn = (e: MediaQueryListEvent) => setMatches(e.matches);
    mq.addEventListener("change", fn);
    return () => mq.removeEventListener("change", fn);
  }, [query]);
  return matches;
}

// Overflowing text scrolls on hover (mouse) or automatically (touch, where
// hover doesn't exist); otherwise it ellipsizes.
const MarqueeText = memo(function MarqueeText({ children, className }: { children: string; className?: string }) {
  const outerRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLSpanElement>(null);
  const [overflows, setOverflows] = useState(false);
  const [hovering, setHovering] = useState(false);
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

  const animating = overflows && !reducedMotion && (coarse || hovering);

  return (
    <div
      ref={outerRef}
      className={cn("overflow-hidden whitespace-nowrap", !animating && "text-ellipsis", className)}
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
    >
      {/* Hidden measurer — always single copy, never affected by animation */}
      <span ref={measureRef} className="invisible absolute whitespace-nowrap">{children}</span>

      {animating ? (
        <span className="inline-block pr-16" style={{ animation: "marquee 14s linear infinite" }}>
          {children}
          <span className="pl-16">{children}</span>
        </span>
      ) : (
        children
      )}
    </div>
  );
});

export default MarqueeText;
