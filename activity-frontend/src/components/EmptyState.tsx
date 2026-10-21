"use client";

import { cn } from "@/lib/utils";
import { AlertIcon } from "./ui/icons";

export interface EmptyStateAction {
  label: string;
  onClick: () => void;
}

/** A quiet icon, a line of text, and the next action when there is one. */
export default function EmptyState({ icon, title, subtitle, compact, action, className }: {
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  compact?: boolean;
  action?: EmptyStateAction;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center text-center px-6 py-8", className ?? "h-full")}>
      <div className={cn(
        "rounded-2xl bg-fg/[0.03] flex items-center justify-center text-muted",
        compact ? "w-12 h-12 mb-2" : "w-14 h-14 mb-3"
      )}>
        {icon}
      </div>
      <p className="text-sm font-medium text-fg/70">{title}</p>
      {subtitle && <p className="text-xs text-muted mt-1">{subtitle}</p>}
      {action && (
        <button
          type="button"
          onClick={action.onClick}
          className="mt-3 min-h-8 px-4 rounded-full bg-accent/15 text-accent hover:bg-accent/25 text-xs font-semibold transition-colors"
        >
          {action.label}
        </button>
      )}
    </div>
  );
}

/** A load failure in plain words, with a retry. */
export function ErrorState({ message, onRetry, compact }: { message: string; onRetry: () => void; compact?: boolean }) {
  return (
    <EmptyState
      compact={compact}
      icon={<AlertIcon className={compact ? "w-6 h-6" : "w-7 h-7"} strokeWidth={1.5} />}
      title={message}
      action={{ label: "Try again", onClick: onRetry }}
    />
  );
}
