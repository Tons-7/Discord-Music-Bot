import { cn, proxyImg } from "@/lib/utils";
import MarqueeText from "./MarqueeText";
import { NoteIcon } from "./ui/icons";

type RowState = "default" | "added" | "pending";

const STATE_CLASSES: Record<RowState, string> = {
  default: "bg-white/[0.02] border-white/[0.04] hover:bg-white/[0.06] hover:border-white/[0.08]",
  added: "bg-success/[0.06] border-success/20",
  pending: "bg-white/[0.02] border-white/[0.04] opacity-70",
};

// div[role=button] instead of <button>: trailing can contain real buttons
// (FavHeart, remove) and nested buttons are invalid HTML.
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

  const activate = () => {
    if (!disabled) onClick?.();
  };

  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled || undefined}
      onClick={activate}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          activate();
        }
      }}
      className={cn(
        "w-full flex items-center gap-3 p-2.5 rounded-2xl text-left border cursor-pointer",
        "transition-[background-color,border-color,opacity] duration-200",
        STATE_CLASSES[state],
        disabled && "cursor-default",
        group && "group",
        className,
      )}
    >
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
          ? <MarqueeText className={titleCls}>{title}</MarqueeText>
          : <p className={titleCls}>{title}</p>}
        {subtitle && <p className="text-xs text-white/40 truncate mt-0.5">{subtitle}</p>}
      </div>

      {trailing && (
        <div className="flex items-center gap-1.5 flex-shrink-0">{trailing}</div>
      )}
    </div>
  );
}
