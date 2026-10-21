import { cn } from "@/lib/utils";

// Shared icon set; className overrides the default w-4 h-4 sizing. Icons are decorative:
// the button or text around them carries the name.

type IconProps = { className?: string; strokeWidth?: number };

function Filled({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <svg className={cn("w-4 h-4", className)} fill="currentColor" viewBox="0 0 24 24" aria-hidden="true">
      {children}
    </svg>
  );
}

function Stroked({ className, strokeWidth = 2, d }: IconProps & { d: string | string[] }) {
  return (
    <svg className={cn("w-4 h-4", className)} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={strokeWidth} aria-hidden="true">
      {(Array.isArray(d) ? d : [d]).map(path => (
        <path key={path} strokeLinecap="round" strokeLinejoin="round" d={path} />
      ))}
    </svg>
  );
}

export function PlayIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M8 5v14l11-7z" /></Filled>;
}

export function PauseIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" /></Filled>;
}

export function StopIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M6 6h12v12H6z" /></Filled>;
}

export function SkipPrevIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z" /></Filled>;
}

export function SkipNextIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z" /></Filled>;
}

export function ShuffleIcon({ className }: IconProps) {
  return (
    <Filled className={className}>
      <path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z" />
    </Filled>
  );
}

export function RepeatIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z" /></Filled>;
}

export function RepeatOneIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4zm-4-2V9h-1l-2 1v1h1.5v4H13z" /></Filled>;
}

export function VolumeIcon({ className }: IconProps) {
  return (
    <Filled className={className}>
      <path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z" />
    </Filled>
  );
}

export function VolumeOffIcon({ className }: IconProps) {
  return (
    <Filled className={className}>
      <path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51A8.796 8.796 0 0021 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 003.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z" />
    </Filled>
  );
}

export function CloseIcon({ className }: IconProps) {
  return (
    <Filled className={className}>
      <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
    </Filled>
  );
}

export function PlusIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z" /></Filled>;
}

export function MinusIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M19 13H5v-2h14v2z" /></Filled>;
}

export function TrashIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" /></Filled>;
}

export function CheckIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" /></Filled>;
}

export function BackIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z" /></Filled>;
}

export function MoreIcon({ className }: IconProps) {
  return (
    <Filled className={className}>
      <path d="M6 10c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm12 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm-6 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z" />
    </Filled>
  );
}

export function NoteIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z" /></Filled>;
}

export function ChevronUpIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M7.41 15.41L12 10.83l4.59 4.58L18 14l-6-6-6 6z" /></Filled>;
}

export function ChevronDownIcon({ className }: IconProps) {
  return <Filled className={className}><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z" /></Filled>;
}

export function HeartFilledIcon({ className }: IconProps) {
  return (
    <Filled className={className}>
      <path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z" />
    </Filled>
  );
}

export function HeartIcon(props: IconProps) {
  return <Stroked {...props} d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z" />;
}

export function SearchIcon(props: IconProps) {
  return <Stroked {...props} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />;
}

export function QueueIcon(props: IconProps) {
  return <Stroked {...props} d="M4 6h16M4 10h16M4 14h10" />;
}

export function PlaylistIcon(props: IconProps) {
  return <Stroked {...props} d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 010 3.75H5.625a1.875 1.875 0 010-3.75z" />;
}

export function LyricsIcon(props: IconProps) {
  return (
    <Stroked
      {...props}
      d="M9 9l10.5-3m0 6.553v3.75a2.25 2.25 0 01-1.632 2.163l-1.32.377a1.803 1.803 0 11-.99-3.467l2.31-.66a2.25 2.25 0 001.632-2.163zm0 0V2.25L9 5.25v10.303m0 0v3.75a2.25 2.25 0 01-1.632 2.163l-1.32.377a1.803 1.803 0 01-.99-3.467l2.31-.66A2.25 2.25 0 009 15.553z"
    />
  );
}

export function StatsIcon(props: IconProps) {
  return (
    <Stroked
      {...props}
      d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z"
    />
  );
}

