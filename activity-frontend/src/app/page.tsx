"use client";

import { useDiscord } from "@/hooks/useDiscord";
import { GuildStateProvider } from "@/components/GuildStateProvider";
import { ToastProvider } from "@/components/Toast";
import Dashboard from "@/components/Dashboard";

export default function ActivityPage() {
  const { ready, auth, error } = useDiscord();

  if (error) {
    return (
      <div className="h-screen flex flex-col items-center justify-center bg-surface-1 gap-4 px-8">
        <div className="w-16 h-16 rounded-2xl bg-danger/10 flex items-center justify-center">
          <svg className="w-8 h-8 text-danger" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
          </svg>
        </div>
        <p className="text-sm text-white/60 text-center max-w-[280px]">{error}</p>
        <button
          onClick={() => window.location.reload()}
          className="px-5 py-2 bg-accent hover:bg-accent/80 rounded-full text-sm font-medium transition-all active:scale-95"
        >
          Retry
        </button>
      </div>
    );
  }

  if (!ready || !auth) {
    return (
      <div className="h-screen flex flex-col items-center justify-center bg-surface-1 gap-4">
        <div className="w-10 h-10 border-2 border-accent border-t-transparent rounded-full animate-spin" />
        <p className="text-sm text-muted">Connecting...</p>
      </div>
    );
  }

  if (!auth.guildId) {
    return (
      <div className="h-screen flex flex-col items-center justify-center bg-surface-1 gap-3 px-8">
        <p className="text-sm text-muted text-center">
          Launch this Activity from a server to get started.
        </p>
      </div>
    );
  }

  return (
    <ToastProvider>
      <GuildStateProvider guildId={auth.guildId} accessToken={auth.accessToken}>
        <Dashboard />
      </GuildStateProvider>
    </ToastProvider>
  );
}
