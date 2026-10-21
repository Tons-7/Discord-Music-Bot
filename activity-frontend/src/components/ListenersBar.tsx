"use client";

import useSWR from "swr";
import { useGuildId } from "./GuildStateProvider";
import { proxyImg, cn } from "@/lib/utils";

interface Listener {
  id: string;
  name: string;
  avatar: string | null;
}

const MAX_SHOWN = 4;

/** Avatars of everyone with the Activity open. Polled — connects/disconnects
 *  don't broadcast, and a listener list is not worth a WS event. */
export default function ListenersBar({ className }: { className?: string }) {
  const { guildId } = useGuildId();
  const { data } = useSWR<{ listeners: Listener[]; count: number }>(
    `/api/guild/${guildId}/listeners`,
    { refreshInterval: 15000, revalidateOnFocus: true },
  );

  const listeners = data?.listeners ?? [];
  if (listeners.length === 0) return null;

  const shown = listeners.slice(0, MAX_SHOWN);
  const extra = listeners.length - shown.length;
  const names = listeners.map(l => l.name).join(", ");

  return (
    <div className={cn("flex items-center gap-1.5", className)} title={names} role="img" aria-label={`Listening: ${names}`}>
      <div className="flex -space-x-2">
        {shown.map(l => (
          <div
            key={l.id}
            className="w-6 h-6 rounded-full overflow-hidden bg-surface-3 ring-2 ring-surface-1 flex items-center justify-center"
          >
            {l.avatar ? (
              <img src={proxyImg(l.avatar)} alt="" className="w-full h-full object-cover" />
            ) : (
              <span className="text-[11px] font-semibold text-fg/70">
                {l.name.slice(0, 1).toUpperCase()}
              </span>
            )}
          </div>
        ))}
      </div>
      {extra > 0 && <span className="text-xs text-muted tabular-nums">+{extra}</span>}
    </div>
  );
}
