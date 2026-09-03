"use client";

import { cn } from "@/lib/utils";

type Tone = "default" | "accent" | "danger" | "success";
type Size = "xs" | "sm" | "md";

const SIZE_CLS: Record<Size, string> = {
  xs: "w-6 h-6 rounded-md",
  sm: "w-7 h-7 rounded-lg",
  md: "w-8 h-8 rounded-full",
};

const TONE_CLS: Record<Tone, string> = {
  default: "text-muted hover:text-white hover:bg-white/[0.06]",
  accent: "text-muted hover:text-accent hover:bg-accent/10",
  danger: "text-muted hover:text-danger hover:bg-danger/10",
  success: "text-muted hover:text-success hover:bg-success/10",
};

// Icon-only button; hit area is enlarged on touch devices via ::after.
export default function IconButton({
  label,
  size = "sm",
  tone = "default",
  className,
  children,
  ref,
  ...rest
}: {
  label: string;
  size?: Size;
  tone?: Tone;
  ref?: React.Ref<HTMLButtonElement>;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      ref={ref}
      type="button"
      aria-label={label}
      title={label}
      {...rest}
      className={cn(
        "relative flex items-center justify-center flex-shrink-0 transition-colors active:scale-90",
        "disabled:opacity-40 disabled:pointer-events-none",
        "pointer-coarse:after:absolute pointer-coarse:after:-inset-1.5 pointer-coarse:after:content-['']",
        SIZE_CLS[size],
        TONE_CLS[tone],
        className,
      )}
    >
      {children}
    </button>
  );
}
