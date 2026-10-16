// Per-listener colour themes. Colours live in globals.css under [data-theme="<id>"].
export const THEMES = [
  { id: "midnight", label: "Midnight", hint: "Default" },
  { id: "oled", label: "True black", hint: "Pure black, easy on OLED screens" },
  { id: "discord", label: "Discord", hint: "Matches Discord's dark theme" },
  { id: "ocean", label: "Ocean", hint: "Deep blue" },
  { id: "violet", label: "Violet", hint: "Dark purple" },
] as const;

export type ThemeId = (typeof THEMES)[number]["id"];

export const DEFAULT_THEME: ThemeId = "midnight";
export const THEME_KEY = "activity_theme";

// Runs before first paint (layout.tsx): applies the local copy of the theme until the server's arrives.
export const APPLY_SAVED_THEME = `try{var t=localStorage.getItem("${THEME_KEY}");if(t)document.documentElement.dataset.theme=t}catch(e){}`;

export function toTheme(id: string | null | undefined): ThemeId {
  return THEMES.find(t => t.id === id)?.id ?? DEFAULT_THEME;
}

/** Shows a theme and keeps a local copy for the next first paint. */
export function applyTheme(id: ThemeId) {
  document.documentElement.dataset.theme = id;
  try {
    localStorage.setItem(THEME_KEY, id);
  } catch {
    // private mode / storage disabled — the server copy still applies after load
  }
}
