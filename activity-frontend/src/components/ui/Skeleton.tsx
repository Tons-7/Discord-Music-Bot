import { cn } from "@/lib/utils";

export function SongRowSkeleton({ count = 6, className }: { count?: number; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-1.5", className)} aria-hidden>
      {Array.from({ length: count }, (_, i) => (
        <div
          key={i}
          className="flex items-center gap-3 p-2.5 rounded-2xl bg-white/[0.02] border border-white/[0.04] animate-pulse"
        >
          <div className="w-12 h-12 rounded-xl bg-white/[0.06] flex-shrink-0" />
          <div className="flex-1 min-w-0">
            <div className="h-3 rounded bg-white/[0.08] w-3/4" />
            <div className="h-2.5 rounded bg-white/[0.05] w-1/2 mt-2" />
          </div>
          <div className="h-2.5 rounded bg-white/[0.05] w-8 flex-shrink-0" />
        </div>
      ))}
    </div>
  );
}