export function SettingsIcon(props: IconProps) {
  return (
    <Stroked
      {...props}
      d={[
        "M10.343 3.94c.09-.542.56-.94 1.11-.94h1.093c.55 0 1.02.398 1.11.94l.149.894c.07.424.384.764.78.93.398.164.855.142 1.205-.108l.737-.527a1.125 1.125 0 011.45.12l.773.774c.39.389.44 1.002.12 1.45l-.527.737c-.25.35-.272.806-.107 1.204.165.397.505.71.93.78l.893.15c.543.09.94.559.94 1.109v1.094c0 .55-.397 1.02-.94 1.11l-.893.149c-.425.07-.765.383-.93.78-.165.398-.143.854.107 1.204l.527.738c.32.447.269 1.06-.12 1.45l-.774.773a1.125 1.125 0 01-1.449.12l-.738-.527c-.35-.25-.806-.272-1.203-.107-.397.165-.71.505-.781.929l-.149.894c-.09.542-.56.94-1.11.94h-1.094c-.55 0-1.019-.398-1.11-.94l-.148-.894c-.071-.424-.384-.764-.781-.93-.398-.164-.854-.142-1.204.108l-.738.527c-.447.32-1.06.269-1.45-.12l-.773-.774a1.125 1.125 0 01-.12-1.45l.527-.737c.25-.35.273-.806.108-1.204-.165-.397-.505-.71-.93-.78l-.894-.15c-.542-.09-.94-.56-.94-1.109v-1.094c0-.55.398-1.02.94-1.11l.894-.149c.424-.07.765-.383.93-.78.165-.398.143-.854-.107-1.204l-.527-.738a1.125 1.125 0 01.12-1.45l.773-.773a1.125 1.125 0 011.45-.12l.737.527c.35.25.807.272 1.204.107.397-.165.71-.505.78-.929l.15-.894z",
        "M15 12a3 3 0 11-6 0 3 3 0 016 0z",
      ]}
    />
  );
}

export function HistoryIcon(props: IconProps) {
  return <Stroked {...props} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />;
}

export function MusicIcon(props: IconProps) {
  return (
    <Stroked
      {...props}
      d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"
    />
  );
}

export function TrophyIcon(props: IconProps) {
  return (
    <Stroked
      {...props}
      d="M16.5 18.75h-9m9 0a3 3 0 013 3h-15a3 3 0 013-3m9 0v-3.375c0-.621-.503-1.125-1.125-1.125h-.871M7.5 18.75v-3.375c0-.621.504-1.125 1.125-1.125h.872m5.007 0H9.497m5.007 0a7.454 7.454 0 01-.982-3.172M9.497 14.25a7.454 7.454 0 00.981-3.172M5.25 4.236c-.982.143-1.954.317-2.916.52A6.003 6.003 0 007.73 9.728M5.25 4.236V4.5c0 2.108.966 3.99 2.48 5.228M5.25 4.236V2.721C7.456 2.41 9.71 2.25 12 2.25c2.291 0 4.545.16 6.75.47v1.516M18.75 4.236c.982.143 1.954.317 2.916.52A6.003 6.003 0 0016.27 9.728M18.75 4.236V4.5c0 2.108-.966 3.99-2.48 5.228m4.974-5.492a10.509 10.509 0 01-1.292 4.18M7.73 9.728a6.726 6.726 0 002.748 1.35m3.043 0a6.726 6.726 0 002.749-1.35m-5.792 0a24.585 24.585 0 003.043 0"
    />
  );
}

export function UsersIcon(props: IconProps) {
  return (
    <Stroked
      {...props}
      d="M15 19.128a9.38 9.38 0 002.625.372 9.337 9.337 0 004.121-.952 4.125 4.125 0 00-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 018.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0111.964-3.07M12 6.375a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zm8.25 2.25a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z"
    />
  );
}

export function InviteIcon(props: IconProps) {
  return <Stroked {...props} d="M18 9v3m0 0v3m0-3h3m-3 0h-3M13 7a4 4 0 11-8 0 4 4 0 018 0zM3 20a6 6 0 0112 0H3z" />;
}

export function AlertIcon(props: IconProps) {
  return <Stroked {...props} d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />;
}

export function PlaylistAddIcon(props: IconProps) {
  return <Stroked {...props} d="M4 6h11M4 11h11M4 16h6m5 2h6m-3-3v6" />;
}

export function CopyIcon(props: IconProps) {
  return <Stroked {...props} d="M8 7V5a2 2 0 012-2h9a2 2 0 012 2v9a2 2 0 01-2 2h-2M5 7h9a2 2 0 012 2v10a2 2 0 01-2 2H5a2 2 0 01-2-2V9a2 2 0 012-2z" />;
}

export function AddIcon(props: IconProps) {
  return <Stroked {...props} d="M12 6v12m6-6H6" />;
}

export function InfinityIcon(props: IconProps) {
  return <Stroked {...props} d="M12 12c-2-2.67-4-4-6-4a4 4 0 1 0 0 8c2 0 4-1.33 6-4Zm0 0c2 2.67 4 4 6 4a4 4 0 0 0 0-8c-2 0-4 1.33-6 4Z" />;
}

export function KeyboardIcon({ className }: IconProps) {
  return (
    <svg className={cn("w-4 h-4", className)} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} aria-hidden="true">
      <rect x="2" y="6" width="20" height="12" rx="2" />
      <path strokeLinecap="round" d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10" />
    </svg>
  );
}

export function Spinner({ className, label = "Loading" }: IconProps & { label?: string }) {
  return (
    <div
      className={cn("w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin", className)}
      role="status"
      aria-label={label}
    />
  );
}
