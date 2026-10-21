"use client";

import { SWRConfig } from "swr";
import { useDiscord } from "@/hooks/useDiscord";
import { GuildStateProvider } from "@/components/GuildStateProvider";
import { ToastProvider } from "@/components/Toast";
import Dashboard from "@/components/Dashboard";
import StatusScreen from "@/components/StatusScreen";
import { apiFetch } from "@/lib/api";

const swrConfig = { fetcher: (key: string) => apiFetch(key), dedupingInterval: 5000 };

export default function ActivityPage() {
  const { ready, auth, error } = useDiscord();

  if (error) {
    return <StatusScreen kind="error" message={error} action={{ label: "Try again", onClick: () => window.location.reload() }} />;
  }

  if (!ready || !auth) {
    return (
      <StatusScreen
        kind="loading"
        message="Connecting…"
        footer={<p className="text-xs text-muted font-mono">build {process.env.NEXT_PUBLIC_BUILD_ID}</p>}
      />
    );
  }

  if (!auth.guildId) {
    return <StatusScreen kind="info" message="Launch this Activity from a server to get started." />;
  }

  return (
    <SWRConfig value={swrConfig}>
      <ToastProvider>
        <GuildStateProvider guildId={auth.guildId} channelId={auth.channelId} instanceId={auth.instanceId}>
          <Dashboard />
        </GuildStateProvider>
      </ToastProvider>
    </SWRConfig>
  );
}
