"use client";

import { createContext, useContext, useState, useCallback, useRef } from "react";
import { cn } from "@/lib/utils";

interface ToastItem {
  id: number;
  message: string;
  type: "info" | "success" | "error";
}

interface ToastContextValue {
  toast: (message: string, type?: "info" | "success" | "error") => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const idRef = useRef(0);

  const toast = useCallback((message: string, type: "info" | "success" | "error" = "info") => {
    const id = ++idRef.current;
    setToasts(prev => [...prev.slice(-4), { id, message, type }]); // max 5 visible
    setTimeout(() => {
      setToasts(prev => prev.filter(t => t.id !== id));
    }, 2500);
  }, []);

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      {/* Toast container — top left */}
      <div className="fixed top-3 left-3 z-50 flex flex-col gap-1.5 pointer-events-none max-w-[280px]">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={cn(
              "px-3 py-2 rounded-xl text-xs font-medium shadow-[0_4px_20px_rgba(0,0,0,0.4)] backdrop-blur-md animate-[toast-in_0.2s_ease-out] pointer-events-auto border",
              t.type === "success" && "bg-success/15 text-success border-success/20",
              t.type === "error" && "bg-danger/15 text-danger border-danger/20",
              t.type === "info" && "bg-accent/15 text-accent border-accent/20",
            )}
          >
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
