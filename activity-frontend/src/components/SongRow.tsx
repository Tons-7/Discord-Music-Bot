"use client";

import { cn, proxyImg } from "@/lib/utils";
import MarqueeText from "./MarqueeText";

type RowState = "default" | "added" | "pending";

const STATE_CLASSES: Record<RowState, string> = {
  default: "bg-white/[0.02] border-white/[0.04] hover:bg-white/[0.06] hover:border-white/[0.08]",
  added: "bg-success/[0.06] border-success/20",
  pending: "bg-white/[0.02] border-white/[0.04] opacity-70",
};

export default function SongRow({
  title, subtitle, thumbnail, marquee,
  state = "default", onClick, disabled, trailing, className, group,
}: {
  title: string;
  subtitle?: string;
  thumbnail?: string;
  marquee?: boolean;
  state?: RowState;
  onClick?: () => void;
  disabled?: boolean;
  trailing?: React.ReactNode;
  className?: string;
  group?: boolean;
}) {
  const titleCls = cn(
    "text-sm font-medium truncate",
    state === "added" ? "text-success" : "text-white"
  );

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "w-full flex items-center gap-3 p-2.5 rounded-2xl text-left border",
        "transition-[background-color,border-color,opacity] duration-200",
        STATE_CLASSES[state],
        group && "group",
        className,
      )}
    >
      <div className="w-12 h-12 rounded-xl overflow-hidden bg-surface-3 flex-shrink-0">
        {thumbnail ? (
          <img src={proxyImg(thumbnail)} alt="" className="w-full h-full object-cover" loading="lazy" />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            <svg className="w-5 h-5 text-muted" fill="currentColor" viewBox="0 0 24 24">
              <path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z" />
            </svg>
          </div>
        )}
      </div>

      <div className="flex-1 min-w-0">
        {marquee
          ? <MarqueeText className={titleCls}>{title}</MarqueeText>
          : <p className={titleCls}>{title}</p>}
        {subtitle && <p className="text-xs text-white/30 truncate mt-0.5">{subtitle}</p>}
      </div>

      {trailing && (
        <div className="flex items-center gap-1.5 flex-shrink-0">{trailing}</div>
      )}
    </button>
  );
}
