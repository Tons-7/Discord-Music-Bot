"use client";

import useSWR from "swr";
import { useGuildState } from "./GuildStateProvider";
import { useToast } from "./Toast";
import { apiFetch } from "@/lib/api";
import { THEMES, type ThemeId } from "@/lib/themes";
import { useTheme } from "@/hooks/useTheme";
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
        <p className="text-sm font-semibold text-white">Theme</p>
        <p className="text-xs text-muted mt-0.5">Only you see it, on every server and device.</p>
      </div>
      <div className="flex flex-col gap-1">
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
      className="w-11 h-8 flex-shrink-0 rounded-lg overflow-hidden flex border border-white/[0.12] bg-surface-1"
    >
      <span className="w-1/2 bg-surface-2" />
      <span className="flex-1 flex items-center justify-center">
        <span className="w-2.5 h-2.5 rounded-full bg-accent" />
      </span>
    </span>
  );
}

function ServerSettings() {
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
      <div className="flex justify-center py-4">
        <div className="w-5 h-5 border-2 border-accent border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (error || !data) {
    return <p className="text-xs text-muted">Couldn't load server settings. Try reopening this panel.</p>;
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
    </>
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
        <p className="text-xs text-muted mt-0.5">{hint}</p>
      </div>
      <div className="flex flex-col gap-1">
        <Row selected={!value} onClick={() => onChange(null)}>
          <span className="block truncate">{emptyLabel}</span>
        </Row>
        {options.map(o => (
          <Row key={o.id} selected={value === o.id} onClick={() => onChange(o.id)}>
            <span className="block truncate">{prefix}{o.name}</span>
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
        "flex items-center justify-between gap-2 px-3 py-1.5 min-h-9 rounded-xl text-left text-xs transition-colors",
        selected
          ? "bg-accent/15 text-accent font-medium"
          : "text-white/70 hover:bg-white/[0.06]"
      )}
    >
      <span className="min-w-0 flex-1">{children}</span>
      {selected && (
        <svg className="w-3.5 h-3.5 flex-shrink-0" fill="currentColor" viewBox="0 0 24 24">
          <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
        </svg>
      )}
    </button>
  );
}
