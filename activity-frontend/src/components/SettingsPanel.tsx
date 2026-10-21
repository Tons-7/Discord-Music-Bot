"use client";

import { useState } from "react";
import useSWR from "swr";
import { useGuildId } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { apiFetch, errorMessage } from "@/lib/api";
import { THEMES, type ThemeId } from "@/lib/themes";
import { useTheme } from "@/hooks/useTheme";
import { cn } from "@/lib/utils";
import { CheckIcon } from "./ui/icons";
import { OptionRowsSkeleton } from "./ui/Skeleton";

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

export default function SettingsPanel() {
  // h-full, not flex-1: the parent is not a flex container, so it never sized.
  return (
    <div className="h-full overflow-y-auto px-4 py-4 flex flex-col gap-8">
      <Group title="Appearance">
        <ThemeSetting />
      </Group>
      <Group title="Server">
        <ServerSettings />
      </Group>
    </div>
  );
}

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">{title}</h2>
      {children}
    </section>
  );
}

function ThemeSetting() {
  const [theme, setTheme] = useTheme();
  const { toast } = useToast();

  const pick = (id: ThemeId) => {
    setTheme(id).catch(() => toast("Couldn't save your theme", "error"));
  };

  return (
    <div className="flex flex-col gap-2">
      <div>
        <p className="text-sm font-semibold text-fg" id="theme-label">Theme</p>
        <p className="text-xs text-muted mt-0.5">Only you see it, on every server and device.</p>
      </div>
      <div className="flex flex-col gap-1" role="radiogroup" aria-labelledby="theme-label">
        {THEMES.map(t => (
          <Row key={t.id} selected={theme === t.id} onClick={() => pick(t.id)}>
            <span className="flex items-center gap-3">
              <ThemePreview id={t.id} />
              <span className="min-w-0">
                <span className="block text-sm">{t.label}</span>
                <span className="block text-xs text-muted font-normal">{t.hint}</span>
              </span>
            </span>
          </Row>
        ))}
      </div>
    </div>
  );
}

// data-theme on the swatch applies that theme's colours to it alone.
function ThemePreview({ id }: { id: ThemeId }) {
  return (
    <span
      data-theme={id}
      aria-hidden="true"
      className="w-11 h-8 flex-shrink-0 rounded-lg overflow-hidden flex border border-fg/[0.12] bg-surface-1"
    >
      <span className="w-1/2 bg-surface-2" />
      <span className="flex-1 flex items-center justify-center">
        <span className="w-2.5 h-2.5 rounded-full bg-accent" />
      </span>
    </span>
  );
}

function ServerSettings() {
  const { guildId } = useGuildId();
  const { toast } = useToast();
  const key = `/api/guild/${guildId}/settings`;
  const { data, error, isLoading, mutate } = useSWR<Settings>(key);
  const [saving, setSaving] = useState(false);

  // Optimistic: the check moves at once and rolls back if the server refuses.
  const save = (path: string, body: Record<string, string | null>, patch: Partial<Settings>, label: string) => {
    if (saving) return;
    setSaving(true);
    mutate(
      apiFetch(`${key}/${path}`, { method: "POST", body: JSON.stringify(body) }).then(() => undefined),
      {
        optimisticData: (d) => (d ? { ...d, ...patch } : d) as Settings,
        rollbackOnError: true,
        populateCache: false,
        revalidate: true,
      },
    ).then(
      () => {
        setSaving(false);
        toast(label, "success");
      },
      (e: unknown) => {
        setSaving(false);
        toast(errorMessage(e, "Couldn't save"), "error");
      },
    );
  };

  if (isLoading) {
    return (
      <div className="flex flex-col gap-4">
        <OptionRowsSkeleton />
        <OptionRowsSkeleton />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-muted">Couldn&apos;t load server settings.</p>
        <button
          type="button"
          onClick={() => mutate()}
          className="min-h-8 px-3 rounded-full bg-accent/15 text-accent hover:bg-accent/25 text-xs font-semibold transition-colors flex-shrink-0"
        >
          Try again
        </button>
      </div>
    );
  }

  if (!data.is_admin) {
    return (
      <p className="text-xs text-muted">
        Only admins can change these.{" "}
        {data.dj_role_name ? `DJ role: ${data.dj_role_name}.` : "No DJ role is set, so everyone can control playback."}
      </p>
    );
  }

  return (
    <>
      <Setting
        id="dj-role"
        label="DJ role"
        hint="Only this role (and admins) can skip, stop, seek, or edit the queue."
        value={data.dj_role_id}
        options={data.roles ?? []}
        emptyLabel="Everyone"
        disabled={saving}
        onChange={id => save(
          "dj-role", { role_id: id },
          { dj_role_id: id, dj_role_name: data.roles?.find(r => r.id === id)?.name ?? null },
          id ? "DJ role updated" : "DJ role removed",
        )}
      />
      <Setting
        id="music-channel"
        label="Music channel"
        hint="Where the bot posts now-playing messages."
        value={data.music_channel_id}
        options={data.channels ?? []}
        emptyLabel="Wherever commands are used"
        prefix="#"
        disabled={saving}
        onChange={id => save(
          "music-channel", { channel_id: id },
          { music_channel_id: id, music_channel_name: data.channels?.find(c => c.id === id)?.name ?? null },
          id ? "Music channel updated" : "Music channel cleared",
        )}
      />
    </>
  );
}

function Setting({ id, label, hint, value, options, emptyLabel, prefix, disabled, onChange }: {
  id: string;
  label: string;
  hint: string;
  value: string | null;
  options: Option[];
  emptyLabel: string;
  prefix?: string;
  disabled?: boolean;
  onChange: (id: string | null) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <div>
        <p className="text-sm font-semibold text-fg" id={`${id}-label`}>{label}</p>
        <p className="text-xs text-muted mt-0.5">{hint}</p>
      </div>
      <div className="flex flex-col gap-1" role="radiogroup" aria-labelledby={`${id}-label`} aria-busy={disabled || undefined}>
        <Row selected={!value} disabled={disabled} onClick={() => onChange(null)}>
          <span className="block truncate">{emptyLabel}</span>
        </Row>
        {options.map(o => (
          <Row key={o.id} selected={value === o.id} disabled={disabled} onClick={() => onChange(o.id)}>
            <span className="block truncate">{prefix}{o.name}</span>
          </Row>
        ))}
      </div>
    </div>
  );
}

function Row({ selected, disabled, onClick, children }: {
  selected: boolean; disabled?: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      disabled={disabled}
      onClick={() => { if (!selected) onClick(); }}
      className={cn(
        "flex items-center justify-between gap-2 px-3 py-1.5 min-h-9 rounded-xl text-left text-sm transition-colors disabled:opacity-60",
        selected
          ? "bg-accent/15 text-accent font-medium"
          : "text-fg/80 hover:bg-fg/[0.06]"
      )}
    >
      <span className="min-w-0 flex-1">{children}</span>
      {selected && <CheckIcon className="w-3.5 h-3.5 flex-shrink-0" />}
    </button>
  );
}
