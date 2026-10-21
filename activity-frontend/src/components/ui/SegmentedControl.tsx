import { cn } from "@/lib/utils";

interface Option<T> {
  value: T;
  label: string;
}

/**
 * One-of-several choice shown as a pill track. `kind="tabs"` for views (tablist/tab),
 * otherwise a radiogroup. Arrow keys move the selection, as in a native radio group.
 */
export default function SegmentedControl<T extends string | boolean>({
  options, value, onChange, label, kind = "radio", size = "md", className, disabled,
}: {
  options: Option<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  kind?: "radio" | "tabs";
  size?: "sm" | "md";
  className?: string;
  disabled?: boolean;
}) {
  const index = options.findIndex(o => o.value === value);

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const step = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!step || disabled) return;
    e.preventDefault();
    const next = (Math.max(0, index) + step + options.length) % options.length;
    onChange(options[next].value);
    const buttons = e.currentTarget.querySelectorAll<HTMLButtonElement>("button");
    buttons[next]?.focus();
  };

  return (
    <div
      role={kind === "tabs" ? "tablist" : "radiogroup"}
      aria-label={label}
      onKeyDown={onKeyDown}
      className={cn(
        "flex bg-fg/[0.04]",
        size === "sm" ? "gap-1 p-0.5 rounded-lg" : "p-[3px] rounded-xl",
        className,
      )}
    >
      {options.map((o, i) => {
        const selected = i === index;
        return (
          <button
            key={String(o.value)}
            type="button"
            role={kind === "tabs" ? "tab" : "radio"}
            aria-selected={kind === "tabs" ? selected : undefined}
            aria-checked={kind === "tabs" ? undefined : selected}
            tabIndex={selected || (index < 0 && i === 0) ? 0 : -1}
            disabled={disabled}
            onClick={() => onChange(o.value)}
            className={cn(
              "flex-1 font-semibold transition-[background-color,color,box-shadow] duration-200 disabled:opacity-50",
              size === "sm"
                ? "min-h-7 pointer-coarse:min-h-8 px-1.5 rounded-md text-xs"
                : "py-1.5 pointer-coarse:py-2 rounded-[9px] text-xs",
              selected
                ? "bg-accent text-white shadow-[0_1px_4px_var(--color-accent-glow)]"
                : "text-muted hover:text-fg",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
