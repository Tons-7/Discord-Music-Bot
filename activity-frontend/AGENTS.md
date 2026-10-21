<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# Audio playback

`useAudioPlayer` (`src/hooks/useAudioPlayer.ts`) owns the single `<audio>` element. When the current song changes it sets `src = /api/guild/{id}/stream?token=<session token>`, seeks to the server position on `loadedmetadata` (so a mid-song join never plays the intro), and on `canplay` calls `play()` only if the room is not paused. If playback hangs on song change, suspect the backend stream endpoint (`activity/routes/stream_routes.py`) — it must stream chunks, not buffer the upstream body, since Chromium sends `Range: bytes=0-` for media elements.

Other invariants in the hook: play/pause is decided from the shared state (the room's pause flag), never from the local element, which may be loading, blocked or silent in voice mode; `play()` rejections with `NotAllowedError` set `blocked` (UI shows a tap-to-play overlay calling `resume()`), and a suspended effects `AudioContext` does too; a null current song (remote stop) or voice mode must tear the element down and reset its state; every load's listeners hang off one `AbortController`, aborted by the next load or `stop()`; a failing stream is retried with backoff, then `error` is set (UI offers Try again and Skip); error recovery resumes from `positionRef`, not `currentTime` (a failed seek reverts the element's clock); `defaultPlaybackRate` is set with `playbackRate`, because every `load()` resets the rate to it. Bass/Treble/8D run through a lazily-created Web Audio chain — once `createMediaElementSource` exists the element is permanently routed through it, so it is only built when one of those effects is first used (never in voice mode).

# Position is never React state

The per-second WS position tick lives in a store + subscription (`ServerPosition` from `useWebSocket`; each value carries its arrival time and song). Display components read it via `requestAnimationFrame` and write the DOM only when a value changes (`SeekBar`/`ProgressStrip`/`LiveTime` in `src/components/ui/`); logic (drift correction) subscribes. Never put playback position into `useState` — the whole tree re-rendering per second was the app's original perf problem. The unified getter is `audio.getPosition()` (also on `usePlaybackView`): the element while it plays, else the server tick advanced to now at the playback rate.

# Conventions

- React Compiler is enabled — don't hand-memoize new code unless profiling says so. Every component and hook compiles today; check with `reactCompiler: { panicThreshold: "all_errors" }` in a local build (don't commit it). `try`/`finally` and conditionals inside `try` make the compiler skip a function: use promise chains. So does `x++` on a variable a closure captures: write `x += 1`.
- Prefer `useEffectEvent` over ref-mirroring for "read latest value inside a stable Effect/listener" (see `useAudioPlayer`/`useWebSocket`/`useRichPresence`).
- Components that only need the guild use `useGuildId()` (stable, never re-renders); `useGuildState()` also subscribes to live state. `sendCommand` resolves to the route's JSON: toast what the server did.
- Side panels stay mounted inside `<Activity>` (`Panels` in `Dashboard.tsx`) — hidden panels keep local state/scroll with effects unmounted. Don't add unmount-based cleanup assumptions to panel components; PiP hides the whole layout the same way.
- Panel data goes through SWR (global fetcher = `apiFetch`, which throws `ApiError` with `status`); mutations use optimistic `mutate` + revalidate, and a load error shows `ErrorState` with a retry, never an empty state. Playlist keys come from `lib/playlists.ts`. Lyrics are cached per song (immutable) and warmed ~1.5s after song start.
- Adding songs to the queue goes through `useQueueAdd` (per-row pending/added, duplicate notice, error toast). Queue and playlist rows are addressed by `webpage_url` as well as position.
- Rows: `SongRow` is one full-size button with the trailing actions painted above it — trailing controls must be positioned (`IconButton` is). Sortable rows set the row as dnd-kit's activator node, so keys on their buttons never start a drag.
- Touch first: hover-revealed actions need `pointer-coarse:opacity-100` and `group-has-focus-visible:opacity-100`; icon buttons use `ui/IconButton` (enlarged coarse hit area + aria-label); reordering uses dnd-kit (MouseSensor distance 6 + TouchSensor delay 250, Space to pick up by keyboard) — suppress the post-drag click via a `dragOccurred` ref.
- Popovers use `ui/Popover` (portal + fixed positioning) — absolutely-positioned menus get clipped by the mobile scroll containers. It stops its events from reaching the row it was opened from, moves focus in and back, and follows its anchor on resize and scroll.
- The session token lives in memory (`lib/api.ts`), never in localStorage, and is fetched before the WebSocket opens.
- Text inputs: `text-base sm:text-sm` (16px on mobile or iOS zooms the page on focus).
- Pure logic lives in `src/lib` with `*.test.ts` beside it; `npm test` runs them (vitest).

# Deploying UI changes

Chunk filenames are NOT content-hashed across builds; Discord's activity proxy + Cloudflare cache them. `next.config.ts` therefore sets a per-build `assetPrefix` (`/v-<stamp>`), and the backend strips that segment (`FrontendStaticFiles` in `activity/app.py`). Docker rebuilds `out/` during the image build (`docker compose build bot && docker compose up -d`). Wispbyte and a local `uv run python main.py` serve the committed `out/` as-is, so after any UI change run `npm run build` and commit `out/`. On every target, verify the `build <stamp>` line on the Connecting screen before debugging "change didn't apply".

# UI rules

Condensed from the project's UI/UX guide; follow them for every UI change.

- **Fit the product.** Use the theme tokens in `globals.css` (`surface-*`, `fg`, `accent`, `accent-glow`, `muted`, `success`, `danger`, `rank-*`) and the existing components (`SongRow`, `IconButton`, `Popover`, `KeyCap`, `ui/icons`). No new aesthetic, no raw hex or rgba in components: every colour must follow the listener's theme (text is `text-fg`; plain white only on accent or danger fills and over artwork). Dark only: Activities run inside Discord.
- **Themes** are per listener, saved on the server per Discord account (`useTheme` → `/api/me/preferences`); the localStorage copy only paints the theme before the server answers. Each theme is a `[data-theme]` block in `globals.css` plus an entry in `lib/themes.ts`; `@theme` must stay non-`inline` so utilities read the variables. New themes keep `muted` at 4.5:1 or better against `surface-1` and `surface-2`.
- **Quiet by default.** Neutral surfaces and one accent; status colours only where earned (`success` done, `danger` destructive/error).
- **Rows** read as a strong title, then muted details joined by " · ". Siblings share height, padding and alignment; the whole row is the click target.
- **Type.** Body and titles `text-sm` (14px), secondary details `text-xs` (12px). Nothing smaller, except badges (`text-[11px]`), the PiP tile and the mobile tab labels.
- **Time, duration and index columns** use `tabular-nums` with a `min-w-*`, never a fixed width that long values overflow.
- **Feedback on every action at once:** per-row pending/added state plus a toast. Never block other rows while one action is in flight.
- **Loading** uses skeletons shaped like the content (`ui/Skeleton`), one indicator at a time, no flash of empty state.
- **Errors** in plain language, with the fix as a button where there is one. Destructive actions ask for confirmation.
- **Empty states** are minimal: a quiet icon and the next action.
- **Motion** is short (150–250ms). `globals.css` honours `prefers-reduced-motion`; JS-driven motion (`scrollIntoView`, marquee) must check it too.
- **Shortcuts** are listed in `SHORTCUTS` (`useKeyboardShortcuts.ts`) and shown as `KeyCap`s in the shortcuts popover; add new ones there.
- **Words.** Short labels, plain language, the same name for each thing everywhere (Queue, Playlists, Favorites, History).
