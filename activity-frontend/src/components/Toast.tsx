"use client";

import { createContext, useContext, useState, useCallback, useRef } from "react";
import { cn } from "@/lib/utils";

type ToastType = "info" | "success" | "error";

export interface ToastAction {
  label: string;
  onClick: () => void;
}

interface ToastItem {
  id: number;
  message: string;
  type: ToastType;
  action?: ToastAction;
  leaving?: boolean;
}

interface ToastContextValue {
  toast: (message: string, type?: ToastType, action?: ToastAction) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const VISIBLE_MS = 2300;
// Actionable toasts (Undo) need long enough to read and reach for
const ACTION_VISIBLE_MS = 6000;
const LEAVE_MS = 200;

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const idRef = useRef(0);

  const dismiss = useCallback((id: number) => {
    setToasts(prev => prev.map(t => t.id === id ? { ...t, leaving: true } : t));
    setTimeout(() => {
      setToasts(prev => prev.filter(t => t.id !== id));
    }, LEAVE_MS);
  }, []);

  const toast = useCallback((message: string, type: ToastType = "info", action?: ToastAction) => {
    const id = ++idRef.current;
    setToasts(prev => [...prev.slice(-4), { id, message, type, action }]); // max 5 visible
    setTimeout(() => dismiss(id), action ? ACTION_VISIBLE_MS : VISIBLE_MS);
  }, [dismiss]);

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      {/* Toast container — top left */}
      <div className="fixed top-3 left-3 z-50 flex flex-col gap-1.5 pointer-events-none max-w-[280px]">
        {toasts.map((t) => {
          const action = t.action;
          return (
            <div
              key={t.id}
              className={cn(
                "flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-medium shadow-[0_4px_20px_rgba(0,0,0,0.4)] backdrop-blur-md pointer-events-auto border",
                t.leaving ? "animate-[toast-out_0.2s_ease-in_forwards]" : "animate-[toast-in_0.2s_ease-out]",
                t.type === "success" && "bg-success/15 text-success border-success/20",
                t.type === "error" && "bg-danger/15 text-danger border-danger/20",
                t.type === "info" && "bg-accent/15 text-accent border-accent/20",
              )}
            >
              <span className="min-w-0">{t.message}</span>
              {action && (
                <button
                  type="button"
                  onClick={() => { action.onClick(); dismiss(t.id); }}
                  className="ml-auto -my-0.5 flex-shrink-0 px-2 py-1 rounded-lg bg-white/10 hover:bg-white/20 text-[11px] font-semibold transition-colors"
                >
                  {action.label}
                </button>
              )}
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
