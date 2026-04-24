"use client";

import { useRef, useState, useLayoutEffect, memo } from "react";
import { cn } from "@/lib/utils";

const MarqueeText = memo(function MarqueeText({ children, className }: { children: string; className?: string }) {
  const outerRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLSpanElement>(null);
  const [overflows, setOverflows] = useState(false);
  const [hovering, setHovering] = useState(false);

  useLayoutEffect(() => {
    const outer = outerRef.current;
    const measure = measureRef.current;
    if (outer && measure) {
      setOverflows(measure.offsetWidth > outer.clientWidth);
    }
  }, [children]);

  return (
    <div
      ref={outerRef}
      className={cn("overflow-hidden whitespace-nowrap", className)}
      onMouseEnter={() => overflows && setHovering(true)}
      onMouseLeave={() => setHovering(false)}
    >
      {/* Hidden measurer — always single copy, never affected by animation */}
      <span ref={measureRef} className="invisible absolute whitespace-nowrap">{children}</span>

      {/* Visible content */}
      <span
        className="inline-block"
        style={hovering ? { animation: "marquee 12s linear infinite" } : undefined}
      >
        {children}
        {hovering && <span className="px-10">{children}</span>}
      </span>
    </div>
  );
});

export default MarqueeText;
