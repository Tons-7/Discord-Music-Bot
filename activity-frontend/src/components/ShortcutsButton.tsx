"use client";

import { useRef } from "react";
import { SHORTCUTS } from "@/hooks/useKeyboardShortcuts";
import Popover from "./ui/Popover";
import KeyCap from "./ui/KeyCap";
import { KeyboardIcon } from "./ui/icons";

export default function ShortcutsButton({ open, onOpenChange }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const anchorRef = useRef<HTMLButtonElement>(null);

  return (
    <>
      <button
        ref={anchorRef}
        onClick={() => onOpenChange(!open)}
        aria-label="Keyboard shortcuts"
        title="Keyboard shortcuts (?)"
        className="group relative mt-auto w-10 h-10 rounded-xl flex items-center justify-center text-muted hover:text-white hover:bg-white/[0.06] transition-colors pointer-coarse:hidden"
      >
        <KeyboardIcon className="w-[18px] h-[18px]" />
        <span className="absolute right-full mr-2 z-50 px-2 py-1 rounded-lg bg-surface-3 text-xs text-white font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity shadow-lg">
          Shortcuts
        </span>
      </button>
      <Popover open={open} onClose={() => onOpenChange(false)} anchorRef={anchorRef} className="w-60 p-2">
        <p className="px-1 pb-2 text-xs font-semibold text-muted">Keyboard shortcuts</p>
        <ul>
          {SHORTCUTS.map(({ keys, label }) => (
            <li key={label} className="flex items-center justify-between gap-3 px-1 py-1.5 text-sm text-white/80">
              {label}
              <span className="flex gap-1">
                {keys.map(key => <KeyCap key={key}>{key}</KeyCap>)}
              </span>
            </li>
          ))}
        </ul>
      </Popover>
    </>
  );
}
