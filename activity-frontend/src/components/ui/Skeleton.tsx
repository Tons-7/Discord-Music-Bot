import { cn } from "@/lib/utils";

const BLOCK = "rounded bg-fg/[0.06]";

export function SongRowSkeleton({ count = 6, className, compact }: {
  count?: number;
  className?: string;
  // Matches the denser sortable rows (Queue, playlist detail)
  compact?: boolean;
}) {
  return (
    <div className={cn("flex flex-col", compact ? "gap-1" : "gap-1.5", className)} aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <div
          key={i}
          className={cn(
            "flex items-center gap-3 rounded-2xl bg-fg/[0.02] border border-fg/[0.04] animate-pulse",
            compact ? "p-2" : "p-2.5",
          )}
        >
          <div className={cn("rounded-xl bg-fg/[0.06] flex-shrink-0", compact ? "w-10 h-10 rounded-lg" : "w-12 h-12")} />
          <div className="flex-1 min-w-0">
            <div className="h-3 rounded bg-fg/[0.08] w-3/4" />
            <div className="h-2.5 rounded bg-fg/[0.05] w-1/2 mt-2" />
          </div>
          <div className="h-2.5 rounded bg-fg/[0.05] w-8 flex-shrink-0" />
        </div>
      ))}
    </div>
  );
}

/** Title and artist lines of the Now Playing card before the first state arrives. */
export function NowPlayingTextSkeleton() {
  return (
    <div className="flex flex-col gap-1.5 py-0.5 animate-pulse" aria-hidden="true">
      <div className={cn(BLOCK, "h-3.5 w-48 max-w-full bg-fg/[0.08]")} />
      <div className={cn(BLOCK, "h-3 w-28 max-w-full")} />
    </div>
  );
}

export function StatsSkeleton() {
  return (
    <div className="flex flex-col gap-3 mt-2 animate-pulse" aria-hidden="true">
      <div className="grid grid-cols-2 gap-2">
        {[0, 1].map(i => (
          <div key={i} className="rounded-xl p-3 bg-fg/[0.03] border border-fg/[0.04]">
            <div className={cn(BLOCK, "h-2.5 w-16")} />
            <div className={cn(BLOCK, "h-5 w-12 mt-2 bg-fg/[0.08]")} />
          </div>
        ))}
      </div>
      <div className="flex flex-col gap-1">
        {Array.from({ length: 5 }, (_, i) => (
          <div key={i} className="flex items-center gap-2.5 p-2 rounded-xl bg-fg/[0.02] border border-fg/[0.04]">
            <div className={cn(BLOCK, "h-3 w-5")} />
            <div className={cn(BLOCK, "h-3 flex-1")} />
            <div className={cn(BLOCK, "h-3 w-10")} />
          </div>
        ))}
      </div>
    </div>
  );
}

/** Option rows of a settings list. */
export function OptionRowsSkeleton({ count = 3 }: { count?: number }) {
  return (
    <div className="flex flex-col gap-1 animate-pulse" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="h-9 rounded-xl bg-fg/[0.03] px-3 flex items-center">
          <div className={cn(BLOCK, "h-3 w-1/3")} />
        </div>
      ))}
    </div>
  );
}

/** Synced-lyrics lines while they load. */
export function LyricsSkeleton() {
  return (
    <div className="flex flex-col gap-3 px-5 py-4 animate-pulse" aria-hidden="true">
      {["w-3/4", "w-2/3", "w-5/6", "w-1/2", "w-3/4", "w-2/3", "w-4/5"].map((w, i) => (
        <div key={i} className={cn(BLOCK, "h-3.5", w)} />
      ))}
    </div>
  );
}
