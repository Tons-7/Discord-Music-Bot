"use client";

import { cn } from "@/lib/utils";

export default function EmptyState({ icon, title, subtitle, compact }: {
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  compact?: boolean;
}) {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center px-6">
      <div className={cn(
        "rounded-2xl bg-white/[0.03] flex items-center justify-center",
        compact ? "w-12 h-12 mb-2" : "w-14 h-14 mb-3"
      )}>
        {icon}
      </div>
      <p className={cn("font-medium text-white/60", compact ? "text-xs" : "text-sm")}>{title}</p>
      {subtitle && (
        <p className={cn("text-muted mt-1", compact ? "text-[10px]" : "text-xs")}>{subtitle}</p>
      )}
    </div>
  );
}
