"use client";

import { useState, useEffect } from "react";
import { useGuildState } from "./GuildStateProvider";

// Brief drops (a reconnect between two pings, a backgrounded phone waking up)
// are invisible to the user — only surface a socket that stays down.
const GRACE_MS = 2000;

export default function ConnectionStatus() {
  const { connected } = useGuildState();
  const [show, setShow] = useState(false);

  useEffect(() => {
    if (connected) { setShow(false); return; }
    const t = setTimeout(() => setShow(true), GRACE_MS);
    return () => clearTimeout(t);
  }, [connected]);

  if (!show) return null;

  return (
    // Top centre: Discord overlays the top corners on mobile (bot name / Leave),
    // the InviteButton sits top-right and toasts top-left. On mobile the panel
    // header owns the first row, so drop below it.
    <div
      role="status"
      aria-live="polite"
      className="fixed top-12 sm:top-3 left-1/2 -translate-x-1/2 z-40 pointer-events-none"
    >
      <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-accent/15 border border-accent/30 text-accent text-[11px] font-medium backdrop-blur-md shadow-[0_2px_12px_rgba(0,0,0,0.35)] animate-[toast-in_0.2s_ease-out]">
        <span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
        Reconnecting…
      </div>
    </div>
  );
}
