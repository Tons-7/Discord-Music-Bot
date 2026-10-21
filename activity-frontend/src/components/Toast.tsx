"use client";

import { createContext, useContext, useRef, useState } from "react";
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
// Errors are often the only feedback for a failed action; give them time to be read.
const ERROR_VISIBLE_MS = 5000;
// Actionable toasts (Undo) need long enough to read and reach for
const ACTION_VISIBLE_MS = 6000;
const LEAVE_MS = 200;
const MAX_VISIBLE = 5;

function visibleFor(type: ToastType, action?: ToastAction) {
  if (action) return ACTION_VISIBLE_MS;
  return type === "error" ? ERROR_VISIBLE_MS : VISIBLE_MS;
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const idRef = useRef(0);
  // Per-toast countdown, paused while the pointer or focus is on the toast.
  const timersRef = useRef(new Map<number, { timer?: ReturnType<typeof setTimeout>; remaining: number; startedAt: number }>());

  // "type:message" -> id of the visible toast showing it (toasts without an action only)
  const liveRef = useRef(new Map<string, number>());

  const dismiss = (id: number) => {
    const t = timersRef.current.get(id);
    if (t) clearTimeout(t.timer);
    timersRef.current.delete(id);
    for (const [key, liveId] of liveRef.current) if (liveId === id) liveRef.current.delete(key);
    setToasts(prev => prev.map(item => item.id === id ? { ...item, leaving: true } : item));
    setTimeout(() => setToasts(prev => prev.filter(item => item.id !== id)), LEAVE_MS);
  };

  const startTimer = (id: number, ms: number) => {
    const entry = { remaining: ms, startedAt: Date.now(), timer: setTimeout(() => dismiss(id), ms) };
    timersRef.current.set(id, entry);
  };

  const pause = (id: number) => {
    const t = timersRef.current.get(id);
    if (!t || t.timer === undefined) return;
    clearTimeout(t.timer);
    t.timer = undefined;
    t.remaining = Math.max(500, t.remaining - (Date.now() - t.startedAt));
  };

  const resume = (id: number) => {
    const t = timersRef.current.get(id);
    if (!t || t.timer !== undefined) return;
    startTimer(id, t.remaining);
  };

  const toast = (message: string, type: ToastType = "info", action?: ToastAction) => {
    const key = `${type}:${message}`;
    if (!action) {
      // The same notice twice (a held key, a double click) refreshes the one on screen.
      const liveId = liveRef.current.get(key);
      const t = liveId !== undefined ? timersRef.current.get(liveId) : undefined;
      if (liveId !== undefined && t && idRef.current - liveId < MAX_VISIBLE) {
        if (t.timer !== undefined) {
          clearTimeout(t.timer);
          startTimer(liveId, visibleFor(type));
        }
        return;
      }
    }
    const id = ++idRef.current;
    if (!action) liveRef.current.set(key, id);
    setToasts(prev => [...prev.slice(-(MAX_VISIBLE - 1)), { id, message, type, action }]);
    startTimer(id, visibleFor(type, action));
  };

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      {/* Always mounted so screen readers announce what is added. Top left on desktop;
          on phones below Discord's top-corner overlay, centred. */}
      <div
        aria-live="polite"
        className={cn(
          "fixed z-50 flex flex-col gap-1.5 pointer-events-none max-w-[280px]",
          "top-3 left-3",
          "max-sm:left-1/2 max-sm:-translate-x-1/2 max-sm:items-center max-sm:w-max max-sm:max-w-[calc(100vw-2rem)]",
          "max-sm:top-[calc(env(safe-area-inset-top)+3.5rem)]",
        )}
      >
        {toasts.map((t) => {
          const action = t.action;
          return (
            <div
              key={t.id}
              role={t.type === "error" ? "alert" : "status"}
              onPointerEnter={() => pause(t.id)}
              onPointerLeave={() => resume(t.id)}
              onFocus={() => pause(t.id)}
              onBlur={() => resume(t.id)}
              className={cn(
                "flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-medium shadow-[0_4px_20px_rgba(0,0,0,0.4)] backdrop-blur-md border",
                // Touch: a toast without a button never blocks a tap on what's under it.
                action ? "pointer-events-auto" : "pointer-events-auto pointer-coarse:pointer-events-none",
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
                  className="ml-auto -my-0.5 flex-shrink-0 px-2 py-1 rounded-lg bg-fg/10 hover:bg-fg/20 text-xs font-semibold transition-colors"
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
