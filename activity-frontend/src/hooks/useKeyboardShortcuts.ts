"use client";

import { useEffect, useEffectEvent } from "react";

export interface KeyboardShortcutHandlers {
  playPause: () => void;
  seekBy: (deltaSeconds: number) => void;
  next: () => void;
  previous: () => void;
  toggleMute: () => void;
  focusSearch: () => void;
}

export const SEEK_STEP_SECONDS = 5;

function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.tagName !== "string") return false;
  const tag = el.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable;
}

// Space activates whatever button has focus — don't steal it.
function isActivatableTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  return !!el?.closest?.('button, a, summary, [role="button"]');
}

/**
 * Desktop transport shortcuts. Deliberately inert while typing (the queue
 * filter, search box, playlist name field) and while any modifier is held, so
 * browser and Discord shortcuts keep working.
 */
export function useKeyboardShortcuts(handlers: KeyboardShortcutHandlers) {
  const onKeyDown = useEffectEvent((e: KeyboardEvent) => {
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
    if (e.isComposing) return;
    if (isTypingTarget(e.target)) return;

    switch (e.key) {
      case " ":
        if (e.repeat || isActivatableTarget(e.target)) return;
        e.preventDefault(); // otherwise the page scrolls
        handlers.playPause();
        return;
      case "ArrowLeft":
      case "ArrowRight":
        // Only when nothing is focused — otherwise arrows belong to whatever
        // widget has focus, and to page scrolling.
        if (e.target !== document.body) return;
        e.preventDefault();
        handlers.seekBy(e.key === "ArrowLeft" ? -SEEK_STEP_SECONDS : SEEK_STEP_SECONDS);
        return;
      case "/":
        if (e.repeat) return;
        e.preventDefault(); // Firefox quick-find
        handlers.focusSearch();
        return;
    }

    if (e.repeat) return;
    switch (e.key.toLowerCase()) {
      case "n":
        e.preventDefault();
        handlers.next();
        return;
      case "p":
        e.preventDefault();
        handlers.previous();
        return;
      case "m":
        e.preventDefault();
        handlers.toggleMute();
    }
  });

  useEffect(() => {
    const listener = (e: KeyboardEvent) => onKeyDown(e);
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);
}
