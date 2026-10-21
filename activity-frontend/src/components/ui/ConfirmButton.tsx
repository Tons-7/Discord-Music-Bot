import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";

const CONFIRM_MS = 3000;

/** Two-step confirmation: `arm(key)` asks, a second press within 3s confirms. */
export function useConfirm<K = true>() {
  const [armed, setArmed] = useState<K | null>(null);
  useEffect(() => {
    if (armed === null) return;
    const t = setTimeout(() => setArmed(null), CONFIRM_MS);
    return () => clearTimeout(t);
  }, [armed]);
  return {
    armed,
    /** Returns true when this press confirms an earlier one. */
    press: (key: K): boolean => {
      if (armed === key) {
        setArmed(null);
        return true;
      }
      setArmed(key);
      return false;
    },
    reset: () => setArmed(null),
  };
}

/** Text button for a destructive action that asks once before it runs. */
export default function ConfirmButton({ label, confirmLabel, onConfirm, disabled, className }: {
  label: string;
  confirmLabel: string;
  onConfirm: () => void;
  disabled?: boolean;
  className?: string;
}) {
  const { armed, press } = useConfirm();
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => { if (press(true)) onConfirm(); }}
      aria-live="polite"
      className={cn(
        "relative min-h-8 px-3 -my-1 rounded-lg text-xs font-medium transition-colors disabled:opacity-40",
        "pointer-coarse:after:absolute pointer-coarse:after:-inset-1 pointer-coarse:after:content-['']",
        armed ? "text-white bg-danger" : "text-danger hover:bg-danger/10",
        className,
      )}
    >
      {armed ? confirmLabel : label}
    </button>
  );
}
