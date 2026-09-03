"use client";

import useSWR from "swr";
import { useGuildState } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { apiFetch } from "@/lib/api";
import EmptyState from "./EmptyState";
import { cn } from "@/lib/utils";

interface Option {
  id: string;
  name: string;
}

interface Settings {
  is_admin: boolean;
  dj_role_id: string | null;
  dj_role_name: string | null;
  music_channel_id: string | null;
  music_channel_name: string | null;
  roles?: Option[];
  channels?: Option[];
}

const LockIcon = () => (
  <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
  </svg>
);

export default function SettingsPanel() {
  const { guildId } = useGuildState();
  const { toast } = useToast();
  const key = `/api/guild/${guildId}/settings`;
  const { data, error, isLoading, mutate } = useSWR<Settings>(key);

  const save = async (path: string, body: Record<string, string | null>, label: string) => {
    try {
      await apiFetch(`${key}/${path}`, { method: "POST", body: JSON.stringify(body) });
      toast(label, "success");
      mutate();
    } catch (e: any) {
      toast(e?.message || "Could not save", "error");
    }
  };

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <div className="w-5 h-5 border-2 border-accent border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <EmptyState
        icon={<LockIcon />}
        title="Couldn't load settings"
        subtitle="Try reopening this panel"
      />
    );
  }

  if (!data.is_admin) {
    return (
      <EmptyState
        icon={<LockIcon />}
        title="Admins only"
        subtitle={
          data?.dj_role_name
            ? `DJ role: ${data.dj_role_name}`
            : "No DJ role set — everyone can control playback"
        }
      />
    );
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 py-4 flex flex-col gap-6">
      <Setting
        label="DJ role"
        hint="Only this role (and admins) can skip, stop, seek, or edit the queue."
        value={data.dj_role_id}
        options={data.roles ?? []}
        emptyLabel="Everyone"
        onChange={id => save("dj-role", { role_id: id }, id ? "DJ role updated" : "DJ role removed")}
      />
      <Setting
        label="Music channel"
        hint="Where the bot posts now-playing messages."
        value={data.music_channel_id}
        options={data.channels ?? []}
        emptyLabel="Wherever commands are used"
        prefix="#"
        onChange={id => save("music-channel", { channel_id: id }, id ? "Music channel updated" : "Music channel cleared")}
      />
    </div>
  );
}

function Setting({ label, hint, value, options, emptyLabel, prefix, onChange }: {
  label: string;
  hint: string;
  value: string | null;
  options: Option[];
  emptyLabel: string;
  prefix?: string;
  onChange: (id: string | null) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <div>
        <p className="text-sm font-semibold text-white">{label}</p>
        <p className="text-[11px] text-muted mt-0.5">{hint}</p>
      </div>
      <div className="flex flex-col gap-1">
        <Row selected={!value} onClick={() => onChange(null)}>{emptyLabel}</Row>
        {options.map(o => (
          <Row key={o.id} selected={value === o.id} onClick={() => onChange(o.id)}>
            {prefix}{o.name}
          </Row>
        ))}
      </div>
    </div>
  );
}

function Row({ selected, onClick, children }: {
  selected: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={selected}
      className={cn(
        "flex items-center justify-between gap-2 px-3 h-9 rounded-xl text-left text-xs transition-colors",
        selected
          ? "bg-accent/15 text-accent font-medium"
          : "text-white/70 hover:bg-white/[0.06]"
      )}
    >
      <span className="truncate">{children}</span>
      {selected && (
        <svg className="w-3.5 h-3.5 flex-shrink-0" fill="currentColor" viewBox="0 0 24 24">
          <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
        </svg>
      )}
    </button>
  );
}
