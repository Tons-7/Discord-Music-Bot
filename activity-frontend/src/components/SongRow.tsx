import { useState } from "react";
import { cn, formatDuration, proxyImg } from "@/lib/utils";
import MarqueeText from "./MarqueeText";
import { CheckIcon, NoteIcon, Spinner } from "./ui/icons";
import type { AddStatus } from "@/hooks/useQueueAdd";

const STATE_CLASSES: Record<AddStatus, string> = {
  default: "bg-fg/[0.02] border-fg/[0.04] hover:bg-fg/[0.06] hover:border-fg/[0.08]",
  added: "bg-success/[0.06] border-success/20",
  pending: "bg-fg/[0.02] border-fg/[0.04]",
};

/** Duration column that swaps in place for the pending spinner or the added check. */
export function StatusSlot({ status = "default", duration }: { status?: AddStatus; duration?: number }) {
  return (
    <span className="min-w-10 flex justify-end items-center text-xs tabular-nums text-muted">
      {status === "pending" ? <Spinner className="w-4 h-4" label="Adding" />
        : status === "added" ? <CheckIcon className="w-4 h-4 text-success" />
        : duration && duration > 0 ? formatDuration(duration) : ""}
    </span>
  );
}

/**
 * A song row whose whole surface is one button (the primary action), with the trailing
 * actions as siblings painted above it — no interactive element is nested inside another.
 * Trailing controls must be positioned (IconButton is) to sit above the row's button.
 */
export default function SongRow({
  title, subtitle, thumbnail, marquee, actionLabel,
  state = "default", onClick, disabled, trailing, duration, className,
}: {
  title: string;
  subtitle?: string;
  thumbnail?: string;
  marquee?: boolean;
  // Accessible name of the row's action, e.g. 'Add "Song" to the queue'
  actionLabel: string;
  state?: AddStatus;
  onClick?: () => void;
  disabled?: boolean;
  // Hover-revealed actions (buttons), shown before the status column
  trailing?: React.ReactNode;
  duration?: number;
  className?: string;
}) {
  const [hovered, setHovered] = useState(false);
  const titleCls = cn("text-sm font-medium truncate", state === "added" ? "text-success" : "text-fg");

  return (
    <div
      onPointerEnter={marquee ? e => { if (e.pointerType === "mouse") setHovered(true); } : undefined}
      onPointerLeave={marquee ? () => setHovered(false) : undefined}
      className={cn(
        "group relative w-full flex items-center gap-3 p-2.5 rounded-2xl border",
        "transition-[background-color,border-color,opacity] duration-200",
        STATE_CLASSES[state],
        className,
      )}
    >
      {/* aria-disabled, not disabled: a disabled button would drop keyboard focus mid-add */}
      <button
        type="button"
        onClick={disabled ? undefined : onClick}
        aria-disabled={disabled || undefined}
        aria-label={actionLabel}
        aria-busy={state === "pending" || undefined}
        className={cn("absolute inset-0 rounded-2xl", disabled ? "cursor-default" : "cursor-pointer")}
      />

      <div className="w-12 h-12 rounded-xl overflow-hidden bg-surface-3 flex-shrink-0">
        {thumbnail ? (
          <img src={proxyImg(thumbnail)} alt="" className="w-full h-full object-cover" loading="lazy" />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            <NoteIcon className="w-5 h-5 text-muted" />
          </div>
        )}
      </div>

      <div className="flex-1 min-w-0">
        {marquee
          ? <MarqueeText className={titleCls} hovered={hovered}>{title}</MarqueeText>
          : <p className={titleCls}>{title}</p>}
        {subtitle && <p className="text-xs text-muted truncate mt-0.5">{subtitle}</p>}
      </div>

      <div className="flex items-center gap-1.5 pointer-coarse:gap-2.5 flex-shrink-0">
        {trailing}
        <StatusSlot status={state} duration={duration} />
      </div>
    </div>
  );
}
