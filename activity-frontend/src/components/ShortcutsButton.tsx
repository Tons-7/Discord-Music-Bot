"use client";

import { useRef } from "react";
import { SHORTCUTS } from "@/hooks/useKeyboardShortcuts";
import Popover from "./ui/Popover";
import KeyCap from "./ui/KeyCap";
import { KeyboardIcon } from "./ui/icons";
import { SidebarLabel } from "./SidebarLabel";

export default function ShortcutsButton({ open, onOpenChange }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const anchorRef = useRef<HTMLButtonElement>(null);

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={() => onOpenChange(!open)}
        aria-label="Keyboard shortcuts"
        aria-haspopup="dialog"
        aria-expanded={open}
        className="group relative mt-auto w-10 h-10 rounded-xl flex items-center justify-center text-muted hover:text-fg hover:bg-fg/[0.06] transition-colors pointer-coarse:hidden"
      >
        <KeyboardIcon className="w-[18px] h-[18px]" />
        <SidebarLabel>Shortcuts <KeyCap>?</KeyCap></SidebarLabel>
      </button>
      <Popover open={open} onClose={() => onOpenChange(false)} anchorRef={anchorRef} label="Keyboard shortcuts" className="w-60 p-2">
        <p className="px-1 pb-2 text-xs font-semibold text-muted">Keyboard shortcuts</p>
        <ul>
          {SHORTCUTS.map(({ keys, label }) => (
            <li key={label} className="flex items-center justify-between gap-3 px-1 py-1.5 text-sm text-fg/80">
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
