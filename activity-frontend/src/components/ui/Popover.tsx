import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/utils";

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])';
const GAP = 8;

type Placement = { left: number; top?: number; bottom?: number };

// React bubbles portal events up the component tree, so a popover inside a clickable row
// would also "click" the row. Escape is let through: the document listener closes on it.
const stop = (e: React.SyntheticEvent) => e.stopPropagation();
const stopKey = (e: React.KeyboardEvent) => {
  if (e.key !== "Escape") e.stopPropagation();
};

function place(anchor: HTMLElement, panel: HTMLElement): Placement {
  const a = anchor.getBoundingClientRect();
  const w = panel.offsetWidth;
  const h = panel.offsetHeight;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  // A hidden anchor (e.g. opened by shortcut in a layout without it) has no box: centre instead.
  if (a.width === 0 && a.height === 0) {
    return { left: Math.max(GAP, (vw - w) / 2), top: Math.max(GAP, (vh - h) / 2) };
  }
  const left = Math.max(GAP, Math.min(a.left + a.width / 2 - w / 2, vw - w - GAP));
  // Above the anchor, pinned by its bottom edge so content that grows extends upward.
  if (a.top - h - GAP >= GAP) return { left, bottom: vh - a.top + GAP };
  return { left, top: Math.max(GAP, Math.min(a.bottom + GAP, vh - h - GAP)) };
}

function samePlacement(a: Placement | null, b: Placement) {
  return !!a && a.left === b.left && a.top === b.top && a.bottom === b.bottom;
}

/**
 * Portal + fixed positioning so overflow/scroll containers can't clip it. Light-dismiss on
 * outside pointerdown, Escape and focus leaving. Focus moves in on open (to the checked option
 * when there is one) and back to the trigger when the popover closes with focus inside it.
 * role="menu" adds arrow-key navigation between its menuitem* children.
 */
export default function Popover({
  open,
  onClose,
  anchorRef,
  children,
  className,
  role = "dialog",
  label,
  autoFocus = true,
}: {
  open: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLElement | null>;
  children: React.ReactNode;
  className?: string;
  role?: "dialog" | "menu";
  label?: string;
  autoFocus?: boolean;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const focusInsideRef = useRef(false);
  const [pos, setPos] = useState<Placement | null>(null);

  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    const update = () => {
      const anchor = anchorRef.current;
      const panel = panelRef.current;
      if (!anchor || !panel) return;
      const next = place(anchor, panel);
      setPos(prev => (samePlacement(prev, next) ? prev : next));
    };
    update();
    // Content that loads after opening, and scrolling lists underneath, both move the anchor or the panel.
    const ro = new ResizeObserver(update);
    if (panelRef.current) ro.observe(panelRef.current);
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, { capture: true, passive: true });
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, { capture: true });
    };
  }, [open, anchorRef]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      const target = e.target as Node;
      if (panelRef.current?.contains(target) || anchorRef.current?.contains(target)) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, onClose, anchorRef]);

  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    if (panel && autoFocus) {
      const target = panel.querySelector<HTMLElement>('[aria-checked="true"], [aria-selected="true"]')
        ?? panel.querySelector<HTMLElement>(FOCUSABLE);
      target?.focus({ preventScroll: true });
    }
    const anchor = anchorRef.current;
    return () => {
      // Closing with focus inside (Escape, picking an option) would drop it on <body>.
      const active = document.activeElement;
      if (focusInsideRef.current || !active || active === document.body) anchor?.focus({ preventScroll: true });
      focusInsideRef.current = false;
    };
  }, [open, anchorRef, autoFocus]);

  if (!open) return null;

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    stopKey(e);
    if (role !== "menu") return;
    const items = Array.from(e.currentTarget.querySelectorAll<HTMLElement>('[role^="menuitem"]'));
    if (!items.length) return;
    const i = items.indexOf(document.activeElement as HTMLElement);
    let next = -1;
    if (e.key === "ArrowDown") next = (i + 1) % items.length;
    else if (e.key === "ArrowUp") next = (i - 1 + items.length) % items.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = items.length - 1;
    if (next < 0) return;
    e.preventDefault();
    items[next].focus();
  };

  const onBlur = (e: React.FocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget as Node | null;
    focusInsideRef.current = !!next && e.currentTarget.contains(next);
    // Tabbing away closes it; moving to the trigger is left to the trigger's own toggle.
    if (next && !e.currentTarget.contains(next) && !anchorRef.current?.contains(next)) onClose();
  };

  const style: React.CSSProperties = pos
    ? { left: pos.left, top: pos.top, bottom: pos.bottom }
    : { left: -9999, top: -9999 };

  return createPortal(
    <div
      ref={panelRef}
      role={role}
      aria-label={label}
      style={style}
      onClick={stop}
      onPointerDown={stop}
      onMouseDown={stop}
      onTouchStart={stop}
      onKeyDown={onKeyDown}
      onFocus={() => { focusInsideRef.current = true; }}
      onBlur={onBlur}
      className={cn(
        "fixed z-50 bg-surface-2 border border-fg/10 rounded-xl p-1",
        "shadow-[0_8px_32px_rgba(0,0,0,0.6)] animate-[slide-up_0.15s_ease-out]",
        className,
      )}
    >
      {children}
    </div>,
    document.body,
  );
}
